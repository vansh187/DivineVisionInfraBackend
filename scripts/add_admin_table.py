"""
One-time setup: creates the divine_admin_users table (admin panel signup/login)
on the live Postgres. This is a brand-new table, not an alteration of an
existing one, so Base.metadata.create_all is enough - no ALTER TABLE needed.
The same create_all() also runs automatically on every app startup (see
serviceHealth.init_db()), so this script just applies it once immediately
instead of waiting for the next deploy.

Safe to run more than once - create_all() only creates missing tables.

Usage:
    python scripts/add_admin_table.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import PersistenceDB
import Divinepersistence.persistence_admin  # noqa: F401 - registers AdminModel on Base.metadata


def main():
    print("Creating divine_admin_users (if not already present)...")
    PersistenceDB().create_tables()
    print("Done.")


if __name__ == "__main__":
    main()
