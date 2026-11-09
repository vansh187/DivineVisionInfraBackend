"""
Step C of the prod data load: email imported buyers their login.

  - new accounts: a unique temporary password (valid 7 days, must be changed
    at first login). The hash is stored only after the email was accepted; the
    password itself is never printed, logged or saved.
  - buyers who already had a website account: a "plot added to your account"
    email (password unchanged).
  - accounts without an email are listed so the sales team can collect one;
    after adding it (email + username) rerun this script.

Writes a CSV (no passwords) of who was emailed to migration_reports/.

Usage:
    python scripts/send_offline_buyer_invites.py [--batch buyers-2026...]            # dry run
    python scripts/send_offline_buyer_invites.py --customer C12345 --apply            # test one first
    python scripts/send_offline_buyer_invites.py [--batch ...] --apply [--delay 2]
    python scripts/send_offline_buyer_invites.py --customer C12345 --resend --apply   # new password
"""
import argparse
import sys

from _bootstrap import setup
setup()

from sqlalchemy import text

from _migration_io import finish, save_csv
from DivineService.service_offline_import import OfflineBuyerInvites


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch", default=None, help="only accounts created by this import batch")
    parser.add_argument("--customer", action="append", default=[], help="only this customer id (repeatable)")
    parser.add_argument("--resend", action="store_true", help="with --customer: send a fresh temporary password")
    parser.add_argument("--delay", type=float, default=2.0, help="seconds between emails")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.resend and not args.customer:
        parser.error("--resend needs --customer")

    invites = OfflineBuyerInvites()
    if args.apply and not getattr(invites._email, "enabled", False):
        print("Email is not configured (RESEND_API_KEY / DIVINE_RESEND_EMAIL) - nothing sent.")
        return 2
    if args.resend and args.apply:
        from Divinepersistence.persistence_db import engine
        with engine.begin() as conn:
            for cid in args.customer:
                conn.execute(text("UPDATE divine_customer_users SET invite_status = 'pending' "
                                  "WHERE id = :id AND source = 'offline_import'"), {"id": cid})
    report = invites.run(apply=args.apply, customer_ids=args.customer or None, batch_id=args.batch,
                         delay_seconds=args.delay)
    rows = report.extra.pop("csv_rows", [])
    path = save_csv(f"invites_{'sent' if args.apply else 'dryrun'}_{report.batch_id}.csv",
                    ["customer_id", "name", "email", "plots", "status"], rows)
    print("recipient list saved (no passwords):", path)
    return finish(report, "invites", args.apply)


if __name__ == "__main__":
    sys.exit(main())
