"""
One-time migration: instalment (milestone) payments on top of the plot-booking
flow.

  divine_payments
    - purpose CHECK widened to include 'installment'
    - installment_no INT NULL, due_date DATE NULL

  divine_payment_milestones (new)   one row per booking-plan milestone
  divine_payment_reminders  (new)   dedupe log for the payment-due emails

create_all() only creates missing tables, never alters existing ones, so the
divine_payments changes run here. Idempotent - ADD COLUMN IF NOT EXISTS, the
DROP/ADD CONSTRAINT pair, and CREATE TABLE IF NOT EXISTS.

Usage:
    python scripts/add_installment_payments.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import engine
from sqlalchemy import text


def main():
    with engine.begin() as conn:
        print("Widening divine_payments.purpose CHECK to include 'installment'...")
        conn.execute(text("ALTER TABLE divine_payments DROP CONSTRAINT IF EXISTS divine_payments_purpose_check;"))
        conn.execute(text(
            "ALTER TABLE divine_payments ADD CONSTRAINT divine_payments_purpose_check "
            "CHECK (purpose IN ('plot_booking', 'installment', 'other'));"
        ))
        conn.execute(text("ALTER TABLE divine_payments ADD COLUMN IF NOT EXISTS installment_no integer;"))
        conn.execute(text("ALTER TABLE divine_payments ADD COLUMN IF NOT EXISTS due_date date;"))

        print("Creating divine_payment_milestones (if not already present)...")
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS divine_payment_milestones ("
            "  id varchar(48) PRIMARY KEY,"
            "  booking_id varchar(36) NOT NULL,"
            "  customer_id varchar(6) NOT NULL,"
            "  project_id varchar(120),"
            "  inventory_id varchar(36),"
            "  milestone_no integer NOT NULL,"
            "  label varchar(120),"
            "  percent numeric(6,3),"
            "  due_days integer,"
            "  due_date date,"
            "  amount numeric(14,2) NOT NULL DEFAULT 0,"
            "  status varchar(12) NOT NULL DEFAULT 'upcoming' "
            "    CHECK (status IN ('paid','due','overdue','upcoming')),"
            "  paid_on timestamptz,"
            "  paid_payment_id varchar(36),"
            "  created_date timestamptz DEFAULT now(),"
            "  last_updated_date timestamptz DEFAULT now(),"
            "  UNIQUE (booking_id, milestone_no)"
            ");"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_divine_payment_milestones_customer "
            "ON divine_payment_milestones (customer_id);"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_divine_payment_milestones_due "
            "ON divine_payment_milestones (status, due_date);"
        ))

        print("Creating divine_payment_reminders (if not already present)...")
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS divine_payment_reminders ("
            "  id varchar(36) PRIMARY KEY,"
            "  customer_id varchar(6) NOT NULL,"
            "  booking_id varchar(36),"
            "  milestone_id varchar(48) NOT NULL,"
            "  kind varchar(16) NOT NULL "
            "    CHECK (kind IN ('T_MINUS_20','T_MINUS_5','DUE_TODAY','OVERDUE')),"
            "  week_key varchar(12) NOT NULL DEFAULT '',"
            "  amount numeric(14,2),"
            "  due_date date,"
            "  email_to varchar(255),"
            "  delivery_status varchar(20),"
            "  sent_at timestamptz DEFAULT now(),"
            "  UNIQUE (milestone_id, kind, week_key)"
            ");"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_divine_payment_reminders_customer "
            "ON divine_payment_reminders (customer_id);"
        ))
    print("Done.")


if __name__ == "__main__":
    main()
