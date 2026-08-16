import hashlib
import hmac
import json
import os
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"
# Set before importing DivineAPI.main (which calls load_dotenv()) - load_dotenv() doesn't
# override vars already present in the environment, so this guarantees webhook signature
# tests below use a known value instead of depending on whatever's in the real .env.
_WEBHOOK_SECRET = "test_webhook_secret_value"
os.environ["RAZORPAY_WEBHOOK_SECRET"] = _WEBHOOK_SECRET

from razorpay.errors import SignatureVerificationError
from Divinepersistence.persistence_db import PersistenceDB
from DivineService.service_payment import servicePayment
from DivineAPI.main import app
from DivineAPI import payment_api

client = TestClient(app)

# The app's payment_api module builds one servicePayment() singleton at import time,
# reading RAZORPAY_KEY_ID/SECRET then - too early for a test-file env var to reach it.
# Setting the instance's own attributes directly (rather than the env vars, or patching
# _client() alone) covers create_order()'s own use of self._key_id in its return value,
# not just the razorpay.Client() construction _client() would otherwise gate on.
payment_api._payment_service._key_id = "rzp_test_fake_key_id"
payment_api._payment_service._key_secret = "rzp_test_fake_key_secret"

_TOKEN = None
_OTHER_TOKEN = None


def setup_module(module):
    global _TOKEN, _OTHER_TOKEN
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()

    client.post("/customer/signup", json={"username": "paycust_shared", "password": "strongpassword"})
    lr = client.post("/customer/login", json={"username": "paycust_shared", "password": "strongpassword"})
    assert lr.status_code == 200, lr.text
    _TOKEN = lr.json()["access_token"]

    client.post("/customer/signup", json={"username": "paycust_other", "password": "strongpassword"})
    lr2 = client.post("/customer/login", json={"username": "paycust_other", "password": "strongpassword"})
    assert lr2.status_code == 200, lr2.text
    _OTHER_TOKEN = lr2.json()["access_token"]


def _auth_headers(token=None):
    return {"Authorization": f"Bearer {token or _TOKEN}"}


def _mock_razorpay_client(mock_client_method, order_id=None, signature_ok=None):
    """Patches servicePayment._client() (not module-level razorpay.Client) so it works
    regardless of when the app's payment_api singleton was constructed - _client is looked
    up on the class at call time, so patching the class method reaches the singleton too."""
    mock_client = MagicMock()
    if order_id is not None:
        mock_client.order.create.return_value = {"id": order_id}
    if signature_ok is True:
        mock_client.utility.verify_payment_signature.return_value = True
    elif signature_ok is False:
        mock_client.utility.verify_payment_signature.side_effect = SignatureVerificationError("bad")
    mock_client_method.return_value = mock_client
    return mock_client


# ---------- POST /payments/create-order ----------

def test_create_order_requires_auth():
    r = client.post("/payments/create-order", json={"amount": 1000})
    assert r.status_code == 401


def test_create_order_rejects_non_positive_amount():
    r = client.post("/payments/create-order", json={"amount": 0}, headers=_auth_headers())
    assert r.status_code == 422  # pydantic gt=0 constraint


@patch.object(servicePayment, "_client")
def test_create_order_happy_path(mock_client_method):
    _mock_razorpay_client(mock_client_method, order_id="order_test123")

    r = client.post("/payments/create-order", json={"amount": 25000.50}, headers=_auth_headers())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["razorpay_order_id"] == "order_test123"
    assert data["amount"] == 25000.50
    assert data["amount_paise"] == 2500050
    assert data["currency"] == "INR"
    assert data["status"] == "created"
    assert "razorpay_key_id" in data


@patch.object(servicePayment, "create_order", side_effect=RuntimeError("payment_not_configured"))
def test_create_order_returns_502_when_not_configured(mock_create):
    r = client.post("/payments/create-order", json={"amount": 1000}, headers=_auth_headers())
    assert r.status_code == 502
    assert r.json()["detail"] == "payment_not_configured"


# ---------- POST /payments/verify ----------

def test_verify_requires_auth():
    r = client.post("/payments/verify", json={
        "razorpay_order_id": "order_x", "razorpay_payment_id": "pay_x", "razorpay_signature": "sig_x",
    })
    assert r.status_code == 401


@patch.object(servicePayment, "_client")
def test_verify_payment_full_flow_success(mock_client_method):
    _mock_razorpay_client(mock_client_method, order_id="order_flow1", signature_ok=True)

    create_res = client.post("/payments/create-order", json={"amount": 5000}, headers=_auth_headers())
    assert create_res.status_code == 200, create_res.text

    verify_res = client.post("/payments/verify", json={
        "razorpay_order_id": "order_flow1",
        "razorpay_payment_id": "pay_flow1",
        "razorpay_signature": "sig_flow1",
    }, headers=_auth_headers())
    assert verify_res.status_code == 200, verify_res.text
    data = verify_res.json()
    assert data["status"] == "paid"
    assert data["verified"] is True
    assert data["razorpay_payment_id"] == "pay_flow1"


@patch.object(servicePayment, "_client")
def test_verify_payment_full_flow_forged_signature(mock_client_method):
    _mock_razorpay_client(mock_client_method, order_id="order_flow2", signature_ok=False)

    create_res = client.post("/payments/create-order", json={"amount": 5000}, headers=_auth_headers())
    assert create_res.status_code == 200, create_res.text

    verify_res = client.post("/payments/verify", json={
        "razorpay_order_id": "order_flow2",
        "razorpay_payment_id": "pay_flow2",
        "razorpay_signature": "forged",
    }, headers=_auth_headers())
    assert verify_res.status_code == 200, verify_res.text
    data = verify_res.json()
    assert data["status"] == "failed"
    assert data["verified"] is False


