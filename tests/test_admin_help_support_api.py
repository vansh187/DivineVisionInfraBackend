"""End-to-end coverage for the admin Help & Support ticket API over real HTTP."""
import os
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import jwt
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"
os.environ["ADMIN_JWT_SECRET_KEY"] = "admintestsecret"

from Divinepersistence.persistence_db import PersistenceDB
from Divinepersistence.persistence_admin import persistenceAdmin
from DivineService.service_email import serviceEmail
from DivineAPI.main import app

client = TestClient(app)

_ADMIN_ID = None


def setup_module(module):
    global _ADMIN_ID
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()

    admin = persistenceAdmin().create_user(
        full_name="Priya Nair", employee_id="DV9101",
        email="priya.nair@example.com", password_hash="not-used",
    )
    _ADMIN_ID = admin.id


def _admin_headers(admin_id: str = None, ip: str = None):
    payload = {
        "sub": admin_id or _ADMIN_ID, "username": "priya.nair@example.com",
        "role": "admin", "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    headers = {"Authorization": f"Bearer {jwt.encode(payload, 'admintestsecret', algorithm='HS256')}"}
    if ip:
        # The endpoint's own rate limiter buckets by (client ip, path); each test
        # uses its own fake forwarded-for IP so it gets a fresh bucket instead of
        # tripping over calls made by other tests in this module.
        headers["X-Forwarded-For"] = ip
    return headers


def _customer_headers(ip: str = None):
    payload = {
        "sub": "C00001", "username": "someone",
        "role": "customer", "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    headers = {"Authorization": f"Bearer {jwt.encode(payload, 'testsecret', algorithm='HS256')}"}
    if ip:
        headers["X-Forwarded-For"] = ip
    return headers


def test_submit_ticket_requires_admin():
    r = client.post(
        "/admin/support-tickets", json={"subject": "x", "description": "y"},
        headers={"X-Forwarded-For": "10.0.1.1"},
    )
    assert r.status_code == 401, r.text
    r = client.post(
        "/admin/support-tickets", json={"subject": "x", "description": "y"},
        headers=_customer_headers(ip="10.0.1.1"),
    )
    assert r.status_code == 401, r.text


def test_submit_ticket_rejects_blank_fields():
    for body in ({"subject": "", "description": "y"}, {"subject": "x", "description": ""},
                 {"subject": "   ", "description": "y"}, {"description": "y"}, {"subject": "x"}, {}):
        r = client.post("/admin/support-tickets", json=body, headers=_admin_headers(ip="10.0.1.2"))
        assert r.status_code == 422, r.text


def test_submit_ticket_rejects_oversized_fields():
    r = client.post(
        "/admin/support-tickets",
        json={"subject": "x" * 151, "description": "y"},
        headers=_admin_headers(ip="10.0.1.3"),
    )
    assert r.status_code == 422, r.text

    r = client.post(
        "/admin/support-tickets",
        json={"subject": "x", "description": "y" * 3001},
        headers=_admin_headers(ip="10.0.1.3"),
    )
    assert r.status_code == 422, r.text


def test_submit_ticket_sends_email_and_returns_ticket_number():
    with patch.object(serviceEmail, "_send", return_value=True) as mock_send:
        r = client.post(
            "/admin/support-tickets",
            json={"subject": "UI Issue", "description": "The sidebar overlaps on mobile."},
            headers=_admin_headers(ip="10.0.1.4"),
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert re.match(r"^TTK-\d{6}$", body["ticket_number"])
    assert body["subject"] == "UI Issue"
    assert body["description"] == "The sidebar overlaps on mobile."
    assert body["raised_by"] == "Priya Nair"
    assert body["email_sent"] is True

    assert mock_send.call_count == 1
    kwargs = mock_send.call_args.kwargs
    assert kwargs["to"] == "vansh.duggal@webneststudio.co.in"
    assert body["ticket_number"] in kwargs["subject"]
    assert "UI Issue" in kwargs["subject"]
    assert "Priya Nair" in kwargs["html"]
    assert "The sidebar overlaps on mobile." in kwargs["html"]


def test_submit_ticket_generates_distinct_random_ticket_numbers():
    with patch.object(serviceEmail, "_send", return_value=True):
        first = client.post(
            "/admin/support-tickets", json={"subject": "a", "description": "b"},
            headers=_admin_headers(ip="10.0.1.5"),
        ).json()["ticket_number"]
        second = client.post(
            "/admin/support-tickets", json={"subject": "a", "description": "b"},
            headers=_admin_headers(ip="10.0.1.5"),
        ).json()["ticket_number"]
    # Astronomically unlikely to collide twice in a row over a million-value
    # range; a hardcoded/static ticket number would always fail this.
    assert first != second


def test_submit_ticket_escapes_html_in_description():
    with patch.object(serviceEmail, "_send", return_value=True) as mock_send:
        r = client.post(
            "/admin/support-tickets",
            json={"subject": "XSS check", "description": "<script>alert(1)</script>"},
            headers=_admin_headers(ip="10.0.1.6"),
        )
    assert r.status_code == 200, r.text
    html = mock_send.call_args.kwargs["html"]
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_submit_ticket_returns_502_when_email_delivery_fails():
    with patch.object(serviceEmail, "_send", return_value=False):
        r = client.post(
            "/admin/support-tickets",
            json={"subject": "x", "description": "y"},
            headers=_admin_headers(ip="10.0.1.7"),
        )
    assert r.status_code == 502, r.text
    assert r.json()["detail"] == "ticket_email_failed"


def test_submit_ticket_survives_unknown_raiser_admin_id():
    """The JWT's admin id no longer exists in divine_admin_users (e.g. deleted
    between login and this request) - the ticket must still be submitted and
    emailed, just without a name to attribute it to."""
    with patch.object(serviceEmail, "_send", return_value=True) as mock_send:
        r = client.post(
            "/admin/support-tickets",
            json={"subject": "x", "description": "y"},
            headers=_admin_headers(admin_id="A99999", ip="10.0.1.8"),
        )
    assert r.status_code == 200, r.text
    assert r.json()["raised_by"] is None
    assert "Unknown admin" in mock_send.call_args.kwargs["html"]
