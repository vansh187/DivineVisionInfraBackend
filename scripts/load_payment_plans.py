"""
Load / refresh the payment-plan catalogue from data/payment_plans.json.

  - a new (code, version) is created with its instalments;
  - an existing (code, version) only gets its display fields refreshed
    (name, description, sort order, active flag) - instalments of a version are
    never rewritten, because bookings already reference them. To change the
    instalments, add the plan again with "version": <n+1>; the older version is
    deactivated and existing bookings keep their own snapshot.

Usage:
    python scripts/load_payment_plans.py            # validate + load
    python scripts/load_payment_plans.py --check    # validate the file only
"""
import argparse
import sys

from _bootstrap import setup
setup()

from DivineService.service_payment_plans import load_plan_file, sync_plans


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", default=None)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    plans = load_plan_file(args.file)
    print(f"{len(plans)} plan(s) valid: " + ", ".join(f"{p['short_code']} v{p.get('version', 1)}" for p in plans))
    if args.check:
        return 0
    print("loaded:", sync_plans(path=args.file))
    return 0


if __name__ == "__main__":
    sys.exit(main())
