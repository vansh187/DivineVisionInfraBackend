"""
One-time migration: widens the divine_site_visits.status CHECK constraint to allow
'completed' (a broker can now close out a scheduled visit with an outcome note).
create_all() only creates missing tables, it never alters an existing constraint,
so this has to run separately against the live Postgres.

Safe to run more than once. No-op on SQLite (no such constraint there).

Usage:
    python scripts/add_visit_completed_status.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import engine
from sqlalchemy import text


def main():
    if engine.dialect.name != "postgresql":
        print(f"dialect={engine.dialect.name}: nothing to do (no status CHECK constraint).")
        return
    with engine.begin() as conn:
        print("Dropping the old divine_site_visits status CHECK (if present)...")
        conn.execute(text(
            "ALTER TABLE divine_site_visits "
            "DROP CONSTRAINT IF EXISTS divine_site_visits_status_check;"
        ))
        print("Adding the widened CHECK (scheduled | cancelled | completed)...")
        conn.execute(text(
            "ALTER TABLE divine_site_visits ADD CONSTRAINT divine_site_visits_status_check "
            "CHECK (status IN ('scheduled', 'cancelled', 'completed'));"
        ))
    print("Done.")


if __name__ == "__main__":
    main()
