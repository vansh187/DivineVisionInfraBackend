"""Payment plans (PP1/PP2/...) and the instalment schedule they produce.

Calculation, exactly as in the client's "Payment Plan's For Web" document:

    S      = Basic Sale Price
    Total  = S + PLC + IFMS + CAM
    row basic amount  = S x percent / 100, rounded half-up to the rupee; the
                        last row takes the rounding remainder so the basic
                        amounts add up to exactly S
    row amount        = basic amount + the charge(s) carried by that row
    due date          = booking date + due days            (due_trigger 'booking')
                        possession date + due days         (due_trigger 'possession';
                        NULL until the project's Offer of Possession date is set)

A row's ``charge_head`` is PLC, IFMS, CAM, ALL (all three) or empty. A charge
no row carries is added to the last row, so money is never dropped.

Pure maths (compute_schedule) never touches the DB; servicePaymentPlans adds
lookups with a short in-process cache. Invalid input raises ValueError with a
stable code - nothing else escapes.
"""
import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
CHARGE_HEADS = ("PLC", "IFMS", "CAM")
# Two-charge combinations, always in the order PLC, IFMS, CAM joined by '+'.
PAIR_CHARGE_HEADS = ("PLC+IFMS", "PLC+CAM", "IFMS+CAM")
# What a plan instalment may store. All three charges are stored as 'ALL'
# (never 'PLC+IFMS+CAM' - 12 chars would not fit varchar(10)).
ROW_CHARGE_HEADS = CHARGE_HEADS + PAIR_CHARGE_HEADS + ("ALL",)


def charges_in(head) -> list:
    """The individual charges a plan row carries:
    'PLC+CAM' -> ['PLC', 'CAM'], 'ALL' -> ['PLC', 'IFMS', 'CAM'], None -> []."""
    if not head:
        return []
    head = str(head)
    if head == "ALL":
        return list(CHARGE_HEADS)
    return head.split("+") if head in ROW_CHARGE_HEADS else []
DUE_TRIGGERS = ("booking", "possession", "custom")   # custom = admin-named event (due_from)
MAX_DUE_FROM = 100
MAX_TOTAL_INR = Decimal("500000000")     # 50 crore - far above any plot; guards typos
EXAMPLE_PRICES = {"sale_price": 5000000, "plc": 100000, "ifms": 50000, "cam": 75000}   # the document's example
_CACHE_SECONDS = 60


def ist_today() -> date:
    return datetime.now(IST).date()


def _money(value, code: str, allow_zero: bool = True) -> Decimal:
    """Whole-rupee Decimal from int/float/'25,00,000'/'Rs. 1,00,000'. Raises ValueError(code)."""
    if value is None or value == "":
        if allow_zero:
            return Decimal(0)
        raise ValueError(code)
    if isinstance(value, bool):
        raise ValueError(code)
    try:
        text_value = str(value).replace(",", "").replace("Rs.", "").replace("₹", "").strip()
        amount = Decimal(text_value)
    except (InvalidOperation, ValueError):
        raise ValueError(code)
    if not amount.is_finite() or amount < 0 or (amount == 0 and not allow_zero):
        raise ValueError(code)
    return amount.quantize(Decimal(1), rounding=ROUND_HALF_UP)


def _as_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(IST).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ValueError("invalid_booking_date")


def _num(value):
    """int when whole (JSON-friendly), else float."""
    d = Decimal(value)
    return int(d) if d == d.to_integral_value() else float(d)


