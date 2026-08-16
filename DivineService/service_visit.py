import uuid
from datetime import datetime, date
from Divinepersistence import persistenceVisit


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

    def schedule_visit(self, broker_id: str, customer_name: str, customer_contact: str,
                        visit_date: str, visit_time: str, notes: str):
        customer_name = (customer_name or "").strip()
        if not customer_name:
            raise ValueError("customer_name_required")

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
        )

    def list_visits(self, broker_id: str):
        return self._persistence.list_by_broker(broker_id)

    def cancel_visit(self, visit_id: str, requester_id: str):
        record = self._persistence.get_by_id(visit_id)
        if not record:
            raise ValueError("not_found")
        if record.broker_id != requester_id:
            raise PermissionError("forbidden")
        return self._persistence.update_status(visit_id, "cancelled")
