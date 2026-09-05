import uuid
from datetime import date

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

    def upsert_trend(self, city: str, property_type: str, price_per_sqyd,
                     locality: str = None, period_label: str = None, as_of_date=None,
                     previous_price_per_sqyd=None, rental_yield_percent=None,
                     demand_score=None, supply_score=None, sample_size: int = 0):
        """Replace the market-trend row for this (city, locality, property_type) with a
        fresh figure. Prices are not static - re-run the ingest script whenever the
        sales team revises the going rate, and the newest row wins on every read."""
        city = (city or "").strip()
        property_type = (property_type or "").strip().lower()
        locality = (locality or "").strip() or None
        if not city or not property_type or price_per_sqyd in (None, ""):
            raise ValueError("city, property_type and price_per_sqyd are required")
        as_of_date = as_of_date or date.today()
        period_label = (period_label or "").strip() or as_of_date.isoformat()

        with self._session_factory() as db:
            db.execute(
                text("DELETE FROM divine_market_trends "
                     "WHERE lower(city) = lower(:city) "
                     "AND lower(property_type) = lower(:property_type) "
                     "AND (COALESCE(lower(locality), '') = COALESCE(lower(:locality), ''))"),
                {"city": city, "property_type": property_type, "locality": locality},
            )
            row = db.execute(
                text("INSERT INTO divine_market_trends "
                     "(id, city, locality, property_type, period_label, as_of_date, "
                     " price_per_sqyd, previous_price_per_sqyd, rental_yield_percent, "
                     " demand_score, supply_score, sample_size) "
                     "VALUES (:id, :city, :locality, :property_type, :period_label, :as_of_date, "
                     " :price, :prev, :yield, :demand, :supply, :sample) RETURNING *;"),
                {
                    "id": str(uuid.uuid4()), "city": city, "locality": locality,
                    "property_type": property_type, "period_label": period_label,
                    "as_of_date": as_of_date, "price": price_per_sqyd,
                    "prev": previous_price_per_sqyd, "yield": rental_yield_percent,
                    "demand": demand_score, "supply": supply_score,
                    "sample": int(sample_size or 0),
                },
            ).mappings().first()
            db.commit()
            return RowWrapper(row)

    def average_booking_rate_per_sqyd(self, city: str = None):
        """The live ₹/sq-yd implied by actual paid bookings - total consideration over
        plot area across booking-application documents. Returns (rate, sample_size) or
        (None, 0) when there aren't enough real transactions yet. This is the source a
        scheduled refresh should prefer over a hand-entered figure once bookings exist."""
        query = (
            "SELECT d.form_data AS form_data "
            "FROM divine_documents d "
            "WHERE d.document_type = 'booking_application' AND d.form_data IS NOT NULL"
        )
        rates = []
        with self._session_factory() as db:
            for r in db.execute(text(query)).mappings().all():
                fd = r.get("form_data")
                if isinstance(fd, str):
                    import json
                    try:
                        fd = json.loads(fd)
                    except (TypeError, ValueError):
                        continue
                if not isinstance(fd, dict):
                    continue
                total = _first_num(fd, ("total_consideration", "totalConsideration", "total_value", "totalValue"))
                area = _first_num(fd, ("plot_area_sq_yd", "plotAreaSqYd", "area_sq_yd", "areaSqYd", "plot_area", "plotArea"))
                if total and area and area > 0:
                    if city and str(fd.get("city") or fd.get("City") or "").strip().lower() not in ("", city.strip().lower()):
                        continue
                    rates.append(total / area)
        if len(rates) < 3:
            return None, len(rates)
        rates.sort()
        return round(rates[len(rates) // 2], 2), len(rates)  # median


def _first_num(data: dict, keys):
    for k in keys:
        v = data.get(k)
        if v is None:
            continue
        try:
            n = float(str(v).replace(",", "").replace("Rs.", "").replace("₹", "").strip())
            if n > 0:
                return n
        except (TypeError, ValueError):
            continue
    return None
