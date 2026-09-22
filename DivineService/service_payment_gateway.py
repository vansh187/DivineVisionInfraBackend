import hashlib
import hmac
import logging
import os
import re
import threading
import time
from decimal import Decimal, ROUND_HALF_UP

import requests

logger = logging.getLogger(__name__)

_TOKEN_URL_TMPL = "https://{domain}/oauth/v2/token"
_SESSION_CREATE_URL_TMPL = "{api_base}/api/v1/paymentsessions"
_SESSION_RETRIEVE_URL_TMPL = "{api_base}/api/v1/paymentsessions/{session_id}"
_REFUND_CREATE_URL_TMPL = "{api_base}/api/v1/payments/{payment_id}/refunds"
_REFUND_RETRIEVE_URL_TMPL = "{api_base}/api/v1/refunds/{refund_id}"
_TOKEN_REFRESH_MARGIN_SECONDS = 60
_REQUEST_TIMEOUT_SECONDS = 15
_WEBHOOK_REPLAY_WINDOW_SECONDS = 15 * 60

_REFUND_REASONS = (
    "duplicate", "fraudulent", "requested_by_customer",
    "others", "system_initiated", "expired_uncaptured_charge",
)
_REFUND_TYPES = ("initiated_by_merchant", "initiated_by_customer", "initiated_by_system")


