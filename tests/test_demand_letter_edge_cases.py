"""Corner cases for the Demand Letter generator - the inputs real bookings throw
at it: odd keys, partial / early / over-payments, missing data, odd date and
amount formats, long text, many instalments and failing lookups."""
import io
import json
import os
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_db.sqlite")
os.environ.setdefault("JWT_SECRET_KEY", "testsecret")

import pytest
from pypdf import PdfReader

import DivineService.service_demand_letter as dl
from DivineService.service_demand_letter import (
    _indian, _num, _to_date, _words, demand_letter_figures, generate_demand_letter_pdf, project_profile,
)
from DivineService.service_document import serviceDocument

ISSUED = date(2026, 11, 5)


def _row(label, due, amount, status=None, **extra):
    r = {"label": label, "due_date": due, "amount": amount, **extra}
    if status is not None:
        r["status"] = status
    return r


def _plan(statuses=("paid", "due", "due", "upcoming"), received=262746):
    dues = ("2024-02-29", "2024-04-14", "2026-12-01", "2027-02-23")
    amounts = (262746, 394119, 656865, 1313730)
    labels = ("On Booking", "Within 45 days of booking", "Within 90 days of booking",
              "Within 360 days of booking")
    return {
        "projectId": "suraksha-enclave", "applicantName": "Dheeraj Nirwal", "gender": "Male",
        "unitNo": "C61", "plotAreaSqYd": "119.43",
        "permanentAddress": "H.No. 543 Bhainswal Kalan, Sonipat, Haryana-131409",
        "total_consideration": sum(amounts), "amount_received": received,
        "payment_schedule": [_row(l, d, a, s) for l, d, a, s in zip(labels, dues, amounts, statuses)],
    }


def _text(form, issued=ISSUED, customer_id="C00001"):
    pdf = generate_demand_letter_pdf(form, customer_id, issued_on=issued)
    assert pdf[:5] == b"%PDF-"
    reader = PdfReader(io.BytesIO(pdf))
    return "\n".join(p.extract_text() for p in reader.pages), len(reader.pages)


def _labels(fig):
    return [r["label"] for r in fig["rows"]]


# --------------------------------------------------------------------------
# Paid instalments never appear in the table
# --------------------------------------------------------------------------

def test_paid_rows_hidden_unpaid_rows_shown():
    fig = demand_letter_figures(_plan(), ISSUED)
    assert _labels(fig) == ["Within 45 days of booking", "Within 90 days of booking",
                            "Within 360 days of booking"]
    text, _ = _text(_plan())
    assert "On Booking" not in text


@pytest.mark.parametrize("status", ["paid", "PAID", " Paid ", "Paid"])
def test_paid_status_is_case_and_space_insensitive(status):
    fig = demand_letter_figures(_plan(statuses=(status, "due", "due", "due")), ISSUED)
    assert "On Booking" not in _labels(fig)


@pytest.mark.parametrize("status", ["due", "overdue", "upcoming", "pending_review", "partially_paid", "failed"])
def test_any_non_paid_status_keeps_the_row(status):
    fig = demand_letter_figures(_plan(statuses=("paid", status, "due", "due")), ISSUED)
    assert "Within 45 days of booking" in _labels(fig)


def test_early_payment_of_a_later_instalment_hides_only_that_row():
    # Customer paid instalment 4 early but still owes instalment 2.
    form = _plan(statuses=("paid", "overdue", "upcoming", "paid"), received=262746 + 1313730)
    fig = demand_letter_figures(form, ISSUED)
    assert _labels(fig) == ["Within 45 days of booking", "Within 90 days of booking"]
    assert fig["overdue"] == 394119


def test_all_instalments_paid_prints_no_rows_and_no_dues_sentence():
    form = _plan(statuses=("paid",) * 4, received=2627460)
    fig = demand_letter_figures(form, ISSUED)
    assert fig["rows"] == [] and fig["outstanding"] == 0 and fig["overdue"] == 0 and fig["demand"] == 0
    text, _ = _text(form)
    assert "No instalment is pending." in text
    assert "no amount is due" in text
    assert "remit the total dues" not in text


def test_totals_still_show_full_plot_amount_when_rows_are_hidden():
    fig = demand_letter_figures(_plan(), ISSUED)
    assert fig["total_plot"] == 2627460
    text, _ = _text(_plan())
    assert "26,27,460" in text and "2,62,746" in text   # plot total + received


def test_mixed_rows_with_and_without_status_trust_statuses():
    form = _plan()
    del form["payment_schedule"][1]["status"]            # one row lost its status
    fig = demand_letter_figures(form, ISSUED)
    assert "On Booking" not in _labels(fig)
    assert "Within 45 days of booking" in _labels(fig)   # unknown -> still demanded


# --------------------------------------------------------------------------
# Old snapshots without per-row status
# --------------------------------------------------------------------------

def _statusless(received):
    form = _plan(received=received)
    for r in form["payment_schedule"]:
        r.pop("status", None)
    return form


@pytest.mark.parametrize("received, hidden", [
    (0, []),
    (100000, []),                                              # token < first instalment
    (262746, ["On Booking"]),                                  # exactly the first
    (262746 + 394119, ["On Booking", "Within 45 days of booking"]),
    (262746 + 394119 + 1, ["On Booking", "Within 45 days of booking"]),  # spare rupee doesn't settle row 3
    (2627460, ["On Booking", "Within 45 days of booking", "Within 90 days of booking",
               "Within 360 days of booking"]),
])
def test_statusless_snapshot_settles_rows_in_due_order(received, hidden):
    fig = demand_letter_figures(_statusless(received), ISSUED)
    for label in hidden:
        assert label not in _labels(fig)
    assert len(fig["rows"]) == 4 - len(hidden)


def test_statusless_snapshot_settles_by_due_date_not_list_order():
    form = _statusless(262746)
    form["payment_schedule"].reverse()
    assert "On Booking" not in _labels(demand_letter_figures(form, ISSUED))


# --------------------------------------------------------------------------
# Received / outstanding / overdue arithmetic
# --------------------------------------------------------------------------

def test_overpayment_never_shows_negative_outstanding_or_demand():
    form = _plan(statuses=("paid", "due", "due", "due"), received=5000000)
    fig = demand_letter_figures(form, ISSUED)
    assert fig["outstanding"] == 0 and fig["overdue"] == 0 and fig["demand"] == 0


def test_overdue_is_capped_at_outstanding():
    # Received records more money than the paid rows explain (advance part-payment).
    form = _plan(statuses=("paid", "overdue", "due", "due"), received=2500000)
    fig = demand_letter_figures(form, ISSUED)
    assert fig["outstanding"] == 2627460 - 2500000
    assert fig["overdue"] == fig["outstanding"]


def test_received_missing_is_summed_from_paid_rows():
    form = _plan(statuses=("paid", "paid", "due", "due"))
    form.pop("amount_received")
    assert demand_letter_figures(form, ISSUED)["received"] == 262746 + 394119


def test_total_missing_is_summed_from_rows():
    form = _plan()
    form.pop("total_consideration")
    assert demand_letter_figures(form, ISSUED)["total_plot"] == 2627460


def test_schedule_missing_is_rebuilt_from_total_and_received():
    form = {"projectId": "suraksha-enclave", "total_consideration": 1000000,
            "amount_received": 100000, "booking_date": "2026-01-01"}
    fig = demand_letter_figures(form, ISSUED)
    assert fig["total_plot"] == 1000000 and fig["received"] == 100000
    assert "On Booking" not in _labels(fig)              # derived plan marks it paid
    assert len(fig["rows"]) == 4


@pytest.mark.parametrize("schedule", ["not-a-list", None, [], ["junk", 3, None]])
def test_unusable_schedule_falls_back_without_crashing(schedule):
    form = {"total_consideration": 500000, "amount_received": 50000, "payment_schedule": schedule}
    fig = demand_letter_figures(form, ISSUED)
    assert fig["total_plot"] == 500000
    _text(form)


def test_no_money_data_at_all_still_renders():
    fig = demand_letter_figures({"unitNo": "A-1"}, ISSUED)
    assert fig["rows"] == [] and fig["total_plot"] == 0
    text, _ = _text({"unitNo": "A-1"})
    assert "Plot No. A-1" in text


@pytest.mark.parametrize("raw, expected", [
    ("26,27,460", 2627460), ("Rs. 1,00,000", 100000), ("Rs.1,00,000/-", 100000),
    ("₹ 5,00,000", 500000), ("  250000 ", 250000), (Decimal("394119.00"), 394119),
    (394119.4, 394119.4), ("abc", None), (None, None), ("", None), ("nan", None), ("inf", None),
])
def test_amount_parsing(raw, expected):
    assert _num(raw) == expected


def test_amount_strings_in_schedule_are_understood():
    form = _plan()
    for r in form["payment_schedule"]:
        r["amount"] = f"Rs. {_indian(r['amount'])}/-"
    form["total_consideration"] = "26,27,460"
    form["amount_received"] = "2,62,746"
    fig = demand_letter_figures(form, ISSUED)
    assert fig["total_plot"] == 2627460 and fig["outstanding"] == 2364714


def test_bad_row_amount_counts_as_zero():
    form = _plan()
    form["payment_schedule"][1]["amount"] = "n/a"
    fig = demand_letter_figures(form, ISSUED)
    assert fig["overdue"] == 0                         # the only overdue row had no usable amount
    _text(form)


@pytest.mark.parametrize("n, expected", [
    (0, "0"), (7, "7"), (999, "999"), (1000, "1,000"), (99999, "99,999"), (100000, "1,00,000"),
    (2627460, "26,27,460"), (10000000, "1,00,00,000"), (1234567890, "1,23,45,67,890"),
    (262746.5, "2,62,746"), (-150000, "-1,50,000"), (None, "0"),
])
def test_indian_grouping(n, expected):
    assert _indian(n) == expected


@pytest.mark.parametrize("amount, expected", [
    (0, "Rupees Zero Only"),
    (1, "Rupees One Only"),
    (100000, "Rupees One Lakh Only"),
    (1731735, "Rupees Seventeen Lakh Thirty-One Thousand Seven Hundred Thirty-Five Only"),
    (10000000, "Rupees One Crore Only"),
])
def test_amount_in_words(amount, expected):
    assert _words(amount) == expected


# --------------------------------------------------------------------------
# Dates, overdue and the "payable on or before" logic
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("2024-02-29", date(2024, 2, 29)), ("29-02-2024", date(2024, 2, 29)),
    ("29/02/2024", date(2024, 2, 29)), ("29.02.2024", date(2024, 2, 29)),
    ("2024-02-29T10:30:00+05:30", date(2024, 2, 29)), (date(2024, 2, 29), date(2024, 2, 29)),
    (datetime(2024, 2, 29, 23, 59), date(2024, 2, 29)),
    ("2023-02-29", None), ("soon", None), (None, None), ("", None),
])
def test_due_date_parsing(raw, expected):
    assert _to_date(raw) == expected


def test_instalment_due_today_is_not_overdue_but_yesterday_is():
    base = {"total_consideration": 200000, "amount_received": 0}
    today = demand_letter_figures({**base, "payment_schedule": [_row("A", "2026-11-05", 200000, "due")]}, ISSUED)
    yday = demand_letter_figures({**base, "payment_schedule": [_row("A", "2026-11-04", 200000, "due")]}, ISSUED)
    assert today["overdue"] == 0 and yday["overdue"] == 200000


def test_overdue_dues_are_payable_within_seven_days():
    fig = demand_letter_figures(_plan(), ISSUED)
    assert fig["overdue"] == 394119
    assert fig["demand"] == 394119 and fig["payable_by"] == date(2026, 11, 12)
    text, _ = _text(_plan())
    assert "Rs. 3,94,119/-" in text and "12 Nov 2026" in text


def test_nothing_overdue_asks_for_next_instalment_by_its_due_date():
    form = _plan(statuses=("paid", "paid", "upcoming", "upcoming"), received=262746 + 394119)
    fig = demand_letter_figures(form, ISSUED)
    assert fig["overdue"] == 0
    assert fig["demand"] == 656865 and fig["payable_by"] == date(2026, 12, 1)


def test_next_instalment_is_earliest_by_date_even_if_listed_later():
    form = _plan(statuses=("paid", "paid", "upcoming", "upcoming"), received=262746 + 394119)
    form["payment_schedule"].reverse()
    assert demand_letter_figures(form, ISSUED)["payable_by"] == date(2026, 12, 1)


def test_unpaid_rows_without_due_dates_are_not_overdue():
    form = {"total_consideration": 300000, "amount_received": 0,
            "payment_schedule": [_row("A", None, 100000, "due"), _row("B", "garbage", 200000, "due")]}
    fig = demand_letter_figures(form, ISSUED)
    assert fig["overdue"] == 0 and fig["demand"] == 100000
    assert fig["payable_by"] == date(2026, 11, 12)
    _text(form)


@pytest.mark.parametrize("issued", ["2026-11-05", date(2026, 11, 5), datetime(2026, 11, 5, 18, 0)])
def test_issue_date_formats(issued):
    text, _ = _text(_plan(), issued=issued)
    assert "Dated: 05 Nov 2026" in text


def test_issue_date_defaults_to_today():
    text, _ = _text(_plan(), issued=None)
    assert f"Dated: {date.today().strftime('%d %b %Y')}" in text


def test_leap_day_due_date_renders():
    text, _ = _text(_plan(statuses=("due", "due", "due", "due")))
    assert "29 Feb 2024" in text


# --------------------------------------------------------------------------
# Plot number, project letterhead, customer identity
# --------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["unit_number", "unitNumber", "unit_no", "unitNo",
                                 "plot_number", "plotNumber", "plot_no", "plotNo"])
def test_plot_number_read_from_every_key_variant(key):
    form = _plan()
    form.pop("unitNo")
    form[key] = "B-12"
    text, _ = _text(form)
    assert "Plot No. B-12" in text


def test_missing_plot_and_area_print_dash():
    form = _plan()
    form.pop("unitNo"); form.pop("plotAreaSqYd")
    text, _ = _text(form)
    assert "Plot No. - having Area - Sq.Yrd." in text


@pytest.mark.parametrize("form, company", [
    ({"projectId": "suraksha-enclave"}, "Rama Krishna Buildwell Pvt. Ltd."),
    ({"project_name": "Suraksha Enclave"}, "Rama Krishna Buildwell Pvt. Ltd."),
    ({"township_label": "Suraksha Enclave · Ganaur"}, "Rama Krishna Buildwell Pvt. Ltd."),
    ({"project": "SURAKSHA"}, "Rama Krishna Buildwell Pvt. Ltd."),
    ({"projectId": "ops-divine-greens"}, "KCG Resorts Pvt. Ltd."),
    ({"project_name": "OPS Divine Greens"}, "KCG Resorts Pvt. Ltd."),
    ({}, "KCG Resorts Pvt. Ltd."),
])
def test_letterhead_follows_project(form, company):
    assert project_profile(form)["company"] == company


def test_suraksha_letter_carries_only_its_own_bank_and_tax_details():
    text, _ = _text(_plan())
    for expected in ("06AADCR1976J1Z4", "U45201DL2005PTC140802", "165105003309",
                     "RAMA KRISHNA BUILDWELL PVT LTD", "ICICI Bank", "ICIC0001651",
                     "GURGAON - UNIVERSAL TRADE TOWER, HARYANA", "State Code: 06",
                     'in favour of "Rama Krishna Buildwell Pvt. Ltd."', "@18% p.a.",
                     "For Rama Krishna Buildwell Pvt. Ltd.",
                     "This is system generated document no signature required."):
        assert expected in text, expected
    for wrong in ("KCG", "06AAECK2303D1Z8", "370305500283", "ICIC0003703", "Karnal"):
        assert wrong not in text, wrong


def test_ops_letter_carries_only_kcg_details():
    form = {**_plan(), "projectId": "ops-divine-greens"}
    text, _ = _text(form)
    assert "KCG Resorts Pvt. Ltd." in text and "06AAECK2303D1Z8" in text and "370305500283" in text
    assert "Rama Krishna" not in text and "165105003309" not in text
    assert '"OPS Divine Greens" situated at Sec-16, Taraori' in text


def test_missing_logo_file_still_renders(monkeypatch):
    monkeypatch.setitem(dl._SURAKSHA_PROFILE, "logo", "Z:/does/not/exist.png")
    text, _ = _text(_plan())
    assert "Rama Krishna Buildwell Pvt. Ltd." in text


@pytest.mark.parametrize("gender, prefix", [
    ("Male", "MR. "), ("male", "MR. "), ("M", "MR. "), ("Female", "MS. "), ("F", "MS. "),
    ("", ""), ("Other", ""), (None, ""),
])
def test_salutation_from_gender(gender, prefix):
    form = {**_plan(), "gender": gender}
    text, _ = _text(form)
    assert f"{prefix}DHEERAJ NIRWAL" in text
    if not prefix:
        assert "MR. DHEERAJ" not in text and "MS. DHEERAJ" not in text


def test_missing_name_and_address():
    form = _plan()
    form.pop("applicantName"); form.pop("permanentAddress")
    text, _ = _text(form)
    assert "R/o-" not in text


@pytest.mark.parametrize("key", ["applicant_address", "address", "correspondenceAddress", "permanentAddress"])
def test_address_key_variants(key):
    form = _plan()
    form.pop("permanentAddress")
    form[key] = "Flat 9, Model Town, Panipat"
    text, _ = _text(form)
    assert "R/o-Flat 9, Model Town, Panipat" in text


def test_multiline_address_is_collapsed_to_one_line():
    form = {**_plan(), "permanentAddress": "H.No. 5\nWard 3\r\n  Ganaur"}
    text, _ = _text(form)
    assert "R/o-H.No. 5 Ward 3 Ganaur" in text


def test_non_latin_characters_do_not_crash():
    form = {**_plan(), "applicantName": "धीरज Nirwal", "permanentAddress": "गाँव भैंसवाल, सोनीपत — 131409"}
    text, pages = _text(form)
    assert pages == 1 and "NIRWAL" in text


# --------------------------------------------------------------------------
# Layout stress
# --------------------------------------------------------------------------

def test_very_long_address_and_name_wrap_on_one_page():
    form = {**_plan(), "applicantName": "Venkata Satya Narayana Murthy Subrahmanya Raghunatha",
            "permanentAddress": "House No. 1234, Street 56, " * 8 + "Sonipat, Haryana-131409"}
    _, pages = _text(form)
    assert pages == 1


def test_crore_amounts_and_long_words_fit():
    amount = 999999999
    form = {"projectId": "suraksha-enclave", "unitNo": "X-1", "total_consideration": amount,
            "amount_received": 0, "payment_schedule": [_row("On Booking", "2025-01-01", amount, "due")]}
    text, pages = _text(form)
    assert "99,99,99,999" in text and pages == 1
    assert "Crore" in text


def test_long_monthly_plan_flows_onto_more_pages():
    rows = [_row(f"Instalment {i + 1}", f"20{25 + i // 12}-{i % 12 + 1:02d}-10", 100000, "due")
            for i in range(60)]
    form = {"projectId": "suraksha-enclave", "unitNo": "M-1", "total_consideration": 6000000,
            "amount_received": 0, "payment_schedule": rows}
    text, pages = _text(form)
    assert pages >= 2
    assert "Instalment 60" in text and "Total Overdue Amount" in text
    assert text.count("E-Mail : sales1@divinevisioninfra.com") == pages   # footer on every page


def test_custom_head_on_a_row_is_used():
    form = _plan()
    form["payment_schedule"][1]["head"] = "PLC"
    text, _ = _text(form)
    assert "PLC" in text


# --------------------------------------------------------------------------
# Never raises
# --------------------------------------------------------------------------

@pytest.mark.parametrize("form", [None, {}, [], "string", 42, {"payment_schedule": {"not": "a list"}}])
def test_garbage_form_data_never_raises(form):
    assert generate_demand_letter_pdf(form, "C1", issued_on=ISSUED)[:5] == b"%PDF-"


def test_render_failure_returns_fallback_pdf(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("render broke")
    monkeypatch.setattr(dl, "_render_demand_letter_pdf", boom)
    pdf = generate_demand_letter_pdf(_plan(), "C42")
    text = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
    assert "C42" in text and "could not be generated" in text


# --------------------------------------------------------------------------
# Customer download path (serviceDocument.get_demand_letter)
# --------------------------------------------------------------------------

def _doc_service(form, booking_unit="C61", milestones=None, milestone_error=None, booking_error=None):
    doc = MagicMock(owner_id="C1", owner_role="customer", payment_id="pay-1",
                    document_type="project_booking_application", form_data=json.dumps(form))
    persistence = MagicMock()
    persistence.get_by_id.return_value = doc
    svc = serviceDocument(persistence)
    svc._booking_persistence = MagicMock()
    if booking_error:
        svc._booking_persistence.get_by_payment_id.side_effect = booking_error
    else:
        svc._booking_persistence.get_by_payment_id.return_value = (
            SimpleNamespace(unit_number=booking_unit) if booking_unit is not None else None)
    svc._milestone_persistence = MagicMock()
    if milestone_error:
        svc._milestone_persistence.list_for_booking.side_effect = milestone_error
    else:
        svc._milestone_persistence.list_for_booking.return_value = milestones or []
    return svc


def _capture(monkeypatch):
    captured = {}
    monkeypatch.setattr(dl, "generate_demand_letter_pdf",
                        lambda form, cid, issued_on=None: captured.update(form=form) or b"%PDF-x")
    return captured


def _milestone(no, status, amount=100000, due=date(2026, 1, 1)):
    return SimpleNamespace(label=f"M{no}", percent=Decimal("10.000"), due_date=due,
                           amount=Decimal(f"{amount}.00"), status=status)


def test_customer_path_booking_row_plot_number_wins(monkeypatch):
    captured = _capture(monkeypatch)
    _doc_service({**_plan(), "unitNo": "OLD-1"}, booking_unit="C61").get_demand_letter(
        "d1", requester_id="C1", requester_role="customer")
    assert captured["form"]["unit_number"] == "C61"


def test_customer_path_booking_project_decides_company(monkeypatch):
    # Old form without projectId -> booking row says Suraksha -> Rama Krishna letterhead.
    form = _plan()
    form.pop("projectId")
    svc = _doc_service(form)
    svc._booking_persistence.get_by_payment_id.return_value = SimpleNamespace(
        unit_number="C61", project_name="Suraksha Enclave")
    pdf, _ = svc.get_demand_letter("d1", requester_id="C1", requester_role="customer")
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    assert "Rama Krishna Buildwell Pvt. Ltd." in text and "06AADCR1976J1Z4" in text
    assert "Plot No. C61" in text and '"Suraksha Enclave"' in text


def test_customer_path_booking_project_beats_wrong_form_project(monkeypatch):
    captured = _capture(monkeypatch)
    svc = _doc_service({**_plan(), "projectId": "ops-divine-greens"})
    svc._booking_persistence.get_by_payment_id.return_value = SimpleNamespace(
        unit_number="C61", project_name="Suraksha Enclave")
    svc.get_demand_letter("d1", requester_id="C1", requester_role="customer")
    assert project_profile(captured["form"])["company"] == "Rama Krishna Buildwell Pvt. Ltd."


def test_customer_path_uses_document_project_id_when_no_booking_row(monkeypatch):
    captured = _capture(monkeypatch)
    form = _plan()
    form.pop("projectId")
    svc = _doc_service(form, booking_unit=None)
    svc._persistence.get_by_id.return_value.project_id = "suraksha-enclave"
    svc.get_demand_letter("d1", requester_id="C1", requester_role="customer")
    assert project_profile(captured["form"])["company"] == "Rama Krishna Buildwell Pvt. Ltd."
    assert captured["form"]["unitNo"] == "C61"      # plot still from the form


def test_multi_plot_customer_gets_each_documents_own_plot(monkeypatch):
    """Two plots, two booking documents: each letter looks up the booking row by
    THAT document's payment, never the customer's latest booking."""
    captured = _capture(monkeypatch)
    rows = {"pay-A": SimpleNamespace(unit_number="A-7", project_name="OPS Divine Greens"),
            "pay-B": SimpleNamespace(unit_number="C61", project_name="Suraksha Enclave")}
    for pay, unit, company in (("pay-A", "A-7", "KCG Resorts Pvt. Ltd."),
                               ("pay-B", "C61", "Rama Krishna Buildwell Pvt. Ltd.")):
        svc = _doc_service(_plan())
        svc._persistence.get_by_id.return_value.payment_id = pay
        svc._booking_persistence.get_by_payment_id.side_effect = rows.get
        svc.get_demand_letter("d1", requester_id="C1", requester_role="customer")
        assert captured["form"]["unit_number"] == unit
        assert project_profile(captured["form"])["company"] == company


@pytest.mark.parametrize("same, expected", [
    (True, "R/o-Permanent House, Sonipat"), ("true", "R/o-Permanent House, Sonipat"),
    (False, "R/o-Office 4, Delhi"), (None, "R/o-Office 4, Delhi"),
])
def test_correspondence_vs_permanent_address(same, expected):
    form = {**_plan(), "permanentAddress": "Permanent House, Sonipat",
            "correspondenceAddress": "Office 4, Delhi", "correspondenceSameAsPermanent": same}
    text, _ = _text(form)
    assert expected in text


def test_customer_name_and_plot_exactly_as_booked():
    text, _ = _text({**_plan(), "applicantName": "  Dheeraj   Nirwal ", "unitNo": " C61 "})
    assert "Plot No. C61 having Area 119.43 Sq.Yrd." in text
    assert "DHEERAJ" in text and "NIRWAL" in text


def test_customer_path_falls_back_to_form_plot_when_booking_lookup_fails(monkeypatch):
    captured = _capture(monkeypatch)
    svc = _doc_service(_plan(), booking_error=RuntimeError("db down"))
    svc.get_demand_letter("d1", requester_id="C1", requester_role="customer")
    assert "unit_number" not in captured["form"] and captured["form"]["unitNo"] == "C61"


def test_customer_path_uses_live_milestones_with_decimal_amounts(monkeypatch):
    captured = _capture(monkeypatch)
    ms = [_milestone(1, "paid"), _milestone(2, "paid"), _milestone(3, "overdue"), _milestone(4, "upcoming")]
    _doc_service(_plan(), milestones=ms).get_demand_letter("d1", requester_id="C1", requester_role="customer")
    form = captured["form"]
    assert form["amount_received"] == 200000
    assert [r["amount"] for r in form["payment_schedule"]] == [100000] * 4
    assert form["payment_schedule"][0]["due_date"] == "2026-01-01"
    assert _labels(demand_letter_figures(form, ISSUED)) == ["M3", "M4"]


def test_customer_path_keeps_snapshot_when_milestones_missing_or_failing(monkeypatch):
    for kwargs in ({"milestones": []}, {"milestone_error": RuntimeError("db down")}):
        captured = _capture(monkeypatch)
        _doc_service(_plan(), **kwargs).get_demand_letter("d1", requester_id="C1", requester_role="customer")
        assert captured["form"]["payment_schedule"] == _plan()["payment_schedule"]
        assert captured["form"]["amount_received"] == 262746


def test_customer_path_end_to_end_pdf_hides_paid_milestones():
    ms = [SimpleNamespace(label=r["label"], percent=None, due_date=_to_date(r["due_date"]),
                          amount=r["amount"], status=s)
          for r, s in zip(_plan()["payment_schedule"], ("paid", "paid", "due", "upcoming"))]
    pdf, _ = _doc_service(_plan(), milestones=ms).get_demand_letter(
        "d1", requester_id="C1", requester_role="customer")
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    assert "Plot No. C61" in text
    assert "On Booking" not in text and "Within 45 days of booking" not in text
    assert "Within 90 days of booking" in text and "Within 360 days of booking" in text
    assert "6,56,865" in text                                  # received = two paid rows
