"""
One-time migration: adds the email_verified column to the live
divine_admin_users table, backing the signup-OTP verification flow.

Existing admin rows (created before this migration) are backfilled to
verified=true so already-active admins are not locked out.

Safe to run more than once - ADD COLUMN IF NOT EXISTS makes it idempotent.

Usage:
    python scripts/add_admin_email_verified_column.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import engine
from sqlalchemy import text


def main():
    with engine.begin() as conn:
        print("Adding email_verified column to divine_admin_users (if not already present)...")
        conn.execute(text(
            "ALTER TABLE divine_admin_users ADD COLUMN IF NOT EXISTS email_verified boolean NOT NULL DEFAULT false;"
        ))
        print("Backfilling existing admin rows as verified...")
        conn.execute(text(
            "UPDATE divine_admin_users SET email_verified = true WHERE email_verified = false;"
        ))
        print("Widening password-reset OTP role column for admin signup OTPs...")
        conn.execute(text(
            "ALTER TABLE divine_password_reset_otp ALTER COLUMN role TYPE varchar(32);"
        ))
    print("Done.")


if __name__ == "__main__":
    main()
