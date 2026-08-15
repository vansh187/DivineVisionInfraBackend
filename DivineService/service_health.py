from Divinepersistence import PersistenceDB


class serviceHealth:
    def __init__(self, db: PersistenceDB = None):
        self._db = db or PersistenceDB()

    def check_connection(self) -> bool:
        return self._db.test_connection()

    def init_db(self) -> None:
        self._db.create_tables()
