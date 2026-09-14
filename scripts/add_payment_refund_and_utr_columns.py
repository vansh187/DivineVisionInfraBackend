"""
One-time migration: adds refund bookkeeping + RTGS/NEFT support to the
already-live divine_payments table, for the Booking KYC review workflow's
reject/cancel-triggers-a-refund step (see
DivineService/service_payment.py::initiate_refund).

New columns (all nullable except refund_status, which defaults 'none' so
every existing row backfills correctly - no payment before this feature had
a refund):
  - utr_number             customer-entered bank reference for an 'rtgs_neft'
                            payment (no gateway transaction id exists for those)
  - refund_status           'none' | 'pending' | 'processing' | 'completed' | 'failed'
  - refund_amount
  - razorpay_refund_id
  - refund_initiated_date
  - refund_completed_date
  - refund_note

Also widens the method CHECK (added by add_payment_method_column.py) from
('razorpay', 'cash') to include 'rtgs_neft'.

create_all() only creates missing tables, it never alters an existing one, so
this has to run separately against the live Postgres. Safe to run more than
once - ADD COLUMN IF NOT EXISTS / DROP+ADD CONSTRAINT make it idempotent.

Usage:
    python scripts/add_payment_refund_and_utr_columns.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import engine
from sqlalchemy import text, inspect

_REFUND_STATUS_VALUES = ("none", "pending", "processing", "completed", "failed")
_METHOD_VALUES = ("razorpay", "cash", "rtgs_neft")

_NEW_COLUMNS = (
    ("utr_number", "varchar(50)", None),
    ("refund_status", "varchar(20)", "'none'"),
    ("refund_amount", "numeric(12,2)", None),
    ("razorpay_refund_id", "varchar(64)", None),
    ("refund_initiated_date", "timestamp", None),
    ("refund_completed_date", "timestamp", None),
    ("refund_note", "text", None),
)


def _add_column_if_missing(conn, table: str, column_name: str, column_type: str, default_sql: str, dialect: str):
    """Postgres understands ADD COLUMN IF NOT EXISTS; SQLite's ALTER TABLE
    grammar doesn't accept IF NOT EXISTS at all (a syntax error, not a no-op),
    so on SQLite the existing columns are inspected first and the plain ADD
    COLUMN is only issued when genuinely missing."""
    not_null_default = f" NOT NULL DEFAULT {default_sql}" if default_sql else ""
    if dialect == "postgresql":
        conn.execute(text(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column_name} {column_type}{not_null_default};"
        ))
        return
    existing = {col["name"] for col in inspect(conn).get_columns(table)}
    if column_name not in existing:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column_name} {column_type}{not_null_default};"))


def main():
    try:
        with engine.begin() as conn:
            print("Adding utr_number / refund columns to divine_payments (if not already present)...")
            for column_name, column_type, default_sql in _NEW_COLUMNS:
                _add_column_if_missing(conn, "divine_payments", column_name, column_type, default_sql, engine.dialect.name)

            if engine.dialect.name != "postgresql":
                print(f"dialect={engine.dialect.name}: skipping CHECK constraints (not supported the same way).")
                print("Done.")
                return

            print("Widening the divine_payments method CHECK to include 'rtgs_neft'...")
            conn.execute(text(
                "ALTER TABLE divine_payments DROP CONSTRAINT IF EXISTS divine_payments_method_check;"
            ))
            method_list = ", ".join(f"'{m}'" for m in _METHOD_VALUES)
            conn.execute(text(
                "ALTER TABLE divine_payments ADD CONSTRAINT divine_payments_method_check "
                f"CHECK (method IN ({method_list}));"
            ))

            print("Adding a refund_status CHECK constraint...")
            conn.execute(text(
                "ALTER TABLE divine_payments DROP CONSTRAINT IF EXISTS divine_payments_refund_status_check;"
            ))
            status_list = ", ".join(f"'{s}'" for s in _REFUND_STATUS_VALUES)
            conn.execute(text(
                "ALTER TABLE divine_payments ADD CONSTRAINT divine_payments_refund_status_check "
                f"CHECK (refund_status IN ({status_list}));"
            ))

        print("Done.")
    except Exception as exc:
        print(f"Migration failed, no partial changes were committed (transaction rolled back): {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
