"""
One-time migration: adds the "method" column to the already-live divine_payments
table (cash payments need it; create_all() only creates missing tables, it never
alters existing ones, so this has to run separately).

Safe to run more than once - IF NOT EXISTS / DEFAULT make it idempotent, and
existing rows all backfill to method='razorpay' (correct, since every payment
before this feature went through Razorpay).

Usage:
    python scripts/add_payment_method_column.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from Divinepersistence.persistence_db import engine
from sqlalchemy import text


def main():
    with engine.begin() as conn:
        print("Adding method column to divine_payments (if not already present)...")
        conn.execute(text(
            "ALTER TABLE divine_payments "
            "ADD COLUMN IF NOT EXISTS method varchar(20) NOT NULL DEFAULT 'razorpay';"
        ))
        print("Adding CHECK constraint (if not already present)...")
        conn.execute(text(
            "DO $$ BEGIN "
            "  IF NOT EXISTS ("
            "    SELECT 1 FROM pg_constraint WHERE conname = 'divine_payments_method_check'"
            "  ) THEN "
            "    ALTER TABLE divine_payments ADD CONSTRAINT divine_payments_method_check "
            "    CHECK (method IN ('razorpay', 'cash')); "
            "  END IF; "
            "END $$;"
        ))
    print("Done.")


if __name__ == "__main__":
    main()