import os
from unittest.mock import MagicMock

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"

import pytest
from sqlalchemy.exc import IntegrityError
from DivineService.service_admin_customers import serviceAdminCustomers
from DivineDTO.models import CustomerCreateDTO


def _service():
    persistence = MagicMock()
    customer_persistence = MagicMock()
    return serviceAdminCustomers(persistence, customer_persistence), persistence, customer_persistence


def _row(total_count, **overrides):
    values = dict(id="X1", full_name="A B", email="a@b.com", phone="123",
                  source="WEBSITE", status="ACTIVE", created_at="2026-01-01", last_activity_at="2026-01-01",
                  total_count=total_count)
    values.update(overrides)
    return MagicMock(**values)


def test_list_customers_reads_total_from_the_page_when_rows_come_back():
    svc, persistence, _ = _service()
    persistence.list_customers.return_value = [_row(total_count=45)]

    result = svc.list_customers(page=2, page_size=20)

    assert result["pagination"] == {"page": 2, "page_size": 20, "total_items": 45, "total_pages": 3}
    persistence.list_customers.assert_called_once_with(
        search=None, source=None, status=None, sort="-created_at", limit=20, offset=20,
    )
    persistence.count_customers.assert_not_called()  # window-function total was enough


def test_list_customers_falls_back_to_count_query_when_page_is_empty():
    svc, persistence, _ = _service()
    persistence.list_customers.return_value = []
    persistence.count_customers.return_value = 0

    result = svc.list_customers(page=1, page_size=20)

    assert result["pagination"]["total_items"] == 0
    assert result["pagination"]["total_pages"] == 0
    persistence.count_customers.assert_called_once_with(search=None, source=None, status=None)


def test_list_customers_blank_search_is_treated_as_no_filter():
    svc, persistence, _ = _service()
    persistence.list_customers.return_value = []
    persistence.count_customers.return_value = 0

    svc.list_customers(search="   ")
    _, kwargs = persistence.list_customers.call_args
    assert kwargs["search"] is None


def test_list_customers_formats_rows_into_plain_dicts_without_total_count():
    svc, persistence, _ = _service()
    persistence.list_customers.return_value = [_row(total_count=1)]

    result = svc.list_customers()
    assert result["items"] == [{
        "id": "X1", "full_name": "A B", "email": "a@b.com", "phone": "123",
        "source": "WEBSITE", "status": "ACTIVE", "created_at": "2026-01-01", "last_activity_at": "2026-01-01",
    }]


def test_create_customer_rejects_email_already_used_as_username():
    svc, _, customer_persistence = _service()
    customer_persistence.get_by_username.return_value = MagicMock()
    customer_persistence.get_by_email.return_value = None

    dto = CustomerCreateDTO(full_name="Someone", email="taken@example.com", phone="123")
    with pytest.raises(ValueError) as exc_info:
        svc.create_customer(dto)
    assert str(exc_info.value) == "email_already_exists"
    customer_persistence.create_user.assert_not_called()


def test_create_customer_rejects_email_already_used_by_a_different_username():
    svc, _, customer_persistence = _service()
    customer_persistence.get_by_username.return_value = None
    customer_persistence.get_by_email.return_value = MagicMock()

    dto = CustomerCreateDTO(full_name="Someone", email="taken@example.com", phone="123")
    with pytest.raises(ValueError) as exc_info:
        svc.create_customer(dto)
    assert str(exc_info.value) == "email_already_exists"
    customer_persistence.create_user.assert_not_called()


def test_create_customer_converts_integrity_error_race_into_email_already_exists():
    svc, _, customer_persistence = _service()
    customer_persistence.get_by_username.return_value = None
    customer_persistence.get_by_email.return_value = None
    customer_persistence.create_user.side_effect = IntegrityError("stmt", {}, Exception("dup"))

    dto = CustomerCreateDTO(full_name="Someone", email="race@example.com", phone="123")
    with pytest.raises(ValueError) as exc_info:
        svc.create_customer(dto)
    assert str(exc_info.value) == "email_already_exists"


def test_create_customer_uses_email_as_username_with_a_random_password():
    svc, _, customer_persistence = _service()
    customer_persistence.get_by_username.return_value = None
    customer_persistence.get_by_email.return_value = None
    customer_persistence.create_user.return_value = MagicMock(
        id="C00001", email="someone@example.com", phone="123",
        created_date="2026-01-01", last_updated_date="2026-01-01",
    )

    dto = CustomerCreateDTO(full_name="Someone Else", email="Someone@Example.com", phone="123")
    result = svc.create_customer(dto, created_by="admin:A00001")

    args, kwargs = customer_persistence.create_user.call_args
    assert args[0] == "someone@example.com"  # username = lowercased email
    assert kwargs["email"] == "someone@example.com"
    assert kwargs["created_by"] == "admin:A00001"
    assert kwargs["first_name"] == "Someone"
    assert kwargs["last_name"] == "Else"
    # a real random password was hashed and passed - never a fixed/guessable value
    assert args[1] and args[1].startswith("$2b$")

    assert result["source"] == "WEBSITE"
    assert result["status"] == "ACTIVE"
    assert result["full_name"] == "Someone Else"
