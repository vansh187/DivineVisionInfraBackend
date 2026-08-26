"""
One-time migration: adds Channel Partner reservation support to the already-live
divine_project_inventory table, and creates the divine_inventory_reservations
audit table (create_all() only creates missing tables, it never alters existing
ones, so the inventory-table change has to run separately).

Safe to run more than once - IF NOT EXISTS / guarded DO blocks make it idempotent.

Usage:
    python scripts/add_inventory_reservation_columns.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import engine
from sqlalchemy import text


def main():
    with engine.begin() as conn:
        print("Adding reservation columns to divine_project_inventory (if not already present)...")
        conn.execute(text(
            "ALTER TABLE divine_project_inventory "
            "ADD COLUMN IF NOT EXISTS reserved_by_broker_id varchar(6) REFERENCES divine_broker_users(id);"
        ))
        conn.execute(text(
            "ALTER TABLE divine_project_inventory ADD COLUMN IF NOT EXISTS reserved_at timestamptz;"
        ))
        conn.execute(text(
            "ALTER TABLE divine_project_inventory ADD COLUMN IF NOT EXISTS reserved_until timestamptz;"
        ))

        print("Widening the status CHECK constraint to include 'reserved'...")
        conn.execute(text(
            "ALTER TABLE divine_project_inventory DROP CONSTRAINT IF EXISTS divine_project_inventory_status_check;"
        ))
        conn.execute(text(
            "ALTER TABLE divine_project_inventory ADD CONSTRAINT divine_project_inventory_status_check "
            "CHECK (status IN ('available', 'held', 'sold', 'reserved'));"
        ))

        print("Adding reservation indexes (if not already present)...")
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_divine_project_inventory_reserved_by "
            "ON divine_project_inventory (reserved_by_broker_id);"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_divine_project_inventory_reserved_until "
            "ON divine_project_inventory (status, reserved_until);"
        ))

        print("Creating divine_inventory_reservations (if not already present)...")
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS divine_inventory_reservations ("
            "  id varchar(36) PRIMARY KEY,"
            "  inventory_id varchar(36) NOT NULL REFERENCES divine_project_inventory(id),"
            "  broker_id varchar(6) NOT NULL REFERENCES divine_broker_users(id),"
            "  reserved_at timestamptz NOT NULL,"
            "  expires_at timestamptz NOT NULL,"
            "  ended_at timestamptz,"
            "  outcome varchar(20) NOT NULL DEFAULT 'active' "
            "    CHECK (outcome IN ('active', 'converted', 'expired', 'released')),"
            "  created_date timestamptz DEFAULT now()"
            ");"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_divine_inventory_reservations_inventory_id "
            "ON divine_inventory_reservations (inventory_id);"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_divine_inventory_reservations_broker_id "
            "ON divine_inventory_reservations (broker_id);"
        ))
    print("Done.")


if __name__ == "__main__":
    main()
