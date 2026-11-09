"""
Step A of the prod data load: make divine_project_inventory match the client's
inventory sheet (.xlsx or .csv; columns Plot No., Width(mtr.), Length(mtr.),
Area in sq mtrs, Area in sq yard, Status, Project).

  Sold -> sold (sale_source=offline) | Unsold -> available | Hold -> held
  Plots with app activity (booked / pending_kyc_review / reserved) are never
  changed - reported as CONFLICT. Project plots not in the sheet -> held.

Dry run by default. --apply saves a JSON backup of the project's rows, writes
everything in one transaction and records each change in
divine_inventory_status_history under the printed batch id (--rollback undoes it).

Usage:
    python scripts/sync_inventory_from_sheet.py "Web Site Data.xlsx" \
        --project "Suraksha Enclave" --city Sonipat --locality "Sector-15, Ganaur" \
        [--override B1-B=sold --override C14=sold] [--apply]
    python scripts/sync_inventory_from_sheet.py --rollback inv-20261106...
"""
import argparse
import sys

from _bootstrap import setup
setup()

from sqlalchemy import text

from _migration_io import finish, save_json
from DivineService.service_offline_import import InventorySheetSync, read_sheet_rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sheet", nargs="?")
    parser.add_argument("--project")
    parser.add_argument("--city")
    parser.add_argument("--locality", default=None)
    parser.add_argument("--override", action="append", default=[],
                        help="PLOT=STATUS (sold/available/held) - overrides the sheet for one plot")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", metavar="BATCH_ID")
    args = parser.parse_args()

    sync = InventorySheetSync()
    if args.rollback:
        return finish(sync.rollback(args.rollback), "inventory_rollback", True)
    if not (args.sheet and args.project and args.city):
        parser.error("sheet, --project and --city are required")
    overrides = {}
    for item in args.override:
        plot, _, status = item.partition("=")
        overrides[plot.strip()] = status.strip().lower()

    if args.apply:
        from Divinepersistence.persistence_db import engine
        with engine.connect() as conn:
            rows = [dict(r) for r in conn.execute(text(
                "SELECT * FROM divine_project_inventory WHERE project_name = :p"), {"p": args.project}).mappings()]
        print("backup saved:", save_json(f"inventory_backup_{args.project}.json", rows))

    report = sync.run(read_sheet_rows(args.sheet), args.project, args.city, args.locality,
                      overrides=overrides, apply=args.apply, source=f"client_sheet:{args.sheet}")
    return finish(report, "inventory", args.apply)


if __name__ == "__main__":
    sys.exit(main())
