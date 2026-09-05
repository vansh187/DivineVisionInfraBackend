import os
import json
import uuid

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from sqlalchemy import text
from Divinepersistence.persistence_db import PersistenceDB, engine
from Divinepersistence.persistence_market_trend import persistenceMarketTrend


def setup_module(module):
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()


def test_upsert_trend_replaces_row_for_same_city_locality_type():
    p = persistenceMarketTrend()
    p.upsert_trend("Sonipat", "plot", 30000, locality="Sector-15, Ganaur", previous_price_per_sqyd=29000)
    p.upsert_trend("Sonipat", "plot", 33000, locality="Sector-15, Ganaur", previous_price_per_sqyd=31000)

    rows = p.list_market_trends(city="Sonipat", property_type="plot")
    assert len(rows) == 1
    assert float(rows[0].price_per_sqyd) == 33000.0
    assert float(rows[0].previous_price_per_sqyd) == 31000.0


def test_upsert_trend_keeps_distinct_localities_separate():
    p = persistenceMarketTrend()
    p.upsert_trend("Karnal", "plot", 36000)                       # no locality
    p.upsert_trend("Karnal", "plot", 41000, locality="Sector 32")  # different locality
    rows = p.list_market_trends(city="Karnal", property_type="plot", limit=10)
    assert len(rows) == 2


def test_upsert_trend_requires_core_fields():
    p = persistenceMarketTrend()
    for bad in (dict(city="", property_type="plot", price_per_sqyd=1),
                dict(city="X", property_type="", price_per_sqyd=1),
                dict(city="X", property_type="plot", price_per_sqyd=None)):
        try:
            p.upsert_trend(**bad)
            assert False, f"expected ValueError for {bad}"
        except ValueError:
            pass


def test_average_booking_rate_needs_at_least_three_bookings():
    p = persistenceMarketTrend()
    rate, n = p.average_booking_rate_per_sqyd(city="Sonipat")
    assert (rate, n) == (None, 0)

    def _add_booking(total, area):
        with engine.begin() as c:
            c.execute(
                text("INSERT INTO divine_documents "
                     "(id, owner_id, owner_role, document_type, form_data, storage_path, status) "
                     "VALUES (:id, 'C1', 'customer', 'booking_application', :fd, 'p', 'generated')"),
                {"id": str(uuid.uuid4()),
                 "fd": json.dumps({"total_consideration": total, "plot_area_sq_yd": area, "city": "Sonipat"})},
            )

    _add_booking(3_400_000, 100)   # 34,000 /sq yd
    _add_booking(3_600_000, 100)   # 36,000
    assert p.average_booking_rate_per_sqyd(city="Sonipat")[0] is None  # still < 3

    _add_booking(3_300_000, 100)   # 33,000
    rate, n = p.average_booking_rate_per_sqyd(city="Sonipat")
    assert n == 3
    assert rate == 34000.0  # median of 33k, 34k, 36k