def test_verify_payment_unknown_order_is_404():
    r = client.post("/payments/verify", json={
        "razorpay_order_id": "order_does_not_exist",
        "razorpay_payment_id": "pay_x",
        "razorpay_signature": "sig_x",
    }, headers=_auth_headers())
    assert r.status_code == 404


@patch.object(servicePayment, "_client")
def test_verify_payment_rejects_other_owners_order(mock_client_method):
    _mock_razorpay_client(mock_client_method, order_id="order_owner_check")

    create_res = client.post("/payments/create-order", json={"amount": 5000}, headers=_auth_headers())
    assert create_res.status_code == 200, create_res.text

    verify_res = client.post("/payments/verify", json={
        "razorpay_order_id": "order_owner_check",
        "razorpay_payment_id": "pay_x",
        "razorpay_signature": "sig_x",
    }, headers=_auth_headers(_OTHER_TOKEN))
    assert verify_res.status_code == 403


# ---------- POST /payments/webhook ----------

def _sign_webhook(body: str) -> str:
    return hmac.new(key=_WEBHOOK_SECRET.encode("utf-8"), msg=body.encode("utf-8"), digestmod=hashlib.sha256).hexdigest()


def _webhook_body(event: str, order_id: str, payment_id: str = "pay_webhook_test") -> str:
    return json.dumps({
        "event": event,
        "payload": {"payment": {"entity": {"id": payment_id, "order_id": order_id, "status": "captured"}}},
    })


def test_webhook_does_not_require_auth():
    # No Authorization header at all - Razorpay calls this server-to-server with no user
    # JWT. A valid signature (not a bearer token) is what authenticates the request, so a
    # correctly-signed call with no auth header must not be rejected as 401.
    body = _webhook_body("order.paid", order_id="order_no_auth_needed")
    r = client.post(
        "/payments/webhook",
        content=body,
        headers={"X-Razorpay-Signature": _sign_webhook(body), "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ignored_event:order.paid"


def test_webhook_rejects_invalid_signature():
    body = _webhook_body("payment.captured", order_id="order_bad_sig")
    r = client.post(
        "/payments/webhook",
        content=body,
        headers={"X-Razorpay-Signature": "not-the-real-signature", "Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_webhook_signature"


def test_webhook_rejects_missing_signature_header():
    body = _webhook_body("payment.captured", order_id="order_no_sig_header")
    r = client.post("/payments/webhook", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_webhook_signature"


@patch.object(servicePayment, "_client")
def test_webhook_marks_payment_paid_end_to_end(mock_client_method):
    # Proves the webhook alone is sufficient to settle a payment, independent of the
    # client ever calling /payments/verify - the scenario this endpoint exists for (the
    # browser closing/crashing right after a successful checkout).
    _mock_razorpay_client(mock_client_method, order_id="order_webhook_e2e")

    create_res = client.post("/payments/create-order", json={"amount": 2500}, headers=_auth_headers())
    assert create_res.status_code == 200, create_res.text
    payment_id = create_res.json()["payment_id"]

    get_before = client.get(f"/payments/{payment_id}", headers=_auth_headers())
    assert get_before.json()["status"] == "created"

    body = _webhook_body("payment.captured", order_id="order_webhook_e2e", payment_id="pay_webhook_e2e")
    webhook_res = client.post(
        "/payments/webhook",
        content=body,
        headers={"X-Razorpay-Signature": _sign_webhook(body), "Content-Type": "application/json"},
    )
    assert webhook_res.status_code == 200, webhook_res.text
    assert webhook_res.json()["status"] == "processed:paid"

    get_after = client.get(f"/payments/{payment_id}", headers=_auth_headers())
    assert get_after.json()["status"] == "paid"
    assert get_after.json()["razorpay_payment_id"] == "pay_webhook_e2e"


# ---------- POST /payments/cash ----------

def test_record_cash_payment_requires_auth():
    r = client.post("/payments/cash", json={"amount": 1000})
    assert r.status_code == 401


def test_record_cash_payment_rejects_non_positive_amount():
    r = client.post("/payments/cash", json={"amount": 0}, headers=_auth_headers())
    assert r.status_code == 422  # pydantic gt=0 constraint


def test_record_cash_payment_happy_path_settles_immediately():
    # No razorpay.Client mocking needed at all - cash never touches the gateway.
    r = client.post(
        "/payments/cash",
        json={"amount": 18500.50, "note": "Paid at site office"},
        headers=_auth_headers(),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["amount"] == 18500.50
    assert data["status"] == "paid"
    assert data["method"] == "cash"
    assert data["verified"] is True
    assert data["razorpay_order_id"].startswith("cash_")
    assert data["razorpay_payment_id"] is None


def test_record_cash_payment_works_without_a_note():
    r = client.post("/payments/cash", json={"amount": 500}, headers=_auth_headers())
    assert r.status_code == 200, r.text
    assert r.json()["method"] == "cash"


def test_record_cash_payment_is_owner_scoped():
    r = client.post("/payments/cash", json={"amount": 999}, headers=_auth_headers())
    assert r.status_code == 200, r.text
    payment_id = r.json()["id"]

    other_get = client.get(f"/payments/{payment_id}", headers=_auth_headers(_OTHER_TOKEN))
    assert other_get.status_code == 403