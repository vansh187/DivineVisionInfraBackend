from datetime import date
from DivineService.service_payment_schedule import build_payment_schedule


def test_schedule_uses_10_15_25_25_25_plan():
    p = build_payment_schedule(total_amount=10_000_000, booking_amount=2_000_000,
                               booking_date="2026-09-07")
    assert p["total_receivable"] == 10_000_000
    assert p["total_received"] == 2_000_000
    assert p["total_outstanding"] == 8_000_000
    assert p["total_outstanding_words"].endswith("Rupees")

    rows = p["rows"]
    assert [r["percent"] for r in rows] == [10, 15, 25, 25, 25]
    assert [r["amount"] for r in rows] == [1_000_000, 1_500_000, 2_500_000, 2_500_000, 2_500_000]
    assert [r["due_days"] for r in rows] == [0, 45, 90, 180, 270]
    assert rows[0]["due_date"] == "2026-09-07"
    assert rows[1]["due_date"] == "2026-10-22"
    assert rows[4]["due_date"] == "2027-06-04"
    assert sum(r["amount"] for r in rows) == 10_000_000
    # 20L paid covers the 10L on-booking instalment
    assert rows[0]["status"] == "paid"
    assert all(r["status"] == "due" for r in rows[1:])


def test_last_row_absorbs_rounding_so_rows_sum_to_total():
    p = build_payment_schedule(total_amount=10_000_001, booking_amount=1, booking_date="2026-01-01")
    amts = [r["amount"] for r in p["rows"]]
    assert amts[:4] == [1_000_000, 1_500_000, 2_500_000, 2_500_000]
    assert amts[4] == 10_000_001 - 7_500_000        # last row carries the +1
    assert sum(amts) == 10_000_001


def test_on_booking_row_is_due_when_paid_below_the_instalment():
    p = build_payment_schedule(total_amount=10_000_000, booking_amount=500_000)
    assert p["rows"][0]["status"] == "due"          # 5L < 10L on-booking
    assert p["total_received"] == 500_000
    assert p["total_outstanding"] == 9_500_000


def test_schedule_handles_missing_or_bad_total():
    for bad in (None, 0, -5, "abc"):
        p = build_payment_schedule(total_amount=bad, booking_amount=1000)
        assert p["rows"] == [] and p["total_receivable"] is None


def test_schedule_clamps_paid_over_total():
    p = build_payment_schedule(total_amount=1_000_000, booking_amount=9_999_999, booking_date="2026-01-01")
    assert p["total_received"] == 1_000_000 and p["total_outstanding"] == 0


def test_schedule_defaults_booking_date_to_today_on_bad_input():
    p = build_payment_schedule(total_amount=2_000_000, booking_amount=500_000, booking_date="not-a-date")
    assert p["rows"][0]["due_date"] == date.today().isoformat()
