"""Token-gated scheduled-job endpoints.

This repo has no scheduler, so a daily cron (cron-job.org, same as the /health
keep-alive) hits this URL with ?key=<DIVINE_JOBS_TOKEN>. The token is a static
shared secret in the environment - there is no user identity, and no request
body: everything the job needs is the query-string key.

Both GET and POST are accepted on the same path so the cron only has to store a
URL - it does not need to be configured to send a particular HTTP method or body.
The call is safe to repeat: every (milestone, kind) reminder is de-duplicated in
divine_payment_reminders, so a double fire on a given day sends nothing extra.
"""
import logging
import os

from fastapi import APIRouter, HTTPException, Query

from DivineService.service_payment_reminders import serviceReminders

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/jobs", tags=["jobs"])


def _require_job_token(key: str) -> None:
    expected = (os.getenv("DIVINE_JOBS_TOKEN") or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="jobs_not_configured")
    if not key or key.strip() != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


def _run_payment_reminders(key: str):
    """Run the daily payment-due reminder sweep. Idempotent within a day - each
    (milestone, kind) reminder is sent at most once (OVERDUE once per ISO week)."""
    _require_job_token(key)
    try:
        return serviceReminders().run()
    except Exception:
        logger.warning("payment_reminders_job_failed", exc_info=True)
        raise HTTPException(status_code=500, detail="job_failed")


@router.get("/payment-reminders")
def run_payment_reminders_get(key: str = Query(None, max_length=200)):
    return _run_payment_reminders(key)


@router.post("/payment-reminders")
def run_payment_reminders_post(key: str = Query(None, max_length=200)):
    return _run_payment_reminders(key)
