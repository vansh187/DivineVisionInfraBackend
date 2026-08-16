from sqlalchemy import Column, Date, Integer, Numeric, String, text
from .persistence_db import Base, SessionLocal, engine, RowWrapper, load_queries


class MarketTrendModel(Base):
    __tablename__ = "divine_market_trends"
    id = Column(String(36), primary_key=True)
    city = Column(String(100), nullable=False, index=True)
    locality = Column(String(150), index=True)
    property_type = Column(String(80), nullable=False, index=True)
    period_label = Column(String(50), nullable=False)
    as_of_date = Column(Date, nullable=False, index=True)
    price_per_sqyd = Column(Numeric(12, 2), nullable=False)
    previous_price_per_sqyd = Column(Numeric(12, 2))
    rental_yield_percent = Column(Numeric(5, 2))
    demand_score = Column(Numeric(5, 2))
    supply_score = Column(Numeric(5, 2))
    sample_size = Column(Integer, nullable=False, default=0)


class persistenceMarketTrend:
    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory
        queries = load_queries("market_trend_queries.yaml")
        queries.setdefault("list_market_trends", (
            "SELECT * FROM divine_market_trends "
            "WHERE (:city IS NULL OR lower(city) = lower(:city)) "
            "AND (:locality IS NULL OR lower(locality) = lower(:locality)) "
            "AND (:property_type IS NULL OR lower(property_type) = lower(:property_type)) "
            "ORDER BY as_of_date DESC, city ASC, locality ASC, property_type ASC "
            "LIMIT :limit;"
        ))
        self._queries = queries
        self._engine = engine

    def list_market_trends(self, city: str = None, locality: str = None,
                           property_type: str = None, limit: int = 20):
        with self._session_factory() as db:
            result = db.execute(text(self._queries["list_market_trends"]), {
                "city": city,
                "locality": locality,
                "property_type": property_type,
                "limit": limit,
            })
            return [RowWrapper(row) for row in result.mappings().all()]
