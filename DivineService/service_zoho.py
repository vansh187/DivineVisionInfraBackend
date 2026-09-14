import logging
import os
import threading
import time
from pathlib import Path

import requests
from dotenv import load_dotenv, set_key

load_dotenv()

logger = logging.getLogger(__name__)

_TOKEN_URL_TMPL = "https://{domain}/oauth/v2/token"
_UPSERT_URL_TMPL = "{api_base}/crm/v3/{module}/upsert"
_TOKEN_REFRESH_MARGIN_SECONDS = 60
_REQUEST_TIMEOUT_SECONDS = 8
_ENV_FILE_PATH = Path(__file__).resolve().parent.parent / ".env"


class serviceZoho:
    """Best-effort sync of leads/signups into Zoho CRM, alongside (never instead of)
    Postgres. Postgres is always the source of truth and is written first by the
    caller; every public method here is called after that write succeeds and is
    designed to NEVER raise - a Zoho outage, bad credentials, or malformed payload
    must not fail, slow down meaningfully, or roll back the caller's actual request.
    Every method therefore returns a bool (synced or not) instead of raising, and every
    code path - including "unexpected" ones - is wrapped in try/except."""

    _token_lock = threading.Lock()
    _cached_token = None
    _cached_token_expiry = 0.0
    _cached_api_base = None

    def __init__(self):
        try:
            self._client_id = os.getenv("ZOHO_CLIENT_KEY")
            self._client_secret = os.getenv("ZOHO_SECRET_KEY")
            self._refresh_token = os.getenv("ZOHO_REFRESH_TOKEN")
            self._accounts_domain = os.getenv("ZOHO_ACCOUNTS_DOMAIN", "accounts.zoho.in")
            self._api_domain = os.getenv("ZOHO_API_DOMAIN", "www.zohoapis.in")
        except Exception as e:
            logger.warning("zoho_init_failed: %s", e)
            self._client_id = None
            self._client_secret = None
            self._refresh_token = None
            self._accounts_domain = "accounts.zoho.in"
            self._api_domain = "www.zohoapis.in"

    # ---- Config / auth ----------------------------------------------------
    def _configured(self) -> bool:
        try:
            return bool(self._client_id and self._client_secret and self._refresh_token)
        except Exception as e:
            logger.warning("zoho_configured_check_failed: %s", e)
            return False

    def _get_access_token(self):
        try:
            if not self._configured():
                logger.info("zoho_sync_skipped reason=not_configured")
                return None
            with serviceZoho._token_lock:
                now = time.time()
                if serviceZoho._cached_token and now < serviceZoho._cached_token_expiry - _TOKEN_REFRESH_MARGIN_SECONDS:
                    return serviceZoho._cached_token
                try:
                    url = _TOKEN_URL_TMPL.format(domain=self._accounts_domain)
                    resp = requests.post(
                        url,
                        params={
                            "refresh_token": self._refresh_token,
                            "client_id": self._client_id,
                            "client_secret": self._client_secret,
                            "grant_type": "refresh_token",
                        },
                        timeout=_REQUEST_TIMEOUT_SECONDS,
                    )
                except requests.RequestException as e:
                    logger.warning("zoho_token_request_failed: %s", e)
                    return None

                try:
                    if resp.status_code != 200:
                        logger.warning("zoho_token_refresh_failed status=%s body=%s", resp.status_code, resp.text[:300])
                        return None
                    payload = resp.json()
                except Exception as e:
                    logger.warning("zoho_token_response_parse_failed: %s", e)
                    return None

                access_token = payload.get("access_token") if isinstance(payload, dict) else None
                if not access_token:
                    logger.warning("zoho_token_refresh_no_token body=%s", payload)
                    return None

                serviceZoho._cached_token = access_token
                try:
                    expires_in = float(payload.get("expires_in", 3600))
                except (TypeError, ValueError):
                    expires_in = 3600.0
                serviceZoho._cached_token_expiry = now + expires_in

                # Zoho's token response is the authoritative source for which API domain
                # this account's data lives on - prefer it over the static env var guess.
                try:
                    api_domain = payload.get("api_domain") if isinstance(payload, dict) else None
                    if api_domain:
                        serviceZoho._cached_api_base = api_domain
                except Exception as e:
                    logger.warning("zoho_api_domain_cache_failed: %s", e)

                return access_token
        except Exception as e:
            logger.warning("zoho_token_refresh_exception: %s", e)
            return None

    def _invalidate_cached_token(self) -> None:
        try:
            with serviceZoho._token_lock:
                serviceZoho._cached_token = None
                serviceZoho._cached_token_expiry = 0.0
        except Exception as e:
            logger.warning("zoho_token_invalidate_failed: %s", e)

    def _get_api_base(self) -> str:
        try:
            if serviceZoho._cached_api_base:
                return serviceZoho._cached_api_base
            return f"https://{self._api_domain}"
        except Exception as e:
            logger.warning("zoho_api_base_resolve_failed: %s", e)
            return "https://www.zohoapis.in"

    # ---- One-time OAuth bootstrap ---------------------------------------------
    def complete_oauth_setup(self, code: str, redirect_uri: str, accounts_server: str = None) -> dict:
        """Exchanges a ONE-TIME Zoho grant/authorization code for a long-lived refresh
        token, then activates it immediately (updates this process's environment and
        in-memory cache) and best-effort persists it to the local .env file so it
        survives a restart in dev. On a hosted platform without a writable/authoritative
        .env file (e.g. Render), env_file_updated will be False - the caller is expected
        to also copy refresh_token into that platform's own env var dashboard.

        Never raises - always returns a dict with at least {"success": bool}."""
        try:
            if not code:
                return {"success": False, "error": "missing_code"}
            if not self._client_id or not self._client_secret:
                return {"success": False, "error": "zoho_client_not_configured"}

            domain = None
            try:
                if accounts_server:
                    domain = accounts_server.split("://", 1)[-1].strip("/")
            except Exception as e:
                logger.warning("zoho_accounts_server_parse_failed: %s", e)
            domain = domain or self._accounts_domain

            try:
                url = _TOKEN_URL_TMPL.format(domain=domain)
                resp = requests.post(
                    url,
                    params={
                        "grant_type": "authorization_code",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "redirect_uri": redirect_uri,
                        "code": code,
                    },
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as e:
                logger.warning("zoho_oauth_setup_request_failed: %s", e)
                return {"success": False, "error": "request_failed"}

            try:
                payload = resp.json()
            except Exception as e:
                logger.warning("zoho_oauth_setup_response_parse_failed: %s", e)
                return {"success": False, "error": "invalid_response"}

            if resp.status_code != 200 or not isinstance(payload, dict) or not payload.get("refresh_token"):
                logger.warning("zoho_oauth_setup_failed status=%s body=%s", resp.status_code, payload)
                error = payload.get("error") if isinstance(payload, dict) else None
                return {"success": False, "error": error or "no_refresh_token_returned"}

            refresh_token = payload["refresh_token"]
            api_domain = payload.get("api_domain")

            # Activate immediately in THIS process, no restart needed.
            try:
                self._refresh_token = refresh_token
                os.environ["ZOHO_REFRESH_TOKEN"] = refresh_token
                serviceZoho._cached_token = None
                serviceZoho._cached_token_expiry = 0.0
                if api_domain:
                    serviceZoho._cached_api_base = api_domain
            except Exception as e:
                logger.warning("zoho_oauth_setup_activate_failed: %s", e)

            env_file_updated = self._persist_refresh_token_to_env_file(refresh_token)
            render_updated = self._persist_refresh_token_to_render(refresh_token)

            return {
                "success": True,
                "refresh_token": refresh_token,
                "api_domain": api_domain,
                "env_file_updated": env_file_updated,
                "render_updated": render_updated,
            }
        except Exception as e:
            logger.warning("zoho_oauth_setup_exception: %s", e)
            return {"success": False, "error": "unexpected_exception"}

    def _persist_refresh_token_to_env_file(self, refresh_token: str) -> bool:
        try:
            if not _ENV_FILE_PATH.exists():
                logger.info("zoho_env_file_not_found path=%s - skipping local persist", _ENV_FILE_PATH)
                return False
            set_key(str(_ENV_FILE_PATH), "ZOHO_REFRESH_TOKEN", refresh_token)
            return True
        except Exception as e:
            logger.warning("zoho_env_persist_failed: %s", e)
            return False

    def _persist_refresh_token_to_render(self, refresh_token: str) -> bool:
        """Best-effort: writes ZOHO_REFRESH_TOKEN into this service's Render env vars via
        the Render API, so the token survives a restart/redeploy with no manual copy-paste.
        Requires RENDER_API_KEY and RENDER_SERVICE_ID - without either, this is a silent
        no-op (env_file persistence and the in-process activation above still happened)."""
        try:
            api_key = os.getenv("RENDER_API_KEY")
            service_id = os.getenv("RENDER_SERVICE_ID")
            if not api_key or not service_id:
                logger.info("zoho_render_persist_skipped reason=render_not_configured")
                return False

            try:
                resp = requests.put(
                    f"https://api.render.com/v1/services/{service_id}/env-vars/ZOHO_REFRESH_TOKEN",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    json={"value": refresh_token},
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as e:
                logger.warning("zoho_render_persist_request_failed: %s", e)
                return False

            if resp.status_code not in (200, 201):
                logger.warning("zoho_render_persist_failed status=%s body=%s", resp.status_code, resp.text[:300])
                return False
            return True
        except Exception as e:
            logger.warning("zoho_render_persist_exception: %s", e)
            return False

    # ---- Generic upsert -----------------------------------------------------
    def _upsert(self, module: str, record: dict, duplicate_check_fields: list) -> bool:
        try:
            token = self._get_access_token()
            if not token:
                return False

            try:
                clean_record = {k: v for k, v in record.items() if v not in (None, "")}
            except Exception as e:
                logger.warning("zoho_record_clean_failed module=%s error=%s", module, e)
                return False
            if not clean_record:
                logger.info("zoho_upsert_skipped module=%s reason=empty_record", module)
                return False

            body = {"data": [clean_record], "duplicate_check_fields": duplicate_check_fields}
            url = _UPSERT_URL_TMPL.format(api_base=self._get_api_base(), module=module)
            try:
                resp = requests.post(
                    url,
                    headers={
                        "Authorization": f"Zoho-oauthtoken {token}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as e:
                logger.warning("zoho_upsert_request_failed module=%s error=%s", module, e)
                return False

            try:
                if resp.status_code not in (200, 201):
                    logger.warning("zoho_upsert_failed module=%s status=%s body=%s", module, resp.status_code, resp.text[:300])
                    if resp.status_code in (401, 403):
                        # Access token was rejected even though our cache thought it was
                        # still valid (revoked/invalidated server-side) - drop it so the
                        # NEXT call refreshes instead of failing silently for up to an hour.
                        self._invalidate_cached_token()
                    return False
                result = resp.json()
                entries = result.get("data") if isinstance(result, dict) else None
                entry = entries[0] if entries else {}
                status = entry.get("status") if isinstance(entry, dict) else None
            except Exception as e:
                logger.warning("zoho_upsert_response_parse_failed module=%s error=%s", module, e)
                return False

            if status != "success":
                logger.warning("zoho_upsert_rejected module=%s response=%s", module, entry)
                try:
                    if entry.get("code") in ("INVALID_TOKEN", "AUTHENTICATION_FAILURE", "OAUTH_SCOPE_MISMATCH"):
                        self._invalidate_cached_token()
                except Exception as e:
                    logger.warning("zoho_token_invalidate_check_failed: %s", e)
                return False
            logger.info(
                "zoho_upsert_succeeded module=%s action=%s record_id=%s",
                module, entry.get("action"), (entry.get("details") or {}).get("id"),
            )
            return True
        except Exception as e:
            # Final safety net - this method must never raise into the caller.
            logger.warning("zoho_upsert_unexpected_exception module=%s error=%s", module, e)
            return False

    def find_by_email(self, module: str, email: str) -> dict:
        """Diagnostic helper: looks up a record by email directly in Zoho (not our own
        DB) so a sync can be verified/debugged without opening the Zoho UI. Never raises;
        returns {"found": bool, "records": [...]} or {"found": False, "error": ...}."""
        try:
            if not email:
                return {"found": False, "error": "missing_email"}
            token = self._get_access_token()
            if not token:
                return {"found": False, "error": "not_configured_or_token_unavailable"}

            url = f"{self._get_api_base()}/crm/v3/{module}/search"
            try:
                resp = requests.get(
                    url,
                    headers={"Authorization": f"Zoho-oauthtoken {token}"},
                    params={"email": email},
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as e:
                logger.warning("zoho_find_by_email_request_failed module=%s error=%s", module, e)
                return {"found": False, "error": "request_failed"}

            if resp.status_code == 204:
                return {"found": False, "records": []}
            if resp.status_code != 200:
                logger.warning("zoho_find_by_email_failed module=%s status=%s body=%s", module, resp.status_code, resp.text[:300])
                return {"found": False, "error": f"status_{resp.status_code}", "body": resp.text[:300]}

            try:
                payload = resp.json()
                records = payload.get("data", []) if isinstance(payload, dict) else []
            except Exception as e:
                logger.warning("zoho_find_by_email_parse_failed module=%s error=%s", module, e)
                return {"found": False, "error": "invalid_response"}

            return {"found": bool(records), "records": records}
        except Exception as e:
            logger.warning("zoho_find_by_email_exception module=%s error=%s", module, e)
            return {"found": False, "error": "unexpected_exception"}

    @staticmethod
    def _split_name(full_name: str):
        try:
            if not full_name:
                return None, None
            parts = full_name.strip().split(maxsplit=1)
            if len(parts) == 1:
                return None, parts[0]
            return parts[0], parts[1]
        except Exception as e:
            logger.warning("zoho_split_name_failed: %s", e)
            return None, full_name

    # ---- Fire-and-forget helpers ---------------------------------------------
    def _run_async(self, target, *args, **kwargs) -> None:
        """Runs a sync push_* call on a background daemon thread so a slow/unreachable
        Zoho never adds latency to the caller's actual (Postgres-backed) request. The
        target methods already never raise, but this is wrapped too as a final guard -
        even failing to START the thread must not propagate."""
        try:
            threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True).start()
        except Exception as e:
            logger.warning("zoho_async_dispatch_failed target=%s error=%s", getattr(target, "__name__", target), e)

    def push_lead_async(self, **kwargs) -> None:
        self._run_async(self.push_lead, **kwargs)

    def push_customer_signup_async(self, **kwargs) -> None:
        self._run_async(self.push_customer_signup, **kwargs)

    def push_broker_signup_async(self, **kwargs) -> None:
        self._run_async(self.push_broker_signup, **kwargs)

    def push_booking_contact_async(self, **kwargs) -> None:
        self._run_async(self.push_booking_contact, **kwargs)

    # ---- Public sync methods ------------------------------------------------
    def push_lead(self, lead_id: str, visitor_name: str = None, visitor_phone: str = None,
                  visitor_email: str = None, lead_temperature: str = None) -> bool:
        """Syncs a chatbot lead into the Zoho CRM Leads module. Upserts on every contact
        identifier known so far (Email and/or Phone) so that as a visitor's info fills in
        across separate calls, later calls still match and update the same Zoho record
        instead of creating a duplicate keyed on whichever single field an earlier call
        happened to have. Callers should pass the lead's full current known state (not
        just the field that just changed) for this to hold."""
        try:
            dup_fields = []
            if visitor_email:
                dup_fields.append("Email")
            if visitor_phone:
                dup_fields.append("Phone")
            if not dup_fields:
                logger.info("zoho_push_lead_skipped lead_id=%s reason=no_email_or_phone", lead_id)
                return False

            first_name, last_name = self._split_name(visitor_name)
            record = {
                "Last_Name": last_name or "Website Visitor",
                "First_Name": first_name,
                "Email": visitor_email,
                "Phone": visitor_phone,
                "Lead_Source": "Website Chatbot",
                "Description": f"Divine chatbot lead_id={lead_id}" + (f", temperature={lead_temperature}" if lead_temperature else ""),
            }
            return self._upsert("Leads", record, dup_fields)
        except Exception as e:
            logger.warning("zoho_push_lead_exception lead_id=%s error=%s", lead_id, e)
            return False

    def push_customer_signup(self, customer_id: str, username: str, first_name: str = None,
                              last_name: str = None, email: str = None, phone: str = None) -> bool:
        return self._push_signup_lead(
            record_id=customer_id, role="Customer", username=username,
            first_name=first_name, last_name=last_name, email=email, phone=phone,
        )

    def push_broker_signup(self, broker_id: str, username: str, first_name: str = None,
                            last_name: str = None, email: str = None, phone: str = None) -> bool:
        return self._push_signup_lead(
            record_id=broker_id, role="Broker", username=username,
            first_name=first_name, last_name=last_name, email=email, phone=phone,
        )

    def _push_signup_lead(self, record_id: str, role: str, username: str, first_name: str,
                           last_name: str, email: str, phone: str) -> bool:
        """Syncs a customer/broker signup into the Zoho CRM Leads module - per the client,
        every synced record (signups and chatbot leads alike) lands in Leads, never
        Contacts. Upserts on Email and/or Phone so a retry updates the same Lead instead
        of duplicating it; with neither identifier there's nothing safe to dedupe on, so
        the sync is skipped."""
        try:
            dup_fields = []
            if email:
                dup_fields.append("Email")
            if phone:
                dup_fields.append("Phone")
            if not dup_fields:
                logger.info("zoho_push_signup_skipped role=%s id=%s reason=no_email_or_phone", role, record_id)
                return False
            record = {
                "Last_Name": last_name or username or record_id,
                "First_Name": first_name,
                "Email": email,
                "Phone": phone,
                "Lead_Source": f"Website {role} Signup",
                "Description": f"Divine {role} signup, id={record_id}, username={username}",
            }
            return self._upsert("Leads", record, dup_fields)
        except Exception as e:
            logger.warning("zoho_push_signup_exception role=%s id=%s error=%s", role, record_id, e)
            return False

    def push_booking_contact(self, customer_id: str, first_name: str = None, last_name: str = None,
                              email: str = None, phone: str = None, inventory_id: str = None,
                              payment_id: str = None, purpose: str = None, payment_method: str = None,
                              booking_id: str = None, project_name: str = None, unit_number: str = None,
                              booking_amount=None, booking_status: str = None, kyc_status: str = None) -> bool:
        """Syncs a customer into the Zoho CRM Contacts module the moment their FIRST
        plot booking is confirmed (payment settled AND the unit actually flipped to
        'booked' - Razorpay or a trusted cash/RTGS/cheque booking alike). Per the
        client, only the booking itself lands in Contacts - later instalments on that
        same plot do not push again (signups and chatbot activity still land in Leads,
        untouched - see push_lead / push_customer_signup / push_broker_signup). Upserts
        on Email and/or Phone so a retry (e.g. the webhook re-confirming a booking
        /verify already settled) updates the same Contact instead of duplicating it;
        with neither identifier there's nothing safe to dedupe on, so the sync is
        skipped."""
        try:
            dup_fields = []
            if email:
                dup_fields.append("Email")
            if phone:
                dup_fields.append("Phone")
            if not dup_fields:
                logger.info("zoho_push_booking_contact_skipped customer_id=%s reason=no_email_or_phone", customer_id)
                return False
            details = [
                ("customer_id", customer_id),
                ("booking_id", booking_id),
                ("project_name", project_name),
                ("unit_number", unit_number),
                ("booking_amount", booking_amount),
                ("booking_status", booking_status),
                ("kyc_status", kyc_status),
                ("purpose", purpose),
                ("payment_method", payment_method),
                ("inventory_id", inventory_id),
                ("payment_id", payment_id),
            ]
            description = "Divine plot booking confirmed" + "".join(
                f", {key}={value}" for key, value in details if value is not None and value != ""
            )
            record = {
                "Last_Name": last_name or customer_id,
                "First_Name": first_name,
                "Email": email,
                "Phone": phone,
                "Lead_Source": "Website Plot Booking",
                "Description": description,
            }
            return self._upsert("Contacts", record, dup_fields)
        except Exception as e:
            logger.warning("zoho_push_booking_contact_exception customer_id=%s error=%s", customer_id, e)
            return False