class servicePaymentGateway:
    """Zoho Payments gateway client - creates hosted-checkout payment sessions,
    verifies the redirect signature and webhook signature Zoho sends back, and
    calls the refund API. This is real money, so - unlike serviceZoho (CRM sync,
    which is best-effort and never raises) - every method here RAISES
    (RuntimeError) on a configuration problem or a gateway failure. The caller
    (DivineService/service_payment.py) is responsible for deciding what a
    failure means for the payment (e.g. its own retry-once-then-'pending'
    policy for refunds); this module never silently pretends a failed call
    succeeded.

    Token caching mirrors serviceZoho's pattern (same refresh_token OAuth grant,
    same accounts.zoho.in domain), but is a SEPARATE Zoho app/connected-app from
    Zoho CRM - different client id/secret/refresh token/account id, since Zoho
    Payments and Zoho CRM are different products with different credentials."""

    _token_lock = threading.Lock()
    _cached_token = None
    _cached_token_expiry = 0.0

    def __init__(self):
        self._client_id = os.getenv("ZOHO_PAYMENTS_CLIENT_ID")
        self._client_secret = os.getenv("ZOHO_PAYMENTS_CLIENT_SECRET")
        self._refresh_token = os.getenv("ZOHO_PAYMENTS_REFRESH_TOKEN")
        self._account_id = os.getenv("ZOHO_PAYMENTS_ACCOUNT_ID")
        self._accounts_domain = os.getenv("ZOHO_PAYMENTS_ACCOUNTS_DOMAIN", "accounts.zoho.in")
        self._api_domain = os.getenv("ZOHO_PAYMENTS_API_DOMAIN", "payments.zoho.in")
        self._signing_key = os.getenv("ZOHO_PAYMENTS_SIGNING_KEY")
        self._webhook_secret = os.getenv("ZOHO_PAYMENTS_WEBHOOK_SECRET")

    # ---- Config -------------------------------------------------------------
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret and self._refresh_token and self._account_id)

    def _require_configured(self) -> None:
        if not self.configured():
            raise RuntimeError("payment_not_configured")

    @staticmethod
    def to_gateway_amount(amount) -> str:
        """Zoho Payments takes a DECIMAL amount string (e.g. "50000.00"), never
        paise/cents like Razorpay's amount*100 convention. Getting this wrong
        would overcharge or undercharge every customer by 100x - this is the
        single call site that formats an amount for the gateway; every other
        method here takes an already-formatted string or Decimal."""
        return str(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    # ---- Auth -----------------------------------------------------------------
    def _get_access_token(self) -> str:
        self._require_configured()
        with servicePaymentGateway._token_lock:
            now = time.time()
            if (servicePaymentGateway._cached_token and
                    now < servicePaymentGateway._cached_token_expiry - _TOKEN_REFRESH_MARGIN_SECONDS):
                return servicePaymentGateway._cached_token
            return self._refresh_token_locked()

    def _refresh_token_locked(self) -> str:
        """Caller must already hold _token_lock."""
        url = _TOKEN_URL_TMPL.format(domain=self._accounts_domain)
        try:
            logger.info("payment_gateway.token_request_start domain=%s", self._accounts_domain)
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
            logger.info("payment_gateway.token_request_done domain=%s status=%s", self._accounts_domain, resp.status_code)
        except requests.RequestException as e:
            raise RuntimeError("payment_gateway_auth_failed") from e

        if resp.status_code != 200:
            logger.warning("payment_gateway.token_refresh_failed status=%s body=%s", resp.status_code, resp.text[:300])
            raise RuntimeError("payment_gateway_auth_failed")

        try:
            payload = resp.json()
        except ValueError as e:
            raise RuntimeError("payment_gateway_auth_failed") from e

        access_token = payload.get("access_token") if isinstance(payload, dict) else None
        if not access_token:
            logger.warning("payment_gateway.token_refresh_no_token body=%s", payload)
            raise RuntimeError("payment_gateway_auth_failed")

        servicePaymentGateway._cached_token = access_token
        try:
            expires_in = float(payload.get("expires_in", 3600))
        except (TypeError, ValueError):
            expires_in = 3600.0
        servicePaymentGateway._cached_token_expiry = time.time() + expires_in

        return access_token

    def _invalidate_cached_token(self) -> None:
        with servicePaymentGateway._token_lock:
            servicePaymentGateway._cached_token = None
            servicePaymentGateway._cached_token_expiry = 0.0

    def _get_api_base(self) -> str:
        return f"https://{self._api_domain}"

    def _auth_headers(self, token: str) -> dict:
        return {"Authorization": f"Zoho-oauthtoken {token}", "Content-Type": "application/json"}

    def _request(self, method: str, url: str, retry_on_auth_error: bool = True, **kwargs) -> dict:
        """One authenticated gateway call, with exactly one retry (fetching a
        fresh token) if the first attempt comes back 401/403 - the same
        "cache thought the token was still valid but the server disagreed"
        case serviceZoho guards against. Raises RuntimeError on any failure
        that survives the retry; never returns a partial/ambiguous result."""
        token = self._get_access_token()
        try:
            resp = requests.request(method, url, headers=self._auth_headers(token), timeout=_REQUEST_TIMEOUT_SECONDS, **kwargs)
        except requests.RequestException as e:
            raise RuntimeError("payment_gateway_request_failed") from e

        if resp.status_code in (401, 403) and retry_on_auth_error:
            self._invalidate_cached_token()
            return self._request(method, url, retry_on_auth_error=False, **kwargs)

        try:
            body = resp.json()
        except ValueError:
            body = None

        if resp.status_code not in (200, 201):
            detail = self._gateway_error_detail(resp.status_code, body, resp.text)
            logger.warning("payment_gateway.request_failed method=%s url=%s status=%s body=%s",
                            method, url, resp.status_code, (resp.text or "")[:300])
            raise RuntimeError(f"payment_gateway_error:{detail}")

        if not isinstance(body, dict):
            raise RuntimeError("payment_gateway_invalid_response")
        return body

    @staticmethod
    def _gateway_error_detail(status_code: int, body, raw_text: str) -> str:
        message = None
        if isinstance(body, dict):
            message = body.get("message") or (body.get("error") if isinstance(body.get("error"), str) else None)
        raw = (message or raw_text or "").strip()
        safe = re.sub(r"[^a-zA-Z0-9_.:-]+", "_", raw).strip("_").lower()
        return f"status_{status_code}" if not safe else f"status_{status_code}:{safe[:120]}"

    # ---- Payment sessions (order creation) -------------------------------------
    def create_payment_session(self, amount, currency: str, description: str,
                                success_url: str, failure_url: str, invoice_number: str = None,
                                name: str = None, email: str = None, phone: str = None,
                                udf1: str = None, udf2: str = None, udf3: str = None,
                                udf4: str = None, udf5: str = None) -> dict:
        self._require_configured()
        hosted_checkout_parameters = {
            "description": (description or "")[:500],
            "success_url": success_url,
            "failure_url": failure_url,
        }
        if name:
            hosted_checkout_parameters["name"] = name
        if email:
            hosted_checkout_parameters["email"] = email
        if phone:
            hosted_checkout_parameters["phone"] = phone
        for key, value in (("udf1", udf1), ("udf2", udf2), ("udf3", udf3), ("udf4", udf4), ("udf5", udf5)):
            if value:
                hosted_checkout_parameters[key] = str(value)[:200]

        body = {
            "amount": self.to_gateway_amount(amount),
            "currency": currency,
            "description": (description or "")[:500],
            "configurations": {"hosted_checkout_parameters": hosted_checkout_parameters},
        }
        if invoice_number:
            body["invoice_number"] = invoice_number[:50]

        url = f"{_SESSION_CREATE_URL_TMPL.format(api_base=self._get_api_base())}?account_id={self._account_id}"
        logger.info("payment_gateway.session_create_start amount=%s currency=%s", body["amount"], currency)
        result = self._request("POST", url, json=body)
        session = result.get("payments_session") or {}
        payments_session_id = session.get("payments_session_id")
        access_key = session.get("access_key")
        if not payments_session_id or not access_key:
            raise RuntimeError("payment_gateway_invalid_response")
        logger.info("payment_gateway.session_create_done payments_session_id=%s", payments_session_id)
        return {
            "payments_session_id": payments_session_id,
            "access_key": access_key,
            "checkout_url": f"https://{self._api_domain}/hostedcheckout/{access_key}",
            "amount": session.get("amount", body["amount"]),
            "currency": session.get("currency", currency),
        }

    def retrieve_payment_session(self, payments_session_id: str) -> dict:
        self._require_configured()
        url = (f"{_SESSION_RETRIEVE_URL_TMPL.format(api_base=self._get_api_base(), session_id=payments_session_id)}"
               f"?account_id={self._account_id}")
        result = self._request("GET", url)
        session = result.get("payments_session") or {}
        return {
            "payments_session_id": session.get("payments_session_id", payments_session_id),
            "status": session.get("status"),
            "amount": session.get("amount"),
            "currency": session.get("currency"),
            "payments": session.get("payments") or [],
        }

    # ---- Redirect signature (hosted-checkout success/failure callback) --------
    def verify_redirect_signature(self, payments_session_id: str, payment_session_status: str,
                                   payment_id: str, payment_status: str, amount: str,
                                   signature: str, udf1: str = None, udf2: str = None,
                                   udf3: str = None, udf4: str = None, udf5: str = None) -> bool:
        """HMAC-SHA256 over the dot-joined redirect fields, keyed with the
        per-account signing key (Settings > Developer Space, separate from the
        OAuth client secret). Never raises on a bad/missing signature - that's
        an expected "verification failed" outcome, not a gateway error; only
        a missing signing key configuration raises."""
        if not self._signing_key:
            raise RuntimeError("payment_signing_key_not_configured")
        if not signature:
            return False
        parts = [
            payments_session_id or "", payment_session_status or "",
            payment_id or "", payment_status or "", amount or "",
            udf1 or "", udf2 or "", udf3 or "", udf4 or "", udf5 or "",
        ]
        signed_payload = ".".join(parts)
        expected = hmac.new(self._signing_key.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature.strip().lower())

    # ---- Webhook signature ---------------------------------------------------
    def verify_webhook_signature(self, raw_body: bytes, header_value: str) -> bool:
        """X-Zoho-Webhook-Signature: "t=<unix_ms>,v=<hex hmac-sha256>". Signed
        payload is f"{t}.{raw_body}" keyed with the webhook signing secret
        (configured separately per webhook in Zoho Payments settings). Rejects
        a signature whose timestamp is outside a 15-minute window even if the
        HMAC matches, to block a captured/replayed webhook delivery - Zoho's
        own docs only describe the HMAC check, this replay guard is a
        deliberate hardening on top of the documented minimum. Never raises on
        a bad/malformed header - only a missing webhook secret configuration
        raises."""
        if not self._webhook_secret:
            raise RuntimeError("payment_webhook_not_configured")
        if not header_value:
            return False

        fields = {}
        for part in header_value.split(","):
            if "=" not in part:
                continue
            key, _, value = part.strip().partition("=")
            fields[key.strip().lower()] = value.strip()
        timestamp = fields.get("t")
        signature = fields.get("v")
        if not timestamp or not signature:
            return False

        try:
            timestamp_seconds = int(timestamp) / 1000.0
        except (TypeError, ValueError):
            return False
        if abs(time.time() - timestamp_seconds) > _WEBHOOK_REPLAY_WINDOW_SECONDS:
            logger.info("payment_gateway.webhook_signature_stale timestamp=%s", timestamp)
            return False

        try:
            body_text = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            return False
        signed_payload = f"{timestamp}.{body_text}"
        expected = hmac.new(self._webhook_secret.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature.strip().lower())

    # ---- Refunds ----------------------------------------------------------
    def create_refund(self, payment_id: str, amount, reason: str = "requested_by_customer",
                       type_: str = "initiated_by_merchant", description: str = None) -> dict:
        self._require_configured()
        clean_reason = reason if reason in _REFUND_REASONS else "others"
        clean_type = type_ if type_ in _REFUND_TYPES else "initiated_by_merchant"
        body = {"amount": self.to_gateway_amount(amount), "reason": clean_reason, "type": clean_type}
        if description:
            body["description"] = description[:500]

        url = (f"{_REFUND_CREATE_URL_TMPL.format(api_base=self._get_api_base(), payment_id=payment_id)}"
               f"?account_id={self._account_id}")
        logger.info("payment_gateway.refund_create_start payment_id=%s amount=%s", payment_id, body["amount"])
        result = self._request("POST", url, json=body)
        refund = result.get("refund") or {}
        refund_id = refund.get("refund_id")
        if not refund_id:
            raise RuntimeError("payment_gateway_invalid_response")
        logger.info("payment_gateway.refund_create_done payment_id=%s refund_id=%s status=%s",
                    payment_id, refund_id, refund.get("status"))
        return {"refund_id": refund_id, "status": refund.get("status"), "amount": refund.get("amount")}

    def retrieve_refund(self, refund_id: str) -> dict:
        self._require_configured()
        url = (f"{_REFUND_RETRIEVE_URL_TMPL.format(api_base=self._get_api_base(), refund_id=refund_id)}"
               f"?account_id={self._account_id}")
        result = self._request("GET", url)
        refund = result.get("refund") or {}
        return {"refund_id": refund.get("refund_id", refund_id), "status": refund.get("status"), "amount": refund.get("amount")}
