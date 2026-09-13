import os
from unittest.mock import MagicMock

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"

from DivineService.service_admin_brokers import serviceAdminBrokers


def _service():
    persistence = MagicMock()
    return serviceAdminBrokers(persistence), persistence


def _row(total_count, **overrides):
    values = dict(id="B00001", full_name="A B", email="a@b.com", phone="123",
                  project="suraksha-enclave", created_at="2026-01-01", last_activity_at="2026-01-01",
                  total_count=total_count)
    values.update(overrides)
    return MagicMock(**values)


def test_list_brokers_reads_total_from_the_page_when_rows_come_back():
    svc, persistence = _service()
    persistence.list_brokers.return_value = [_row(total_count=7)]

    result = svc.list_brokers(page=1, page_size=20)

    assert result["pagination"] == {"page": 1, "page_size": 20, "total_items": 7, "total_pages": 1}
    persistence.list_brokers.assert_called_once_with(
        search=None, project=None, sort="-created_at", limit=20, offset=0,
    )
    persistence.count_brokers.assert_not_called()


def test_list_brokers_falls_back_to_count_query_when_page_is_empty():
    svc, persistence = _service()
    persistence.list_brokers.return_value = []
    persistence.count_brokers.return_value = 0

    result = svc.list_brokers(page=1, page_size=20)

    assert result["pagination"]["total_items"] == 0
    assert result["pagination"]["total_pages"] == 0
    persistence.count_brokers.assert_called_once_with(search=None, project=None)


def test_list_brokers_blank_search_is_treated_as_no_filter():
    svc, persistence = _service()
    persistence.list_brokers.return_value = []
    persistence.count_brokers.return_value = 0

    svc.list_brokers(search="   ")
    _, kwargs = persistence.list_brokers.call_args
    assert kwargs["search"] is None


def test_list_brokers_formats_rows_into_plain_dicts_without_total_count():
    svc, persistence = _service()
    persistence.list_brokers.return_value = [_row(total_count=1)]

    result = svc.list_brokers()
    assert result["items"] == [{
        "id": "B00001", "full_name": "A B", "email": "a@b.com", "phone": "123",
        "project": "suraksha-enclave", "created_at": "2026-01-01", "last_activity_at": "2026-01-01",
    }]
