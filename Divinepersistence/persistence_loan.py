import json
from sqlalchemy import text
from datetime import datetime, timezone
from .persistence_db import SessionLocal, engine, RowWrapper, load_queries


class persistenceLoan:
    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory
        self._queries = load_queries("loan_queries.yaml")
        self._engine = engine

    def _q(self, name: str) -> str:
        query = self._queries.get(name)
        if not query:
            raise RuntimeError(f"missing_query:{name}")
        return query

    def create_loan_report(self, id: str, snapshot: dict, session_id: str = None, lead_id: str = None):
        with self._session_factory() as db:
            try:
                params = {
                    "id": id, "session_id": session_id, "lead_id": lead_id,
                    "snapshot_json": json.dumps(snapshot),
                    "created_date": datetime.now(timezone.utc),
                }
                result = db.execute(text(self._q("create_loan_report")), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    def get_loan_report_by_id(self, id: str):
        with self._session_factory() as db:
            result = db.execute(text(self._q("get_loan_report_by_id")), {"id": id})
            row = result.mappings().first()
            return RowWrapper(row) if row else None
