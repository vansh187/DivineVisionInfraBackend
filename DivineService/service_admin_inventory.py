"""Admin panel Inventory section: plots per project, their status, and the
history of every status change (divine_inventory_status_log).

Admins can move a plot between available / held / sold by hand. Plots tied to
the booking flow (pending_kyc_review, booked, or carrying a booking payment),
reserved by a channel partner, or linked to an offline buyer are locked here:
those change through their own flows, which record their own history rows.
"""
import logging
from datetime import datetime, timezone
from decimal import Decimal

from DivineDTO.models import INVENTORY_STATUSES, MANUAL_INVENTORY_STATUSES as MANUAL_STATUSES
from Divinepersistence import persistenceAdmin, persistenceInventory
from Divinepersistence.persistence_inventory import status_actor

logger = logging.getLogger(__name__)

_LOCK_MESSAGES = {
    "partner_reservation": (
        "A channel partner has reserved this plot. It becomes available again when they "
        "release it or the reservation expires."
    ),
    "booking": (
        "This plot is booked through the website. Change it from the booking: approve, "
        "reject or cancel it in the Bookings section."
    ),
    "buyer_linked": (
        "This plot is linked to a buyer's account. Its status can't be changed here while "
        "that buyer record exists."
    ),
}


class InventoryStatusError(ValueError):
    """A refused status change, with a message written for the admin."""

    def __init__(self, code: str, message: str, http_status: int = 409, current_status: str = None):
        super().__init__(code)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.current_status = current_status


