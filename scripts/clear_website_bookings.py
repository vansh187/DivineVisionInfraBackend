"""
Step 0 of the prod data load: delete the plot-booking data created through the
website/app before go-live (test bookings), so only the client's real data
remains. See DivineService/service_offline_import.WebsiteBookingCleanup for
exactly what is removed (customer accounts and KYC uploads are kept).

A JSON backup of every deleted/changed row is written to migration_reports/.

Usage:
    python scripts/clear_website_bookings.py                    # dry run: lists what would go
    python scripts/clear_website_bookings.py --apply --confirm DELETE-WEBSITE-BOOKINGS
"""
import argparse
import sys

from _bootstrap import setup
setup()

from _migration_io import finish, save_json
from DivineService.service_offline_import import CLEANUP_CONFIRM, WebsiteBookingCleanup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if args.apply and args.confirm != CLEANUP_CONFIRM:
        print(f"Refusing to delete: pass --confirm {CLEANUP_CONFIRM} together with --apply.")
        return 2
    report = WebsiteBookingCleanup().run(apply=args.apply)
    backup = report.extra.pop("backup", None)
    if backup is not None:
        print("backup saved:", save_json(f"cleanup_backup_{report.batch_id}.json", backup))
    return finish(report, "cleanup", args.apply)


if __name__ == "__main__":
    sys.exit(main())
