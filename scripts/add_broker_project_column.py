"""
One-time migration: adds the "project" column to the already-live
divine_broker_users table (channel partners now pick which township they work
with at signup). create_all() only creates missing tables, it never alters
existing ones, so this has to run separately against the live Postgres.

The column is added nullable, with a CHECK constraint restricting it to the
two known project slugs. It is deliberately left nullable here rather than
NOT NULL: existing broker rows predate this field and have no project value,
so a NOT NULL constraint would need a backfill decision (which slug to assign
brokers signed up before this feature existed) that's an ops call, not
something this script should guess. New signups already get a value via the
API's required-field validation once that code ships.

Safe to run more than once - IF NOT EXISTS / DROP CONSTRAINT IF EXISTS make it
idempotent. No-op on SQLite (no CHECK constraint support the same way).

Usage:
    python scripts/add_broker_project_column.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import engine
from sqlalchemy import text


def main():
    if engine.dialect.name != "postgresql":
        print(f"dialect={engine.dialect.name}: adding column without a CHECK constraint.")
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE divine_broker_users ADD COLUMN IF NOT EXISTS project VARCHAR(32);"
            ))
        print("Done.")
        return

    with engine.begin() as conn:
        print("Adding project column to divine_broker_users (if not already present)...")
        conn.execute(text(
            "ALTER TABLE divine_broker_users ADD COLUMN IF NOT EXISTS project VARCHAR(32);"
        ))
        print("Adding CHECK constraint (if not already present)...")
        conn.execute(text(
            "ALTER TABLE divine_broker_users DROP CONSTRAINT IF EXISTS divine_broker_users_project_check;"
        ))
        conn.execute(text(
            "ALTER TABLE divine_broker_users ADD CONSTRAINT divine_broker_users_project_check "
            "CHECK (project IS NULL OR project IN ('suraksha-enclave', 'ops-divine-greens'));"
        ))
        missing = conn.execute(text(
            "SELECT count(*) FROM divine_broker_users WHERE project IS NULL;"
        )).scalar()
        print(f"Done. {missing} existing broker row(s) still have project = NULL "
              f"(pre-dates this field) - backfill separately if needed.")


if __name__ == "__main__":
    main()
