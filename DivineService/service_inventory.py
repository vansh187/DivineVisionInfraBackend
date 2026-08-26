import uuid
from collections import Counter
from decimal import Decimal, InvalidOperation
from Divinepersistence import persistenceInventory, persistenceMarketTrend, persistenceChatbot
from .llm_gemini import llmGemini, GeminiError
from .llm_groq import llmGroq, GroqError


MAX_SEARCH_LIMIT = 100
MAX_RECOMMEND_LIMIT = 50
# Safety cap on the pre-filter fetch when a budget filter forces Python-side pagination
# (see search()) - large enough to cover this dataset's realistic size without an unbounded query.
SEARCH_FETCH_CAP = 2000
ALLOWED_UNIT_TYPES = {"plot", "floor", "flat", "commercial"}
ALLOWED_STATUSES = {"available", "held", "sold"}
AREA_MATCH_TOLERANCE_SQYD = 100.0
AREA_TOLERANCE_RATIO = 0.15

NL_SEARCH_TOOL = {
    "name": "extract_search_filters",
    "description": "Extract structured property-search filters from the visitor's natural-language query.",
    "parameters": {"type": "object", "properties": {
        "project_name": {"type": "string", "description": "specific project/township name, if mentioned"},
        "city": {"type": "string", "description": "city or district"},
        "unit_type": {"type": "string", "description": "one of: plot, floor, flat, commercial"},
        "min_area_sqyd": {"type": "number", "description": "minimum plot/unit size in square yards"},
        "max_area_sqyd": {"type": "number", "description": "maximum plot/unit size in square yards"},
        "min_budget": {"type": "number", "description": "minimum budget in INR"},
        "max_budget": {"type": "number", "description": "maximum budget in INR"},
    }, "required": []},
}
NL_SEARCH_SYSTEM_INSTRUCTION = (
    "Extract property search filters from the visitor's query by calling extract_search_filters. "
    "Only include fields the visitor actually specified or clearly implied - e.g. a single size "
    "mentioned ('150 sq yd plot') becomes both min_area_sqyd and max_area_sqyd equal to that value, "
    "and 'under 50 lakh' becomes max_budget only. Never invent a city, budget, or project name that "
    "wasn't stated in the query."
)


