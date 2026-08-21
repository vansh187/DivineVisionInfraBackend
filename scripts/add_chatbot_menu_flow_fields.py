"""
One-time migration: adds the menu-driven sales-flow fields (greeting -> main
menu -> About/Sales/Support/Browsing branches -> qualification -> lead
capture) to the chatbot tables.

Safe to run more than once on Postgres.

Usage:
    python scripts/add_chatbot_menu_flow_fields.py
"""
from _bootstrap import setup
setup()

from Divinepersistence.persistence_db import engine
from sqlalchemy import text


def main():
    with engine.begin() as conn:
        print("Adding chatbot menu-flow columns (if not already present)...")

        conn.execute(text(
            "ALTER TABLE divine_chatbot_sessions ADD COLUMN IF NOT EXISTS menu_state varchar(80);"
        ))
        conn.execute(text(
            "ALTER TABLE divine_chatbot_sessions ADD COLUMN IF NOT EXISTS menu_payload text;"
        ))

        conn.execute(text(
            "ALTER TABLE divine_chatbot_leads ADD COLUMN IF NOT EXISTS notify_updates_opt_in boolean;"
        ))

        qualification_columns = {
            "buyer_type": "varchar(20) CHECK (buyer_type IS NULL OR buyer_type IN ('end_client','investor_dealer'))",
            "working_profile_type": "varchar(80)",
            "location_preference": "varchar(200)",
            "opportunity_type": "varchar(80)",
            "investment_size_band": "varchar(40)",
            "investment_goal": "varchar(80)",
            "proceed_preference": "varchar(40)",
            "source_flow": "varchar(20) CHECK (source_flow IS NULL OR source_flow IN ('llm_signals','menu_sales','menu_browsing'))",
        }
        for column, definition in qualification_columns.items():
            conn.execute(text(
                f"ALTER TABLE divine_chatbot_qualification ADD COLUMN IF NOT EXISTS {column} {definition};"
            ))

        conn.execute(text(
            "ALTER TABLE divine_chatbot_callback_requests ADD COLUMN IF NOT EXISTS notes text;"
        ))
        conn.execute(text(
            "ALTER TABLE divine_chatbot_callback_requests ADD COLUMN IF NOT EXISTS request_type varchar(20) "
            "NOT NULL DEFAULT 'callback' CHECK (request_type IN ('callback','support'));"
        ))
    print("Done.")


if __name__ == "__main__":
    main()
