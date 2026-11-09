"""
One-time migration for payment plans (additive, idempotent; the app also does
this on startup):

  divine_payment_plans, divine_payment_plan_instalments, divine_project_settings  (new tables)
  divine_payment_milestones + basic_amount, charge_head, charge_amount, due_trigger

then loads data/payment_plans.json into an empty catalogue.

Usage:
    python scripts/add_payment_plans.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import Base, engine, ensure_offline_import_columns
from Divinepersistence.persistence_payment_plan import (
    PaymentPlanInstalmentModel, PaymentPlanModel, ProjectSettingsModel,
)
from DivineService.service_payment_plans import seed_plans_if_empty


def main():
    Base.metadata.create_all(bind=engine, tables=[
        PaymentPlanModel.__table__, PaymentPlanInstalmentModel.__table__, ProjectSettingsModel.__table__,
    ])
    with engine.begin() as conn:
        added = ensure_offline_import_columns(conn)
    print("Added columns:", ", ".join(added) if added else "none (already present)")
    seed_plans_if_empty()
    print("Done.")


if __name__ == "__main__":
    main()
