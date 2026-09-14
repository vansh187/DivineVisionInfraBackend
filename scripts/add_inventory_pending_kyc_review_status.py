"""
One-time migration: widens the divine_project_inventory status CHECK to
include 'pending_kyc_review' - a plot-booking payment now HOLDS the unit in
this state instead of booking it outright; an admin's KYC-review Approve
flips it to 'booked', Reject/Cancel releases it back to 'available' (see
DivineService/service_payment.py::_apply_booking_to_inventory and
DivineService/service_booking_kyc.py).

All prior values (available, held, booked, sold, reserved) are kept - this
only widens the set, following the same pattern as
scripts/add_inventory_booked_status.py.

create_all() only creates missing tables, it never alters an existing
constraint, so this has to run separately against the live Postgres. Safe to
run more than once - DROP CONSTRAINT IF EXISTS makes it idempotent. No-op on
SQLite (no CHECK constraint support the same way).

Usage:
    python scripts/add_inventory_pending_kyc_review_status.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import engine
from sqlalchemy import text

_STATUS_VALUES = ("available", "held", "booked", "sold", "reserved", "pending_kyc_review")


def main():
    if engine.dialect.name != "postgresql":
        print(f"dialect={engine.dialect.name}: nothing to do (no CHECK constraint on this dialect).")
        return
    try:
        with engine.begin() as conn:
            print("Widening the divine_project_inventory status CHECK to include 'pending_kyc_review'...")
            conn.execute(text(
                "ALTER TABLE divine_project_inventory DROP CONSTRAINT IF EXISTS divine_project_inventory_status_check;"
            ))
            status_list = ", ".join(f"'{s}'" for s in _STATUS_VALUES)
            conn.execute(text(
                "ALTER TABLE divine_project_inventory ADD CONSTRAINT divine_project_inventory_status_check "
                f"CHECK (status IN ({status_list}));"
            ))
        print("Done.")
    except Exception as exc:
        print(f"Migration failed, no partial changes were committed (transaction rolled back): {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
