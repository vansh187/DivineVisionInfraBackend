import hashlib
import hmac
import json
import os
import time
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"
# Set before importing DivineAPI.main (which calls load_dotenv()) - load_dotenv() doesn't
# override vars already present in the environment, so this guarantees webhook/signature
# tests below use known values instead of depending on whatever's in the real .env.
_WEBHOOK_SECRET = "test_webhook_secret_value"
_SIGNING_KEY = "test_signing_key_value"
os.environ["ZOHO_PAYMENTS_WEBHOOK_SECRET"] = _WEBHOOK_SECRET
os.environ["ZOHO_PAYMENTS_SIGNING_KEY"] = _SIGNING_KEY
os.environ["ZOHO_PAYMENTS_SUCCESS_URL"] = "https://www.divinevisioninfra.com/customer/payments/success"
os.environ["ZOHO_PAYMENTS_FAILURE_URL"] = "https://www.divinevisioninfra.com/customer/payments/failure"

from Divinepersistence.persistence_db import PersistenceDB
from DivineService.service_payment_gateway import servicePaymentGateway
from DivineAPI.main import app
from DivineAPI import payment_api

client = TestClient(app)

# The app's payment_api module builds one servicePayment() singleton (and its own
# servicePaymentGateway) at import time - too early for this test file's own env vars
# to reach it if some earlier-imported test file already triggered that import in this
# same pytest session. Setting the singleton's gateway attributes directly (rather than
# relying on the env vars alone) guarantees this file's signing key/webhook secret are
# actually the ones in effect, regardless of import order across the test session.
payment_api._payment_service._gateway._webhook_secret = _WEBHOOK_SECRET
payment_api._payment_service._gateway._signing_key = _SIGNING_KEY

_TOKEN = None
_OTHER_TOKEN = None
_BROKER_TOKEN = None


def setup_module(module):
    global _TOKEN, _OTHER_TOKEN, _BROKER_TOKEN
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()

    client.post("/customer/signup", json={"username": "paycust_shared", "password": "strongpassword", "phone": "9876500011"})
    lr = client.post("/customer/login", json={"username": "paycust_shared", "password": "strongpassword"})
    assert lr.status_code == 200, lr.text
    _TOKEN = lr.json()["access_token"]

    client.post("/customer/signup", json={"username": "paycust_other", "password": "strongpassword", "phone": "9876500012"})
    lr2 = client.post("/customer/login", json={"username": "paycust_other", "password": "strongpassword"})
    assert lr2.status_code == 200, lr2.text
    _OTHER_TOKEN = lr2.json()["access_token"]

    client.post("/broker/signup", json={"username": "paybroker_shared", "password": "strongpassword", "phone": "9876500013", "project": "suraksha-enclave"})
    lr3 = client.post("/broker/login", json={"username": "paybroker_shared", "password": "strongpassword"})
    assert lr3.status_code == 200, lr3.text
    _BROKER_TOKEN = lr3.json()["access_token"]


def _auth_headers(token=None):
    return {"Authorization": f"Bearer {token or _TOKEN}"}


def _redirect_signature(payments_session_id, payment_session_status, payment_id, payment_status, amount):
    parts = [payments_session_id, payment_session_status, payment_id, payment_status, amount, "", "", "", "", ""]
    return hmac.new(_SIGNING_KEY.encode("utf-8"), ".".join(parts).encode("utf-8"), hashlib.sha256).hexdigest()


