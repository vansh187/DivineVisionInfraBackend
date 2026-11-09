"""Daily payment-due reminder job.

Run by a token-gated endpoint (POST /jobs/payment-reminders) that cron-job.org
calls once a day. For every (customer, booking) with an unpaid next milestone it
sends at most one email per run - the most urgent kind not already sent
(OVERDUE > DUE_TODAY > T_MINUS_5 > T_MINUS_20) - and records a dedupe row. A
customer who holds more than one plot gets an independent cadence per plot.

Fully wrapped: one bad customer is counted and skipped, never aborts the run,
and nothing here raises into the endpoint.
"""
import logging
import os
import time
import uuid
from datetime import date

from Divinepersistence import persistenceMilestone, persistenceCustomer, persistenceCustomerProfile
from DivineService.service_milestones import (
    serviceMilestones, ist_today, PAY_WINDOW_DAYS, REMINDER_LEAD_DAYS, _to_date,
)
from DivineService.service_email import serviceEmail, dispatch_payment_reminder_email

logger = logging.getLogger(__name__)

_UNIT_KEYS = ("unit_number", "unitNumber", "unit_no", "unitNo", "plot_number", "plotNumber", "plot_no", "plotNo")
# Offline buyers imported from the client's sheet get no reminders (client decision).
_NO_REMINDER_SOURCES = ("offline_import",)


def reminders_enabled() -> bool:
    """Global switch: PAYMENT_REMINDERS_ENABLED=false pauses every reminder email."""
    return (os.getenv("PAYMENT_REMINDERS_ENABLED") or "true").strip().lower() not in ("0", "false", "no", "off")


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name) or default)
        return value if value > 0 else default
    except ValueError:
        return default
_PROJECT_NAME_KEYS = ("project_name", "projectName", "township", "township_name", "project")


def _week_key(today: date) -> str:
    try:
        y, w, _ = today.isocalendar()
        return f"{y}W{int(w):02d}"
    except Exception:  # pragma: no cover
        return today.isoformat()


def pick_kind(days_until_due, today: date):
    """(kind, week_key) or (None, None) when no reminder is due today."""
    if days_until_due is None:
        return None, None
    if days_until_due < 0:
        return "OVERDUE", _week_key(today)
    if days_until_due == 0:
        return "DUE_TODAY", ""
    if 1 <= days_until_due <= PAY_WINDOW_DAYS:
        return "T_MINUS_5", ""
    if PAY_WINDOW_DAYS < days_until_due <= REMINDER_LEAD_DAYS:
        return "T_MINUS_20", ""
    return None, None


