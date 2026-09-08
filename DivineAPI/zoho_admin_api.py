import logging
import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from DivineService import serviceZoho

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/zoho", tags=["admin-zoho"])
_zoho_service = serviceZoho()


def _setup_token_valid(key: str) -> bool:
    try:
        expected = os.getenv("ZOHO_OAUTH_SETUP_TOKEN")
        return bool(expected) and bool(key) and key == expected
    except Exception as e:
        logger.warning("zoho_oauth_setup_token_check_failed: %s", e)
        return False


@router.get("/oauth/start")
def start_oauth(key: str = Query(...)):
    """One-click bootstrap: visit this URL (with the setup key) in a browser, log into
    Zoho, approve access - Zoho then redirects to /oauth/callback below, which exchanges
    the grant code for a refresh token automatically. Never used by the running app
    itself; this exists purely so a human doesn't have to hand-run curl commands."""
    try:
        if not _setup_token_valid(key):
            return JSONResponse({"detail": "forbidden"}, status_code=403)

        redirect_uri = os.getenv("ZOHO_REDIRECT_URI")
        client_id = os.getenv("ZOHO_CLIENT_KEY")
        accounts_domain = os.getenv("ZOHO_ACCOUNTS_DOMAIN", "accounts.zoho.in")
        if not redirect_uri or not client_id:
            return JSONResponse({"detail": "zoho_not_configured"}, status_code=500)

        authorize_url = (
            f"https://{accounts_domain}/oauth/v2/auth"
            f"?scope=ZohoCRM.modules.ALL"
            f"&client_id={client_id}"
            f"&response_type=code"
            f"&access_type=offline"
            f"&prompt=consent"
            f"&redirect_uri={redirect_uri}"
            f"&state={key}"
        )
        return RedirectResponse(authorize_url)
    except Exception as e:
        logger.warning("zoho_oauth_start_failed: %s", e)
        return JSONResponse({"detail": "internal_error"}, status_code=500)


@router.get("/oauth/callback")
def oauth_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    accounts_server: str = Query(None, alias="accounts-server"),
):
    """Zoho redirects here after the user approves access in /oauth/start. Exchanges the
    one-time grant code for a refresh token and activates/persists it - see
    serviceZoho.complete_oauth_setup for exactly what that does and doesn't guarantee on
    a hosted platform like Render."""
    try:
        if not _setup_token_valid(state):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        if error:
            return JSONResponse({"detail": "zoho_authorization_denied", "error": error}, status_code=400)
        if not code:
            return JSONResponse({"detail": "missing_code"}, status_code=400)

        redirect_uri = os.getenv("ZOHO_REDIRECT_URI")
        if not redirect_uri:
            return JSONResponse({"detail": "zoho_not_configured"}, status_code=500)

        result = _zoho_service.complete_oauth_setup(code=code, redirect_uri=redirect_uri, accounts_server=accounts_server)

        if not result.get("success"):
            return JSONResponse({"detail": "zoho_oauth_setup_failed", "reason": result.get("error")}, status_code=502)

        notes = ["It is active for this running process right now - Zoho sync works immediately."]
        if result.get("env_file_updated"):
            notes.append("Saved to the local .env file automatically.")
        if result.get("render_updated"):
            notes.append("Saved to Render's env vars automatically - it will survive restarts/redeploys "
                          "(Render is redeploying this service now to pick it up).")
        elif not result.get("env_file_updated"):
            notes.append("Could not persist it anywhere durable (no local .env file, and RENDER_API_KEY/"
                          "RENDER_SERVICE_ID aren't set) - copy the token below into ZOHO_REFRESH_TOKEN "
                          "in your Render dashboard yourself so it survives a restart.")

        return HTMLResponse(
            "<html><body style='font-family:sans-serif'>"
            "<h3>Zoho CRM connected</h3>"
            + "".join(f"<p>{n}</p>" for n in notes)
            + f"<p><b>ZOHO_REFRESH_TOKEN</b>: <code>{result.get('refresh_token')}</code></p>"
            "</body></html>"
        )
    except Exception as e:
        logger.warning("zoho_oauth_callback_failed: %s", e)
        return JSONResponse({"detail": "internal_error"}, status_code=500)


@router.get("/status")
def status(key: str = Query(...)):
    """Reports whether Zoho sync is configured, WITHOUT exposing any secret values -
    just presence/absence of each piece. Useful to confirm bootstrap succeeded without
    needing to open Zoho's UI or the .env file."""
    try:
        if not _setup_token_valid(key):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        return {
            "zoho_client_configured": bool(os.getenv("ZOHO_CLIENT_KEY") and os.getenv("ZOHO_SECRET_KEY")),
            "zoho_refresh_token_set": bool(os.getenv("ZOHO_REFRESH_TOKEN")),
            "render_persistence_configured": bool(os.getenv("RENDER_API_KEY") and os.getenv("RENDER_SERVICE_ID")),
        }
    except Exception as e:
        logger.warning("zoho_status_check_failed: %s", e)
        return JSONResponse({"detail": "internal_error"}, status_code=500)


@router.get("/lookup")
def lookup(key: str = Query(...), email: str = Query(...), module: str = Query("Leads")):
    """Diagnostic: checks whether a record with this email actually exists in Zoho right
    now (queries Zoho directly, not our own DB) - lets a specific signup/lead be
    confirmed or ruled out without opening the Zoho UI. module defaults to Leads (all
    signups and chatbot leads sync there now); pass module=Contacts only to look up
    legacy records written before that change."""
    try:
        if not _setup_token_valid(key):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        if module not in ("Contacts", "Leads"):
            return JSONResponse({"detail": "invalid_module"}, status_code=400)
        return _zoho_service.find_by_email(module, email)
    except Exception as e:
        logger.warning("zoho_lookup_failed: %s", e)
        return JSONResponse({"detail": "internal_error"}, status_code=500)


@router.post("/test-push")
def test_push(key: str = Query(...)):
    """Writes ONE real dummy record into Zoho CRM's Leads module (visible in your Zoho
    account, easy to spot/delete: name 'Zoho Integration Test', email
    zoho-integration-test+<uuid>@example.com) to confirm end-to-end connectivity -
    token refresh, API domain resolution, and the upsert call all actually work against
    your live Zoho account, not just against this backend's own code."""
    try:
        if not _setup_token_valid(key):
            return JSONResponse({"detail": "forbidden"}, status_code=403)

        marker = uuid.uuid4().hex[:8]
        test_email = f"zoho-integration-test+{marker}@example.com"
        pushed = _zoho_service.push_lead(
            lead_id=f"diagnostic-{marker}",
            visitor_name="Zoho Integration Test",
            visitor_email=test_email,
            lead_temperature="cold",
        )
        return {
            "pushed": pushed,
            "test_email": test_email,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "hint": "pushed=false usually means ZOHO_REFRESH_TOKEN isn't set yet - run /oauth/start first."
                    if not pushed else "Check Zoho CRM > Leads for this record to confirm.",
        }
    except Exception as e:
        logger.warning("zoho_test_push_failed: %s", e)
        return JSONResponse({"detail": "internal_error"}, status_code=500)
