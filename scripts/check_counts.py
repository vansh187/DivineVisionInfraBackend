from Divinepersistence.persistence_db import engine
from sqlalchemy import text

names=["\"DIVINE_CUSTOMER_USERS\"","divine_customer_users","\"DIVINE_BROKER_USERS\"","divine_broker_users"]
with engine.connect() as conn:
    for name in names:
        try:
            r = conn.execute(text(f'SELECT count(*) FROM {name}'))
            print(name, r.scalar())
        except Exception as e:
            print(name, 'ERROR', e)
