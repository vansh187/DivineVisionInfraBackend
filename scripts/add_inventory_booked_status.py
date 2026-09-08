"""
One-time migration: adds the customer booking flow to the already-live
divine_project_inventory and divine_payments tables.

  divine_project_inventory
    - status CHECK widened to include 'booked'
    - booked_at / booked_payment_id / booked_by columns

  divine_payments
    - purpose            ('plot_booking' | 'other', default 'other')
    - inventory_id       (nullable FK -> divine_project_inventory.id)
    - needs_manual_review / manual_review_reason

create_all() only creates missing tables, never alters existing ones, so these
column / constraint changes have to run separately.

Safe to run more than once - ADD COLUMN IF NOT EXISTS and the DROP/ADD CONSTRAINT
pair make it idempotent.

Usage:
    python scripts/add_inventory_booked_status.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import engine
from sqlalchemy import text


def main():
    with engine.begin() as conn:
        print("Widening divine_project_inventory.status CHECK to include 'booked'...")
        conn.execute(text(
            "ALTER TABLE divine_project_inventory DROP CONSTRAINT IF EXISTS divine_project_inventory_status_check;"
        ))
        conn.execute(text(
            "ALTER TABLE divine_project_inventory ADD CONSTRAINT divine_project_inventory_status_check "
            "CHECK (status IN ('available', 'held', 'booked', 'sold', 'reserved'));"
        ))

        print("Adding booking columns to divine_project_inventory (if not already present)...")
        conn.execute(text(
            "ALTER TABLE divine_project_inventory ADD COLUMN IF NOT EXISTS booked_at timestamptz;"
        ))
        conn.execute(text(
            "ALTER TABLE divine_project_inventory ADD COLUMN IF NOT EXISTS booked_payment_id varchar(36);"
        ))
        conn.execute(text(
            "ALTER TABLE divine_project_inventory ADD COLUMN IF NOT EXISTS booked_by varchar(6);"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_divine_project_inventory_booked_by "
            "ON divine_project_inventory (booked_by);"
        ))

        print("Adding booking columns to divine_payments (if not already present)...")
        conn.execute(text(
            "ALTER TABLE divine_payments ADD COLUMN IF NOT EXISTS purpose varchar(20) NOT NULL DEFAULT 'other';"
        ))
        conn.execute(text(
            "ALTER TABLE divine_payments DROP CONSTRAINT IF EXISTS divine_payments_purpose_check;"
        ))
        conn.execute(text(
            "ALTER TABLE divine_payments ADD CONSTRAINT divine_payments_purpose_check "
            "CHECK (purpose IN ('plot_booking', 'other'));"
        ))
        conn.execute(text(
            "ALTER TABLE divine_payments ADD COLUMN IF NOT EXISTS inventory_id varchar(36);"
        ))
        conn.execute(text(
            "ALTER TABLE divine_payments ADD COLUMN IF NOT EXISTS needs_manual_review boolean NOT NULL DEFAULT false;"
        ))
        conn.execute(text(
            "ALTER TABLE divine_payments ADD COLUMN IF NOT EXISTS manual_review_reason varchar(60);"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_divine_payments_inventory_id ON divine_payments (inventory_id);"
        ))
    print("Done.")


if __name__ == "__main__":
    main()
