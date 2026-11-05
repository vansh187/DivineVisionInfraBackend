import os
from unittest.mock import MagicMock
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_db.sqlite")
os.environ.setdefault("JWT_SECRET_KEY", "testsecret")

from DivineService.service_demand_letter import generate_demand_letter_pdf
from DivineService.service_document import serviceDocument

_FD = {
    "applicant_name": "Vansh Duggal", "father_name": "Ajit Duggal",
    "address": "B-1050, Gurugram", "project_name": "OPS Divine Greens",
    "plot_number": "C-14", "plot_area_sq_yd": "131.43", "booking_date": "2026-09-07",
    "total_consideration": 10000000, "amount_received": 2000000,
    "payment_schedule": [
        {"label": "On Booking", "due_date": "2026-09-07", "due_days": 0, "amount": 1000000, "percent": 10, "status": "paid"},
        {"label": "Within 45 days of booking", "due_date": "2026-10-22", "due_days": 45, "amount": 1500000, "percent": 15, "status": "due"},
        {"label": "Within 90 days of booking", "due_date": "2026-12-06", "due_days": 90, "amount": 2500000, "percent": 25, "status": "due"},
        {"label": "Within 180 days of booking", "due_date": "2027-03-06", "due_days": 180, "amount": 2500000, "percent": 25, "status": "due"},
        {"label": "Within 270 days of booking", "due_date": "2027-06-04", "due_days": 270, "amount": 2500000, "percent": 25, "status": "due"},
    ],
}


def test_generate_demand_letter_pdf_from_stored_form_data():
    pdf = generate_demand_letter_pdf(_FD, "C14144")
    assert pdf[:5] == b"%PDF-" and len(pdf) > 1500


def test_generate_demand_letter_recomputes_schedule_when_missing():
    fd = {k: v for k, v in _FD.items() if k != "payment_schedule"}
    pdf = generate_demand_letter_pdf(fd, "C14144")   # only total + received present
    assert pdf[:5] == b"%PDF-"


def test_generate_demand_letter_never_raises_on_empty_form_data():
    assert generate_demand_letter_pdf({}, "C00001")[:5] == b"%PDF-"
    assert generate_demand_letter_pdf(None, "C00001")[:5] == b"%PDF-"


def _pdf_text(pdf: bytes) -> str:
    import io
    from pypdf import PdfReader
    return "".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)


# Camel-case keys exactly as the booking form (documentStore.ts) stores them.
_SURAKSHA_FD = {
    "projectId": "suraksha-enclave", "applicantName": "Dheeraj Nirwal", "gender": "Male",
    "unitNo": "C61", "plotAreaSqYd": "119.43",
    "permanentAddress": "H.No. 543 Bhainswal Kalan Mithan (68) Sonipat, Haryana-131409",
    "total_consideration": 2627460, "amount_received": 656865,
    "payment_schedule": [
        {"label": "On Booking", "due_date": "2024-02-29", "amount": 262746, "status": "paid"},
        {"label": "Within 45 days of booking", "due_date": "2024-04-14", "amount": 394119, "status": "paid"},
        {"label": "Within 90 days of booking", "due_date": "2024-05-29", "amount": 656865, "status": "due"},
        {"label": "Within 360 days of booking", "due_date": "2027-02-23", "amount": 1313730, "status": "upcoming"},
    ],
}


def test_suraksha_letter_shows_plot_number_and_rama_krishna_details():
    from datetime import date
    text = _pdf_text(generate_demand_letter_pdf(_SURAKSHA_FD, "C00001", issued_on=date(2026, 11, 5)))
    assert "Plot No. C61" in text and "119.43" in text and '"Suraksha Enclave"' in text
    assert "MR. DHEERAJ NIRWAL" in text
    for expected in ("Rama Krishna Buildwell Pvt. Ltd.", "06AADCR1976J1Z4", "U45201DL2005PTC140802",
                     "165105003309", "RAMA KRISHNA BUILDWELL PVT LTD", "ICIC0001651",
                     "GURGAON - UNIVERSAL TRADE TOWER, HARYANA", "Ganaur, Sonipat",
                     "crm3@divinevisioninfra.com"):
        assert expected in text, expected
    assert "KCG" not in text and "370305500283" not in text


def test_paid_instalments_are_left_out_and_totals_follow_the_reference():
    from datetime import date
    from DivineService.service_demand_letter import demand_letter_figures
    fig = demand_letter_figures(_SURAKSHA_FD, date(2026, 11, 5))
    assert [r["label"] for r in fig["rows"]] == ["Within 90 days of booking", "Within 360 days of booking"]
    assert fig["total_plot"] == 2627460 and fig["received"] == 656865
    assert fig["outstanding"] == 1970595
    assert fig["overdue"] == 656865                      # only the past-due unpaid row
    assert fig["demand"] == 656865 and fig["payable_by"] == date(2026, 11, 12)

    text = _pdf_text(generate_demand_letter_pdf(_SURAKSHA_FD, "C00001", issued_on=date(2026, 11, 5)))
    assert "On Booking" not in text and "Within 45 days" not in text
    assert "Total Overdue Amount" in text and "6,56,865" in text
    assert "Rs. 6,56,865/-" in text and "12 Nov 2026" in text