def validate_plan(plan: dict) -> list:
    """The plan's instalments, sorted and checked. Raises ValueError('invalid_payment_plan')."""
    raw = (plan or {}).get("instalments") or []
    if not isinstance(raw, list) or not raw or not all(isinstance(r, dict) for r in raw):
        raise ValueError("invalid_payment_plan")
    try:
        rows = sorted(raw, key=lambda r: int(r.get("seq")))
        seqs = [int(r.get("seq")) for r in rows]
        for r in rows:
            if r.get("due_days") is not None:
                int(r["due_days"])
    except (TypeError, ValueError):
        raise ValueError("invalid_payment_plan")
    if len(set(seqs)) != len(seqs) or any(n < 1 for n in seqs):
        raise ValueError("invalid_payment_plan")
    total_pct = Decimal(0)
    for r in rows:
        if not str(r.get("label") or "").strip():
            raise ValueError("invalid_payment_plan")
        try:
            pct = Decimal(str(r.get("percent")))
        except (InvalidOperation, ValueError):
            raise ValueError("invalid_payment_plan")
        if pct <= 0 or pct > 100:
            raise ValueError("invalid_payment_plan")
        if (r.get("due_trigger") or "booking") not in DUE_TRIGGERS:
            raise ValueError("invalid_payment_plan")
        if (r.get("due_trigger") or "booking") == "custom":
            event = str(r.get("due_from") or "").strip()
            if not 1 <= len(event) <= MAX_DUE_FROM:
                raise ValueError("invalid_payment_plan")
        if r.get("charge_head") not in (None, "") and r.get("charge_head") not in ROW_CHARGE_HEADS:
            raise ValueError("invalid_payment_plan")
        if r.get("due_days") is not None and int(r["due_days"]) < 0:
            raise ValueError("invalid_payment_plan")
        total_pct += pct
    if abs(total_pct - 100) > Decimal("0.001"):
        raise ValueError("invalid_payment_plan")
    # Each of PLC / IFMS / CAM may be placed on at most one row.
    used = [c for r in rows for c in charges_in(r.get("charge_head"))]
    if len(set(used)) != len(used):
        raise ValueError("invalid_payment_plan")
    # Row 1 is always what the customer pays to book (and is marked paid at
    # booking) whatever wording / due the admin gives it.
    return rows


def due_text(row: dict) -> str:
    """'On booking' / '45 days after booking' / 'On Offer of Possession' /
    'On Completion of plinth' / '30 days after Completion of plinth'."""
    days = row.get("due_days")
    trigger = (row.get("due_trigger") or "booking").lower()
    if trigger == "custom":
        event = str(row.get("due_from") or "").strip() or "the stated event"
        return f"On {event}" if not days else f"{days} days after {event}"
    if trigger == "possession":
        return "On Offer of Possession" if not days else f"{days} days after Offer of Possession"
    return "On booking" if not days else f"{days} days after booking"


def is_event_linked(row: dict) -> bool:
    """A row due from an event that has no date yet (possession not offered,
    or a custom event) - never overdue, never reminded, never demanded."""
    trigger = str(row.get("due_trigger") or "booking").lower()
    due = row.get("due_date") if isinstance(row, dict) else getattr(row, "due_date", None)
    return trigger in ("possession", "custom") and not due


