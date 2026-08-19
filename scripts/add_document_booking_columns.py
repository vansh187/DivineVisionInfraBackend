"""
One-time migration: adds storage_bucket, project_id, payment_id, razorpay_order_id,
razorpay_payment_id columns to the already-live divine_documents table (create_all()
only creates missing tables, it never alters existing ones, so this has to run
separately).

Safe to run more than once - ADD COLUMN IF NOT EXISTS makes it idempotent.
storage_bucket backfills existing rows to 'documents' (every document generated
before the booking-application upload flow existed was written to that bucket) so
GET /documents/{id} keeps signing against the right bucket for those rows; the other
three columns are nullable with no default, since only booking-application uploads
populate them.

Usage:
    python scripts/add_document_booking_columns.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import engine
from sqlalchemy import text


def main():
    with engine.begin() as conn:
        print("Adding storage_bucket column to divine_documents (if not already present)...")
        conn.execute(text(
            "ALTER TABLE divine_documents ADD COLUMN IF NOT EXISTS storage_bucket varchar(100) DEFAULT 'documents';"
        ))
        conn.execute(text(
            "UPDATE divine_documents SET storage_bucket = 'documents' WHERE storage_bucket IS NULL;"
        ))
        print("Adding project_id column to divine_documents (if not already present)...")
        conn.execute(text(
            "ALTER TABLE divine_documents ADD COLUMN IF NOT EXISTS project_id varchar(100);"
        ))
        print("Adding payment_id column to divine_documents (if not already present)...")
        conn.execute(text(
            "ALTER TABLE divine_documents ADD COLUMN IF NOT EXISTS payment_id varchar(36);"
        ))
        print("Adding razorpay_order_id column to divine_documents (if not already present)...")
        conn.execute(text(
            "ALTER TABLE divine_documents ADD COLUMN IF NOT EXISTS razorpay_order_id varchar(64);"
        ))
        print("Adding razorpay_payment_id column to divine_documents (if not already present)...")
        conn.execute(text(
            "ALTER TABLE divine_documents ADD COLUMN IF NOT EXISTS razorpay_payment_id varchar(64);"
        ))
        print("Adding index on payment_id (if not already present)...")
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_divine_documents_payment_id ON divine_documents (payment_id);"
        ))
    print("Done.")


if __name__ == "__main__":
    main()
