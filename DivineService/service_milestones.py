"""Booking-plan milestone state: build rows from a payment plan, compute
per-milestone status against 'today' (Asia/Kolkata), validate an instalment
payment, and mark a milestone paid.

IST has no DST, so a fixed +05:30 offset is exact - no tzdata dependency.

Every public method is wrapped: a DB fault degrades to an empty / unchanged
result and is logged, never raised into the caller (same contract as the
market-trend read path)."""
import json
import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from Divinepersistence import persistenceMilestone, persistenceCustomerProfile, persistencePayment
from DivineService.service_payment_schedule import build_payment_schedule

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
PAY_WINDOW_DAYS = 5      # "Pay now" unlocks this many days before due_date
REMINDER_LEAD_DAYS = 20  # "due" status / first reminder starts this many days before

_BOOKING_DOC_HINT = "booking"
_TOTAL_KEYS = (
    "total_consideration", "totalConsideration", "total_amount", "totalAmount",
    "total_price", "totalPrice", "consideration", "sale_value", "saleValue",
)
_BOOKING_DATE_KEYS = ("booking_date", "bookingDate", "date")
_PROJECT_KEYS = ("project_id", "projectId", "project_name", "projectName")


def ist_today() -> date:
    try:
        return datetime.now(IST).date()
    except Exception:  # pragma: no cover - clock is always available
        return date.today()


def _to_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _to_num(value):
    try:
        n = float(str(value).replace(",", "").replace("Rs.", "").replace("₹", "").strip())
    except (TypeError, ValueError):
        return None
    if n != n:
        return None
    return n


def compute_status(due_date, is_paid: bool, today=None) -> str:
    """paid | overdue | due | upcoming, per the spec's windows."""
    if is_paid:
        return "paid"
    d = _to_date(due_date)
    if d is None:
        return "upcoming"
    today = today or ist_today()
    if today > d:
        return "overdue"
    if d - timedelta(days=REMINDER_LEAD_DAYS) <= today <= d:
        return "due"
    return "upcoming"


def pay_enabled_from(due_date):
    d = _to_date(due_date)
    return (d - timedelta(days=PAY_WINDOW_DAYS)).isoformat() if d else None


def _row_is_on_booking(row: dict, index: int) -> bool:
    try:
        return index == 0 or int(row.get("due_days") or 0) == 0
    except (TypeError, ValueError):
        return index == 0


