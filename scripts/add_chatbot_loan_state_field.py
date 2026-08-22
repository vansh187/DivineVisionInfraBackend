"""One-time migration: adds the loan-assistant structured-state column to the
chatbot sessions table, and the loan-report snapshot table.

Safe to run more than once on Postgres.

Usage:
    python scripts/add_chatbot_loan_state_field.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import engine
from sqlalchemy import text


def main():
    with engine.begin() as conn:
        print("Adding chatbot loan-state column (if not already present)...")

        conn.execute(text(
            "ALTER TABLE divine_chatbot_sessions ADD COLUMN IF NOT EXISTS loan_payload text;"
        ))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS divine_loan_reports (
              id varchar(36) PRIMARY KEY,
              session_id varchar(36) REFERENCES divine_chatbot_sessions(id),
              lead_id varchar(36) REFERENCES divine_chatbot_leads(id),
              snapshot_json text NOT NULL,
              created_date timestamptz DEFAULT now()
            );
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_loan_reports_session_id ON divine_loan_reports (session_id);"
        ))
    print("Done.")


if __name__ == "__main__":
    main()