def compute_schedule(plan: dict, sale_price, plc=0, ifms=0, cam=0, booking_date=None,
                     possession_date=None) -> dict:
    """Instalment schedule for ``plan`` and the given prices. Pure; raises
    ValueError with codes invalid_payment_plan / invalid_sale_price /
    invalid_plc_amount / invalid_ifms_amount / invalid_cam_amount /
    total_amount_too_large / invalid_booking_date."""
    rows = validate_plan(plan)
    s = _money(sale_price, "invalid_sale_price", allow_zero=False)
    charges = {"PLC": _money(plc, "invalid_plc_amount"), "IFMS": _money(ifms, "invalid_ifms_amount"),
               "CAM": _money(cam, "invalid_cam_amount")}
    total = s + sum(charges.values())
    if total > MAX_TOTAL_INR:
        raise ValueError("total_amount_too_large")
    booked_on = _as_date(booking_date)
    possession_on = _as_date(possession_date)

    # Which row carries each charge: the row whose head includes it (a single
    # head, a pair such as 'PLC+IFMS', or 'ALL'); a charge no row carries goes on
    # the last row so it is never dropped.
    carrier = {}
    for head in CHARGE_HEADS:
        carrier[head] = next((i for i, r in enumerate(rows) if head in charges_in(r.get("charge_head"))),
                             len(rows) - 1)

    out_rows = []
    basic_so_far = Decimal(0)
    for i, r in enumerate(rows):
        pct = Decimal(str(r["percent"]))
        if i == len(rows) - 1:
            basic = s - basic_so_far
        else:
            basic = (s * pct / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        basic_so_far += basic
        heads = [h for h in CHARGE_HEADS if carrier[h] == i and charges[h] > 0]
        charge = sum((charges[h] for h in heads), Decimal(0))
        trigger = r.get("due_trigger") or "booking"
        days = r.get("due_days")
        # booking -> booking date; possession -> project possession date (once
        # set); custom -> an event the system has no date for (always undated).
        anchor = booked_on if trigger == "booking" else possession_on if trigger == "possession" else None
        due = (anchor + timedelta(days=int(days or 0))) if anchor else None
        out_rows.append({
            "seq": int(r["seq"]),
            "label": r["label"],
            "percent": _num(pct),
            "due_trigger": trigger,
            "due_days": None if days is None else int(days),
            "due_from": (str(r.get("due_from") or "").strip() or None) if trigger == "custom" else None,
            "due_text": due_text(r),
            "due_date": due.isoformat() if due else None,
            "basic_amount": _num(basic),
            "charge_head": "+".join(heads) or None,
            "charge_amount": _num(charge),
            "amount": _num(basic + charge),
        })

    if any(Decimal(str(r["amount"])) <= 0 for r in out_rows):
        # e.g. a tiny sale price making a small-percentage row round to 0
        raise ValueError("sale_price_too_small")
    return {
        "plan": {"code": plan.get("code"), "short_code": plan.get("short_code"), "name": plan.get("name"),
                 "version": int(plan.get("version") or 1)},
        "sale_price": _num(s),
        "plc": _num(charges["PLC"]),
        "ifms": _num(charges["IFMS"]),
        "cam": _num(charges["CAM"]),
        "total": _num(total),
        "booking_amount": out_rows[0]["amount"],
        "booking_date": booked_on.isoformat() if booked_on else None,
        "rows": out_rows,
    }


def public_plan(plan: dict) -> dict:
    """API shape of a plan: its rules plus the document's worked example."""
    rows = validate_plan(plan)
    example = compute_schedule(plan, **EXAMPLE_PRICES)
    return {
        "code": plan["code"],
        "short_code": plan.get("short_code"),
        "name": plan.get("name"),
        "description": plan.get("description"),
        "project_id": plan.get("project_id"),
        "version": int(plan.get("version") or 1),
        "instalments": [{
            "seq": int(r["seq"]),
            "label": r["label"],
            "percent": _num(Decimal(str(r["percent"]))),
            "due_trigger": r.get("due_trigger") or "booking",
            "due_days": None if r.get("due_days") is None else int(r["due_days"]),
            "due_from": (str(r.get("due_from") or "").strip() or None)
                        if (r.get("due_trigger") or "booking") == "custom" else None,
            "due_text": due_text(r),
            "charge_head": r.get("charge_head") or None,
        } for r in rows],
        "booking_percent": _num(Decimal(str(rows[0]["percent"]))),
        "example": {
            "sale_price": example["sale_price"], "plc": example["plc"], "ifms": example["ifms"],
            "cam": example["cam"], "total": example["total"], "booking_amount": example["booking_amount"],
            "rows": [{k: row[k] for k in ("seq", "label", "percent", "due_text", "basic_amount",
                                          "charge_head", "charge_amount", "amount")} for row in example["rows"]],
        },
    }


class servicePaymentPlans:
    def __init__(self, persistence=None, milestone_persistence=None):
        if persistence is None:
            from Divinepersistence import persistencePaymentPlan
            persistence = persistencePaymentPlan()
        self._persistence = persistence
        self._milestone_persistence = milestone_persistence
        self._cache = {}
        self._lock = threading.Lock()

    def _cached(self, key, loader):
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < _CACHE_SECONDS:
                return hit[1]
        value = loader()
        with self._lock:
            self._cache[key] = (now, value)
        return value

    def clear_cache(self):
        with self._lock:
            self._cache.clear()

    # ---- catalogue ---------------------------------------------------------
    def list_plans(self, project_id: str = None) -> list:
        def load():
            out = []
            for plan in self._persistence.list_active(project_id):
                try:
                    out.append(public_plan(plan))
                except ValueError:
                    logger.warning("payment_plan_invalid_skipped code=%s", plan.get("code"))
            return out
        return self._cached(("list", project_id or ""), load)

    def get_plan(self, code: str, version: int = None) -> dict:
        """Raw plan dict (with instalments). Raises ValueError('payment_plan_not_found')."""
        code = (code or "").strip()
        if not code or len(code) > 80:
            raise ValueError("payment_plan_not_found")
        plan = self._cached(("plan", code, version), lambda: self._persistence.get_by_code(code, version))
        if not plan and version is None:
            # Also accept the short code ("PP1", any case) of an active plan.
            plan = next((p for p in self._cached(("all_active",), lambda: self._persistence.list_active(None))
                         if str(p.get("short_code") or "").lower() == code.lower()), None)
        if not plan:
            raise ValueError("payment_plan_not_found")
        return plan

    def get_public_plan(self, code: str) -> dict:
        return public_plan(self.get_plan(code))

    def possession_date(self, project_id: str):
        if not project_id:
            return None
        try:
            settings = self._persistence.get_project_settings(project_id)
        except Exception as e:
            logger.warning("project_settings_lookup_failed project_id=%s error=%s", project_id, type(e).__name__)
            return None
        return getattr(settings, "possession_offer_date", None) if settings else None

    # ---- schedules ---------------------------------------------------------
    def preview(self, code: str, sale_price, plc=0, ifms=0, cam=0, booking_date=None,
                project_id: str = None) -> dict:
        plan = self.get_plan(code)
        if project_id and plan.get("project_id") and plan["project_id"] != project_id:
            raise ValueError("payment_plan_not_for_project")
        project = project_id or plan.get("project_id")
        return compute_schedule(plan, sale_price, plc, ifms, cam,
                                booking_date=booking_date or ist_today(),
                                possession_date=self.possession_date(project))

    def booking_terms(self, code: str, sale_price, plc=0, ifms=0, cam=0, project_id: str = None) -> dict:
        """What a booking payment must be for this plan + prices, and the terms to
        store on the payment (re-verified at application upload)."""
        schedule = self.preview(code, sale_price, plc, ifms, cam, project_id=project_id)
        return {
            "payment_plan_code": schedule["plan"]["code"],
            "payment_plan_version": schedule["plan"]["version"],
            "sale_price": schedule["sale_price"], "plc_amount": schedule["plc"],
            "ifms_amount": schedule["ifms"], "cam_amount": schedule["cam"],
            "total_amount": schedule["total"], "booking_amount": schedule["booking_amount"],
        }

    def schedule_for_booking(self, terms: dict, booking_date, project_id: str = None) -> dict:
        """Final schedule for an uploaded application, from the terms stored on the
        booking payment (exact plan version the customer paid against)."""
        plan = self.get_plan(terms["payment_plan_code"], terms.get("payment_plan_version"))
        project = project_id or plan.get("project_id")
        return compute_schedule(plan, terms["sale_price"], terms.get("plc_amount") or 0,
                                terms.get("ifms_amount") or 0, terms.get("cam_amount") or 0,
                                booking_date=booking_date, possession_date=self.possession_date(project))

    # ---- possession --------------------------------------------------------
    def set_possession_date(self, project_id: str, possession_date, updated_by: str) -> dict:
        project_id = (project_id or "").strip()
        if not project_id or len(project_id) > 100:
            raise ValueError("invalid_project_id")
        possession_on = _as_date(possession_date)
        if possession_on is None:
            raise ValueError("invalid_possession_date")
        self._persistence.set_possession_date(project_id, possession_on, updated_by)
        milestones = self._milestone_persistence
        if milestones is None:
            from Divinepersistence import persistenceMilestone
            milestones = persistenceMilestone()
        dated = milestones.date_possession_milestones(project_id, possession_on)
        self.clear_cache()
        return {"project_id": project_id, "possession_offer_date": possession_on.isoformat(),
                "instalments_dated": dated}

    # ---- admin: plan management -------------------------------------------
    def admin_list(self, project_id: str = None) -> list:
        """Every plan version (active and retired) for the admin panel, newest
        version of each code first."""
        out = []
        for plan in self._persistence.list_all(project_id):
            try:
                view = public_plan(plan)
                valid = True
            except ValueError:
                view = {k: plan.get(k) for k in ("code", "short_code", "name", "description", "project_id")}
                valid = False
            view.update({
                "version": int(plan.get("version") or 1), "is_active": bool(plan.get("is_active")),
                "sort_order": int(plan.get("sort_order") or 0), "valid": valid,
                "created_date": _iso(plan.get("created_date")),
                "last_updated_date": _iso(plan.get("last_updated_date")),
            })
            out.append(view)
        return out

    def validate_definition(self, body: dict) -> dict:
        """Dry run: the normalised plan + its worked example. Saves nothing."""
        return public_plan(normalize_definition(body))

    def create_plan(self, body: dict, admin_id: str) -> dict:
        """A brand-new plan code (version 1, active)."""
        plan = normalize_definition(body)
        if self._persistence.list_versions(plan["code"]):
            raise ValueError("payment_plan_exists")
        self._check_short_code_free(plan["short_code"], plan["code"])
        plan.update({"version": 1, "is_active": True})
        try:
            self._persistence.upsert_plan(plan)
        except Exception as e:
            if _is_unique_violation(e):
                raise ValueError("payment_plan_exists")
            raise
        self.clear_cache()
        logger.info("payment_plan_created code=%s by=%s", plan["code"], admin_id)
        return self._admin_view(plan["code"])

    def new_version(self, code: str, body: dict, admin_id: str) -> dict:
        """New instalments for an existing plan: saved as version n+1 and made
        active; earlier versions retire, bookings keep the version they paid for.
        Display fields not sent are carried over from the latest version."""
        versions = self._persistence.list_versions((code or "").strip())
        if not versions:
            raise ValueError("payment_plan_not_found")
        latest = versions[0]
        merged = {k: latest.get(k) for k in ("short_code", "name", "description", "project_id", "sort_order")}
        merged.update({k: v for k, v in (body or {}).items() if v is not None and k != "code"})
        merged["code"] = latest["code"]
        plan = normalize_definition(merged)
        self._check_short_code_free(plan["short_code"], plan["code"])
        plan.update({"version": max(int(v.get("version") or 1) for v in versions) + 1, "is_active": True})
        try:
            self._persistence.upsert_plan(plan)
        except Exception as e:
            if _is_unique_violation(e):
                raise ValueError("payment_plan_version_conflict")
            raise
        self.clear_cache()
        logger.info("payment_plan_new_version code=%s version=%s by=%s", plan["code"], plan["version"], admin_id)
        return self._admin_view(plan["code"])

    def update_plan(self, code: str, changes: dict, admin_id: str) -> dict:
        """Display fields (name, description, sort_order) and active on/off of the
        current version. Instalments never change here - use new_version."""
        versions = self._persistence.list_versions((code or "").strip())
        if not versions:
            raise ValueError("payment_plan_not_found")
        target = next((v for v in versions if v.get("is_active")), versions[0])
        full = self._persistence.get_by_code(target["code"], int(target["version"]))
        changes = {k: v for k, v in (changes or {}).items() if v is not None}
        merged = {k: full.get(k) for k in ("code", "short_code", "name", "description", "project_id", "sort_order")}
        merged.update({k: changes[k] for k in ("name", "description", "sort_order") if k in changes})
        merged["instalments"] = full["instalments"]
        plan = normalize_definition(merged, keep_seq=True)
        active = bool(changes["is_active"]) if "is_active" in changes else bool(full.get("is_active"))
        plan.update({"version": int(target["version"]), "is_active": active})
        if active:
            self._check_short_code_free(plan["short_code"], plan["code"])
        self._persistence.upsert_plan(plan)          # existing version: display fields + active flag only
        if not active:
            self._persistence.set_active(plan["code"], plan["version"], False)
        self.clear_cache()
        logger.info("payment_plan_updated code=%s version=%s active=%s by=%s",
                    plan["code"], plan["version"], active, admin_id)
        return self._admin_view(plan["code"])

    def _check_short_code_free(self, short_code: str, code: str) -> None:
        for p in self._persistence.list_active(None):
            if str(p.get("short_code") or "").lower() == short_code.lower() and p.get("code") != code:
                raise ValueError("short_code_in_use")

    def _admin_view(self, code: str) -> dict:
        """The active version of a code, else its newest version."""
        views = [p for p in self.admin_list(None) if p["code"] == code]
        return next((p for p in views if p["is_active"]), views[0])


_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/_.\-]{1,79}$")
_SHORT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{0,19}$")
_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,99}$")
MAX_INSTALMENTS = 24
MAX_DUE_DAYS = 3650


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _is_unique_violation(e) -> bool:
    return "unique" in str(e).lower() or "integrity" in type(e).__name__.lower()