def _sign_webhook(body: str) -> str:
    timestamp = str(int(time.time() * 1000))
    signature = hmac.new(_WEBHOOK_SECRET.encode("utf-8"), f"{timestamp}.{body}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"t={timestamp},v={signature}"


def _webhook_body(event: str, session_id: str, payment_id: str = "pay_webhook_test") -> str:
    return json.dumps({
        "event": event,
        "payload": {"payment": {"payments_session_id": session_id, "payment_id": payment_id}},
    })


# ---------- POST /payments/create-order ----------

def test_create_order_requires_auth():
    r = client.post("/payments/create-order", json={"amount": 1000})
    assert r.status_code == 401


def test_create_order_rejects_non_positive_amount():
    r = client.post("/payments/create-order", json={"amount": 0}, headers=_auth_headers())
    assert r.status_code == 422  # pydantic gt=0 constraint


@patch.object(servicePaymentGateway, "create_payment_session")
def test_create_order_happy_path(mock_create_session):
    mock_create_session.return_value = {
        "payments_session_id": "session_test123", "access_key": "key_test123",
        "checkout_url": "https://payments.zoho.in/hostedcheckout/key_test123",
        "amount": "25000.50", "currency": "INR",
    }

    r = client.post("/payments/create-order", json={"amount": 25000.50}, headers=_auth_headers())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["zoho_payments_session_id"] == "session_test123"
    assert data["amount"] == 25000.50
    assert data["currency"] == "INR"
    assert data["status"] == "created"
    assert data["checkout_url"] == "https://payments.zoho.in/hostedcheckout/key_test123"
    assert data["access_key"] == "key_test123"
    # The critical, highest-risk assertion in this whole migration: the gateway
    # must receive a decimal amount, never Razorpay's amount*100 paise convention.
    _, call_kwargs = mock_create_session.call_args
    assert call_kwargs["amount"] == 25000.50


@patch.object(servicePaymentGateway, "create_payment_session", side_effect=RuntimeError("payment_not_configured"))
def test_create_order_returns_502_when_not_configured(mock_create_session):
    r = client.post("/payments/create-order", json={"amount": 1000}, headers=_auth_headers())
    assert r.status_code == 502
    assert r.json()["detail"] == "payment_order_failed:payment_not_configured"


@patch.object(
    servicePaymentGateway,
    "create_payment_session",
    side_effect=RuntimeError("payment_gateway_error:status_400:amount_exceeds_maximum_allowed"),
)
def test_create_order_returns_400_for_gateway_bad_request(mock_create_session):
    r = client.post("/payments/create-order", json={"amount": 600000}, headers=_auth_headers())
    assert r.status_code == 400
    assert "amount_exceeds_maximum_allowed" in r.json()["detail"]


# ---------- POST /payments/verify ----------

def test_verify_requires_auth():
    r = client.post("/payments/verify", json={
        "payments_session_id": "session_x", "payment_id": "pay_x", "payment_status": "succeeded",
        "amount": "100.00", "signature": "sig_x",
    })
    assert r.status_code == 401


@patch.object(servicePaymentGateway, "retrieve_payment_session")
@patch.object(servicePaymentGateway, "create_payment_session")
def test_verify_payment_full_flow_success(mock_create_session, mock_retrieve_session):
    mock_create_session.return_value = {
        "payments_session_id": "session_flow1", "access_key": "key_flow1",
        "checkout_url": "https://payments.zoho.in/hostedcheckout/key_flow1",
        "amount": "5000.00", "currency": "INR",
    }
    mock_retrieve_session.return_value = {"status": "succeeded"}

    create_res = client.post("/payments/create-order", json={"amount": 5000}, headers=_auth_headers())
    assert create_res.status_code == 200, create_res.text

    signature = _redirect_signature("session_flow1", "succeeded", "pay_flow1", "succeeded", "5000.00")
    verify_res = client.post("/payments/verify", json={
        "payments_session_id": "session_flow1",
        "payment_id": "pay_flow1",
        "payment_status": "succeeded",
        "amount": "5000.00",
        "signature": signature,
    }, headers=_auth_headers())
    assert verify_res.status_code == 200, verify_res.text
    data = verify_res.json()
    assert data["status"] == "paid"
    assert data["verified"] is True
    assert data["zoho_payment_id"] == "pay_flow1"


@patch.object(servicePaymentGateway, "create_payment_session")
def test_verify_payment_full_flow_forged_signature(mock_create_session):
    mock_create_session.return_value = {
        "payments_session_id": "session_flow2", "access_key": "key_flow2",
        "checkout_url": "https://payments.zoho.in/hostedcheckout/key_flow2",
        "amount": "5000.00", "currency": "INR",
    }

    create_res = client.post("/payments/create-order", json={"amount": 5000}, headers=_auth_headers())
    assert create_res.status_code == 200, create_res.text

    verify_res = client.post("/payments/verify", json={
        "payments_session_id": "session_flow2",
        "payment_id": "pay_flow2",
        "payment_status": "succeeded",
        "amount": "5000.00",
        "signature": "forged",
    }, headers=_auth_headers())
    assert verify_res.status_code == 200, verify_res.text
    data = verify_res.json()
    assert data["status"] == "failed"
    assert data["verified"] is False


def test_verify_payment_unknown_session_is_404():
    r = client.post("/payments/verify", json={
        "payments_session_id": "session_does_not_exist",
        "payment_id": "pay_x",
        "payment_status": "succeeded",
        "amount": "100.00",
        "signature": "sig_x",
    }, headers=_auth_headers())
    assert r.status_code == 404


@patch.object(servicePaymentGateway, "create_payment_session")
def test_verify_payment_rejects_other_owners_session(mock_create_session):
    mock_create_session.return_value = {
        "payments_session_id": "session_owner_check", "access_key": "key_owner_check",
        "checkout_url": "https://payments.zoho.in/hostedcheckout/key_owner_check",
        "amount": "5000.00", "currency": "INR",
    }

    create_res = client.post("/payments/create-order", json={"amount": 5000}, headers=_auth_headers())
    assert create_res.status_code == 200, create_res.text

    verify_res = client.post("/payments/verify", json={
        "payments_session_id": "session_owner_check",
        "payment_id": "pay_x",
        "payment_status": "succeeded",
        "amount": "5000.00",
        "signature": "sig_x",
    }, headers=_auth_headers(_OTHER_TOKEN))
    assert verify_res.status_code == 403


# ---------- POST /payments/webhook ----------

def test_webhook_does_not_require_auth():
    # No Authorization header at all - Zoho calls this server-to-server with no user
    # JWT. A valid signature (not a bearer token) is what authenticates the request, so a
    # correctly-signed call with no auth header must not be rejected as 401.
    body = _webhook_body("payment.dispute_created", session_id="session_no_auth_needed")
    r = client.post(
        "/payments/webhook",
        content=body,
        headers={"X-Zoho-Webhook-Signature": _sign_webhook(body), "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ignored_event:payment.dispute_created"


def test_webhook_rejects_invalid_signature():
    body = _webhook_body("payment.success", session_id="session_bad_sig")
    r = client.post(
        "/payments/webhook",
        content=body,
        headers={"X-Zoho-Webhook-Signature": "t=123,v=not-the-real-signature", "Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_webhook_signature"


def test_webhook_rejects_missing_signature_header():
    body = _webhook_body("payment.success", session_id="session_no_sig_header")
    r = client.post("/payments/webhook", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_webhook_signature"


@patch.object(servicePaymentGateway, "create_payment_session")
def test_webhook_marks_payment_paid_end_to_end(mock_create_session):
    # Proves the webhook alone is sufficient to settle a payment, independent of the
    # client ever calling /payments/verify - the scenario this endpoint exists for (the
    # browser closing/crashing right after a successful checkout).
    mock_create_session.return_value = {
        "payments_session_id": "session_webhook_e2e", "access_key": "key_webhook_e2e",
        "checkout_url": "https://payments.zoho.in/hostedcheckout/key_webhook_e2e",
        "amount": "2500.00", "currency": "INR",
    }

    create_res = client.post("/payments/create-order", json={"amount": 2500}, headers=_auth_headers())
    assert create_res.status_code == 200, create_res.text
    payment_id = create_res.json()["payment_id"]

    get_before = client.get(f"/payments/{payment_id}", headers=_auth_headers())
    assert get_before.json()["status"] == "created"

    body = _webhook_body("payment.success", session_id="session_webhook_e2e", payment_id="pay_webhook_e2e")
    webhook_res = client.post(
        "/payments/webhook",
        content=body,
        headers={"X-Zoho-Webhook-Signature": _sign_webhook(body), "Content-Type": "application/json"},
    )
    assert webhook_res.status_code == 200, webhook_res.text
    assert webhook_res.json()["status"] == "processed:paid"

    get_after = client.get(f"/payments/{payment_id}", headers=_auth_headers())
    assert get_after.json()["status"] == "paid"
    assert get_after.json()["zoho_payment_id"] == "pay_webhook_e2e"


# ---------- POST /payments/cash ----------

def test_record_cash_payment_requires_auth():
    r = client.post("/payments/cash", json={"amount": 1000})
    assert r.status_code == 401


def test_record_cash_payment_allows_customer_caller():
    r = client.post("/payments/cash", json={"amount": 1000}, headers=_auth_headers())
    assert r.status_code == 200, r.text
    assert r.json()["method"] == "cash"
    assert r.json()["owner_role"] == "customer"


def test_record_cash_payment_rejects_non_positive_amount():
    r = client.post("/payments/cash", json={"amount": 0}, headers=_auth_headers(_BROKER_TOKEN))
    assert r.status_code == 422  # pydantic gt=0 constraint


def test_record_cash_payment_happy_path_settles_immediately():
    # No gateway mocking needed at all - cash never touches the gateway.
    r = client.post(
        "/payments/cash",
        json={"amount": 18500.50, "note": "Paid at site office"},
        headers=_auth_headers(_BROKER_TOKEN),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["amount"] == 18500.50
    assert data["status"] == "paid"
    assert data["method"] == "cash"
    assert data["verified"] is True
    assert data["zoho_payments_session_id"] is None
    assert data["zoho_payment_id"] is None


def test_record_cash_payment_works_without_a_note():
    r = client.post("/payments/cash", json={"amount": 500}, headers=_auth_headers(_BROKER_TOKEN))
    assert r.status_code == 200, r.text
    assert r.json()["method"] == "cash"


def test_record_cash_payment_is_owner_scoped():
    r = client.post("/payments/cash", json={"amount": 999}, headers=_auth_headers(_BROKER_TOKEN))
    assert r.status_code == 200, r.text
    payment_id = r.json()["id"]

    other_get = client.get(f"/payments/{payment_id}", headers=_auth_headers(_OTHER_TOKEN))
    assert other_get.status_code == 403
