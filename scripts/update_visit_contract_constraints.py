"""
One-time migration: aligns divine_site_visits' CHECK constraints with the
finalized customer/broker "site visit" API contract.

scripts/add_admin_site_visit_fields.py and scripts/add_site_visit_request_fields.py
have since been corrected to ship the right values (status now includes
'requested' from the start; preferred_window uses 'weekend', not the
originally-shipped 'this_weekend') - a *brand new* environment running only
those two no longer needs this script at all. This one stays for two reasons:
it's the actual patch that was applied to the already-live production
database (which had the earlier, wrong values), and it's still what an
environment that happened to run the old versions of those two scripts before
this fix needs to catch up. Safe to run redundantly on a fresh environment too
- DROP CONSTRAINT IF EXISTS + re-ADD the same values is a no-op in effect.

- status: adds 'requested' (a website self-service request awaiting a broker
  to confirm a slot - see POST /visits/request in DivineAPI/visit_api.py).
  All prior values (scheduled, confirmed, completed, follow_up, no_show,
  converted, cancelled) are kept - this only widens the set.
- preferred_window: corrects the CHECK to 'today' | 'tomorrow' | 'weekend'.
- project_name: adds a CHECK restricting it to the two known project slugs
  (matching the existing divine_broker_users.project convention - see
  scripts/add_broker_project_column.py) OR NULL, since older rows and any
  future non-project-specific visit predate/lack this field.

create_all() only creates missing tables, it never alters an existing
constraint, so this has to run separately against the live Postgres. Safe to
run more than once - DROP CONSTRAINT IF EXISTS makes it idempotent. No-op on
SQLite (no CHECK constraint support the same way).

Usage:
    python scripts/update_visit_contract_constraints.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import engine
from sqlalchemy import text

_STATUS_VALUES = (
    "requested", "scheduled", "confirmed", "completed", "follow_up", "no_show", "converted", "cancelled",
)
_PREFERRED_WINDOW_VALUES = ("today", "tomorrow", "weekend")
_PROJECT_VALUES = ("suraksha-enclave", "ops-divine-greens")


def main():
    if engine.dialect.name != "postgresql":
        print(f"dialect={engine.dialect.name}: nothing to do (no CHECK constraints on this dialect).")
        return

    try:
        with engine.begin() as conn:
            print("Widening the divine_site_visits status CHECK to include 'requested'...")
            conn.execute(text(
                "ALTER TABLE divine_site_visits DROP CONSTRAINT IF EXISTS divine_site_visits_status_check;"
            ))
            status_list = ", ".join(f"'{s}'" for s in _STATUS_VALUES)
            conn.execute(text(
                "ALTER TABLE divine_site_visits ADD CONSTRAINT divine_site_visits_status_check "
                f"CHECK (status IN ({status_list}));"
            ))

            print("Correcting the divine_site_visits preferred_window CHECK to 'weekend' (not 'this_weekend')...")
            conn.execute(text(
                "ALTER TABLE divine_site_visits DROP CONSTRAINT IF EXISTS divine_site_visits_preferred_window_check;"
            ))
            window_list = ", ".join(f"'{w}'" for w in _PREFERRED_WINDOW_VALUES)
            conn.execute(text(
                "ALTER TABLE divine_site_visits ADD CONSTRAINT divine_site_visits_preferred_window_check "
                f"CHECK (preferred_window IS NULL OR preferred_window IN ({window_list}));"
            ))

            print("Adding a project_name CHECK constraint...")
            conn.execute(text(
                "ALTER TABLE divine_site_visits DROP CONSTRAINT IF EXISTS divine_site_visits_project_name_check;"
            ))
            project_list = ", ".join(f"'{p}'" for p in _PROJECT_VALUES)
            conn.execute(text(
                "ALTER TABLE divine_site_visits ADD CONSTRAINT divine_site_visits_project_name_check "
                f"CHECK (project_name IS NULL OR project_name IN ({project_list}));"
            ))

        print("Done.")
    except Exception as exc:
        print(f"Migration failed, no partial changes were committed (transaction rolled back): {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
