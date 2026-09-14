import uuid
from datetime import datetime, date, timedelta
from Divinepersistence import persistenceVisit

_SATURDAY = 5
_VALID_PROJECTS = ("ops-divine-greens", "suraksha-enclave")


class serviceVisit:
    def __init__(self, persistence: persistenceVisit = None):
        self._persistence = persistence or persistenceVisit()

    def _parse_date(self, value: str) -> date:
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("invalid_date")

    def _normalize_time(self, value: str) -> str:
        try:
            return datetime.strptime(value, "%H:%M").strftime("%H:%M")
        except ValueError:
            raise ValueError("invalid_time")

    def _validate_project(self, project: str) -> str:
        """Raises ValueError("invalid_project") for a present-but-unrecognized
        value. A missing project is a separate case handled by the DTO's own
        required-field validation (-> 422) before this is ever called; project=None
        here (only reachable from direct/internal callers, not the API) is left
        as-is rather than rejected, since it's not user input to validate."""
        if project is None:
            return None
        if project not in _VALID_PROJECTS:
            raise ValueError("invalid_project")
        return project

    def schedule_visit(self, broker_id: str, customer_name: str, customer_contact: str,
                        visit_date: str, visit_time: str, notes: str, project: str = None):
        customer_name = (customer_name or "").strip()
        if not customer_name:
            raise ValueError("customer_name_required")

        validated_project = self._validate_project(project)
        parsed_date = self._parse_date(visit_date)
        normalized_time = self._normalize_time(visit_time)

        visit_id = str(uuid.uuid4())
        return self._persistence.create_visit(
            id=visit_id,
            broker_id=broker_id,
            customer_name=customer_name,
            customer_contact=(customer_contact or "").strip() or None,
            visit_date=parsed_date,
            visit_time=normalized_time,
            notes=(notes or "").strip() or None,
            status="scheduled",
            project_name=validated_project,
        )

    def _resolve_preferred_window_date(self, preferred_window: str, today: date) -> date:
        try:
            if preferred_window == "today":
                return today
            if preferred_window == "tomorrow":
                return today + timedelta(days=1)
            if preferred_window == "weekend":
                days_ahead = (_SATURDAY - today.weekday()) % 7
                return today + timedelta(days=days_ahead)
            raise ValueError("invalid_preferred_window")
        except (TypeError, OverflowError):
            raise ValueError("invalid_preferred_window")

    def request_callback(self, project: str, preferred_window: str, customer_name: str, customer_contact: str,
                          customer_email: str = None, notes: str = None):
        """Public website 'request a callback' form - no broker/exact date-time
        yet (see persistenceVisit.create_visit_request). Raises ValueError on
        bad input (caught by the router and turned into a 400); anything
        unexpected from the persistence layer is surfaced as a RuntimeError so
        the router never sees a raw, unhandled exception."""
        try:
            validated_project = self._validate_project(project)
            if validated_project is None:
                raise ValueError("invalid_project")

            clean_name = (customer_name or "").strip()
            if not clean_name:
                raise ValueError("customer_name_required")
            clean_contact = (customer_contact or "").strip()
            if not clean_contact:
                raise ValueError("invalid_contact")

            visit_date = self._resolve_preferred_window_date(preferred_window, datetime.now().date())
            visit_id = str(uuid.uuid4())
            return self._persistence.create_visit_request(
                id=visit_id,
                customer_name=clean_name,
                customer_contact=clean_contact,
                customer_email=(customer_email or "").strip() or None,
                visit_date=visit_date,
                notes=(notes or "").strip() or None,
                project_name=validated_project,
                preferred_window=preferred_window,
                status="requested",
                origin_type="CUSTOMER",
                source="Website",
            )
        except ValueError:
            raise
        except Exception:
            raise RuntimeError("request_callback_failed")

    def list_visits(self, broker_id: str):
        return self._persistence.list_by_broker(broker_id)

    def get_visit_history(self, broker_id: str):
        now = datetime.now()
        records = self._persistence.list_history_by_broker(broker_id, today=now.date(), now_time=now.strftime("%H:%M"))
        for record in records:
            if record.status != "cancelled":
                record.status = "completed"
        return records

    def cancel_visit(self, visit_id: str, requester_id: str):
        record = self._persistence.get_by_id(visit_id)
        if not record:
            raise ValueError("not_found")
        if record.broker_id != requester_id:
            raise PermissionError("forbidden")
        return self._persistence.update_status(visit_id, "cancelled")

    def complete_visit(self, visit_id: str, requester_id: str, notes: str):
        """Close out a scheduled visit with an outcome note. Only the owning broker,
        only while still 'scheduled'. Raises ValueError("not_found") -> 404,
        PermissionError -> 403, ValueError("visit_not_scheduled") -> 409."""
        record = self._persistence.get_by_id(visit_id)
        if not record:
            raise ValueError("not_found")
        if record.broker_id != requester_id:
            raise PermissionError("forbidden")
        if record.status != "scheduled":
            raise ValueError("visit_not_scheduled")
        clean_notes = (notes or "").strip()
        return self._persistence.complete_visit(visit_id, clean_notes or None)
