import os
import logging
import yaml
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Support explicit Supabase/Postgres URL via SUPABASE_DATABASE_URL or generic DATABASE_URL
DATABASE_URL = os.getenv("SUPABASE_DATABASE_URL") or os.getenv("DATABASE_URL") or "sqlite:///divine.db"

# For postgres, users may include ?sslmode=require in the URL (common for Supabase)
engine = create_engine(DATABASE_URL, echo=False, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class PersistenceDB:
    def __init__(self):
        self.engine = engine
        self.SessionLocal = SessionLocal

    def create_tables(self):
        Base.metadata.create_all(bind=self.engine)
        self.apply_lightweight_migrations()

    def apply_lightweight_migrations(self):
        """Apply tiny idempotent schema fixes that create_all() cannot handle.

        This keeps deployed databases that already have older tables aligned with
        additive model changes without requiring a separate migration command for
        every deploy.
        """
        try:
            with self.engine.begin() as conn:
                dialect = self.engine.dialect.name
                inspector = inspect(conn)
                tables = set(inspector.get_table_names())
                if "divine_admin_users" in tables:
                    columns = {c["name"] for c in inspector.get_columns("divine_admin_users")}
                    if "email_verified" not in columns:
                        if dialect == "postgresql":
                            conn.execute(text(
                                "ALTER TABLE divine_admin_users "
                                "ADD COLUMN IF NOT EXISTS email_verified boolean NOT NULL DEFAULT false;"
                            ))
                        else:
                            conn.execute(text(
                                "ALTER TABLE divine_admin_users "
                                "ADD COLUMN email_verified boolean NOT NULL DEFAULT 0;"
                            ))
                        conn.execute(text(
                            "UPDATE divine_admin_users SET email_verified = true "
                            "WHERE email_verified = false;"
                        ))
                    if "approval_status" not in columns:
                        # Existing admins predate the approval step - backfill them
                        # as approved so nobody already working gets locked out.
                        conn.execute(text(
                            "ALTER TABLE divine_admin_users "
                            "ADD COLUMN approval_status varchar(20) NOT NULL DEFAULT 'approved';"
                        ))
                        if dialect == "postgresql":
                            conn.execute(text(
                                "ALTER TABLE divine_admin_users "
                                "ALTER COLUMN approval_status SET DEFAULT 'pending';"
                            ))
                    for name, ddl in (
                        ("approval_decided_by", "varchar(255)"),
                        ("approval_decided_date", "timestamp"),
                        ("approval_note", "varchar(500)"),
                    ):
                        if name not in columns:
                            conn.execute(text(f"ALTER TABLE divine_admin_users ADD COLUMN {name} {ddl};"))

                if dialect == "postgresql" and "divine_password_reset_otp" in tables:
                    conn.execute(text(
                        "ALTER TABLE divine_password_reset_otp "
                        "ALTER COLUMN role TYPE varchar(32);"
                    ))
        except Exception:
            # Keep health/startup behavior consistent: table creation is best effort
            # in this app today, and request handlers still log concrete DB failures.
            logger.exception("lightweight_migrations_failed")

        # Own transaction: the offline-submission INSERT/SELECT hard-require this
        # column, so a failure in the admin-user migrations above must not skip it.
        try:
            with self.engine.begin() as conn:
                inspector = inspect(conn)
                if "divine_offline_payment_submissions" in set(inspector.get_table_names()):
                    columns = {c["name"] for c in inspector.get_columns("divine_offline_payment_submissions")}
                    if "cheque_number" not in columns:
                        conn.execute(text(
                            "ALTER TABLE divine_offline_payment_submissions ADD COLUMN cheque_number varchar(50);"
                        ))
        except Exception:
            logger.exception("offline_submission_cheque_number_migration_failed")

        # Own transaction: login/password-reset and the inventory sheet sync read
        # these columns, so they must exist the moment this code is deployed.
        try:
            with self.engine.begin() as conn:
                ensure_offline_import_columns(conn)
        except Exception:
            logger.exception("offline_import_columns_migration_failed")

    def test_connection(self) -> bool:
        """Attempt a simple connection to validate DB connectivity. Returns True on success."""
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False


class RowWrapper:
    def __init__(self, mapping):
        if mapping:
            self.__dict__.update(mapping)


def format_date_value(value) -> str:
    """Shared by anything that returns a raw SQLAlchemy date/datetime column
    value in a response DTO: isoformat() when the driver already gave us a
    date/datetime object, str() as a fallback for a dialect that returned it
    as text, None through unchanged. Used by DivineAPI/visit_api.py and
    DivineService/service_admin_visits.py so the two don't drift apart."""
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def load_queries(filename: str) -> dict:
    """Load a flat query-name -> SQL dict from DivineDatabasequeries/<filename>, safely defaulting to {} on any error."""
    root = os.path.dirname(os.path.dirname(__file__))
    qpath = os.path.join(root, "DivineDatabasequeries", filename)
    try:
        if os.path.exists(qpath):
            with open(qpath, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    except Exception:
        pass
    return {}


# Columns added for the offline-buyer import + client inventory sheet sync.
# Shared by PersistenceDB.apply_lightweight_migrations (app startup) and
# scripts/add_offline_import_support.py. Additive and idempotent.
OFFLINE_IMPORT_COLUMNS = {
    "divine_customer_users": (
        ("source", "varchar(30) NOT NULL DEFAULT 'website'"),
        ("must_change_password", "boolean NOT NULL DEFAULT false"),
        ("temp_password_expires_at", "timestamptz"),
        ("invite_status", "varchar(20)"),
        ("invite_sent_at", "timestamptz"),
    ),
    "divine_project_inventory": (
        ("sale_source", "varchar(20)"),
        ("status_note", "varchar(255)"),
        ("status_updated_at", "timestamptz"),
    ),
    # Payment plans (scripts/add_payment_plans.py)
    "divine_payment_milestones": (
        ("basic_amount", "numeric(14,2)"),
        ("charge_head", "varchar(20)"),
        ("charge_amount", "numeric(14,2)"),
        ("due_trigger", "varchar(12)"),
        ("due_from", "varchar(100)"),          # custom "Counted from" event
    ),
    "divine_payment_plan_instalments": (
        ("due_from", "varchar(100)"),
    ),
}

# Columns that must be at least this wide (Postgres only; widening a varchar is
# a metadata-only change). charge_head holds e.g. 'PLC+IFMS+CAM' (12 chars);
# milestone labels come from plan labels of up to 150 chars.
MIN_VARCHAR_WIDTHS = {
    ("divine_payment_milestones", "charge_head"): 20,
    ("divine_payment_milestones", "label"): 150,
}


def ensure_offline_import_columns(conn) -> list:
    """Add any missing OFFLINE_IMPORT_COLUMNS. Returns the 'table.column' names
    it added. SQLite gets portable types (no timestamptz / boolean literals)."""
    added = []
    dialect = conn.engine.dialect.name if hasattr(conn, "engine") else "postgresql"
    inspector = inspect(conn)
    tables = set(inspector.get_table_names())
    for table, columns in OFFLINE_IMPORT_COLUMNS.items():
        if table not in tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for name, ddl in columns:
            if name in existing:
                continue
            if dialect != "postgresql":
                ddl = ddl.replace("timestamptz", "timestamp").replace("DEFAULT false", "DEFAULT 0")
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl};"))
            added.append(f"{table}.{name}")
    if dialect == "postgresql":
        for (table, column), width in MIN_VARCHAR_WIDTHS.items():
            if table not in tables:
                continue
            current = conn.execute(text(
                "SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"),
                {"t": table, "c": column}).scalar()
            if current is not None and int(current) < width:
                conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE varchar({int(width)});"))
                added.append(f"{table}.{column} varchar({current}->{width})")
    return added
