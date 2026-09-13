import logging
from Divinepersistence import persistenceAdminBrokers

logger = logging.getLogger(__name__)


def _as_item(row) -> dict:
    return {
        "id": row.id,
        "full_name": row.full_name,
        "email": row.email,
        "phone": row.phone,
        "project": row.project,
        "created_at": row.created_at,
        "last_activity_at": row.last_activity_at,
    }


class serviceAdminBrokers:
    def __init__(self, persistence: persistenceAdminBrokers = None):
        self._persistence = persistence or persistenceAdminBrokers()

    def list_brokers(self, search: str = None, project: str = None,
                      sort: str = "-created_at", page: int = 1, page_size: int = 20) -> dict:
        search = (search or "").strip() or None
        offset = (page - 1) * page_size
        rows = self._persistence.list_brokers(
            search=search, project=project, sort=sort, limit=page_size, offset=offset,
        )
        if rows:
            # One indexed query already carries the filtered total via a window
            # function (see list_brokers in admin_brokers_queries.yaml) - no
            # second round-trip needed on the common, non-empty path.
            total_items = int(rows[0].total_count)
        else:
            # Nothing to read a window-function total from an empty row set -
            # only reached for a genuinely empty result or a page past the last one.
            total_items = self._persistence.count_brokers(search=search, project=project)
        total_pages = (total_items + page_size - 1) // page_size
        return {
            "items": [_as_item(r) for r in rows],
            "pagination": {
                "page": page, "page_size": page_size,
                "total_items": total_items, "total_pages": total_pages,
            },
        }
