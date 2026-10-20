"""
One-time migration: adds the super-admin approval columns to the live
divine_admin_users table, backing the signup -> email OTP -> super admin
approval -> login flow.

Existing admin rows (created before this migration) are backfilled to
approval_status='approved' so already-active admins are not locked out; the
column default is then switched to 'pending' for every new signup.

Safe to run more than once - ADD COLUMN IF NOT EXISTS makes it idempotent.

Usage:
    python scripts/add_admin_approval_columns.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import engine
from sqlalchemy import text


def main():
    with engine.begin() as conn:
        print("Adding approval_status column to divine_admin_users (existing rows -> approved)...")
        conn.execute(text(
            "ALTER TABLE divine_admin_users ADD COLUMN IF NOT EXISTS approval_status varchar(20) NOT NULL DEFAULT 'approved';"
        ))
        conn.execute(text(
            "ALTER TABLE divine_admin_users ALTER COLUMN approval_status SET DEFAULT 'pending';"
        ))
        print("Adding approval audit columns...")
        conn.execute(text(
            "ALTER TABLE divine_admin_users ADD COLUMN IF NOT EXISTS approval_decided_by varchar(255);"
        ))
        conn.execute(text(
            "ALTER TABLE divine_admin_users ADD COLUMN IF NOT EXISTS approval_decided_date timestamp;"
        ))
        conn.execute(text(
            "ALTER TABLE divine_admin_users ADD COLUMN IF NOT EXISTS approval_note varchar(500);"
        ))
    print("Done.")


if __name__ == "__main__":
    main()
