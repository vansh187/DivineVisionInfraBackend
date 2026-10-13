"""
One-time migration: adds cheque_number and recorded_by_admin_id columns to the
already-live divine_payments table, for admin-side installment recording (see
DivineService/service_booking_kyc.py::record_installment_payment,
DivineService/service_payment.py::record_cash_payment) and the new "cheque"
payment method.

Purely ADDITIVE and nullable - every existing row/column is untouched.

Also widens the divine_payments method CHECK constraint to add 'cheque'
alongside the existing 'zoho'/'razorpay'/'cash'/'rtgs_neft' values, following
the same pattern scripts/add_zoho_payment_columns.py used for 'zoho'.

Safe to run more than once - ADD COLUMN IF NOT EXISTS / DROP+ADD CONSTRAINT
make it idempotent.

Usage:
    python scripts/add_installment_admin_payment_columns.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import engine
from sqlalchemy import text, inspect

_METHOD_VALUES = ("zoho", "razorpay", "cash", "rtgs_neft", "cheque")

_PAYMENTS_COLUMNS = (
    ("cheque_number", "varchar(50)", None),
    ("recorded_by_admin_id", "varchar(20)", None),
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

            print("Adding cheque_number/recorded_by_admin_id to divine_payments (if not already present)...")
            for column_name, column_type, default_sql in _PAYMENTS_COLUMNS:
                _add_column_if_missing(conn, "divine_payments", column_name, column_type, default_sql, dialect)

            if dialect == "postgresql":
                print("Widening the divine_payments method CHECK to include 'cheque'...")
                conn.execute(text(
                    "ALTER TABLE divine_payments DROP CONSTRAINT IF EXISTS divine_payments_method_check;"
                ))
                method_list = ", ".join(f"'{m}'" for m in _METHOD_VALUES)
                conn.execute(text(
                    "ALTER TABLE divine_payments ADD CONSTRAINT divine_payments_method_check "
                    f"CHECK (method IN ({method_list}));"
                ))
            else:
                print(f"dialect={dialect}: skipping CHECK constraint widening (not supported the same way).")

        print("Done.")
    except Exception as exc:
        print(f"Migration failed, no partial changes were committed (transaction rolled back): {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
