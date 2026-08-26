"""
One-off/cron script: load a plot/unit inventory CSV into divine_project_inventory.

Expected CSV columns (header row required): Plot Number, Width (Mtr.), Length (Mtr.),
Area in Sq Mtrs, Area in Sq Yard - matching the sheets the sales team exports per block.
Width/Length may be blank for a handful of rows; Area columns must always be present.

Usage:
  python scripts/ingest_inventory.py --project "Suraksha Enclave" --city Sonipat \
      --locality "Sector-15, Ganaur" --block-default C path/to/C-block_inventory.csv

Re-running against a corrected CSV is safe: rows are upserted on (project_name, unit_number),
so an existing plot's dimensions get updated in place rather than duplicated.
"""
import sys
import csv
import re
import uuid
import argparse
from decimal import Decimal, InvalidOperation

from _bootstrap import setup
setup()

from Divinepersistence import persistenceInventory

UNIT_NUMBER_PREFIX_RE = re.compile(r"^([A-Za-z]+)")


def _parse_decimal(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _derive_block(unit_number: str, block_default: str) -> str:
    if block_default:
        return block_default
    match = UNIT_NUMBER_PREFIX_RE.match(unit_number)
    return match.group(1) if match else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source_file")
    parser.add_argument("--project", required=True, help="Project/township name, e.g. 'Suraksha Enclave'")
    parser.add_argument("--city", required=True)
    parser.add_argument("--locality", default=None)
    parser.add_argument("--unit-type", default="plot", choices=["plot", "floor", "flat", "commercial"])
    parser.add_argument("--block-default", default=None, help="Force this block for every row instead of deriving it from the unit number's letter prefix")
    parser.add_argument("--status", default="available", choices=["available", "held", "sold"])
    args = parser.parse_args()

    persistence = persistenceInventory()
    ingested, skipped = 0, 0

    with open(args.source_file, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            unit_number = (row.get("Plot Number") or "").strip()
            area_sqmt = _parse_decimal(row.get("Area in Sq Mtrs"))
            area_sqyd = _parse_decimal(row.get("Area in Sq Yard"))
            if not unit_number or area_sqmt is None or area_sqyd is None:
                skipped += 1
                continue

            persistence.upsert_unit(
                id=str(uuid.uuid4()),
                project_name=args.project,
                city=args.city,
                locality=args.locality,
                block=_derive_block(unit_number, args.block_default),
                unit_number=unit_number,
                unit_type=args.unit_type,
                width_mtr=_parse_decimal(row.get("Width (Mtr.)")),
                length_mtr=_parse_decimal(row.get("Length (Mtr.)")),
                area_sqmt=area_sqmt,
                area_sqyd=area_sqyd,
                status=args.status,
            )
            ingested += 1
            print(f"  {unit_number}: {area_sqyd} sq yd")

    print(f"Done. Ingested {ingested} unit(s), skipped {skipped} row(s) without a plot number/area.")
    if ingested == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
