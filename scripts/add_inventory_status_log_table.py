"""
One-time migration: creates divine_inventory_status_log, the permanent history
of every plot status change (previous status, new status, who, when) shown in
the admin panel's Inventory section - see
Divinepersistence/persistence_inventory.py::InventoryStatusLogModel.

The app's startup create_all() also creates it; this script is for applying
it ahead of a deploy. Brand new table, so it just runs SQLAlchemy's idempotent
create_all() targeted at that one model. Safe to run more than once.

Plots changed before this table existed have no history rows; their last
change time still shows from divine_project_inventory.status_updated_at.

Usage:
    python scripts/add_inventory_status_log_table.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import Base, engine
from Divinepersistence.persistence_inventory import InventoryStatusLogModel


def main():
    print("Creating divine_inventory_status_log (if not already present)...")
    Base.metadata.create_all(bind=engine, tables=[InventoryStatusLogModel.__table__])
    print("Done.")


if __name__ == "__main__":
    main()