def normalize_definition(body: dict, keep_seq: bool = False) -> dict:
    """Validate an admin-entered plan and return it in storage shape. Raises
    ValueError: invalid_plan_code, invalid_short_code, invalid_plan_name,
    invalid_description, invalid_project_id, invalid_sort_order,
    invalid_instalments, invalid_payment_plan (percents must add up to 100, the
    first row must be due on booking, each charge may sit on at most one row)."""
    body = body if isinstance(body, dict) else {}
    code = str(body.get("code") or "").strip()
    if not _CODE_RE.match(code) or code.lower() == "preview":
        raise ValueError("invalid_plan_code")
    short_code = str(body.get("short_code") or "").strip()
    if not _SHORT_RE.match(short_code):
        raise ValueError("invalid_short_code")
    name = str(body.get("name") or "").strip()
    if not 2 <= len(name) <= 150:
        raise ValueError("invalid_plan_name")
    description = body.get("description")
    description = str(description).strip() if description not in (None, "") else None
    if description and len(description) > 1000:
        raise ValueError("invalid_description")
    project_id = body.get("project_id")
    project_id = str(project_id).strip() if project_id not in (None, "") else None
    if project_id and not _PROJECT_RE.match(project_id):
        raise ValueError("invalid_project_id")
    sort_raw = body.get("sort_order")
    if isinstance(sort_raw, bool):
        raise ValueError("invalid_sort_order")
    try:
        sort_order = int(sort_raw or 0)
    except (TypeError, ValueError):
        raise ValueError("invalid_sort_order")
    if not 0 <= sort_order <= 1000:
        raise ValueError("invalid_sort_order")

    raw = body.get("instalments")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_INSTALMENTS:
        raise ValueError("invalid_instalments")
    instalments = []
    used_heads = []
    for i, r in enumerate(raw, start=1):
        if not isinstance(r, dict):
            raise ValueError("invalid_instalments")
        label = str(r.get("label") or "").strip()
        if not 1 <= len(label) <= 150:
            raise ValueError("invalid_instalments")
        if isinstance(r.get("percent"), bool):
            raise ValueError("invalid_instalments")
        try:
            percent = Decimal(str(r.get("percent")))
        except (InvalidOperation, ValueError):
            raise ValueError("invalid_instalments")
        if not percent.is_finite() or percent <= 0 or percent > 100 or percent.as_tuple().exponent < -3:
            raise ValueError("invalid_instalments")
        trigger = str(r.get("due_trigger") or "booking").strip().lower()
        if trigger not in DUE_TRIGGERS:
            raise ValueError("invalid_instalments")
        days = r.get("due_days")
        if days in (None, ""):
            days = 0
        else:
            if isinstance(days, bool) or (isinstance(days, float) and not days.is_integer()):
                raise ValueError("invalid_instalments")
            try:
                days = int(str(days).strip().split(".")[0]) if isinstance(days, str) else int(days)
            except (TypeError, ValueError):
                raise ValueError("invalid_instalments")
            if isinstance(r.get("due_days"), str) and not r["due_days"].strip().isdigit():
                raise ValueError("invalid_instalments")
            if not 0 <= days <= MAX_DUE_DAYS:
                raise ValueError("invalid_instalments")
        due_from = None
        if trigger == "custom":
            due_from = re.sub(r"\s+", " ", str(r.get("due_from") or "")).strip()
            if not 1 <= len(due_from) <= MAX_DUE_FROM:
                raise ValueError("invalid_instalments")
        head = r.get("charge_head")
        head = str(head).strip().upper() if head not in (None, "") else None
        if head is not None and head not in ROW_CHARGE_HEADS:
            raise ValueError("invalid_instalments")
        if head:
            used_heads.extend(charges_in(head))
        seq = i
        if keep_seq and r.get("seq") is not None:
            seq = int(r["seq"])
        instalments.append({"seq": seq, "label": label, "percent": float(percent), "due_trigger": trigger,
                            "due_days": days, "charge_head": head, "due_from": due_from})
    # Each of PLC / IFMS / CAM sits on at most one row (pairs and 'ALL' count
    # every charge they contain).
    if len(set(used_heads)) != len(used_heads):
        raise ValueError("invalid_payment_plan")
    plan = {"code": code, "short_code": short_code, "name": name, "description": description,
            "project_id": project_id, "sort_order": sort_order, "instalments": instalments}
    validate_plan(plan)
    compute_schedule(plan, **EXAMPLE_PRICES)
    return plan


