"""
One-time migration: adds chatbot auth-flow fields used for customer/broker
signup and login conversations.

Safe to run more than once on Postgres.

Usage:
    python scripts/add_chatbot_auth_flow_fields.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import engine
from sqlalchemy import text


def main():
    with engine.begin() as conn:
        print("Adding chatbot auth flow columns (if not already present)...")
        conn.execute(text(
            "ALTER TABLE divine_chatbot_sessions ADD COLUMN IF NOT EXISTS auth_state varchar(80);"
        ))
        conn.execute(text(
            "ALTER TABLE divine_chatbot_sessions ADD COLUMN IF NOT EXISTS auth_payload text;"
        ))
    print("Done.")


if __name__ == "__main__":
    main()