class serviceReminders:
    def __init__(self, milestone_persistence: persistenceMilestone = None,
                 customer_persistence: persistenceCustomer = None,
                 profile_persistence: persistenceCustomerProfile = None,
                 milestone_service: serviceMilestones = None,
                 email_service: serviceEmail = None):
        self._milestones = milestone_persistence or persistenceMilestone()
        self._customers = customer_persistence or persistenceCustomer()
        self._profile = profile_persistence or persistenceCustomerProfile()
        self._milestone_service = milestone_service or serviceMilestones(
            milestone_persistence=self._milestones, profile_persistence=self._profile
        )
        self._email = email_service if email_service is not None else self._safe_email()

    def _safe_email(self):
        try:
            return serviceEmail()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("reminder_email_init_failed error=%s", e)
            return None

    def run(self, today: date = None, max_seconds: int = None, max_emails: int = None) -> dict:
        """One sweep. Bounded so a cron HTTP call can't time out: it stops after
        ``max_seconds`` (REMINDER_RUN_MAX_SECONDS, default 240) or ``max_emails``
        sends (REMINDER_RUN_MAX_EMAILS, default 200); the rest go out next run
        (nothing is lost - dedupe only records what was attempted)."""
        today = today or ist_today()
        summary = {"scanned_customers": 0, "sent": {}, "skipped": 0, "errors": 0, "no_email": 0}
        self._applications_cache = {}      # customer_id -> booking applications, for this run only
        if not reminders_enabled():
            return {**summary, "disabled": True}
        deadline = time.monotonic() + (max_seconds or _env_int("REMINDER_RUN_MAX_SECONDS", 240))
        email_cap = max_emails or _env_int("REMINDER_RUN_MAX_EMAILS", 200)
        try:
            unpaid = self._milestones.list_unpaid()
        except Exception as e:
            logger.warning("reminder_run_list_failed error=%s", e)
            return {**summary, "errors": 1, "fatal": "list_unpaid_failed"}

        # One (customer, booking) pair per iteration - a multi-plot customer gets a
        # reminder cadence per plot, not just for whichever single milestone is most
        # urgent across all of them. Booking ids are grouped under their customer so
        # that customer's milestone rows are fetched once, not once per plot.
        booking_ids_by_customer = {}
        for m in unpaid:
            cid = getattr(m, "customer_id", None)
            if not cid:
                continue
            bid = getattr(m, "booking_id", None)
            bucket = booking_ids_by_customer.setdefault(cid, [])
            if bid not in bucket:
                bucket.append(bid)

        for cid, booking_ids in booking_ids_by_customer.items():
            if time.monotonic() > deadline or sum(summary["sent"].values()) >= email_cap:
                summary["truncated"] = True
                break
            summary["scanned_customers"] += 1
            try:
                all_rows = self._milestone_service.ensure_for_customer(cid)
            except Exception as e:
                all_rows = None
                logger.warning("reminder_rows_fetch_failed customer_id=%s error=%s", cid, e)
            for bid in booking_ids:
                try:
                    self._process_customer(cid, today, summary, booking_id=bid, all_rows=all_rows)
                except Exception as e:
                    summary["errors"] += 1
                    logger.warning("reminder_customer_failed customer_id=%s booking_id=%s error=%s",
                                   cid, bid, e)
        return summary

    def _process_customer(self, customer_id: str, today: date, summary: dict,
                          booking_id: str = None, all_rows: list = None) -> None:
        enriched = self._milestone_service.enriched_schedule(customer_id, booking_id, rows=all_rows)
        nd = (enriched or {}).get("next_due")
        if not nd or not nd.get("milestone_id"):
            summary["skipped"] += 1
            return

        kind, week_key = pick_kind(nd.get("days_until_due"), today)
        if not kind:
            summary["skipped"] += 1
            return

        milestone_id = nd["milestone_id"]
        try:
            if self._milestones.reminder_sent(milestone_id, kind, week_key or ""):
                summary["skipped"] += 1
                return
        except Exception as e:
            logger.warning("reminder_dedupe_check_failed milestone_id=%s error=%s", milestone_id, e)
            summary["errors"] += 1
            return
        project_name, unit_number, source = self._booking_labels(customer_id, booking_id)
        if source in _NO_REMINDER_SOURCES:
            summary["skipped"] += 1
            return

        customer = None
        try:
            customer = self._customers.get_by_id(customer_id)
        except Exception:
            customer = None
        email = getattr(customer, "email", None) if customer else None
        if not email:
            summary["no_email"] += 1
            return

        outstanding = self._outstanding_after(enriched, milestone_id)

        ok = dispatch_payment_reminder_email(
            self._email, email,
            first_name=getattr(customer, "first_name", None),
            project_name=project_name or nd.get("label"),
            unit_number=unit_number,
            milestone_label=self._milestone_label(enriched, milestone_id, nd.get("label")),
            amount=nd.get("amount"),
            due_date=nd.get("due_date"),
            days_remaining=nd.get("days_until_due"),
            outstanding=outstanding,
            kind=kind,
        )

        record_booking_id = booking_id or (
            milestone_id.rsplit("-m", 1)[0] if "-m" in milestone_id else None
        )
        try:
            self._milestones.record_reminder(
                id=str(uuid.uuid4()), customer_id=customer_id, booking_id=record_booking_id,
                milestone_id=milestone_id, kind=kind, week_key=week_key or "",
                amount=nd.get("amount"), due_date=_to_date(nd.get("due_date")),
                email_to=email, delivery_status="sent" if ok else "failed",
            )
        except Exception as e:
            logger.warning("reminder_record_failed milestone_id=%s error=%s", milestone_id, e)

        if ok:
            summary["sent"][kind] = summary["sent"].get(kind, 0) + 1
        else:
            summary["errors"] += 1

    @staticmethod
    def _milestone_label(enriched: dict, milestone_id: str, fallback):
        """'Within 90 days of booking (incl. PLC)' for a plan row carrying a charge."""
        try:
            row = next((r for r in (enriched or {}).get("rows") or [] if r.get("id") == milestone_id), None)
            if row and row.get("charge_head") and (row.get("charge_amount") or 0) > 0:
                return f"{row.get('label') or fallback} (incl. {row['charge_head']})"
        except Exception:
            pass
        return fallback

    def _booking_labels(self, customer_id: str, booking_id: str = None):
        """(project, unit, source) for the reminder email. ``booking_id`` picks the
        matching booking for a multi-plot customer; without it (or on any failure)
        it falls back to the most-recent booking application. ``source`` is the
        booking form's origin ('offline_import' for imported buyers)."""
        record = None
        try:
            if booking_id:
                cache = getattr(self, "_applications_cache", None)
                if cache is None:
                    cache = self._applications_cache = {}
                if customer_id not in cache:
                    cache[customer_id] = self._profile.list_booking_applications(customer_id) or []
                for r in cache[customer_id]:
                    if isinstance(r, dict) and r.get("id") == booking_id:
                        record = r
                        break
        except Exception:
            record = None
        if record is None:
            try:
                record = self._profile.get_booking_application(customer_id)
            except Exception:
                record = None
        if not record:
            return None, None, None
        form = record.get("form_data") if isinstance(record, dict) else getattr(record, "form_data", None)
        form = form if isinstance(form, dict) else {}
        project = None
        for k in _PROJECT_NAME_KEYS:
            if form.get(k):
                project = str(form[k]).strip()
                break
        project = project or (record.get("project_id") if isinstance(record, dict) else None)
        unit = None
        for k in _UNIT_KEYS:
            if form.get(k):
                unit = str(form[k]).strip()
                break
        source = str(form.get("source") or "").strip().lower() or None
        return project, unit, source

    def _outstanding_after(self, enriched: dict, milestone_id: str):
        try:
            rows = (enriched or {}).get("rows") or []
            total = sum(int(r.get("amount") or 0) for r in rows)
            received = 0
            hit = False
            for r in rows:
                if r.get("status") == "paid":
                    received += int(r.get("amount") or 0)
                if r.get("id") == milestone_id:
                    received += int(r.get("amount") or 0)
                    hit = True
            return max(0, total - received) if hit else None
        except Exception:
            return None
