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
