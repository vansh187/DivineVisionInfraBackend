import logging
from Divinepersistence import persistenceAdminCustomers
from DivineDTO.models import CustomerCreateDTO

logger = logging.getLogger(__name__)


def _as_item(row) -> dict:
    return {
        "id": row.id,
        "full_name": row.full_name,
        "email": row.email,
        "phone": row.phone,
        "source": row.source,
        "status": row.status,
        "created_at": row.created_at,
        "last_activity_at": row.last_activity_at,
    }


class serviceAdminCustomers:
    def __init__(self, persistence: persistenceAdminCustomers = None):
        self._persistence = persistence or persistenceAdminCustomers()

    def list_customers(self, search: str = None, source: str = None, status: str = None,
                        sort: str = "-created_at", page: int = 1, page_size: int = 20) -> dict:
        search = (search or "").strip() or None
        offset = (page - 1) * page_size
        rows = self._persistence.list_customers(
            search=search, source=source, status=status, sort=sort, limit=page_size, offset=offset,
        )
        total_items = self._persistence.count_customers(search=search, source=source, status=status)
        total_pages = (total_items + page_size - 1) // page_size
        return {
            "items": [_as_item(r) for r in rows],
            "pagination": {
                "page": page, "page_size": page_size,
                "total_items": total_items, "total_pages": total_pages,
            },
        }

    def create_customer(self, dto: CustomerCreateDTO) -> dict:
        """Adds a manually-entered lead (source=WEBSITE, status=LEAD always - see
        DivineDatabasequeries/admin_customers_queries.yaml for why). Raises
        ValueError("email_already_exists") if the email is already used by an
        existing customer account or an unconverted lead."""
        email = dto.email.strip().lower()
        if self._persistence.email_in_use(email):
            raise ValueError("email_already_exists")
        row = self._persistence.create_manual_lead(
            full_name=dto.full_name.strip(), email=email, phone=dto.phone.strip(),
        )
        return {
            "id": row.id,
            "full_name": row.full_name,
            "email": row.email,
            "phone": row.phone,
            "source": "WEBSITE",
            "status": "LEAD",
            "created_at": row.created_at,
            "last_activity_at": row.last_activity_at,
        }
