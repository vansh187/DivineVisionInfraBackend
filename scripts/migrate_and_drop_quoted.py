from Divinepersistence.persistence_db import engine
from sqlalchemy import text
import traceback


def main():
    try:
        with engine.begin() as conn:
            # copy customer rows from quoted to lowercase if not exists
            print('Copying customer rows...')
            conn.execute(text(
                "INSERT INTO divine_customer_users (id, username, first_name, last_name, email, phone, password_hash, created_by, created_date, last_updated_by, last_updated_date) "
                "SELECT id, username, first_name, last_name, email, phone, password_hash, created_by, created_date, last_updated_by, last_updated_date FROM \"DIVINE_CUSTOMER_USERS\" dc "
                "WHERE NOT EXISTS (SELECT 1 FROM divine_customer_users d WHERE d.id = dc.id);"
            ))
            # drop quoted customer table
            print('Dropping quoted customer table if exists...')
            conn.execute(text('DROP TABLE IF EXISTS "DIVINE_CUSTOMER_USERS";'))

            # drop quoted broker table if empty
            print('Checking broker tables...')
            # copy broker rows similarly (if any)
            conn.execute(text(
                "INSERT INTO divine_broker_users (id, username, first_name, last_name, email, phone, password_hash, created_by, created_date, last_updated_by, last_updated_date) "
                "SELECT id, username, first_name, last_name, email, phone, password_hash, created_by, created_date, last_updated_by, last_updated_date FROM \"DIVINE_BROKER_USERS\" db "
                "WHERE NOT EXISTS (SELECT 1 FROM divine_broker_users b WHERE b.id = db.id);"
            ))
            print('Dropping quoted broker table if exists...')
            conn.execute(text('DROP TABLE IF EXISTS "DIVINE_BROKER_USERS";'))
        print('Migration and cleanup completed')
    except Exception:
        traceback.print_exc()


if __name__ == '__main__':
    main()
