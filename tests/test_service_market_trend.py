from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from DivineService.service_market_trend import serviceMarketTrend


def _record(**overrides):
    values = {
        "id": "trend1",
        "city": "Gurugram",
        "locality": "Sector 57",
        "property_type": "plot",
        "period_label": "Q3 2026",
        "as_of_date": date(2026, 9, 30),
        "price_per_sqyd": "100000.00",
        "previous_price_per_sqyd": "95000.00",
        "rental_yield_percent": "2.50",
        "demand_score": "72.5",
        "supply_score": "44.2",
        "sample_size": 18,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _service(records):
    persistence = MagicMock()
    persistence.list_market_trends.return_value = records
    return serviceMarketTrend(persistence), persistence


def test_list_market_trends_formats_price_change_and_labels():
    svc, _ = _service([_record()])

    result = svc.list_market_trends()

    assert result["count"] == 1
    trend = result["trends"][0]
    assert trend["price_change_percent"] == 5.26
    assert trend["trend_direction"] == "up"
    assert trend["demand_label"] == "high"
    assert trend["as_of_date"] == "2026-09-30"


def test_list_market_trends_handles_zero_previous_price_as_new():
    svc, _ = _service([_record(previous_price_per_sqyd=0)])

    trend = svc.list_market_trends()["trends"][0]

    assert trend["previous_price_per_sqyd"] is None
    assert trend["price_change_percent"] is None
    assert trend["trend_direction"] == "new"


def test_list_market_trends_sanitizes_optional_out_of_range_metrics():
    svc, _ = _service([
        _record(demand_score=120, supply_score=-1, rental_yield_percent=-4, sample_size=-5),
    ])

    trend = svc.list_market_trends()["trends"][0]

    assert trend["demand_score"] is None
    assert trend["supply_score"] is None
    assert trend["rental_yield_percent"] is None
    assert trend["demand_label"] == "unknown"
    assert trend["sample_size"] == 0


def test_list_market_trends_skips_unusable_rows_without_failing_response():
    svc, _ = _service([
        _record(id="bad", price_per_sqyd="not-a-number"),
        _record(id="good"),
    ])

    result = svc.list_market_trends()

    assert result["count"] == 1
    assert result["trends"][0]["id"] == "good"


def test_list_market_trends_normalizes_blank_filters_and_clamps_limit():
    svc, persistence = _service([])

    result = svc.list_market_trends(city="  ", locality=" Sector 57 ", property_type=" plot ", limit=500)

    assert result == {"count": 0, "trends": []}
    persistence.list_market_trends.assert_called_once_with(
        city=None, locality="Sector 57", property_type="plot", limit=100,
    )


def test_list_market_trends_rejects_invalid_limit():
    svc, _ = _service([])

    try:
        svc.list_market_trends(limit=0)
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "invalid_limit"


def test_list_market_trends_rejects_overlong_filter():
    svc, _ = _service([])

    try:
        svc.list_market_trends(city="x" * 151)
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "city_too_long"
