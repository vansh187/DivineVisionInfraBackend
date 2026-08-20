"""
One-time migration: adds visitor_email to chatbot leads and allows the
email-capture states on existing chatbot sessions.

Safe to run more than once on Postgres.

Usage:
    python scripts/add_chatbot_email_lead_fields.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import engine
from sqlalchemy import text


def main():
    with engine.begin() as conn:
        print("Adding visitor_email column to divine_chatbot_leads (if not already present)...")
        conn.execute(text(
            "ALTER TABLE divine_chatbot_leads ADD COLUMN IF NOT EXISTS visitor_email varchar(255);"
        ))
        print("Adding visitor_email index (if not already present)...")
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_chatbot_leads_email ON divine_chatbot_leads (visitor_email);"
        ))
        print("Updating chatbot session state constraint...")
        conn.execute(text(
            "ALTER TABLE divine_chatbot_sessions "
            "DROP CONSTRAINT IF EXISTS divine_chatbot_sessions_callback_state_check;"
        ))
        conn.execute(text(
            "ALTER TABLE divine_chatbot_sessions ADD CONSTRAINT divine_chatbot_sessions_callback_state_check "
            "CHECK (callback_state IN ('awaiting_name','awaiting_phone','awaiting_time','complete','awaiting_email','email_complete'));"
        ))
    print("Done.")


if __name__ == "__main__":
    main()
