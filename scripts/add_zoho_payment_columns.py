"""
One-time migration: adds Zoho Payments gateway columns to the already-live
divine_payments, divine_broker_commissions, and divine_documents tables, for
the Razorpay -> Zoho Payments cutover (see
DivineService/service_payment_gateway.py, DivineService/service_payment.py).

This is purely ADDITIVE - every existing razorpay_* column, its index, and
every historical method='razorpay' row are left completely untouched. New
payments use the new zoho_* columns; old rows keep reading from razorpay_*
exactly as they always have (receipts, admin refund/revenue screens, and
DivineDTO's gateway_payment_id fields all branch on `method` to pick the
right column).

New columns (all nullable, since a cash/rtgs_neft/legacy-razorpay row
legitimately has none of them):
  divine_payments:
    - zoho_payments_session_id (indexed, like the existing razorpay_order_id index)
    - zoho_payment_id
    - zoho_signature
    - zoho_refund_id
  divine_broker_commissions:
    - zoho_payments_session_id (indexed)
  divine_documents:
    - zoho_payments_session_id
    - zoho_payment_id

Also widens the divine_payments method CHECK constraint to add 'zoho' - kept
alongside the existing 'razorpay'/'cash'/'rtgs_neft' values (this also folds
in the 'rtgs_neft' value that a prior migration added but db_init.sql's own
CHECK was never updated for - matching this repo's established pattern where
db_init.sql is the initial baseline only, and schema evolution after that
lives in these standalone scripts, not back-edited into db_init.sql).

create_all() only creates missing tables, it never alters an existing one, so
this has to run separately against the live Postgres. Safe to run more than
once - ADD COLUMN IF NOT EXISTS / DROP+ADD CONSTRAINT make it idempotent.

Usage:
    python scripts/add_zoho_payment_columns.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import engine
from sqlalchemy import text, inspect

_METHOD_VALUES = ("zoho", "razorpay", "cash", "rtgs_neft")

_PAYMENTS_COLUMNS = (
    ("zoho_payments_session_id", "varchar(64)", None),
    ("zoho_payment_id", "varchar(64)", None),
    ("zoho_signature", "varchar(255)", None),
    ("zoho_refund_id", "varchar(64)", None),
)
_BROKER_COMMISSION_COLUMNS = (
    ("zoho_payments_session_id", "varchar(64)", None),
)
_DOCUMENT_COLUMNS = (
    ("zoho_payments_session_id", "varchar(64)", None),
    ("zoho_payment_id", "varchar(64)", None),
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
            dialect = engine.dialect.name

            print("Adding zoho_* columns to divine_payments (if not already present)...")
            for column_name, column_type, default_sql in _PAYMENTS_COLUMNS:
                _add_column_if_missing(conn, "divine_payments", column_name, column_type, default_sql, dialect)

            print("Adding zoho_payments_session_id to divine_broker_commissions (if not already present)...")
            for column_name, column_type, default_sql in _BROKER_COMMISSION_COLUMNS:
                _add_column_if_missing(conn, "divine_broker_commissions", column_name, column_type, default_sql, dialect)

            print("Adding zoho_* columns to divine_documents (if not already present)...")
            for column_name, column_type, default_sql in _DOCUMENT_COLUMNS:
                _add_column_if_missing(conn, "divine_documents", column_name, column_type, default_sql, dialect)

            if dialect == "postgresql":
                print("Indexing divine_payments.zoho_payments_session_id / divine_broker_commissions.zoho_payments_session_id...")
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS idx_divine_payments_zoho_payments_session_id "
                    "ON divine_payments (zoho_payments_session_id);"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS idx_divine_broker_commissions_zoho_payments_session_id "
                    "ON divine_broker_commissions (zoho_payments_session_id);"
                ))

                print("Widening the divine_payments method CHECK to include 'zoho'...")
                conn.execute(text(
                    "ALTER TABLE divine_payments DROP CONSTRAINT IF EXISTS divine_payments_method_check;"
                ))
                method_list = ", ".join(f"'{m}'" for m in _METHOD_VALUES)
                conn.execute(text(
                    "ALTER TABLE divine_payments ADD CONSTRAINT divine_payments_method_check "
                    f"CHECK (method IN ({method_list}));"
                ))
            else:
                print(f"dialect={dialect}: skipping indexes/CHECK constraints (not supported the same way).")

        print("Done.")
    except Exception as exc:
        print(f"Migration failed, no partial changes were committed (transaction rolled back): {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
