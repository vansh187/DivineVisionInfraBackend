from Divinepersistence.persistence_db import engine
from sqlalchemy import text
import traceback


def main():
    stmts = [
        'ALTER TABLE "DIVINE_CUSTOMER_USERS" RENAME TO divine_customer_users; ',
        'ALTER TABLE "DIVINE_BROKER_USERS" RENAME TO divine_broker_users; '
    ]
    try:
        with engine.begin() as conn:
            for s in stmts:
                print('Executing:', s)
                conn.execute(text(s))
        print('Rename completed')
    except Exception:
        traceback.print_exc()


if __name__ == '__main__':
    main()
