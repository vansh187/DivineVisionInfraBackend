import math
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from Divinepersistence import persistenceMarketTrend


MAX_TRENDS_LIMIT = 100


class serviceMarketTrend:
    def __init__(self, persistence: persistenceMarketTrend = None):
        self._persistence = persistence or persistenceMarketTrend()

    def _clean_filter(self, value: str, field_name: str) -> str:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if len(cleaned) > 150:
            raise ValueError(f"{field_name}_too_long")
        return cleaned

    def _clean_limit(self, limit: int) -> int:
        try:
            parsed = int(limit)
        except (TypeError, ValueError):
            raise ValueError("invalid_limit")
        if parsed < 1:
            raise ValueError("invalid_limit")
        return min(parsed, MAX_TRENDS_LIMIT)

    def _to_float(self, value, field_name: str, required: bool = False):
        if value is None:
            if required:
                raise ValueError(f"{field_name}_required")
            return None
        try:
            parsed = float(Decimal(str(value)))
        except (InvalidOperation, ValueError, TypeError) as e:
            raise ValueError(f"invalid_{field_name}") from e
        if not math.isfinite(parsed):
            raise ValueError(f"invalid_{field_name}")
        return parsed

    def _date_to_string(self, value) -> str:
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        text = str(value or "").strip()
        return text or date.today().isoformat()

    def _demand_label(self, demand_score) -> str:
        if demand_score is None:
            return "unknown"
        if demand_score >= 70:
            return "high"
        if demand_score >= 40:
            return "medium"
        return "low"

    def _trend_direction(self, current_price: float, previous_price: float):
        if previous_price is None or previous_price <= 0:
            return None, "new"
        change = round(((current_price - previous_price) / previous_price) * 100, 2)
        if abs(change) < 0.01:
            return change, "stable"
        return change, "up" if change > 0 else "down"

    def _format_record(self, record):
        current_price = self._to_float(record.price_per_sqyd, "price_per_sqyd", required=True)
        if current_price <= 0:
            raise ValueError("invalid_price_per_sqyd")

        previous_price = self._to_float(getattr(record, "previous_price_per_sqyd", None), "previous_price_per_sqyd")
        rental_yield = self._to_float(getattr(record, "rental_yield_percent", None), "rental_yield_percent")
        demand_score = self._to_float(getattr(record, "demand_score", None), "demand_score")
        supply_score = self._to_float(getattr(record, "supply_score", None), "supply_score")
        price_change, direction = self._trend_direction(current_price, previous_price)

        try:
            sample_size = max(0, int(getattr(record, "sample_size", 0) or 0))
        except (TypeError, ValueError):
            sample_size = 0

        as_of_date = self._date_to_string(getattr(record, "as_of_date", None))
        period_label = (getattr(record, "period_label", None) or "").strip() or as_of_date

        return {
            "id": str(getattr(record, "id", "")),
            "city": (getattr(record, "city", None) or "Unknown").strip() or "Unknown",
            "locality": (getattr(record, "locality", None) or "").strip() or None,
            "property_type": (getattr(record, "property_type", None) or "Unknown").strip() or "Unknown",
            "period_label": period_label,
            "as_of_date": as_of_date,
            "price_per_sqyd": round(current_price, 2),
            "previous_price_per_sqyd": round(previous_price, 2) if previous_price is not None and previous_price > 0 else None,
            "price_change_percent": price_change,
            "trend_direction": direction,
            "rental_yield_percent": round(rental_yield, 2) if rental_yield is not None and rental_yield >= 0 else None,
            "demand_score": round(demand_score, 2) if demand_score is not None and 0 <= demand_score <= 100 else None,
            "supply_score": round(supply_score, 2) if supply_score is not None and 0 <= supply_score <= 100 else None,
            "demand_label": self._demand_label(demand_score if demand_score is not None and 0 <= demand_score <= 100 else None),
            "sample_size": sample_size,
        }

    def list_market_trends(self, city: str = None, locality: str = None,
                           property_type: str = None, limit: int = 20):
        filters = {
            "city": self._clean_filter(city, "city"),
            "locality": self._clean_filter(locality, "locality"),
            "property_type": self._clean_filter(property_type, "property_type"),
            "limit": self._clean_limit(limit),
        }
        records = self._persistence.list_market_trends(**filters)
        trends = []
        for record in records:
            try:
                trends.append(self._format_record(record))
            except ValueError:
                continue
        return {"count": len(trends), "trends": trends}