PLANS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "payment_plans.json")


def load_plan_file(path: str = None) -> list:
    """Plans from data/payment_plans.json, each validated. Raises ValueError on a bad file."""
    with open(path or PLANS_FILE, encoding="utf-8") as fh:
        data = json.load(fh)
    plans = data.get("plans") if isinstance(data, dict) else None
    if not isinstance(plans, list) or not plans:
        raise ValueError("payment_plan_file_empty")
    seen = set()
    for plan in plans:
        for key in ("code", "short_code", "name"):
            if not str(plan.get(key) or "").strip():
                raise ValueError(f"payment_plan_file_missing_{key}")
        ident = (plan["code"], int(plan.get("version") or 1))
        if ident in seen:
            raise ValueError("payment_plan_file_duplicate")
        seen.add(ident)
        validate_plan(plan)
        compute_schedule(plan, **EXAMPLE_PRICES)      # must compute cleanly
    return plans


def sync_plans(persistence=None, path: str = None) -> dict:
    """Create/refresh every plan in the plan file. Idempotent."""
    if persistence is None:
        from Divinepersistence import persistencePaymentPlan
        persistence = persistencePaymentPlan()
    result = {"created": 0, "updated": 0}
    for plan in load_plan_file(path):
        result[persistence.upsert_plan(plan)] += 1
    return result


def seed_plans_if_empty(persistence=None) -> None:
    """App startup: load the plan file only into an EMPTY catalogue, so a fresh
    database works out of the box while later changes stay a deliberate
    scripts/load_payment_plans.py run. Never raises."""
    try:
        if persistence is None:
            from Divinepersistence import persistencePaymentPlan
            persistence = persistencePaymentPlan()
        if persistence.list_active(None):
            return
        logger.info("payment_plans_seeded %s", sync_plans(persistence))
    except Exception:
        logger.exception("payment_plans_seed_failed")
