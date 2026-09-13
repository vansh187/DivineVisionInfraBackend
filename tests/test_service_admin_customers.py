import os
from unittest.mock import MagicMock

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"

import pytest
from DivineService.service_admin_customers import serviceAdminCustomers
from DivineDTO.models import CustomerCreateDTO


def _service():
    persistence = MagicMock()
    return serviceAdminCustomers(persistence), persistence


def _row(total_count, **overrides):
    values = dict(id="X1", full_name="A B", email="a@b.com", phone="123",
                  source="WEBSITE", status="LEAD", created_at="2026-01-01", last_activity_at="2026-01-01",
                  total_count=total_count)
    values.update(overrides)
    return MagicMock(**values)


def test_list_customers_reads_total_from_the_page_when_rows_come_back():
    svc, persistence = _service()
    persistence.list_customers.return_value = [_row(total_count=45)]

    result = svc.list_customers(page=2, page_size=20)

    assert result["pagination"] == {"page": 2, "page_size": 20, "total_items": 45, "total_pages": 3}
    persistence.list_customers.assert_called_once_with(
        search=None, source=None, status=None, sort="-created_at", limit=20, offset=20,
    )
    persistence.count_customers.assert_not_called()  # window-function total was enough


def test_list_customers_falls_back_to_count_query_when_page_is_empty():
    svc, persistence = _service()
    persistence.list_customers.return_value = []
    persistence.count_customers.return_value = 0

    result = svc.list_customers(page=1, page_size=20)

    assert result["pagination"]["total_items"] == 0
    assert result["pagination"]["total_pages"] == 0
    persistence.count_customers.assert_called_once_with(search=None, source=None, status=None)


def test_list_customers_blank_search_is_treated_as_no_filter():
    svc, persistence = _service()
    persistence.list_customers.return_value = []
    persistence.count_customers.return_value = 0

    svc.list_customers(search="   ")
    _, kwargs = persistence.list_customers.call_args
    assert kwargs["search"] is None


def test_list_customers_formats_rows_into_plain_dicts_without_total_count():
    svc, persistence = _service()
    persistence.list_customers.return_value = [_row(total_count=1)]

    result = svc.list_customers()
    assert result["items"] == [{
        "id": "X1", "full_name": "A B", "email": "a@b.com", "phone": "123",
        "source": "WEBSITE", "status": "LEAD", "created_at": "2026-01-01", "last_activity_at": "2026-01-01",
    }]


def test_create_customer_rejects_duplicate_email():
    svc, persistence = _service()
    persistence.create_manual_lead.return_value = None  # atomic check-and-insert found it taken

    dto = CustomerCreateDTO(full_name="Someone", email="taken@example.com", phone="123")
    with pytest.raises(ValueError) as exc_info:
        svc.create_customer(dto)
    assert str(exc_info.value) == "email_already_exists"


def test_create_customer_lowercases_email_before_inserting():
    svc, persistence = _service()
    persistence.create_manual_lead.return_value = MagicMock(
        id="X2", full_name="Someone", email="someone@example.com", phone="123",
        created_at="2026-01-01", last_activity_at="2026-01-01",
    )

    dto = CustomerCreateDTO(full_name="Someone", email="Someone@Example.com", phone="123")
    result = svc.create_customer(dto)

    persistence.create_manual_lead.assert_called_once_with(
        full_name="Someone", email="someone@example.com", phone="123",
    )
    assert result["source"] == "WEBSITE"
    assert result["status"] == "LEAD"
