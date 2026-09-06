from datetime import date
from DivineService.service_payment_schedule import build_payment_schedule


def test_schedule_splits_outstanding_across_later_milestones():
    p = build_payment_schedule(total_amount=5000000, booking_amount=2000000, booking_date="2026-09-07")
    assert p["total_receivable"] == 5000000
    assert p["total_received"] == 2000000
    assert p["total_outstanding"] == 3000000
    assert p["total_outstanding_words"].endswith("Rupees")
    rows = p["rows"]
    assert len(rows) == 5
    assert rows[0]["label"] == "On Booking" and rows[0]["amount"] == 2000000 and rows[0]["status"] == "paid"
    assert rows[0]["due_date"] == "2026-09-07"
    assert rows[1]["due_days"] == 45 and rows[2]["due_days"] == 90
    assert rows[4]["due_days"] == 270
    # 3,000,000 / 4 = 750,000 each, last absorbs remainder
    assert [r["amount"] for r in rows[1:]] == [750000, 750000, 750000, 750000]
    assert sum(r["amount"] for r in rows) == 5000000


def test_schedule_rounding_remainder_on_last_row():
    p = build_payment_schedule(total_amount=1000001, booking_amount=1, booking_date="2026-01-01")
    amts = [r["amount"] for r in p["rows"]]
    assert sum(amts) == 1000001            # exact
    assert amts[-1] >= amts[-2]           # last row carries the remainder


def test_schedule_handles_missing_or_bad_total():
    for bad in (None, 0, -5, "abc"):
        p = build_payment_schedule(total_amount=bad, booking_amount=1000)
        assert p["rows"] == [] and p["total_receivable"] is None


def test_schedule_clamps_booking_over_total():
    p = build_payment_schedule(total_amount=1000000, booking_amount=9999999, booking_date="2026-01-01")
    assert p["total_received"] == 1000000 and p["total_outstanding"] == 0
    assert all(r["amount"] == 0 for r in p["rows"][1:])


def test_schedule_defaults_booking_date_to_today_on_bad_input():
    p = build_payment_schedule(total_amount=2000000, booking_amount=500000, booking_date="not-a-date")
    assert p["rows"][0]["due_date"] == date.today().isoformat()
