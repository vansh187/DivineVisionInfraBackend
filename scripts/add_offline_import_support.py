"""
One-time migration for the prod data load (client inventory sheet + offline
buyers' accounts). Additive and idempotent - safe to run more than once, and
the app also applies the column part on startup
(PersistenceDB.apply_lightweight_migrations).

  divine_customer_users    + source, must_change_password,
                             temp_password_expires_at, invite_status, invite_sent_at
  divine_project_inventory + sale_source, status_note, status_updated_at
  divine_inventory_status_history  (new table: audit trail / rollback)

Usage:
    python scripts/add_offline_import_support.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import Base, engine, ensure_offline_import_columns
from Divinepersistence.persistence_inventory import InventoryStatusHistoryModel


def main():
    try:
        with engine.begin() as conn:
            added = ensure_offline_import_columns(conn)
        Base.metadata.create_all(bind=engine, tables=[InventoryStatusHistoryModel.__table__])
    except Exception as exc:
        print(f"Migration failed (the column step is one transaction and was rolled back): {exc}")
        raise SystemExit(1)
    print("Added columns:", ", ".join(added) if added else "none (already present)")
    print("Ensured table: divine_inventory_status_history")
    print("Done.")


if __name__ == "__main__":
    main()
