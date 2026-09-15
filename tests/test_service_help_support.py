"""Unit coverage for serviceHelpSupport against mocked persistence/email."""
import os
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from DivineService.service_help_support import serviceHelpSupport


def _service(admin_row=None, email_enabled=True, send_result=True):
    admin_persistence = MagicMock()
    admin_persistence.get_by_id.return_value = admin_row
    email_service = MagicMock()
    email_service.enabled = email_enabled
    email_service.send_ticket_notification.return_value = send_result
    return serviceHelpSupport(admin_persistence=admin_persistence, email_service=email_service), \
        admin_persistence, email_service


def test_submit_ticket_happy_path_generates_ticket_and_sends_email():
    admin_row = SimpleNamespace(id="A00001", full_name="Priya Nair", email="priya@example.com")
    svc, admin_persistence, email_service = _service(admin_row=admin_row)

    result = svc.submit_ticket(subject="UI Issue", description="Sidebar overlaps", admin_id="A00001")

    assert re.match(r"^TTK-\d{6}$", result["ticket_number"])
    assert result["subject"] == "UI Issue"
    assert result["description"] == "Sidebar overlaps"
    assert result["raised_by"] == "Priya Nair"
    assert result["email_sent"] is True

    admin_persistence.get_by_id.assert_called_once_with("A00001")
    email_service.send_ticket_notification.assert_called_once()
    kwargs = email_service.send_ticket_notification.call_args.kwargs
    assert kwargs["subject"] == "UI Issue"
    assert kwargs["description"] == "Sidebar overlaps"
    assert kwargs["raised_by_name"] == "Priya Nair"
    assert kwargs["raised_by_email"] == "priya@example.com"


def test_submit_ticket_strips_whitespace():
    svc, _, email_service = _service(admin_row=None)
    result = svc.submit_ticket(subject="  Trimmed  ", description="  also trimmed  ", admin_id=None)
    assert result["subject"] == "Trimmed"
    assert result["description"] == "also trimmed"


@pytest.mark.parametrize("subject,description,expected", [
    ("", "y", "empty_subject"),
    ("   ", "y", "empty_subject"),
    ("x", "", "empty_description"),
    ("x", "   ", "empty_description"),
])
def test_submit_ticket_rejects_blank_input(subject, description, expected):
    svc, _, _ = _service()
    with pytest.raises(ValueError) as exc:
        svc.submit_ticket(subject=subject, description=description)
    assert str(exc.value) == expected


def test_submit_ticket_raises_when_email_send_fails():
    svc, _, _ = _service(send_result=False)
    with pytest.raises(RuntimeError) as exc:
        svc.submit_ticket(subject="x", description="y")
    assert str(exc.value) == "ticket_email_failed"


def test_submit_ticket_raises_when_email_service_disabled():
    svc, _, email_service = _service(email_enabled=False)
    with pytest.raises(RuntimeError) as exc:
        svc.submit_ticket(subject="x", description="y")
    assert str(exc.value) == "ticket_email_failed"
    email_service.send_ticket_notification.assert_not_called()


def test_submit_ticket_survives_raiser_lookup_exception():
    """An admin-lookup blowing up must never block the ticket from going out -
    only the display name is best-effort."""
    admin_persistence = MagicMock()
    admin_persistence.get_by_id.side_effect = RuntimeError("db down")
    email_service = MagicMock()
    email_service.enabled = True
    email_service.send_ticket_notification.return_value = True
    svc = serviceHelpSupport(admin_persistence=admin_persistence, email_service=email_service)

    result = svc.submit_ticket(subject="x", description="y", admin_id="A00001")
    assert result["raised_by"] is None
    assert result["email_sent"] is True


def test_submit_ticket_survives_unknown_admin_id():
    svc, admin_persistence, _ = _service(admin_row=None)
    result = svc.submit_ticket(subject="x", description="y", admin_id="does-not-exist")
    assert result["raised_by"] is None


def test_submit_ticket_ticket_numbers_are_not_static():
    svc, _, _ = _service()
    numbers = {svc.submit_ticket(subject="x", description="y")["ticket_number"] for _ in range(5)}
    assert len(numbers) > 1
