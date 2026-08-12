from Divinepersistence.persistence_db import engine
from sqlalchemy import text
import traceback


def main():
    try:
        with engine.connect() as conn:
            r = conn.execute(text('SELECT count(*) FROM "DIVINE_CUSTOMER_USERS"'))
            print("COUNT", r.scalar())
    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    main()
