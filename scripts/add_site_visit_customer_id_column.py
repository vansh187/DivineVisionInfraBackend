"""
One-time migration: adds a customer_id column to the already-live
divine_site_visits table.

Why: GET /visits/mine (a signed-in customer's own visit history) originally
matched purely by customer_email - correct for an anonymous "request a
callback" submission (no account, so no id to store), but fragile: a customer
who changes their account email later would silently lose the match to any
older visit. POST /visits/request now accepts an optional customer bearer
token (DivineService.auth.get_optional_customer) and, when present, tags the
new visit with that customer_id directly. Older/anonymous rows keep
customer_id NULL and are still matched by email as a fallback - see
persistenceVisit.list_mine.

Nullable, and deliberately NOT a real foreign key - nothing else in this
table is either (see broker_id), and a site visit intentionally outlives a
deleted/renamed customer account without becoming an orphaned-FK error.
Indexed since GET /visits/mine filters on it directly.

create_all() only creates missing tables, it never alters an existing one, so
this has to run separately against the live Postgres. Safe to run more than
once. No-op-safe on SQLite too (column existence is checked first, since
SQLite's ALTER TABLE doesn't support IF NOT EXISTS).

Usage:
    python scripts/add_site_visit_customer_id_column.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import engine
from sqlalchemy import text, inspect


def main():
    try:
        with engine.begin() as conn:
            dialect = engine.dialect.name
            print("Adding customer_id column to divine_site_visits (if not already present)...")
            if dialect == "postgresql":
                conn.execute(text(
                    "ALTER TABLE divine_site_visits ADD COLUMN IF NOT EXISTS customer_id VARCHAR(6);"
                ))
            else:
                existing = {col["name"] for col in inspect(conn).get_columns("divine_site_visits")}
                if "customer_id" not in existing:
                    conn.execute(text("ALTER TABLE divine_site_visits ADD COLUMN customer_id VARCHAR(6);"))

            print("Adding an index on customer_id (if not already present)...")
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_divine_site_visits_customer_id "
                "ON divine_site_visits (customer_id);"
            ))

        print("Done.")
    except Exception as exc:
        print(f"Migration failed, no partial changes were committed (transaction rolled back): {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
