import secrets
import logging
from passlib.context import CryptContext
from sqlalchemy.exc import IntegrityError
from Divinepersistence import persistenceAdminCustomers, persistenceCustomer
from DivineDTO.models import CustomerCreateDTO

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


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


def _split_full_name(full_name: str):
    parts = full_name.split(maxsplit=1)
    first_name = parts[0]
    last_name = parts[1] if len(parts) > 1 else None
    return first_name, last_name


class serviceAdminCustomers:
    def __init__(self, persistence: persistenceAdminCustomers = None,
                 customer_persistence: persistenceCustomer = None):
        self._persistence = persistence or persistenceAdminCustomers()
        self._customer_persistence = customer_persistence or persistenceCustomer()

    def list_customers(self, search: str = None, source: str = None, status: str = None,
                        sort: str = "-created_at", page: int = 1, page_size: int = 20) -> dict:
        search = (search or "").strip() or None
        offset = (page - 1) * page_size
        rows = self._persistence.list_customers(
            search=search, source=source, status=status, sort=sort, limit=page_size, offset=offset,
        )
        if rows:
            # One indexed query already carries the filtered total via a window
            # function (see list_customers in admin_customers_queries.yaml) - no
            # second round-trip needed on the common, non-empty path.
            total_items = int(rows[0].total_count)
        else:
            # Nothing to read a window-function total from an empty row set -
            # only reached for a genuinely empty result or a page past the last one.
            total_items = self._persistence.count_customers(search=search, source=source, status=status)
        total_pages = (total_items + page_size - 1) // page_size
        return {
            "items": [_as_item(r) for r in rows],
            "pagination": {
                "page": page, "page_size": page_size,
                "total_items": total_items, "total_pages": total_pages,
            },
        }

    def create_customer(self, dto: CustomerCreateDTO, created_by: str = None) -> dict:
        """Creates a real divine_customer_users account (not a chatbot lead -
        visitor/lead capture is a later phase). username is set to the email
        since the admin form collects no separate username, and the account gets
        a random password the admin never sees: the customer sets their own via
        the existing forgot-password flow the first time they want to log in.
        Raises ValueError("email_already_exists") on a duplicate email/username."""
        email = dto.email.strip().lower()
        if (self._customer_persistence.get_by_username(email)
                or self._customer_persistence.get_by_email(email)):
            raise ValueError("email_already_exists")

        full_name = dto.full_name.strip()
        first_name, last_name = _split_full_name(full_name)
        password_hash = pwd_context.hash(secrets.token_urlsafe(32))

        try:
            user = self._customer_persistence.create_user(
                email, password_hash, created_by=created_by,
                email=email, phone=dto.phone.strip(), first_name=first_name, last_name=last_name,
            )
        except IntegrityError:
            # Race: two concurrent requests for the same email both passed the
            # check above - the DB's own unique constraint on username (=email
            # here) is the backstop that actually prevents the duplicate.
            raise ValueError("email_already_exists")

        return {
            "id": user.id,
            "full_name": full_name,
            "email": user.email,
            "phone": user.phone,
            "source": "WEBSITE",
            "status": "ACTIVE",
            "created_at": user.created_date,
            "last_activity_at": user.last_updated_date,
        }
