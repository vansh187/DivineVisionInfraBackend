"""
One-time migration: creates divine_booking_document_uploads, the audit +
Idempotency-Key + rate-limit table behind the admin KYC document upload
endpoint (POST /admin/bookings/{id}/documents/{type}) - see
Divinepersistence/persistence_booking.py::BookingDocumentUploadModel.

Brand new table, so like add_booking_kyc_review_tables.py this just runs
SQLAlchemy's idempotent create_all() targeted at that one model.

Usage:
    python scripts/add_booking_document_uploads_table.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import Base, engine
from Divinepersistence.persistence_booking import BookingDocumentUploadModel


def main():
    print("Creating divine_booking_document_uploads (if not already present)...")
    Base.metadata.create_all(bind=engine, tables=[BookingDocumentUploadModel.__table__])
    print("Done.")


if __name__ == "__main__":
    main()
