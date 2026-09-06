"""Derives a plot-booking payment schedule from the total plot amount and the
booking amount already paid, matching the milestone plan used on the demand /
allotment letters (On Booking, +45d, +90d, +180d, +270d).

Pure functions - no DB, no I/O, never raises. The booking amount is what the
customer actually paid at application time; (total - booking) is split across
the remaining milestones, the last one absorbing any rounding remainder so the
rows sum exactly to the total.
"""
from datetime import date, datetime, timedelta

from DivineService.loan_report_data import amount_in_words_indian

# (label, days-after-booking). The first row is always the paid booking amount.
DEFAULT_MILESTONES = (
    ("On Booking", 0),
    ("Within 45 days of booking", 45),
    ("Within 90 days of booking", 90),
    ("Within 180 days of booking", 180),
    ("Within 270 days of booking", 270),
)


def _num(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n != n or n in (float("inf"), float("-inf")):
        return None
    return n


def _as_date(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d.%m.%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def build_payment_schedule(total_amount, booking_amount, booking_date=None, milestones=DEFAULT_MILESTONES):
    """Returns a dict:
      {
        total_receivable, total_received, total_outstanding,
        total_outstanding_words, booking_date,
        rows: [ {label, due_date, due_days, amount, percent, status}, ... ]
      }
    `rows` is [] and totals are None when total_amount is missing/invalid.
    """
    total = _num(total_amount)
    booked = _num(booking_amount) or 0.0
    if total is None or total <= 0:
        return {
            "total_receivable": None, "total_received": None, "total_outstanding": None,
            "total_outstanding_words": None, "booking_date": None, "rows": [],
        }

    booked = max(0.0, min(booked, total))
    outstanding = round(total - booked)
    start = _as_date(booking_date) or date.today()

    later = [m for m in milestones if m[1] > 0] or [("Balance", 90)]
    per = outstanding // len(later)
    remainder = outstanding - per * len(later)

    rows = [{
        "label": milestones[0][0] if milestones else "On Booking",
        "due_date": start.isoformat(),
        "due_days": 0,
        "amount": round(booked),
        "percent": round(booked / total * 100, 2) if total else None,
        "status": "paid",
    }]
    for i, (label, days) in enumerate(later):
        amt = per + (remainder if i == len(later) - 1 else 0)
        rows.append({
            "label": label,
            "due_date": (start + timedelta(days=days)).isoformat(),
            "due_days": days,
            "amount": int(amt),
            "percent": round(amt / total * 100, 2) if total else None,
            "status": "due",
        })

    return {
        "total_receivable": round(total),
        "total_received": round(booked),
        "total_outstanding": outstanding,
        "total_outstanding_words": amount_in_words_indian(outstanding) or None,
        "booking_date": start.isoformat(),
        "rows": rows,
    }