def _num(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dt(value):
    """Datetimes come back as strings from sqlite's textual queries."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def status_lock(unit) -> str:
    status = getattr(unit, "status", None)
    if status == "reserved":
        return "partner_reservation"
    if status in ("pending_kyc_review", "booked") or getattr(unit, "booked_payment_id", None):
        return "booking"
    if getattr(unit, "booked_by", None):
        return "buyer_linked"
    if status not in MANUAL_STATUSES:
        return "booking"
    return None


class serviceAdminInventory:
    def __init__(self, persistence=None, admin_persistence=None):
        self._persistence = persistence or persistenceInventory()
        self._admins = admin_persistence or persistenceAdmin()

    # ---- formatting -------------------------------------------------------
    @staticmethod
    def _unit(row) -> dict:
        last_changed_at = _dt(getattr(row, "last_changed_at", None))
        return {
            "id": str(row.id),
            "project_name": row.project_name,
            "city": getattr(row, "city", None),
            "locality": getattr(row, "locality", None),
            "block": getattr(row, "block", None),
            "unit_number": row.unit_number,
            "unit_type": getattr(row, "unit_type", None),
            "width_mtr": _num(getattr(row, "width_mtr", None)),
            "length_mtr": _num(getattr(row, "length_mtr", None)),
            "area_sqmt": _num(getattr(row, "area_sqmt", None)),
            "area_sqyd": _num(getattr(row, "area_sqyd", None)),
            "status": row.status,
            "sale_source": getattr(row, "sale_source", None),
            "status_note": getattr(row, "status_note", None),
            # Plots changed before the history log existed only have status_updated_at.
            "last_changed_at": last_changed_at or _dt(getattr(row, "status_updated_at", None)),
            "last_changed_by_name": getattr(row, "last_changed_by_name", None),
        }

    @staticmethod
    def _change(row) -> dict:
        return {
            "id": str(row.id),
            "old_status": row.old_status,
            "new_status": row.new_status,
            "changed_by_role": row.changed_by_role,
            "changed_by_name": row.changed_by_name,
            "source": row.source,
            "note": row.note,
            "changed_at": _dt(row.changed_at),
        }

    # ---- reads -------------------------------------------------------------
    def list_projects(self) -> dict:
        projects = {}
        for r in self._persistence.admin_project_status_counts():
            counts = projects.setdefault(r.project_name, {s: 0 for s in INVENTORY_STATUSES})
            if r.status in counts:
                counts[r.status] += int(r.plots or 0)
            else:
                logger.warning("admin_inventory.unknown_status project=%s status=%s", r.project_name, r.status)
            counts["total"] = counts.get("total", 0) + int(r.plots or 0)
        return {"items": [{"project_name": name, **counts} for name, counts in sorted(projects.items())]}

    def list_units(self, project_name: str = None, status: str = None, search: str = None,
                   page: int = 1, page_size: int = 20) -> dict:
        project_name = (project_name or "").strip() or None
        search = (search or "").strip() or None
        rows, total = self._persistence.admin_list_units(
            project_name=project_name, status=status, search=search,
            limit=page_size, offset=(page - 1) * page_size,
        )
        return {
            "items": [self._unit(r) for r in rows],
            "pagination": {
                "page": page, "page_size": page_size, "total_items": total,
                "total_pages": (total + page_size - 1) // page_size if total else 0,
            },
        }

    def get_unit(self, inventory_id: str, unit=None) -> dict:
        """``unit``: the plot row when the caller already has it (saves a read,
        and its expire-reservations pass)."""
        unit = unit or self._persistence.get_by_id(inventory_id)
        if not unit:
            raise ValueError("not_found")
        history = [self._change(h) for h in self._persistence.admin_status_history(inventory_id)]
        lock = status_lock(unit)
        result = self._unit(unit)
        if history:
            result["last_changed_at"] = history[0]["changed_at"]
            result["last_changed_by_name"] = history[0]["changed_by_name"]
        result.update({
            "booked_at": _dt(getattr(unit, "booked_at", None)),
            "status_lock": lock,
            "allowed_statuses": [] if lock else [s for s in MANUAL_STATUSES if s != unit.status],
            "history": history,
        })
        return result

    # ---- the one write ---------------------------------------------------
    def _admin_actor(self, admin_id: str, username: str = None) -> dict:
        name = None
        try:
            admin = self._admins.get_by_id(admin_id) if admin_id else None
            name = (getattr(admin, "full_name", None) or "").strip() or None
        except Exception:
            logger.warning("admin_inventory.admin_lookup_failed admin_id=%s", admin_id)
        return status_actor("admin", admin_id, name or (username or "").strip() or admin_id)

    def change_status(self, inventory_id: str, new_status: str, expected_status: str,
                      admin_id: str, admin_username: str = None, note: str = None) -> dict:
        if new_status not in MANUAL_STATUSES:
            raise InventoryStatusError("invalid_status", "Choose Available, On Hold or Sold.", http_status=400)
        unit = self._persistence.get_by_id(inventory_id)
        if not unit:
            raise ValueError("not_found")
        if unit.status != expected_status:
            raise InventoryStatusError(
                "status_changed",
                "Someone changed this plot's status while you were looking at it. "
                "The latest status is shown; check it and try again.",
                current_status=unit.status,
            )
        lock = status_lock(unit)
        if lock:
            raise InventoryStatusError(lock, _LOCK_MESSAGES[lock], current_status=unit.status)
        if new_status == unit.status:
            raise InventoryStatusError("status_unchanged", "The plot already has this status.", http_status=400,
                                       current_status=unit.status)

        updated = self._persistence.admin_set_status(
            id=inventory_id, expected_status=expected_status, new_status=new_status,
            actor=self._admin_actor(admin_id, admin_username), note=note,
        )
        if updated is None:
            # Lost a race with another change between the read above and the guarded UPDATE.
            current = self._persistence.get_by_id(inventory_id)
            raise InventoryStatusError(
                "status_changed",
                "Someone changed this plot's status at the same time. The latest status is shown; "
                "check it and try again.",
                current_status=getattr(current, "status", None),
            )
        logger.info("admin_inventory.status_changed inventory_id=%s %s->%s admin=%s",
                    inventory_id, expected_status, new_status, admin_id)
        # The guarded UPDATE returned the whole row; no need to read it again.
        return self.get_unit(inventory_id, unit=updated)
