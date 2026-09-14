import logging
from Divinepersistence import persistenceAdminVisits
from Divinepersistence.persistence_db import format_date_value

logger = logging.getLogger(__name__)


class serviceAdminVisits:
    def __init__(self, persistence: persistenceAdminVisits = None):
        self._persistence = persistence or persistenceAdminVisits()

    def _as_item(self, row) -> dict:
        try:
            return {
                "id": row.id,
                "origin_type": row.origin_type,
                "customer_name": row.customer_name,
                "customer_contact": row.customer_contact,
                "project_name": row.project_name or None,
                "plot_number": row.plot_number or None,
                "source": row.source or None,
                "assigned_to": row.assigned_to,
                "customer_email": getattr(row, "customer_email", None),
                "preferred_window": getattr(row, "preferred_window", None),
                "visit_date": format_date_value(row.visit_date),
                "visit_time": row.visit_time,
                "status": row.status,
                "notes": getattr(row, "notes", None),
                "created_at": row.created_at,
                "last_activity_at": row.last_activity_at,
            }
        except AttributeError:
            # A row missing an expected attribute means the SELECT shape and this
            # mapping have drifted out of sync - surface it as a controlled error
            # instead of letting an AttributeError bubble up as an unhandled 500.
            logger.exception("admin_visit_row_shape_mismatch")
            raise RuntimeError("visit_row_shape_mismatch")

    def list_visits(self, search: str = None, origin_type: str = None, status: str = None,
                     sort: str = "-visit_date", page: int = 1, page_size: int = 20) -> dict:
        try:
            search = (search or "").strip() or None
            offset = (page - 1) * page_size
            rows = self._persistence.list_visits(
                search=search, origin_type=origin_type, status=status,
                sort=sort, limit=page_size, offset=offset,
            )
            if rows:
                # One indexed query already carries the filtered total via a
                # window function - no second round-trip needed on the common,
                # non-empty path.
                total_items = int(rows[0].total_count)
            else:
                # Nothing to read a window-function total from an empty row set -
                # only reached for a genuinely empty result or a page past the
                # last one.
                total_items = self._persistence.count_visits(search=search, origin_type=origin_type, status=status)
            total_pages = (total_items + page_size - 1) // page_size if page_size else 0
            return {
                "items": [self._as_item(r) for r in rows],
                "pagination": {
                    "page": page, "page_size": page_size,
                    "total_items": total_items, "total_pages": total_pages,
                },
            }
        except RuntimeError:
            # Already a controlled error (db_error / visit_row_shape_mismatch) -
            # let it pass through as-is for the router to translate to a 500.
            raise
        except Exception:
            logger.exception("admin_list_visits_failed")
            raise RuntimeError("list_visits_failed")

    def get_visit(self, visit_id: str) -> dict:
        try:
            row = self._persistence.get_visit_by_id(visit_id)
            if not row:
                raise ValueError("not_found")
            return self._as_item(row)
        except ValueError:
            raise
        except RuntimeError:
            raise
        except Exception:
            logger.exception("admin_get_visit_failed")
            raise RuntimeError("get_visit_failed")
