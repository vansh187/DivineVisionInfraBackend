"""Daily payment-due reminder job.

Run by a token-gated endpoint (POST /jobs/payment-reminders) that cron-job.org
calls once a day. For every customer with a booked plot and an unpaid next
milestone it sends at most one email per run - the most urgent kind not already
sent (OVERDUE > DUE_TODAY > T_MINUS_5 > T_MINUS_20) - and records a dedupe row.

Fully wrapped: one bad customer is counted and skipped, never aborts the run,
and nothing here raises into the endpoint.
"""
import logging
import uuid
from datetime import date

from Divinepersistence import persistenceMilestone, persistenceCustomer, persistenceCustomerProfile
from DivineService.service_milestones import (
    serviceMilestones, ist_today, PAY_WINDOW_DAYS, REMINDER_LEAD_DAYS, _to_date,
)
from DivineService.service_email import serviceEmail, dispatch_payment_reminder_email

logger = logging.getLogger(__name__)

_UNIT_KEYS = ("unit_number", "unitNumber", "plot_number", "plotNumber", "plot_no", "plotNo")
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

    def run(self, today: date = None) -> dict:
        today = today or ist_today()
        summary = {"scanned_customers": 0, "sent": {}, "skipped": 0, "errors": 0, "no_email": 0}
        try:
            unpaid = self._milestones.list_unpaid()
        except Exception as e:
            logger.warning("reminder_run_list_failed error=%s", e)
            return {**summary, "errors": 1, "fatal": "list_unpaid_failed"}

        seen = set()
        customer_ids = []
        for m in unpaid:
            cid = getattr(m, "customer_id", None)
            if cid and cid not in seen:
                seen.add(cid)
                customer_ids.append(cid)

        for cid in customer_ids:
            summary["scanned_customers"] += 1
            try:
                self._process_customer(cid, today, summary)
            except Exception as e:
                summary["errors"] += 1
                logger.warning("reminder_customer_failed customer_id=%s error=%s", cid, e)
        return summary

    def _process_customer(self, customer_id: str, today: date, summary: dict) -> None:
        enriched = self._milestone_service.enriched_schedule(customer_id)
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

        customer = None
        try:
            customer = self._customers.get_by_id(customer_id)
        except Exception:
            customer = None
        email = getattr(customer, "email", None) if customer else None
        if not email:
            summary["no_email"] += 1
            return

        project_name, unit_number = self._booking_labels(customer_id)
        outstanding = self._outstanding_after(enriched, milestone_id)

        ok = dispatch_payment_reminder_email(
            self._email, email,
            first_name=getattr(customer, "first_name", None),
            project_name=project_name or nd.get("label"),
            unit_number=unit_number,
            milestone_label=nd.get("label"),
            amount=nd.get("amount"),
            due_date=nd.get("due_date"),
            days_remaining=nd.get("days_until_due"),
            outstanding=outstanding,
            kind=kind,
        )

        booking_id = milestone_id.rsplit("-m", 1)[0] if "-m" in milestone_id else None
        try:
            self._milestones.record_reminder(
                id=str(uuid.uuid4()), customer_id=customer_id, booking_id=booking_id,
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

    def _booking_labels(self, customer_id: str):
        try:
            record = self._profile.get_booking_application(customer_id)
        except Exception:
            record = None
        if not record:
            return None, None
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
        return project, unit

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
