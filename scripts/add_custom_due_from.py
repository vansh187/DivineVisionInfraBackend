"""
Payment plans: custom "Counted from" events + column widths (additive, idempotent;
the app also applies this on startup).

  divine_payment_plan_instalments + due_from varchar(100)
  divine_payment_milestones       + due_from varchar(100)
  divine_payment_milestones.charge_head  varchar(10)  -> varchar(20)   ('PLC+IFMS+CAM' is 12 chars)
  divine_payment_milestones.label        varchar(120) -> varchar(150)  (plan labels allow 150)

Widening a varchar in Postgres only changes metadata - no table rewrite, no data change.

Usage:
    python scripts/add_custom_due_from.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import engine, ensure_offline_import_columns


def main():
    with engine.begin() as conn:
        changed = ensure_offline_import_columns(conn)
    print("Changed:", ", ".join(changed) if changed else "nothing (already up to date)")
    print("Done.")


if __name__ == "__main__":
    main()
