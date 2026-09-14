"""
One-time migration: adds the display fields the admin panel's Site Visits page
needs (GET /admin/visits) to the already-live divine_site_visits table, and
widens its status CHECK to the fuller admin lifecycle.

New columns (all nullable - no FK to divine_customer_users, since a site visit
may be logged for someone who never created a website account; see the
free-text customer_name/customer_contact columns already on this table):
  - origin_type   VARCHAR(20)  'CUSTOMER' | 'CHANNEL_PARTNER' - which of the
                  admin page's two tabs this visit belongs to.
  - source        VARCHAR(100) e.g. 'Website' or a channel partner's name.
  - project_name  VARCHAR(200) free text, e.g. 'Green Meadows'.
  - plot_number   VARCHAR(50)  free text, e.g. 'A-112'.
  - assigned_to   VARCHAR(200) free text staff name - no Employee/Staff table
                  exists yet, so this is deliberately not a FK (see the admin
                  API design discussion; may become one in a later phase).

Existing rows predate these columns and were all created via the broker-only
POST /visits flow, so they are backfilled as origin_type='CHANNEL_PARTNER'
with source set to the owning broker's name.

create_all() only creates missing tables, it never alters an existing one, so
this has to run separately against the live Postgres. Safe to run more than
once - IF NOT EXISTS / DROP CONSTRAINT IF EXISTS make it idempotent. Column
additions run outside the CHECK-constraint block on SQLite (no ALTER .. ADD
CONSTRAINT support there).

Usage:
    python scripts/add_admin_site_visit_fields.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import engine
from sqlalchemy import text, inspect

_NEW_COLUMNS = (
    ("origin_type", "VARCHAR(20)"),
    ("source", "VARCHAR(100)"),
    ("project_name", "VARCHAR(200)"),
    ("plot_number", "VARCHAR(50)"),
    ("assigned_to", "VARCHAR(200)"),
)

_STATUS_VALUES = (
    "requested", "scheduled", "confirmed", "completed", "follow_up", "no_show", "converted", "cancelled",
)


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
            print("Adding admin display columns to divine_site_visits (if not already present)...")
            for column_name, column_type in _NEW_COLUMNS:
                _add_column_if_missing(conn, "divine_site_visits", column_name, column_type, engine.dialect.name)

            print("Backfilling origin_type/source for existing (broker-created) rows...")
            if engine.dialect.name == "postgresql":
                conn.execute(text(
                    "UPDATE divine_site_visits v "
                    "SET origin_type = 'CHANNEL_PARTNER', "
                    "    source = COALESCE(source, TRIM(COALESCE(b.first_name, '') || ' ' || COALESCE(b.last_name, ''))) "
                    "FROM divine_broker_users b "
                    "WHERE v.broker_id = b.id AND v.origin_type IS NULL;"
                ))
            else:
                conn.execute(text(
                    "UPDATE divine_site_visits SET origin_type = 'CHANNEL_PARTNER' "
                    "WHERE origin_type IS NULL;"
                ))

            if engine.dialect.name != "postgresql":
                print(f"dialect={engine.dialect.name}: skipping CHECK constraints (not supported the same way).")
                print("Done.")
                return

            print("Widening the divine_site_visits status CHECK...")
            conn.execute(text(
                "ALTER TABLE divine_site_visits DROP CONSTRAINT IF EXISTS divine_site_visits_status_check;"
            ))
            status_list = ", ".join(f"'{s}'" for s in _STATUS_VALUES)
            conn.execute(text(
                "ALTER TABLE divine_site_visits ADD CONSTRAINT divine_site_visits_status_check "
                f"CHECK (status IN ({status_list}));"
            ))

            print("Adding origin_type CHECK constraint...")
            conn.execute(text(
                "ALTER TABLE divine_site_visits DROP CONSTRAINT IF EXISTS divine_site_visits_origin_type_check;"
            ))
            conn.execute(text(
                "ALTER TABLE divine_site_visits ADD CONSTRAINT divine_site_visits_origin_type_check "
                "CHECK (origin_type IS NULL OR origin_type IN ('CUSTOMER', 'CHANNEL_PARTNER'));"
            ))

        print("Done.")
    except Exception as exc:
        print(f"Migration failed, no partial changes were committed (transaction rolled back): {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
