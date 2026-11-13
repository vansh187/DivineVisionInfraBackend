"""
One-time migration: creates divine_admin_import_requests, the Idempotency-Key
store behind the admin CSV import (POST /admin/imports/{entity}) - see
Divinepersistence/persistence_admin_imports.py::AdminImportRequestModel.

The app's startup create_all() also creates it; this script is for applying
it ahead of a deploy. Brand new table, so it just runs SQLAlchemy's idempotent
create_all() targeted at that one model. Safe to run more than once.

Usage:
    python scripts/add_admin_import_requests_table.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import Base, engine
from Divinepersistence.persistence_admin_imports import AdminImportRequestModel


def main():
    print("Creating divine_admin_import_requests (if not already present)...")
    Base.metadata.create_all(bind=engine, tables=[AdminImportRequestModel.__table__])
    print("Done.")


if __name__ == "__main__":
    main()
