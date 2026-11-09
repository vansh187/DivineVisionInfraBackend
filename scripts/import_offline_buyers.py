"""
Step B of the prod data load: create accounts for the client's offline buyers
and attach their plots (customer sheet columns: Plot No., Applicant,
1 Co_Applicant, 2 Co_applicant, Contact No., Alternative Number, Email Id,
Co_applicant Email, Adders, Address_1, Plot_Address). Run AFTER step A.

  - one account per email (login = email + password); an existing website
    account with that email is reused (its password is not touched)
  - a buyer without email gets an account keyed by mobile (can't log in until
    an email is added)
  - one 'imported' plot record per plot with applicant / co-applicants /
    addresses; finance_status=pending (no payments, bookings or milestones ->
    no demand letter and no reminder emails until the price sheet is loaded)
  - NO email is sent here (step C does that)

Usage:
    python scripts/import_offline_buyers.py "WEB Developer.xlsx" --project "Suraksha Enclave" [--apply]
    python scripts/import_offline_buyers.py --rollback buyers-20261106...
"""
import argparse
import sys

from _bootstrap import setup
setup()

from _migration_io import finish
from DivineService.service_offline_import import OfflineBuyerImport, read_sheet_rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sheet", nargs="?")
    parser.add_argument("--project")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", metavar="BATCH_ID")
    args = parser.parse_args()

    importer = OfflineBuyerImport()
    if args.rollback:
        return finish(importer.rollback(args.rollback), "buyers_rollback", True)
    if not (args.sheet and args.project):
        parser.error("sheet and --project are required")
    report = importer.run(read_sheet_rows(args.sheet), args.project, apply=args.apply)
    code = finish(report, "buyers", args.apply)
    if args.apply and not report.errors:
        print(f"\nNext: python scripts/send_offline_buyer_invites.py --batch {report.batch_id}  (dry run first)")
    return code


if __name__ == "__main__":
    sys.exit(main())