def test_snapshot_without_statuses_hides_instalments_covered_by_received_amount():
    from datetime import date
    from DivineService.service_demand_letter import demand_letter_figures
    fd = {**_SURAKSHA_FD, "amount_received": 700000,   # covers rows 1+2 (6,56,865), not row 3
          "payment_schedule": [{k: v for k, v in r.items() if k != "status"}
                               for r in _SURAKSHA_FD["payment_schedule"]]}
    fig = demand_letter_figures(fd, date(2026, 11, 5))
    assert [r["label"] for r in fig["rows"]] == ["Within 90 days of booking", "Within 360 days of booking"]


def test_every_instalment_paid_shows_no_rows_and_nothing_due():
    from datetime import date
    from DivineService.service_demand_letter import demand_letter_figures
    fd = {**_SURAKSHA_FD, "amount_received": 2627460,
          "payment_schedule": [{**r, "status": "paid"} for r in _SURAKSHA_FD["payment_schedule"]]}
    fig = demand_letter_figures(fd, date(2026, 11, 5))
    assert fig["rows"] == [] and fig["outstanding"] == 0 and fig["overdue"] == 0
    assert "On Booking" not in _pdf_text(generate_demand_letter_pdf(fd, "C1", issued_on=date(2026, 11, 5)))


def test_customer_demand_letter_uses_live_milestone_statuses(monkeypatch):
    """An instalment paid after booking (milestone row 'paid') must drop out of
    the customer's letter even though the form snapshot still says 'due'."""
    import json
    from datetime import date
    from types import SimpleNamespace
    import DivineService.service_demand_letter as demand_module
    doc = MagicMock(owner_id="C1", owner_role="customer", payment_id="pay-1",
                    document_type="project_booking_application", form_data=json.dumps(_FD))
    svc = _svc(doc)
    svc._booking_persistence = MagicMock()
    svc._booking_persistence.get_by_payment_id.return_value = SimpleNamespace(unit_number="C-14")
    svc._milestone_persistence = MagicMock()
    svc._milestone_persistence.list_for_booking.return_value = [
        SimpleNamespace(label=r["label"], percent=r["percent"], due_date=date.fromisoformat(r["due_date"]),
                        amount=r["amount"], status="paid" if i < 2 else "upcoming")
        for i, r in enumerate(_FD["payment_schedule"])
    ]
    captured = {}
    monkeypatch.setattr(demand_module, "generate_demand_letter_pdf",
                        lambda form, cid, issued_on=None: captured.update(form=form) or b"%PDF-x")
    svc.get_demand_letter("d1", requester_id="C1", requester_role="customer")

    svc._milestone_persistence.list_for_booking.assert_called_once_with("d1")
    form = captured["form"]
    assert form["unit_number"] == "C-14"
    assert form["amount_received"] == 2500000
    fig = demand_module.demand_letter_figures(form, date(2026, 11, 5))
    assert "On Booking" not in [r["label"] for r in fig["rows"]]
    assert "Within 45 days of booking" not in [r["label"] for r in fig["rows"]]
    assert len(fig["rows"]) == 3


def test_ops_letter_keeps_kcg_details():
    text = _pdf_text(generate_demand_letter_pdf({**_FD, "projectId": "ops-divine-greens"}, "C1"))
    assert "KCG Resorts Pvt. Ltd." in text and "06AAECK2303D1Z8" in text and "Plot No. C-14" in text


def _svc(doc):
    persistence = MagicMock()
    persistence.get_by_id.return_value = doc
    return serviceDocument(persistence)


def test_get_demand_letter_owner_and_type_checks():
    svc = _svc(None)
    try:
        svc.get_demand_letter("d1", requester_id="C1", requester_role="customer"); assert False
    except ValueError as e:
        assert str(e) == "not_found"

    svc = _svc(MagicMock(owner_id="C9", owner_role="customer", document_type="project_booking_application"))
    try:
        svc.get_demand_letter("d1", requester_id="C1", requester_role="customer"); assert False
    except PermissionError:
        pass

    svc = _svc(MagicMock(owner_id="C1", owner_role="customer", document_type="aadhaar_front", form_data={}))
    try:
        svc.get_demand_letter("d1", requester_id="C1", requester_role="customer"); assert False
    except ValueError as e:
        assert str(e) == "not_a_booking_application"


def test_get_demand_letter_happy_path_returns_pdf_bytes():
    import json
    doc = MagicMock(owner_id="C1", owner_role="customer",
                    document_type="project_booking_application", form_data=json.dumps(_FD))
    svc = _svc(doc)
    pdf, name = svc.get_demand_letter("d1", requester_id="C1", requester_role="customer")
    assert pdf[:5] == b"%PDF-"
    assert name == "demand-letter-d1.pdf"
