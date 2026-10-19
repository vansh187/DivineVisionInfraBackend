import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import jwt
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from Divinepersistence.persistence_db import PersistenceDB
from DivineService.service_broker_commission import serviceBrokerCommission
from DivineAPI import broker_commission_api
from DivineAPI.main import app

client = TestClient(app)
broker_commission_api._commission_service._key_id = "rzp_test_commission_key"
broker_commission_api._commission_service._key_secret = "rzp_test_commission_secret"


def _token(role="broker", sub="brk_123"):
    payload = {
        "sub": sub,
        "username": sub,
        "role": role,
        "exp": (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp(),
    }
    return jwt.encode(payload, "testsecret", algorithm="HS256")


def _auth_headers(role="broker", sub="brk_123"):
    return {"Authorization": f"Bearer {_token(role=role, sub=sub)}"}


def setup_module(module):
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()


def _payload(**overrides):
    payload = {
        "brokerId": "brk_123",
        "serialNumber": "SN-1001",
        "unitAddress": "Plot 42, OPS Divine Greens, Indore",
        "customerName": "Rahul Sharma",
        "township": "OPS Divine Greens",
        "saleValue": 4500000,
        "commissionAmount": 45000,
        "transactionMode": "cash",
    }
    payload.update(overrides)
    return payload


def test_broker_can_create_paid_cash_commission_and_fetch_summary():
    created = client.post("/api/broker/commissions", json=_payload(), headers=_auth_headers())
    assert created.status_code == 200, created.text
    data = created.json()
    assert data["success"] is True
    commission = data["commission"]
    assert commission["id"].startswith("com_")
    assert commission["brokerId"] == "brk_123"
    assert commission["status"] == "paid"
    assert commission["transactionMode"] == "cash"
    assert commission["paidAt"] == commission["createdAt"]
    assert commission["rejectedAt"] is None

    listed = client.get("/api/broker/commissions?brokerId=brk_123", headers=_auth_headers())
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["success"] is True
    assert len(body["commissions"]) == 1
    assert body["summary"]["paid"] == 45000
    assert body["summary"]["pending"] == 0
    assert body["summary"]["rejected"] == 0


def test_broker_create_rejects_booking_transaction_mode():
    r = client.post(
        "/api/broker/commissions",
        json=_payload(serialNumber="SN-1002", transactionMode="booking"),
        headers=_auth_headers(),
    )

    assert r.status_code == 400
    assert r.json()["detail"] == "transactionMode_must_be_cash"


@patch.object(serviceBrokerCommission, "_client")
def test_admin_payment_endpoint_initiates_razorpay_booking_commission_for_display(mock_client_method):
    mock_client = MagicMock()
    mock_client.order.create.return_value = {"id": "order_admin_commission_1"}
    mock_client_method.return_value = mock_client

    r = client.post(
        "/api/admin/commission-payments",
        json=_payload(brokerId="brk_admin", serialNumber="SN-2001", transactionMode="booking", commissionAmount=5000),
        headers=_auth_headers(role="admin", sub="admin_1"),
    )

    assert r.status_code == 200, r.text
    assert r.json()["commission"]["status"] == "pending"
    assert r.json()["commission"]["transactionMode"] == "booking"
    assert r.json()["commission"]["paidAt"] is None
    assert r.json()["payment"]["razorpayOrderId"] == "order_admin_commission_1"
    assert r.json()["payment"]["razorpayKeyId"] == "rzp_test_commission_key"

    listed = client.get(
        "/api/broker/commissions?brokerId=brk_admin",
        headers=_auth_headers(role="admin", sub="admin_1"),
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["summary"]["pending"] == 5000


def test_admin_payment_endpoint_rejects_cash_commission():
    r = client.post(
        "/api/admin/commission-payments",
        json=_payload(brokerId="brk_admin_cash", serialNumber="SN-2002", transactionMode="cash", commissionAmount=5000),
        headers=_auth_headers(role="admin", sub="admin_1"),
    )

    assert r.status_code == 400
    assert r.json()["detail"] == "admin_transactionMode_must_be_booking"


def test_list_broker_commissions_requires_broker_id():
    r = client.get("/api/broker/commissions", headers=_auth_headers())

    assert r.status_code == 422


def test_create_rejects_missing_frontend_required_field():
    payload = _payload()
    del payload["unitAddress"]

    r = client.post("/api/broker/commissions", json=payload, headers=_auth_headers())

    assert r.status_code == 422


def test_broker_commissions_require_auth():
    r = client.get("/api/broker/commissions?brokerId=brk_123")

    assert r.status_code == 401


def test_broker_cannot_read_another_brokers_commissions():
    r = client.get("/api/broker/commissions?brokerId=brk_other", headers=_auth_headers())

    assert r.status_code == 403
    assert r.json()["detail"] == "forbidden"


def test_broker_cannot_use_admin_commission_endpoint():
    r = client.post(
        "/api/admin/commission-payments",
        json=_payload(brokerId="brk_123", serialNumber="SN-3001", transactionMode="booking"),
        headers=_auth_headers(),
    )

    assert r.status_code == 403
    assert r.json()["detail"] == "admin_only"
