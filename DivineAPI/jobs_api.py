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
import hmac
import logging
import os

from fastapi import APIRouter, Header, HTTPException, Query

from DivineService.service_payment_reminders import serviceReminders

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/jobs", tags=["jobs"])


def _require_job_token(key: str, header_token: str = None) -> None:
    """The shared secret may come as the X-Jobs-Token header (preferred - kept
    out of access logs) or the legacy ?key= query parameter. Constant-time compare."""
    expected = (os.getenv("DIVINE_JOBS_TOKEN") or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="jobs_not_configured")
    supplied = (header_token or key or "").strip()
    if not supplied or not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="unauthorized")


def _run_payment_reminders(key: str, header_token: str = None):
    """Run the daily payment-due reminder sweep. Idempotent within a day - each
    (milestone, kind) reminder is sent at most once (OVERDUE once per ISO week)."""
    _require_job_token(key, header_token)
    try:
        return serviceReminders().run()
    except Exception:
        logger.warning("payment_reminders_job_failed", exc_info=True)
        raise HTTPException(status_code=500, detail="job_failed")


@router.get("/payment-reminders")
def run_payment_reminders_get(key: str = Query(None, max_length=200),
                              x_jobs_token: str = Header(None, max_length=200)):
    return _run_payment_reminders(key, x_jobs_token)


@router.post("/payment-reminders")
def run_payment_reminders_post(key: str = Query(None, max_length=200),
                               x_jobs_token: str = Header(None, max_length=200)):
    return _run_payment_reminders(key, x_jobs_token)
