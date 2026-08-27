from types import SimpleNamespace
from unittest.mock import MagicMock

from DivineService.service_inventory import serviceInventory
from DivineService.llm_gemini import GeminiError
from DivineService.llm_groq import GroqError


def _unit(**overrides):
    values = {
        "id": "u1",
        "project_name": "Suraksha Enclave",
        "city": "Sonipat",
        "locality": "Sector-15, Ganaur",
        "block": "C",
        "unit_number": "C1",
        "unit_type": "plot",
        "width_mtr": "7.588",
        "length_mtr": "13.404",
        "area_sqmt": "101.71",
        "area_sqyd": "121.65",
        "status": "available",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _trend(price_per_sqyd="100000.00"):
    return SimpleNamespace(price_per_sqyd=price_per_sqyd)


def _service(units=None, trends=None, gemini=None, groq=None, qualification=None, events=None):
    persistence = MagicMock()
    persistence.search.return_value = units or []
    persistence.list_events_for_lead.return_value = events or []
    persistence.list_recent_available_units.return_value = units or []

    market_trend_persistence = MagicMock()
    market_trend_persistence.list_market_trends.return_value = trends if trends is not None else [_trend()]

    chatbot_persistence = MagicMock()
    chatbot_persistence.get_qualification_by_lead.return_value = qualification

    svc = serviceInventory(
        persistence=persistence,
        market_trend_persistence=market_trend_persistence,
        chatbot_persistence=chatbot_persistence,
        gemini=gemini or MagicMock(),
        groq=groq or MagicMock(),
    )
    return svc, persistence, market_trend_persistence, chatbot_persistence


def test_search_estimates_price_from_market_trend():
    svc, persistence, _, _ = _service(units=[_unit()])

    result = svc.search(city="Sonipat")

    assert result["count"] == 1
    unit = result["units"][0]
    assert unit["area_sqyd"] == 121.65
    assert unit["estimated_price"] == 12165000.0
    persistence.search.assert_called_once()


def test_search_filters_by_budget_after_price_estimation():
    svc, _, _, _ = _service(units=[_unit(id="cheap", area_sqyd="50"), _unit(id="pricey", area_sqyd="500")])

    result = svc.search(min_budget=10_000_000, max_budget=60_000_000)

    ids = [u["id"] for u in result["units"]]
    assert ids == ["pricey"]


def test_search_with_budget_filter_fetches_unpaginated_and_paginates_after_filtering():
    # Only every third unit clears the budget threshold - if the DB-level LIMIT were applied
    # before the budget filter (the bug), a small limit would silently drop matching units
    # that lived past the raw (pre-filter) page boundary instead of paginating over them.
    units = []
    for i in range(30):
        area = "500" if i % 3 == 0 else "10"  # area 500 * 100000/sqyd clears a 40M budget; area 10 doesn't
        units.append(_unit(id=f"u{i}", area_sqyd=area))
    svc, persistence, _, _ = _service(units=units)

    result = svc.search(min_budget=40_000_000, limit=5, offset=0)

    assert result["count"] == 5
    assert [u["id"] for u in result["units"]] == ["u0", "u3", "u6", "u9", "u12"]
    # The DB call must not have applied the caller's limit/offset - those get applied in Python
    # only after the budget filter runs, otherwise later pages would permanently skip matches.
    call_kwargs = persistence.search.call_args.kwargs
    assert call_kwargs["offset"] == 0
    assert call_kwargs["limit"] > 30

    result_page2 = svc.search(min_budget=40_000_000, limit=5, offset=5)
    assert [u["id"] for u in result_page2["units"]] == ["u15", "u18", "u21", "u24", "u27"]


def test_search_without_budget_filter_still_paginates_at_the_db_level():
    svc, persistence, _, _ = _service(units=[_unit()])

    svc.search(city="Sonipat", limit=5, offset=10)

    call_kwargs = persistence.search.call_args.kwargs
    assert call_kwargs["limit"] == 5
    assert call_kwargs["offset"] == 10


def test_search_caches_price_lookups_across_units_sharing_city_and_unit_type():
    svc, _, market_trend_persistence, _ = _service(units=[
        _unit(id="a", city="Sonipat", unit_type="plot"),
        _unit(id="b", city="Sonipat", unit_type="plot"),
        _unit(id="c", city="Sonipat", unit_type="plot"),
    ])

    result = svc.search(city="Sonipat")

    assert all(u["estimated_price"] is not None for u in result["units"])
    market_trend_persistence.list_market_trends.assert_called_once()


def test_search_keeps_units_with_no_price_data_when_budget_filter_applied():
    svc, _, _, _ = _service(units=[_unit()], trends=[])

    result = svc.search(min_budget=1000)

    assert result["count"] == 1
    assert result["units"][0]["estimated_price"] is None


def test_search_rejects_invalid_unit_type():
    svc, _, _, _ = _service()

    try:
        svc.search(unit_type="mansion")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "invalid_unit_type"


def test_search_natural_language_uses_gemini_tool_call():
    gemini = MagicMock()
    gemini.generate.return_value = {"text": None, "function_call": {
        "name": "extract_search_filters", "args": {"city": "Sonipat", "min_area_sqyd": 100, "max_area_sqyd": "150"},
    }}
    svc, persistence, _, _ = _service(units=[_unit()], gemini=gemini)

    result = svc.search_natural_language("150 sq yd plot in Sonipat")

    assert result["parsed_filters"] == {"city": "Sonipat", "min_area_sqyd": 100.0, "max_area_sqyd": 150.0}
    persistence.search.assert_called_once()
    call_kwargs = persistence.search.call_args.kwargs
    assert call_kwargs["city"] == "Sonipat"
    assert call_kwargs["min_area_sqyd"] == 100.0


def test_search_natural_language_widens_a_single_stated_size_into_a_tolerance_band():
    gemini = MagicMock()
    gemini.generate.return_value = {"text": None, "function_call": {
        "name": "extract_search_filters", "args": {"min_area_sqyd": 120, "max_area_sqyd": 120},
    }}
    svc, persistence, _, _ = _service(units=[_unit()], gemini=gemini)

    result = svc.search_natural_language("120 sq yd plot")

    assert result["parsed_filters"]["min_area_sqyd"] == 102.0
    assert result["parsed_filters"]["max_area_sqyd"] == 138.0


def test_search_natural_language_falls_back_to_groq_on_gemini_error():
    gemini = MagicMock()
    gemini.generate.side_effect = GeminiError("boom")
    groq = MagicMock()
    groq.generate.return_value = {"text": None, "function_call": {
        "name": "extract_search_filters", "args": {"unit_type": "plot"},
    }}
    svc, _, _, _ = _service(units=[_unit()], gemini=gemini, groq=groq)

    result = svc.search_natural_language("any plot")

    assert result["parsed_filters"] == {"unit_type": "plot"}
    groq.generate.assert_called_once()


def test_search_natural_language_returns_empty_filters_when_both_providers_fail():
    gemini = MagicMock()
    gemini.generate.side_effect = GeminiError("boom")
    groq = MagicMock()
    groq.generate.side_effect = GroqError("boom")
    svc, _, _, _ = _service(units=[_unit()], gemini=gemini, groq=groq)

    result = svc.search_natural_language("any plot")

    assert result["parsed_filters"] == {}


def test_search_natural_language_rejects_empty_query():
    svc, _, _, _ = _service()

    try:
        svc.search_natural_language("   ")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "query_required"


def test_record_view_writes_event_and_returns_recorded_true():
    svc, persistence, _, _ = _service()

    result = svc.record_view("u1", lead_id="lead1", session_id="sess1")

    assert result == {"recorded": True}
    persistence.record_event.assert_called_once()
    assert persistence.record_event.call_args.kwargs["inventory_id"] == "u1"


def test_record_view_swallows_persistence_failure():
    svc, persistence, _, _ = _service()
    persistence.record_event.side_effect = RuntimeError("fk violation")

    result = svc.record_view("bad-id")

    assert result == {"recorded": False}


def test_record_view_rejects_blank_inventory_id():
    svc, _, _, _ = _service()

    try:
        svc.record_view("  ")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "inventory_id_required"


def test_recommend_scores_by_viewed_history():
    viewed = SimpleNamespace(inventory_id="v1", unit_type="plot", area_sqyd="120", city="Sonipat", locality="Sector-15")
    close_match = _unit(id="close", area_sqyd="125")
    far_match = _unit(id="far", area_sqyd="900")
    svc, persistence, _, _ = _service(units=[close_match, far_match], events=[viewed])
    persistence.get_by_id.return_value = _unit(id="v1", area_sqyd="120", unit_type="plot", project_name="Suraksha Enclave")

    result = svc.recommend(lead_id="lead1")

    assert result["best_fit"][0]["id"] == "close"
    assert "v1" in result["similar_alternatives"]


def test_recommend_caches_price_lookups_across_many_candidates():
    qualification = SimpleNamespace(budget_min="5000000", budget_max="15000000", unit_type="plot", location_preference="Sonipat")
    units = [_unit(id=f"u{i}", city="Sonipat", unit_type="plot", area_sqyd="120") for i in range(50)]
    svc, persistence, market_trend_persistence, _ = _service(units=units, events=[], qualification=qualification)

    svc.recommend(lead_id="lead1", limit=10)

    # 50 candidates all sharing (city, unit_type) should hit the market-trend lookup once for
    # scoring's budget check and once more for formatting the top 10 - not once per candidate.
    assert market_trend_persistence.list_market_trends.call_count <= 2


def test_recommend_falls_back_to_qualification_when_no_views():
    qualification = SimpleNamespace(budget_min="5000000", budget_max="15000000", unit_type="plot", location_preference="Sonipat")
    matching = _unit(id="match", area_sqyd="120")
    svc, persistence, _, _ = _service(units=[matching], events=[], qualification=qualification)

    result = svc.recommend(lead_id="lead1")

    assert result["best_fit"][0]["id"] == "match"
    assert result["similar_alternatives"] == {}


def test_recommend_falls_back_to_recent_units_with_no_signal_at_all():
    svc, persistence, _, _ = _service(units=[_unit()], events=[], qualification=None)

    result = svc.recommend(lead_id="lead1")

    assert result["best_fit"][0]["id"] == "u1"
    persistence.list_recent_available_units.assert_called_once()


def test_recommend_with_no_lead_or_session_returns_recent_units():
    svc, persistence, _, _ = _service(units=[_unit()])

    result = svc.recommend()

    assert result["best_fit"][0]["id"] == "u1"
    persistence.list_events_for_lead.assert_not_called()


# ---- Channel Partner reservations ------------------------------------------

def test_reserve_unit_success_returns_formatted_unit_with_expiry():
    from datetime import datetime, timezone
    svc, persistence, _, _ = _service()
    persistence.reserve_unit.return_value = _unit(
        status="reserved", reserved_by_broker_id="B00001",
        reserved_at=datetime.now(timezone.utc), reserved_until=datetime.now(timezone.utc),
    )

    result = svc.reserve_unit("u1", "B00001")

    assert result["id"] == "u1"
    assert result["reserved_at"] is not None
    assert result["reserved_until"] is not None
    call_kwargs = persistence.reserve_unit.call_args.kwargs
    assert call_kwargs["id"] == "u1"
    assert call_kwargs["broker_id"] == "B00001"
    assert (call_kwargs["reserved_until"] - call_kwargs["reserved_at"]).days == 3


def test_reserve_unit_raises_conflict_when_persistence_returns_none():
    svc, persistence, _, _ = _service()
    persistence.reserve_unit.return_value = None

    try:
        svc.reserve_unit("u1", "B00001")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "unit_not_available"


def test_reserve_unit_rejects_blank_ids():
    svc, _, _, _ = _service()

    try:
        svc.reserve_unit("  ", "B00001")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "inventory_id_required"

    try:
        svc.reserve_unit("u1", "  ")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "broker_id_required"


def test_release_reservation_success():
    svc, persistence, _, _ = _service()
    persistence.release_reservation.return_value = _unit(status="available")

    result = svc.release_reservation("u1", "B00001")

    assert result["status"] == "available"
    call_kwargs = persistence.release_reservation.call_args.kwargs
    assert call_kwargs["target_status"] == "available"
    assert call_kwargs["outcome"] == "released"


def test_release_reservation_raises_when_not_owned_by_caller():
    svc, persistence, _, _ = _service()
    persistence.release_reservation.return_value = None

    try:
        svc.release_reservation("u1", "B00002")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "not_reserved_by_you"


def test_mark_sold_success():
    svc, persistence, _, _ = _service()
    persistence.release_reservation.return_value = _unit(status="sold")

    result = svc.mark_sold("u1", "B00001")

    assert result["status"] == "sold"
    call_kwargs = persistence.release_reservation.call_args.kwargs
    assert call_kwargs["target_status"] == "sold"
    assert call_kwargs["outcome"] == "converted"


def test_mark_sold_raises_when_not_owned_by_caller():
    svc, persistence, _, _ = _service()
    persistence.release_reservation.return_value = None

    try:
        svc.mark_sold("u1", "B00002")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "not_reserved_by_you"


def test_list_my_reservations_scopes_to_calling_broker_only():
    svc, persistence, _, _ = _service()
    persistence.list_reservations_for_broker.return_value = [
        _unit(id="mine1", status="reserved"), _unit(id="mine2", status="reserved"),
    ]

    result = svc.list_my_reservations("B00001")

    assert result["count"] == 2
    assert [r["id"] for r in result["reservations"]] == ["mine1", "mine2"]
    persistence.list_reservations_for_broker.assert_called_once_with("B00001")


def test_list_my_reservations_rejects_blank_broker_id():
    svc, _, _, _ = _service()

    try:
        svc.list_my_reservations("  ")
        assert False, "expected ValueError"
    except ValueError as e:
        assert str(e) == "broker_id_required"