class serviceInventory:
    def __init__(self, persistence: persistenceInventory = None,
                 market_trend_persistence: persistenceMarketTrend = None,
                 chatbot_persistence: persistenceChatbot = None,
                 gemini: llmGemini = None, groq: llmGroq = None):
        self._persistence = persistence or persistenceInventory()
        self._market_trend_persistence = market_trend_persistence or persistenceMarketTrend()
        self._chatbot_persistence = chatbot_persistence or persistenceChatbot()
        self._gemini = gemini or llmGemini()
        self._groq = groq or llmGroq()

    # ---- shared helpers -------------------------------------------------
    def _clean_filter(self, value: str, field_name: str, max_length: int = 150) -> str:
        if value is None:
            return None
        cleaned = str(value).strip()
        if not cleaned:
            return None
        if len(cleaned) > max_length:
            raise ValueError(f"{field_name}_too_long")
        return cleaned

    def _clean_choice(self, value: str, field_name: str, allowed: set) -> str:
        cleaned = self._clean_filter(value, field_name, max_length=20)
        if cleaned is None:
            return None
        cleaned = cleaned.lower()
        if cleaned not in allowed:
            raise ValueError(f"invalid_{field_name}")
        return cleaned

    def _clean_limit(self, limit: int, max_limit: int) -> int:
        try:
            parsed = int(limit)
        except (TypeError, ValueError):
            raise ValueError("invalid_limit")
        if parsed < 1:
            raise ValueError("invalid_limit")
        return min(parsed, max_limit)

    def _to_float_or_none(self, value):
        if value is None:
            return None
        try:
            parsed = float(Decimal(str(value)))
        except (InvalidOperation, ValueError, TypeError):
            return None
        return parsed

    def _to_float(self, value, field_name: str):
        if value is None:
            return None
        parsed = self._to_float_or_none(value)
        if parsed is None:
            raise ValueError(f"invalid_{field_name}")
        return parsed

    def _estimate_price(self, unit, price_cache: dict = None) -> float:
        area = self._to_float_or_none(getattr(unit, "area_sqyd", None))
        if area is None:
            return None
        city = getattr(unit, "city", None)
        unit_type = getattr(unit, "unit_type", None)
        cache_key = (city, unit_type)
        if price_cache is not None and cache_key in price_cache:
            price_per_sqyd = price_cache[cache_key]
        else:
            trends = self._market_trend_persistence.list_market_trends(city=city, property_type=unit_type, limit=1)
            if not trends:
                trends = self._market_trend_persistence.list_market_trends(city=city, limit=1)
            price_per_sqyd = self._to_float_or_none(getattr(trends[0], "price_per_sqyd", None)) if trends else None
            if price_cache is not None:
                price_cache[cache_key] = price_per_sqyd
        if price_per_sqyd is None or price_per_sqyd <= 0:
            return None
        return round(area * price_per_sqyd, 2)

    def _format_unit(self, record, price_cache: dict = None) -> dict:
        return {
            "id": str(record.id),
            "project_name": record.project_name,
            "city": record.city,
            "locality": getattr(record, "locality", None),
            "block": getattr(record, "block", None),
            "unit_number": record.unit_number,
            "unit_type": record.unit_type,
            "width_mtr": self._to_float_or_none(getattr(record, "width_mtr", None)),
            "length_mtr": self._to_float_or_none(getattr(record, "length_mtr", None)),
            "area_sqmt": self._to_float_or_none(getattr(record, "area_sqmt", None)),
            "area_sqyd": self._to_float_or_none(getattr(record, "area_sqyd", None)),
            "status": record.status,
            "estimated_price": self._estimate_price(record, price_cache),
        }

    # ---- structured search ------------------------------------------------
    def search(self, project_name: str = None, city: str = None, unit_type: str = None,
               status: str = None, min_area_sqyd=None, max_area_sqyd=None,
               min_budget=None, max_budget=None, limit: int = 20, offset: int = 0) -> dict:
        try:
            offset = max(0, int(offset or 0))
        except (TypeError, ValueError):
            raise ValueError("invalid_offset")

        cleaned_limit = self._clean_limit(limit, MAX_SEARCH_LIMIT)
        min_budget_f = self._to_float(min_budget, "min_budget")
        max_budget_f = self._to_float(max_budget, "max_budget")
        # Budget isn't a stored column - it's estimated live from market trends - so it can't be
        # filtered in SQL. Applying the DB-level LIMIT/OFFSET before that filter would silently
        # drop matching units from later pages (a page's raw pre-filter window might contain only
        # a few budget-matching rows, or none), so when a budget filter is present the DB query
        # fetches a bounded superset instead and offset/limit are re-applied in Python afterwards.
        has_budget_filter = min_budget_f is not None or max_budget_f is not None

        records = self._persistence.search(
            project_name=self._clean_filter(project_name, "project_name"),
            city=self._clean_filter(city, "city"),
            unit_type=self._clean_choice(unit_type, "unit_type", ALLOWED_UNIT_TYPES),
            status=self._clean_choice(status, "status", ALLOWED_STATUSES),
            min_area_sqyd=self._to_float(min_area_sqyd, "min_area_sqyd"),
            max_area_sqyd=self._to_float(max_area_sqyd, "max_area_sqyd"),
            limit=SEARCH_FETCH_CAP if has_budget_filter else cleaned_limit,
            offset=0 if has_budget_filter else offset,
        )

        price_cache = {}
        units = [self._format_unit(record, price_cache) for record in records]

        if has_budget_filter:
            def _in_budget(unit):
                price = unit["estimated_price"]
                if price is None:
                    return True
                if min_budget_f is not None and price < min_budget_f:
                    return False
                if max_budget_f is not None and price > max_budget_f:
                    return False
                return True

            units = [u for u in units if _in_budget(u)]
            units = units[offset:offset + cleaned_limit]

        return {"count": len(units), "units": units}

    # ---- natural-language search -------------------------------------------
    def _sanitize_parsed_filters(self, args: dict) -> dict:
        numeric_fields = {"min_area_sqyd", "max_area_sqyd", "min_budget", "max_budget"}
        text_fields = {"project_name", "city", "unit_type"}
        cleaned = {}
        for key, value in (args or {}).items():
            if value in (None, ""):
                continue
            if key in numeric_fields:
                parsed = self._to_float_or_none(value)
                if parsed is not None:
                    cleaned[key] = parsed
            elif key in text_fields:
                text_value = str(value).strip()
                if text_value:
                    cleaned[key] = text_value
        return cleaned

    def _parse_nl_query(self, query: str) -> dict:
        try:
            step = self._gemini.generate(NL_SEARCH_SYSTEM_INSTRUCTION, [{"role": "user", "text": query}], tools=[NL_SEARCH_TOOL])
        except GeminiError:
            try:
                step = self._groq.generate(NL_SEARCH_SYSTEM_INSTRUCTION, [{"role": "user", "content": query}], tools=[NL_SEARCH_TOOL])
            except GroqError:
                return {}
        call = step.get("function_call") if step else None
        if not call:
            return {}
        return self._sanitize_parsed_filters(call.get("args"))

    def search_natural_language(self, query: str, session_id: str = None, limit: int = 20) -> dict:
        query = (query or "").strip()
        if not query:
            raise ValueError("query_required")
        if len(query) > 500:
            raise ValueError("query_too_long")

        parsed_filters = self._parse_nl_query(query)
        # unit_type parsed from free text may not land exactly on an allowed value (e.g. "flats") -
        # drop it rather than let an otherwise-good search 400 out on a filter the visitor never
        # explicitly chose from a menu.
        if parsed_filters.get("unit_type", "").lower() not in ALLOWED_UNIT_TYPES:
            parsed_filters.pop("unit_type", None)
        # A single stated size ("150 sq yd plot") comes back as min == max - real inventory almost
        # never lands on an exact sq yd figure, so an exact-equality filter would return nothing
        # useful. Widen it into a tolerance band instead of matching the number literally.
        if (parsed_filters.get("min_area_sqyd") is not None
                and parsed_filters.get("min_area_sqyd") == parsed_filters.get("max_area_sqyd")):
            midpoint = parsed_filters["min_area_sqyd"]
            parsed_filters["min_area_sqyd"] = round(midpoint * (1 - AREA_TOLERANCE_RATIO), 2)
            parsed_filters["max_area_sqyd"] = round(midpoint * (1 + AREA_TOLERANCE_RATIO), 2)

        result = self.search(limit=limit, **parsed_filters)
        result["parsed_filters"] = parsed_filters
        return result

    # ---- browsing-behavior tracking ----------------------------------------
    def record_view(self, inventory_id: str, lead_id: str = None, session_id: str = None) -> dict:
        if not (inventory_id or "").strip():
            raise ValueError("inventory_id_required")
        try:
            self._persistence.record_event(
                id=str(uuid.uuid4()), inventory_id=inventory_id,
                lead_id=lead_id or None, session_id=session_id or None, event_type="view",
            )
            return {"recorded": True}
        except Exception:
            # A bad/stale inventory_id (FK violation) or transient DB hiccup shouldn't surface as
            # a hard error to a background telemetry call - the caller (frontend "viewed" ping)
            # doesn't act on this response either way.
            return {"recorded": False}

    # ---- recommendations ----------------------------------------------------
    def _build_preference(self, viewed_events: list, qualification) -> dict:
        if viewed_events:
            unit_types = [e.unit_type for e in viewed_events if getattr(e, "unit_type", None)]
            areas = [self._to_float_or_none(getattr(e, "area_sqyd", None)) for e in viewed_events]
            areas = [a for a in areas if a is not None]
            cities = [e.city for e in viewed_events if getattr(e, "city", None)]
            localities = [e.locality for e in viewed_events if getattr(e, "locality", None)]
            return {
                "unit_type": Counter(unit_types).most_common(1)[0][0] if unit_types else None,
                "area_sqyd": (sum(areas) / len(areas)) if areas else None,
                "location": (Counter(localities).most_common(1)[0][0] if localities
                             else (Counter(cities).most_common(1)[0][0] if cities else None)),
                "budget_min": None,
                "budget_max": None,
            }
        if qualification is not None:
            budget_min = self._to_float_or_none(getattr(qualification, "budget_min", None))
            budget_max = self._to_float_or_none(getattr(qualification, "budget_max", None))
            unit_type = (getattr(qualification, "unit_type", None) or "").strip().lower() or None
            location = (getattr(qualification, "location_preference", None) or "").strip() or None
            if budget_min is None and budget_max is None and unit_type is None and location is None:
                return None
            return {
                "unit_type": unit_type if unit_type in ALLOWED_UNIT_TYPES else None,
                "area_sqyd": None, "location": location,
                "budget_min": budget_min, "budget_max": budget_max,
            }
        return None

    def _score(self, candidate, preference: dict, price_cache: dict = None) -> float:
        score, weight_total = 0.0, 0.0

        if preference.get("unit_type"):
            score += 0.3 * (1.0 if (candidate.unit_type or "").lower() == preference["unit_type"] else 0.0)
            weight_total += 0.3

        if preference.get("area_sqyd") is not None:
            candidate_area = self._to_float_or_none(getattr(candidate, "area_sqyd", None))
            if candidate_area is not None:
                diff = abs(candidate_area - preference["area_sqyd"])
                score += 0.3 * max(0.0, 1.0 - diff / AREA_MATCH_TOLERANCE_SQYD)
                weight_total += 0.3

        if preference.get("location"):
            haystack = f"{candidate.city or ''} {getattr(candidate, 'locality', None) or ''}".lower()
            score += 0.2 * (1.0 if preference["location"].lower() in haystack else 0.0)
            weight_total += 0.2

        if preference.get("budget_min") is not None or preference.get("budget_max") is not None:
            estimated_price = self._estimate_price(candidate, price_cache)
            if estimated_price is not None:
                lo = preference.get("budget_min") or 0.0
                hi = preference.get("budget_max")
                in_range = estimated_price >= lo and (hi is None or estimated_price <= hi)
                score += 0.2 * (1.0 if in_range else 0.0)
                weight_total += 0.2

        return (score / weight_total) if weight_total else 0.0

    def recommend(self, lead_id: str = None, session_id: str = None, limit: int = 10) -> dict:
        limit = self._clean_limit(limit, MAX_RECOMMEND_LIMIT)
        # Shared across every price lookup this call makes - candidates overwhelmingly repeat a
        # small number of (city, unit_type) combos, so this turns what would be up to ~1000
        # sequential market-trend queries (two per candidate, scoring plus formatting, times up
        # to 500 candidates) into at most one query per distinct combo actually seen.
        price_cache = {}

        viewed_events = self._persistence.list_events_for_lead(lead_id, limit=50) if lead_id else []
        viewed_ids = {e.inventory_id for e in viewed_events}
        qualification = self._chatbot_persistence.get_qualification_by_lead(lead_id) if lead_id else None

        preference = self._build_preference(viewed_events, qualification)
        if preference is None:
            units = self._persistence.list_recent_available_units(limit=limit)
            return {"best_fit": [self._format_unit(u, price_cache) for u in units], "similar_alternatives": {}}

        candidates = [c for c in self._persistence.search(status="available", limit=500) if c.id not in viewed_ids]
        scored = sorted(candidates, key=lambda c: self._score(c, preference, price_cache), reverse=True)
        best_fit = [self._format_unit(c, price_cache) for c in scored[:limit]]

        similar_alternatives = {}
        for event in viewed_events[:5]:
            viewed_unit = self._persistence.get_by_id(event.inventory_id)
            if not viewed_unit:
                continue
            viewed_area = self._to_float_or_none(getattr(viewed_unit, "area_sqyd", None)) or 0.0
            neighbors = [
                c for c in candidates
                if c.unit_type == viewed_unit.unit_type and c.project_name == viewed_unit.project_name
            ]
            neighbors.sort(key=lambda c: abs((self._to_float_or_none(getattr(c, "area_sqyd", None)) or 0.0) - viewed_area))
            similar_alternatives[event.inventory_id] = [self._format_unit(c, price_cache) for c in neighbors[:3]]

        return {"best_fit": best_fit, "similar_alternatives": similar_alternatives}
