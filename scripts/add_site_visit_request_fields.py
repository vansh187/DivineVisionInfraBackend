"""
One-time migration: supports the website "Request a callback" form (project +
preferred window + name/phone/email, no broker involved yet and no exact time
picked) on the already-live divine_site_visits table.

- broker_id and visit_time were both NOT NULL (broker-scheduled visits always
  had both). A customer-originated callback request has neither at creation
  time - a broker/admin fills in the exact time later when they call back and
  confirm - so both columns are widened to nullable.
- customer_email VARCHAR(255): the form's optional email field. Kept separate
  from customer_contact (which the broker flow already uses for phone) rather
  than overloading one column with two different kinds of value.
- preferred_window VARCHAR(20): 'today' | 'tomorrow' | 'weekend', for
  display until a broker locks in a real visit_date/visit_time.

create_all() only creates missing tables, it never alters an existing one or
loosens a NOT NULL constraint, so this has to run separately against the live
Postgres. Safe to run more than once. The two new columns are added on SQLite
too (for local dev/test parity); only the NOT NULL/CHECK changes below are
Postgres-only (SQLite doesn't support them the same way).

Usage:
    python scripts/add_site_visit_request_fields.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import engine
from sqlalchemy import text, inspect


def _add_column_if_missing(conn, table: str, column_name: str, column_type: str, dialect: str):
    """Postgres understands ADD COLUMN IF NOT EXISTS; SQLite's ALTER TABLE
    grammar doesn't accept IF NOT EXISTS at all (a syntax error, not a no-op),
    so on SQLite the existing columns are inspected first and the plain ADD
    COLUMN is only issued when genuinely missing."""
    if dialect == "postgresql":
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column_name} {column_type};"))
        return
    existing = {col["name"] for col in inspect(conn).get_columns(table)}
    if column_name not in existing:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column_name} {column_type};"))


def main():
    try:
        with engine.begin() as conn:
            print("Adding customer_email / preferred_window columns to divine_site_visits (if not already present)...")
            _add_column_if_missing(conn, "divine_site_visits", "customer_email", "VARCHAR(255)", engine.dialect.name)
            _add_column_if_missing(conn, "divine_site_visits", "preferred_window", "VARCHAR(20)", engine.dialect.name)

            if engine.dialect.name != "postgresql":
                print(f"dialect={engine.dialect.name}: skipping NOT NULL/CHECK changes (not supported the same way).")
                print("Done.")
                return

            print("Widening broker_id and visit_time to nullable...")
            conn.execute(text("ALTER TABLE divine_site_visits ALTER COLUMN broker_id DROP NOT NULL;"))
            conn.execute(text("ALTER TABLE divine_site_visits ALTER COLUMN visit_time DROP NOT NULL;"))

            print("Adding preferred_window CHECK constraint...")
            conn.execute(text(
                "ALTER TABLE divine_site_visits DROP CONSTRAINT IF EXISTS divine_site_visits_preferred_window_check;"
            ))
            conn.execute(text(
                "ALTER TABLE divine_site_visits ADD CONSTRAINT divine_site_visits_preferred_window_check "
                "CHECK (preferred_window IS NULL OR preferred_window IN ('today', 'tomorrow', 'weekend'));"
            ))

        print("Done.")
    except Exception as exc:
        print(f"Migration failed, no partial changes were committed (transaction rolled back): {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
