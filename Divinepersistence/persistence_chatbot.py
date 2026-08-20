from sqlalchemy import text
from datetime import datetime, timezone
from .persistence_db import SessionLocal, engine, RowWrapper, load_queries


def embedding_to_vector_literal(embedding) -> str:
    # pgvector accepts a text literal like "[0.1,0.2,...]" cast with ::vector - this
    # avoids depending on the separate pgvector-python package just to bind one type.
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


class persistenceChatbot:
    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory
        self._queries = load_queries("chatbot_queries.yaml")
        self._engine = engine

    def _q(self, name: str) -> str:
        query = self._queries.get(name)
        if not query:
            raise RuntimeError(f"missing_query:{name}")
        return query

    # ---- Leads --------------------------------------------------------
    def create_lead(self, id: str, channel: str = "web", visitor_name: str = None,
                     visitor_phone: str = None, lead_temperature: str = "cold",
                     consent_given: bool = False, consent_at=None):
        with self._session_factory() as db:
            try:
                now = datetime.now(timezone.utc)
                params = {
                    "id": id,
                    "channel": channel,
                    "visitor_name": visitor_name,
                    "visitor_phone": visitor_phone,
                    "lead_temperature": lead_temperature,
                    "consent_given": consent_given,
                    "consent_at": consent_at,
                    "created_date": now,
                    "last_updated_date": now,
                }
                result = db.execute(text(self._q("create_lead")), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    def get_lead_by_id(self, id: str):
        with self._session_factory() as db:
            result = db.execute(text(self._q("get_lead_by_id")), {"id": id})
            row = result.mappings().first()
            return RowWrapper(row) if row else None

    def update_lead_fields(self, id: str, visitor_name: str = None, visitor_phone: str = None,
                            lead_temperature: str = None):
        with self._session_factory() as db:
            try:
                params = {
                    "id": id,
                    "visitor_name": visitor_name,
                    "visitor_phone": visitor_phone,
                    "lead_temperature": lead_temperature,
                    "last_updated_date": datetime.now(timezone.utc),
                }
                result = db.execute(text(self._q("update_lead_fields")), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row) if row else None
            except Exception:
                db.rollback()
                raise

    def update_lead_email(self, id: str, visitor_email: str):
        with self._session_factory() as db:
            try:
                params = {
                    "id": id,
                    "visitor_email": visitor_email,
                    "last_updated_date": datetime.now(timezone.utc),
                }
                result = db.execute(text(self._q("update_lead_email")), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row) if row else None
            except Exception:
                db.rollback()
                raise

    def create_lead_source(self, id: str, lead_id: str, ip_address: str = None, ip_geo_city: str = None,
                            ip_geo_region: str = None, precise_lat: float = None, precise_long: float = None,
                            maps_link: str = None, referrer: str = None, utm_source: str = None,
                            utm_medium: str = None, utm_campaign: str = None, device_type: str = None):
        with self._session_factory() as db:
            try:
                params = {
                    "id": id, "lead_id": lead_id, "ip_address": ip_address,
                    "ip_geo_city": ip_geo_city, "ip_geo_region": ip_geo_region,
                    "precise_lat": precise_lat, "precise_long": precise_long,
                    "maps_link": maps_link, "referrer": referrer,
                    "utm_source": utm_source, "utm_medium": utm_medium, "utm_campaign": utm_campaign,
                    "device_type": device_type, "captured_at": datetime.now(timezone.utc),
                }
                result = db.execute(text(self._q("create_lead_source")), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    def update_lead_source_location(self, lead_id: str, precise_lat: float, precise_long: float, maps_link: str):
        with self._session_factory() as db:
            try:
                params = {"lead_id": lead_id, "precise_lat": precise_lat, "precise_long": precise_long, "maps_link": maps_link}
                result = db.execute(text(self._q("update_lead_source_location")), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row) if row else None
            except Exception:
                db.rollback()
                raise

    # ---- Sessions -------------------------------------------------------
    def create_session(self, id: str, lead_id: str):
        with self._session_factory() as db:
            try:
                now = datetime.now(timezone.utc)
                params = {"id": id, "lead_id": lead_id, "created_date": now, "last_activity_date": now}
                result = db.execute(text(self._q("create_session")), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    def get_session_by_id(self, id: str):
        with self._session_factory() as db:
            result = db.execute(text(self._q("get_session_by_id")), {"id": id})
            row = result.mappings().first()
            return RowWrapper(row) if row else None

    def touch_session(self, id: str):
        with self._session_factory() as db:
            try:
                result = db.execute(text(self._q("touch_session")), {"id": id, "last_activity_date": datetime.now(timezone.utc)})
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row) if row else None
            except Exception:
                db.rollback()
                raise

    def update_session_callback_state(self, id: str, callback_state, callback_name: str = None,
                                       callback_phone: str = None, callback_time: str = None):
        with self._session_factory() as db:
            try:
                params = {
                    "id": id, "callback_state": callback_state,
                    "callback_name": callback_name, "callback_phone": callback_phone,
                    "callback_time": callback_time, "last_activity_date": datetime.now(timezone.utc),
                }
                result = db.execute(text(self._q("update_session_callback_state")), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row) if row else None
            except Exception:
                db.rollback()
                raise

    # ---- Messages ---------------------------------------------------------
    def create_message(self, id: str, session_id: str, role: str, content: str, tool_name: str = None,
                        llm_provider: str = None, guardrail_score: float = None,
                        guardrail_passed: bool = None, latency_ms: int = None):
        with self._session_factory() as db:
            try:
                params = {
                    "id": id, "session_id": session_id, "role": role, "content": content,
                    "tool_name": tool_name, "llm_provider": llm_provider,
                    "guardrail_score": guardrail_score, "guardrail_passed": guardrail_passed,
                    "latency_ms": latency_ms, "created_date": datetime.now(timezone.utc),
                }
                result = db.execute(text(self._q("create_message")), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    def list_recent_messages(self, session_id: str, limit: int = 20):
        with self._session_factory() as db:
            result = db.execute(text(self._q("list_recent_messages")), {"session_id": session_id, "limit": limit})
            return [RowWrapper(row) for row in result.mappings().all()]

    # ---- Qualification ------------------------------------------------
    def upsert_qualification(self, id: str, lead_id: str, budget_min: float = None, budget_max: float = None,
                              unit_type: str = None, timeline_days: int = None, intent_signal: str = None,
                              temperature: str = None):
        with self._session_factory() as db:
            try:
                params = {
                    "id": id, "lead_id": lead_id, "budget_min": budget_min, "budget_max": budget_max,
                    "unit_type": unit_type, "timeline_days": timeline_days, "intent_signal": intent_signal,
                    "temperature": temperature, "updated_date": datetime.now(timezone.utc),
                }
                result = db.execute(text(self._q("upsert_qualification")), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    # ---- Callback requests -----------------------------------------------
    def create_callback_request(self, id: str, lead_id: str, visitor_name: str, phone: str, preferred_time: str):
        with self._session_factory() as db:
            try:
                params = {
                    "id": id, "lead_id": lead_id, "visitor_name": visitor_name,
                    "phone": phone, "preferred_time": preferred_time,
                    "requested_at": datetime.now(timezone.utc),
                }
                result = db.execute(text(self._q("create_callback_request")), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    def list_callback_requests(self, limit: int = 100):
        with self._session_factory() as db:
            result = db.execute(text(self._q("list_callback_requests")), {"limit": limit})
            return [RowWrapper(row) for row in result.mappings().all()]

    # ---- Knowledge base / pgvector -----------------------------------
    def search_kb_chunks(self, embedding, top_k: int = 5):
        with self._session_factory() as db:
            params = {"embedding": embedding_to_vector_literal(embedding), "top_k": top_k}
            result = db.execute(text(self._q("search_kb_chunks")), params)
            return [RowWrapper(row) for row in result.mappings().all()]

    def create_kb_document(self, id: str, title: str, category: str, source_uri: str = None):
        with self._session_factory() as db:
            try:
                now = datetime.now(timezone.utc)
                params = {"id": id, "title": title, "category": category, "source_uri": source_uri,
                          "created_date": now, "last_updated_date": now}
                result = db.execute(text(self._q("create_kb_document")), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    def create_kb_chunk(self, id: str, document_id: str, chunk_index: int, content: str, embedding):
        with self._session_factory() as db:
            try:
                params = {
                    "id": id, "document_id": document_id, "chunk_index": chunk_index,
                    "content": content, "embedding": embedding_to_vector_literal(embedding),
                    "created_date": datetime.now(timezone.utc),
                }
                result = db.execute(text(self._q("create_kb_chunk")), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise
