"""Derives a plot-booking payment schedule from the total plot amount, matching
the milestone plan on the demand / allotment letters:

    On Booking             -> 10% of the total plot amount
    Within 45 days          -> 15%
    Within 90 days          -> 25%
    Within 180 days         -> 25%
    Within 270 days         -> 25%   (last milestone carries the rounding remainder)

`total_received` on the returned plan is what the customer ACTUALLY paid at
application time; `total_outstanding` = total - received. The schedule rows show
the plan, independent of how much was paid.

Pure functions - no DB, no I/O, never raises.
"""
from datetime import date, datetime, timedelta

from DivineService.loan_report_data import amount_in_words_indian

# (label, days-after-booking, percent-of-total). Must add up to 100.
DEFAULT_MILESTONES = (
    ("On Booking", 0, 10),
    ("Within 45 days of booking", 45, 15),
    ("Within 90 days of booking", 90, 25),
    ("Within 180 days of booking", 180, 25),
    ("Within 270 days of booking", 270, 25),
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


def build_payment_schedule(total_amount, booking_amount, booking_date=None,
                           milestones=DEFAULT_MILESTONES):
    """Returns a dict:
      {
        total_receivable, total_received, total_outstanding,
        total_outstanding_words, booking_date,
        rows: [ {label, due_date, due_days, amount, percent, status}, ... ]
      }
    `rows` is [] and totals are None when total_amount is missing/invalid.

    Each milestone's amount = round(total * percent / 100); the LAST row is
    adjusted so the rows sum to exactly `total`. `total_received` is what was
    actually paid (`booking_amount`); the first row is 'paid' only when that
    covers the on-booking instalment, else 'due'.
    """
    total = _num(total_amount)
    paid = max(0.0, _num(booking_amount) or 0.0)
    if total is None or total <= 0:
        return {
            "total_receivable": None, "total_received": None, "total_outstanding": None,
            "total_outstanding_words": None, "booking_date": None, "rows": [],
        }

    total = round(total)
    paid = round(min(paid, total))
    start = _as_date(booking_date) or date.today()
    plan = list(milestones) or [("Balance", 90, 100)]

    rows = []
    running = 0
    for i, (label, days, pct) in enumerate(plan):
        amt = total - running if i == len(plan) - 1 else round(total * pct / 100)
        running += amt
        rows.append({
            "label": label,
            "due_date": (start + timedelta(days=days)).isoformat(),
            "due_days": days,
            "amount": int(amt),
            "percent": pct,
            "status": ("paid" if (days == 0 and paid >= amt) else "due"),
        })

    outstanding = total - paid
    return {
        "total_receivable": total,
        "total_received": paid,
        "total_outstanding": outstanding,
        "total_outstanding_words": amount_in_words_indian(outstanding) or None,
        "booking_date": start.isoformat(),
        "rows": rows,
    }
