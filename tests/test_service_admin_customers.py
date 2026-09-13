import os
from unittest.mock import MagicMock

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"

import pytest
from DivineService.service_admin_customers import serviceAdminCustomers
from DivineDTO.models import CustomerCreateDTO


def _service():
    persistence = MagicMock()
    return serviceAdminCustomers(persistence), persistence


def test_list_customers_computes_pagination_from_counts():
    svc, persistence = _service()
    persistence.list_customers.return_value = []
    persistence.count_customers.return_value = 45

    result = svc.list_customers(page=2, page_size=20)

    assert result["pagination"] == {"page": 2, "page_size": 20, "total_items": 45, "total_pages": 3}
    persistence.list_customers.assert_called_once_with(
        search=None, source=None, status=None, sort="-created_at", limit=20, offset=20,
    )


def test_list_customers_zero_total_gives_zero_pages():
    svc, persistence = _service()
    persistence.list_customers.return_value = []
    persistence.count_customers.return_value = 0

    result = svc.list_customers(page=1, page_size=20)
    assert result["pagination"]["total_pages"] == 0


def test_list_customers_blank_search_is_treated_as_no_filter():
    svc, persistence = _service()
    persistence.list_customers.return_value = []
    persistence.count_customers.return_value = 0

    svc.list_customers(search="   ")
    _, kwargs = persistence.list_customers.call_args
    assert kwargs["search"] is None


def test_list_customers_formats_rows_into_plain_dicts():
    svc, persistence = _service()
    row = MagicMock(id="X1", full_name="A B", email="a@b.com", phone="123",
                     source="WEBSITE", status="LEAD", created_at="2026-01-01", last_activity_at="2026-01-01")
    persistence.list_customers.return_value = [row]
    persistence.count_customers.return_value = 1

    result = svc.list_customers()
    assert result["items"] == [{
        "id": "X1", "full_name": "A B", "email": "a@b.com", "phone": "123",
        "source": "WEBSITE", "status": "LEAD", "created_at": "2026-01-01", "last_activity_at": "2026-01-01",
    }]


def test_create_customer_rejects_duplicate_email():
    svc, persistence = _service()
    persistence.email_in_use.return_value = True

    dto = CustomerCreateDTO(full_name="Someone", email="taken@example.com", phone="123")
    with pytest.raises(ValueError) as exc_info:
        svc.create_customer(dto)
    assert str(exc_info.value) == "email_already_exists"
    persistence.create_manual_lead.assert_not_called()


def test_create_customer_lowercases_email_before_checking_and_inserting():
    svc, persistence = _service()
    persistence.email_in_use.return_value = False
    persistence.create_manual_lead.return_value = MagicMock(
        id="X2", full_name="Someone", email="someone@example.com", phone="123",
        created_at="2026-01-01", last_activity_at="2026-01-01",
    )

    dto = CustomerCreateDTO(full_name="Someone", email="Someone@Example.com", phone="123")
    result = svc.create_customer(dto)

    persistence.email_in_use.assert_called_once_with("someone@example.com")
    persistence.create_manual_lead.assert_called_once_with(
        full_name="Someone", email="someone@example.com", phone="123",
    )
    assert result["source"] == "WEBSITE"
    assert result["status"] == "LEAD"