class serviceMilestones:
    def __init__(self, milestone_persistence: persistenceMilestone = None,
                 profile_persistence: persistenceCustomerProfile = None,
                 payment_persistence: persistencePayment = None):
        self._persistence = milestone_persistence or persistenceMilestone()
        self._profile = profile_persistence or persistenceCustomerProfile()
        self._payments = payment_persistence or persistencePayment()

    # ---- building rows from a plan ------------------------------------
    def _rows_from_plan(self, plan: dict, booking_id: str, customer_id: str,
                        project_id: str, inventory_id: str, booking_payment_id: str,
                        booking_date) -> list:
        rows = []
        plan_rows = (plan or {}).get("rows") or []
        for i, r in enumerate(plan_rows):
            if not isinstance(r, dict):
                continue
            on_booking = _row_is_on_booking(r, i)
            rows.append({
                "id": f"{booking_id}-m{i + 1}",
                "booking_id": booking_id,
                "customer_id": customer_id,
                "project_id": project_id,
                "inventory_id": inventory_id,
                "milestone_no": i + 1,
                "label": r.get("label"),
                "percent": _to_num(r.get("percent")),
                "due_days": r.get("due_days"),
                "due_date": _to_date(r.get("due_date")),
                "amount": int(round(_to_num(r.get("amount")) or 0)),
                "status": "paid" if on_booking else "upcoming",
                "paid_on": booking_date if on_booking else None,
                "paid_payment_id": booking_payment_id if on_booking else None,
            })
        return rows

    def _load_booking_doc(self, customer_id: str):
        """The customer's booking-application document as a dict, or None. Best-effort."""
        try:
            return self._profile.get_booking_application(customer_id)
        except Exception as e:
            logger.warning("milestone_booking_doc_lookup_failed customer_id=%s error=%s", customer_id, e)
        return None

    def _form_of(self, record) -> dict:
        form = record.get("form_data") if isinstance(record, dict) else getattr(record, "form_data", None)
        if isinstance(form, str):
            try:
                form = json.loads(form)
            except (TypeError, ValueError):
                form = {}
        return form if isinstance(form, dict) else {}

    def _first(self, form: dict, keys):
        for k in keys:
            v = form.get(k)
            if v not in (None, ""):
                return v
        return None

    def ensure_for_customer(self, customer_id: str) -> list:
        """Return the milestone rows for this customer, creating them from the
        stored booking application + payment plan on first read (self-heal, like
        the lazy reservation-expiry pattern). Never raises."""
        try:
            existing = self._persistence.list_for_customer(customer_id)
        except Exception as e:
            logger.warning("milestone_list_failed customer_id=%s error=%s", customer_id, e)
            return []
        if existing:
            return existing

        record = self._load_booking_doc(customer_id)
        if not record:
            return []
        try:
            booking_id = (record.get("id") if isinstance(record, dict) else getattr(record, "id", None))
            form = self._form_of(record)
            total = _to_num(self._first(form, _TOTAL_KEYS))
            if not total or total <= 0:
                return []
            booking_date = _to_date(self._first(form, _BOOKING_DATE_KEYS)) or _to_date(
                (record.get("created_date") if isinstance(record, dict) else getattr(record, "created_date", None))
            )
            received = _to_num(form.get("amount_received")) or _to_num(form.get("amount")) or 0
            plan = build_payment_schedule(total_amount=total, booking_amount=received,
                                          booking_date=booking_date)
            project_id = (record.get("project_id") if isinstance(record, dict)
                          else getattr(record, "project_id", None)) or self._first(form, _PROJECT_KEYS)
            payment_id = (record.get("payment_id") if isinstance(record, dict)
                          else getattr(record, "payment_id", None))
            inventory_id = self._resolve_inventory_id(record)
            rows = self._rows_from_plan(plan, booking_id, customer_id, project_id,
                                        inventory_id, payment_id, booking_date)
            if not rows:
                return []
            self._persistence.create_milestones(rows)
            return self._persistence.list_for_customer(customer_id)
        except Exception as e:
            logger.warning("milestone_backfill_failed customer_id=%s error=%s", customer_id, e)
            return []

    def _resolve_inventory_id(self, record):
        pid = (record.get("payment_id") if isinstance(record, dict)
               else getattr(record, "payment_id", None))
        if not pid:
            return None
        try:
            pay = self._payments.get_by_id(pid)
            return getattr(pay, "inventory_id", None) if pay else None
        except Exception:
            return None

    # ---- enriched schedule for GET /customer/profile ---------------------
    def enriched_schedule(self, customer_id: str) -> dict:
        """{'rows': [...enriched dicts...], 'next_due': {...} | None}. Never raises."""
        try:
            records = self.ensure_for_customer(customer_id)
        except Exception as e:  # pragma: no cover - ensure_for_customer already guards
            logger.warning("milestone_enrich_failed customer_id=%s error=%s", customer_id, e)
            return {"rows": [], "next_due": None}
        if not records:
            return {"rows": [], "next_due": None}

        today = ist_today()
        records = sorted(records, key=lambda r: (getattr(r, "milestone_no", 0) or 0))
        self._sync_statuses(records, today)

        rows = []
        for r in records:
            is_paid = (getattr(r, "status", None) == "paid")
            due = getattr(r, "due_date", None)
            status = "paid" if is_paid else compute_status(due, False, today)
            rows.append({
                "id": getattr(r, "id", None),
                "label": getattr(r, "label", None),
                "percent": _to_num(getattr(r, "percent", None)),
                "due_days": getattr(r, "due_days", None),
                "due_date": _to_date(due).isoformat() if _to_date(due) else None,
                "amount": int(round(_to_num(getattr(r, "amount", None)) or 0)),
                "status": status,
                "pay_enabled_from": pay_enabled_from(due),
                "paid_on": self._iso(getattr(r, "paid_on", None)),
                "paid_payment_id": getattr(r, "paid_payment_id", None),
            })
        return {"rows": rows, "next_due": self._next_due(customer_id, rows, today)}

    def _sync_statuses(self, records, today) -> None:
        for r in records:
            if getattr(r, "status", None) == "paid":
                continue
            fresh = compute_status(getattr(r, "due_date", None), False, today)
            if fresh != getattr(r, "status", None):
                try:
                    self._persistence.set_status(getattr(r, "id", None), fresh)
                    r.status = fresh
                except Exception as e:
                    logger.warning("milestone_status_sync_failed id=%s error=%s",
                                   getattr(r, "id", None), e)

    def _next_due(self, customer_id: str, rows: list, today: date):
        nxt = next((row for row in rows if row["status"] != "paid"), None)
        if not nxt:
            return None
        d = _to_date(nxt["due_date"])
        days_until = (d - today).days if d else None
        last_kind = last_at = None
        try:
            last = self._persistence.last_reminder_for_customer(customer_id)
            if last:
                last_kind = getattr(last, "kind", None)
                last_at = self._iso(getattr(last, "sent_at", None))
        except Exception:
            pass
        return {
            "milestone_id": nxt["id"], "label": nxt["label"], "amount": nxt["amount"],
            "due_date": nxt["due_date"], "days_until_due": days_until, "status": nxt["status"],
            "last_reminder_kind": last_kind, "last_reminder_at": last_at,
        }

    def _iso(self, value):
        if value is None:
            return None
        try:
            return value.isoformat()
        except Exception:
            return str(value)

    # ---- instalment payment validation ---------------------------------
    def validate_installment(self, customer_id: str, installment_no, amount, due_date=None) -> tuple:
        """Returns (milestone_record | None, error_code | None). error_code is one of
        the spec's guard-rail strings. Never raises - a lookup fault returns
        (None, 'no_booking')."""
        try:
            no = int(installment_no)
        except (TypeError, ValueError):
            return None, "installment_not_found"
        try:
            records = self.ensure_for_customer(customer_id)
        except Exception:
            return None, "no_booking"
        if not records:
            return None, "no_booking"

        records = sorted(records, key=lambda r: (getattr(r, "milestone_no", 0) or 0))
        target = next((r for r in records if (getattr(r, "milestone_no", None) == no)), None)
        if target is None:
            return None, "installment_not_found"
        if getattr(target, "status", None) == "paid":
            return target, "installment_already_paid"

        earlier_unpaid = any(
            (getattr(r, "milestone_no", 0) or 0) < no and getattr(r, "status", None) != "paid"
            for r in records
        )
        if earlier_unpaid:
            return target, "installment_out_of_order"

        due = _to_date(getattr(target, "due_date", None))
        if due is not None:
            window_open = due - timedelta(days=PAY_WINDOW_DAYS)
            if ist_today() < window_open:
                return target, "installment_not_payable"

        milestone_amount = _to_num(getattr(target, "amount", None)) or 0
        paid_amount = _to_num(amount) or 0
        if abs(paid_amount - milestone_amount) > 1:
            return target, "installment_amount_mismatch"

        return target, None

    def mark_paid(self, milestone_id: str, payment_id: str):
        """Flip a milestone to paid and refresh sibling statuses. Never raises;
        returns the updated record or None."""
        try:
            updated = self._persistence.mark_paid(milestone_id, payment_id)
        except Exception as e:
            logger.warning("milestone_mark_paid_failed id=%s error=%s", milestone_id, e)
            return None
        try:
            if updated is not None:
                booking_id = getattr(updated, "booking_id", None)
                if booking_id:
                    siblings = self._persistence.list_for_booking(booking_id)
                    self._sync_statuses(siblings, ist_today())
        except Exception as e:
            logger.warning("milestone_sibling_sync_failed id=%s error=%s", milestone_id, e)
        return updated

    def milestone_by_payment(self, customer_id: str, payment_id: str):
        """The milestone a given payment settled (paid_payment_id match), annotated
        with _amount_received_after / _outstanding_after for the receipt. None if
        not found. Never raises."""
        if not payment_id:
            return None
        try:
            records = self.ensure_for_customer(customer_id)
        except Exception:
            return None
        records = sorted(records or [], key=lambda r: (getattr(r, "milestone_no", 0) or 0))
        target = next((r for r in records if getattr(r, "paid_payment_id", None) == payment_id), None)
        if target is None:
            return None
        running = 0
        total = 0
        for r in records:
            amt = int(round(_to_num(getattr(r, "amount", None)) or 0))
            total += amt
            if getattr(r, "status", None) == "paid" and (getattr(r, "milestone_no", 0) or 0) <= (getattr(target, "milestone_no", 0) or 0):
                running += amt
        try:
            target._amount_received_after = running
            target._outstanding_after = max(0, total - running)
        except Exception:
            pass
        return target

    def amount_received_rupees(self, customer_id: str):
        """Sum of paid milestone amounts for the customer - keeps the profile's
        amount_received consistent with the per-row status. None when unknown."""
        try:
            records = self.ensure_for_customer(customer_id)
        except Exception:
            return None
        if not records:
            return None
        total = 0
        for r in records:
            if getattr(r, "status", None) == "paid":
                total += int(round(_to_num(getattr(r, "amount", None)) or 0))
        return total
