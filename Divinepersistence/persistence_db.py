import os
import yaml
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

load_dotenv()

# Support explicit Supabase/Postgres URL via SUPABASE_DATABASE_URL or generic DATABASE_URL
DATABASE_URL = os.getenv("SUPABASE_DATABASE_URL") or os.getenv("DATABASE_URL") or "sqlite:///divine.db"

# For postgres, users may include ?sslmode=require in the URL (common for Supabase)
engine = create_engine(DATABASE_URL, echo=False, future=True)
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

                if dialect == "postgresql" and "divine_password_reset_otp" in tables:
                    conn.execute(text(
                        "ALTER TABLE divine_password_reset_otp "
                        "ALTER COLUMN role TYPE varchar(32);"
                    ))
        except Exception:
            # Keep health/startup behavior consistent: table creation is best effort
            # in this app today, and request handlers still log concrete DB failures.
            pass

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
