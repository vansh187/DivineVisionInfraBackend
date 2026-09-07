"""
Backfill divine_payment_milestones for bookings made before the instalment
feature existed. Idempotent - serviceMilestones.ensure_for_customer() only
creates rows when a customer has none, and the insert is ON CONFLICT DO NOTHING.

Run once after add_installment_payments.py:
    python scripts/backfill_payment_milestones.py
"""
from _bootstrap import setup
setup()

from sqlalchemy import text
from Divinepersistence.persistence_db import engine
from DivineService.service_milestones import serviceMilestones


def main():
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT DISTINCT owner_id FROM divine_documents "
            "WHERE owner_role = 'customer' AND document_type = 'booking_application';"
        )).mappings().all()
    customer_ids = [r["owner_id"] for r in rows if r.get("owner_id")]
    print(f"Found {len(customer_ids)} customer(s) with a booking application.")

    svc = serviceMilestones()
    created = skipped = failed = 0
    for cid in customer_ids:
        try:
            milestones = svc.ensure_for_customer(cid)
            if milestones:
                created += 1
                print(f"  {cid}: {len(milestones)} milestone(s)")
            else:
                skipped += 1
                print(f"  {cid}: no plan derivable - skipped")
        except Exception as e:  # pragma: no cover - defensive
            failed += 1
            print(f"  {cid}: FAILED - {e}")

    print(f"Done. with_milestones={created} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()
