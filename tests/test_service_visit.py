import os
from unittest.mock import MagicMock

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from DivineService.service_visit import serviceVisit


def _service():
    persistence = MagicMock()
    return serviceVisit(persistence), persistence


# ---------- schedule_visit ----------

def test_schedule_visit_rejects_blank_customer_name():
    svc, _ = _service()
    try:
        svc.schedule_visit(broker_id="B00001", customer_name="   ", customer_contact=None, visit_date="2026-09-01", visit_time="14:30", notes=None)
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "customer_name_required"


def test_schedule_visit_rejects_invalid_date():
    svc, _ = _service()
    try:
        svc.schedule_visit(broker_id="B00001", customer_name="Jane", customer_contact=None, visit_date="01-09-2026", visit_time="14:30", notes=None)
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "invalid_date"


def test_schedule_visit_rejects_invalid_time():
    svc, _ = _service()
    try:
        svc.schedule_visit(broker_id="B00001", customer_name="Jane", customer_contact=None, visit_date="2026-09-01", visit_time="2:30 PM", notes=None)
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "invalid_time"


def test_schedule_visit_happy_path():
    svc, persistence = _service()
    persistence.create_visit.return_value = MagicMock(id="v1", status="scheduled")

    svc.schedule_visit(
        broker_id="B00001", customer_name="  Jane Doe  ", customer_contact="9999999999",
        visit_date="2026-09-01", visit_time="14:30", notes="  Bring brochure  ",
    )

    _, kwargs = persistence.create_visit.call_args
    assert kwargs["broker_id"] == "B00001"
    assert kwargs["customer_name"] == "Jane Doe"  # trimmed
    assert kwargs["customer_contact"] == "9999999999"
    assert kwargs["visit_time"] == "14:30"
    assert kwargs["notes"] == "Bring brochure"  # trimmed
    assert kwargs["status"] == "scheduled"
    assert kwargs["visit_date"].isoformat() == "2026-09-01"


def test_schedule_visit_normalizes_parseable_time():
    svc, persistence = _service()
    persistence.create_visit.return_value = MagicMock(id="v1", status="scheduled")

    svc.schedule_visit(
        broker_id="B00001", customer_name="Jane", customer_contact=None,
        visit_date="2026-09-01", visit_time="2:3", notes=None,
    )

    _, kwargs = persistence.create_visit.call_args
    assert kwargs["visit_time"] == "02:03"


def test_schedule_visit_blanks_optional_fields_to_none():
    svc, persistence = _service()
    persistence.create_visit.return_value = MagicMock(id="v1", status="scheduled")

    svc.schedule_visit(broker_id="B00001", customer_name="Jane", customer_contact="  ", visit_date="2026-09-01", visit_time="09:00", notes="  ")

    _, kwargs = persistence.create_visit.call_args
    assert kwargs["customer_contact"] is None
    assert kwargs["notes"] is None


# ---------- list_visits ----------

def test_list_visits_delegates_to_persistence():
    svc, persistence = _service()
    persistence.list_by_broker.return_value = [MagicMock(id="v1"), MagicMock(id="v2")]

    result = svc.list_visits("B00001")

    persistence.list_by_broker.assert_called_once_with("B00001")
    assert len(result) == 2


# ---------- get_visit_history ----------

def test_get_visit_history_marks_lapsed_scheduled_as_completed():
    svc, persistence = _service()
    persistence.list_history_by_broker.return_value = [
        MagicMock(status="scheduled"),
        MagicMock(status="cancelled"),
    ]

    records = svc.get_visit_history("B00001")

    assert records[0].status == "completed"
    assert records[1].status == "cancelled"
    persistence.list_history_by_broker.assert_called_once()
    args, kwargs = persistence.list_history_by_broker.call_args
    assert args[0] == "B00001"
    assert "today" in kwargs and "now_time" in kwargs


# ---------- cancel_visit ----------

def test_cancel_visit_raises_not_found():
    svc, persistence = _service()
    persistence.get_by_id.return_value = None
    try:
        svc.cancel_visit("v1", requester_id="B00001")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "not_found"


def test_cancel_visit_raises_forbidden_for_other_broker():
    svc, persistence = _service()
    persistence.get_by_id.return_value = MagicMock(broker_id="B99999")
    try:
        svc.cancel_visit("v1", requester_id="B00001")
        assert False, "expected PermissionError"
    except PermissionError as e:
        assert str(e) == "forbidden"


def test_cancel_visit_updates_status_for_owning_broker():
    svc, persistence = _service()
    persistence.get_by_id.return_value = MagicMock(broker_id="B00001")
    persistence.update_status.return_value = MagicMock(id="v1", status="cancelled")

    record = svc.cancel_visit("v1", requester_id="B00001")

    assert record.status == "cancelled"
    persistence.update_status.assert_called_once_with("v1", "cancelled")
