"""
Links payment plans to offline buyers' plots from the client's sheet
(columns: Plot No, Payment Plan, Client Name, Phone, Booking Date; optional
Sale Price / PLC / IFMS / CAM / Amount Received). Run AFTER the buyer import
(scripts/import_offline_buyers.py).

  - each row is matched to its plot by plot number within --project, then to
    the buyer linked to that plot and their record for it
  - the sheet's Client Name and Phone are checked against that record;
    a difference is a warning to look at, never a guess
  - Payment Plan may be a plan code, its short code ("PP1") or the client's
    label listing the percentages ("PL-10-15-25-25-15-10" = PP1)
  - Booking Date: DD-MM-YYYY (or YYYY-MM-DD, or an Excel date cell)
  - without prices the plan and booking date are linked and the plot shows
    "price needed" in the admin panel (no demand letter yet); with prices the
    instalment schedule is built and the demand letter is available
  - reminder emails stay off for these plots

Dry run unless --apply; nothing is saved if any row has an error. Running it
again replaces the terms (it can't change a plot whose instalment was already
paid in the app).

Usage:
    python scripts/link_offline_payment_plans.py "Payment plans.xlsx" --project "Suraksha Enclave" [--apply]
"""
import argparse
import sys

from _bootstrap import setup
setup()

from _migration_io import finish
from DivineService.service_offline_import import read_sheet_rows
from DivineService.service_plot_payment_terms import servicePlotPaymentTerms


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sheet")
    parser.add_argument("--project", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = servicePlotPaymentTerms().link_from_sheet(read_sheet_rows(args.sheet), args.project, apply=args.apply)
    return finish(report, "payment_plans", args.apply)


if __name__ == "__main__":
    sys.exit(main())
