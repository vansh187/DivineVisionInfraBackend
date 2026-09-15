"""
One-time migration: adds admin profile photo columns to the live
divine_admin_users table.

Safe to run more than once - ADD COLUMN IF NOT EXISTS makes it idempotent.

Usage:
    python scripts/add_admin_profile_photo_columns.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import engine
from sqlalchemy import text


def main():
    with engine.begin() as conn:
        print("Adding profile_photo_url column to divine_admin_users (if not already present)...")
        conn.execute(text(
            "ALTER TABLE divine_admin_users ADD COLUMN IF NOT EXISTS profile_photo_url varchar(1000);"
        ))
        print("Adding profile_photo_path column to divine_admin_users (if not already present)...")
        conn.execute(text(
            "ALTER TABLE divine_admin_users ADD COLUMN IF NOT EXISTS profile_photo_path varchar(500);"
        ))
        print("Adding profile_photo_bucket column to divine_admin_users (if not already present)...")
        conn.execute(text(
            "ALTER TABLE divine_admin_users ADD COLUMN IF NOT EXISTS profile_photo_bucket varchar(100);"
        ))
    print("Done.")


if __name__ == "__main__":
    main()
