"""
One-time migration: creates the two new tables the Booking KYC review workflow
needs - divine_bookings and divine_booking_decisions (see
Divinepersistence/persistence_booking.py).

Before this feature, a "booking" had no identity of its own - it was just an
InventoryUnitModel row with status='booked'. divine_bookings gives it one (a
friendly id like "BKG-2026-000001"), a KYC-review lifecycle status distinct
from the plot's own inventory status, and an optimistic-concurrency version
column. divine_booking_decisions is the append-only audit trail behind the
admin panel's "Decision History" timeline.

Brand new tables, so (unlike every other migration script in this folder)
this one just uses SQLAlchemy's own create_all() targeted at these two models
- there's no existing data/constraint to alter, so idempotent CREATE TABLE IF
NOT EXISTS is all that's needed.

Usage:
    python scripts/add_booking_kyc_review_tables.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import Base, engine
from Divinepersistence.persistence_booking import BookingModel, BookingDecisionModel


def main():
    print("Creating divine_bookings / divine_booking_decisions (if not already present)...")
    Base.metadata.create_all(bind=engine, tables=[BookingModel.__table__, BookingDecisionModel.__table__])
    print("Done.")


if __name__ == "__main__":
    main()
