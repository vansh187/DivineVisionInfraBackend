"""GET /admin/inventory/export: each plot with company, payment plan, buyer,
co-applicant and booking date, for the admin Inventory CSV export."""
import os
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import text

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"
os.environ["ADMIN_JWT_SECRET_KEY"] = "admintestsecret"

from Divinepersistence.persistence_db import PersistenceDB, engine
from Divinepersistence.persistence_booking import persistenceBooking
from Divinepersistence.persistence_customer import persistenceCustomer
from Divinepersistence.persistence_document import persistenceDocument
from Divinepersistence.persistence_inventory import persistenceInventory
from Divinepersistence.persistence_payment import persistencePayment
from DivineAPI.main import app
from DivineService.inventory_export import ExportLinks, buyer_fields, company_for_project, load_form

client = TestClient(app)
inventory = persistenceInventory()

SURAKSHA = "Suraksha Enclave"
OPS = "OPS Divine Greens"
IDS = {}


def _unit(project, number, status="available", locality="Sector-15", city="Ganaur"):
    return inventory.upsert_unit(
        id=str(uuid.uuid4()), project_name=project, city=city, locality=locality, unit_number=number,
        block=number.split("-")[0], width_mtr=7.45, length_mtr=13.404, area_sqmt=99.86, area_sqyd=119.43,
        status=status,
    ).id


def _customer(first, last, email, phone):
    return persistenceCustomer().create_user(
        username=f"exp_{uuid.uuid4().hex[:8]}", password_hash="x", first_name=first, last_name=last,
        email=email, phone=phone,
    ).id


def setup_module(module):
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()

    IDS["available"] = _unit(SURAKSHA, "C-1")

    # Offline buyer from the client's sheet: sold, linked by booked_by, form matched by plot number.
    offline_buyer = _customer("Dheeraj", "Nirwal", "dheeraj@gmail.com", "9876543210")
    IDS["offline"] = _unit(SURAKSHA, "C-12", status="sold")
    with engine.begin() as conn:
        conn.execute(text("UPDATE divine_project_inventory SET booked_by = :c, sale_source = 'offline' WHERE id = :i"),
                     {"c": offline_buyer, "i": IDS["offline"]})
    persistenceDocument().create_document(
        id=str(uuid.uuid4()), owner_id=offline_buyer, owner_role="customer",
        document_type="project_booking_application", storage_path="imported://t", project_id="suraksha-enclave",
        form_data={
            "projectId": "suraksha-enclave", "unitNo": "C12", "applicantName": "Mr. Dheeraj Nirwal",
            "phone": "9876543210", "alternate_phone": "9812345678", "email": "dheeraj@gmail.com",
            "permanentAddress": "H.No. 543, Sonipat", "coApplicantName": "Mrs. Pooja Nirwal",
            "coApplicantEmail": "pooja@gmail.com", "coApplicantPermanentAddress": "H.No. 543, Sonipat",
            "plot_address": "Plot No -C-12, Suraksha Enclave Sec-15, Ganaur",
        },
    )
    # Same buyer's form for a different plot must not leak onto C-12.
    persistenceDocument().create_document(
        id=str(uuid.uuid4()), owner_id=offline_buyer, owner_role="customer",
        document_type="project_booking_application", storage_path="imported://t2", project_id="suraksha-enclave",
        form_data={"projectId": "suraksha-enclave", "unitNo": "C-99", "applicantName": "Someone Else"},
    )

    # Website booking: payment with a payment plan, booking row, form attached to the payment.
    web_buyer = _customer("Neha", "Verma", "neha@example.com", "9000000601")
    IDS["web"] = _unit(OPS, "A-7", locality=None, city="Karnal")
    payment_id = str(uuid.uuid4())
    persistencePayment().create_payment(
        id=payment_id, owner_id=web_buyer, owner_role="customer", amount=100000, currency="INR", status="paid",
        purpose="plot_booking", inventory_id=IDS["web"],
        notes={"payment_plan": {"payment_plan_code": "PP2/CLP/10-40-50", "payment_plan_version": 1}},
    )
    assert inventory.hold_for_kyc_review(id=IDS["web"], payment_id=payment_id, customer_id=web_buyer)
    persistenceBooking().create_booking(payment_id=payment_id, inventory_id=IDS["web"], customer_id=web_buyer,
                                        project_name=OPS, unit_number="A-7", amount=100000)
    persistenceDocument().create_document(
        id=str(uuid.uuid4()), owner_id=web_buyer, owner_role="customer", document_type="booking_application",
        storage_path="s3://x", project_id="ops-divine-greens", payment_id=payment_id,
        form_data={
            "projectId": "ops-divine-greens", "unitNo": "A-7", "applicantName": "Neha Verma", "mobile": "9000000601",
            "email": "neha@example.com", "correspondenceAddress": "Flat 4, Model Town, Karnal",
            "coApplicantEmail": "rahul@example.com", "coApplicantCorrespondenceSameAsPermanent": True,
            "coApplicantPermanentAddress": "12 Civil Lines, Panipat",
            "coApplicantCorrespondenceAddress": "ignored because same as permanent",
        },
    )


def _admin():
    payload = {"sub": "DV9100", "username": "export_admin@example.com", "role": "admin",
               "exp": datetime.now(timezone.utc) + timedelta(hours=1)}
    return {"Authorization": f"Bearer {jwt.encode(payload, 'admintestsecret', algorithm='HS256')}"}


def _rows(**params):
    res = client.get("/admin/inventory/export", params={"page_size": 100, **params}, headers=_admin())
    assert res.status_code == 200, res.text
    return {r["unit_number"]: r for r in res.json()["items"]}


def test_admin_only_and_not_mistaken_for_a_plot_id():
    assert client.get("/admin/inventory/export").status_code == 401
    assert client.get("/admin/inventory/export", headers=_admin()).status_code == 200


def test_available_plot_has_plot_and_company_but_no_buyer():
    row = _rows(project_name=SURAKSHA)["C-1"]
    assert row["status"] == "available" and row["width_mtr"] == 7.45 and row["area_sqyd"] == 119.43
    assert row["company_name"] == "Rama Krishna Buildwell Pvt. Ltd."
    assert "Ganaur" in row["company_address"]
    assert row["plot_address"] == "Plot No C-1, Suraksha Enclave, Sector-15, Ganaur"
    for field in ("client_name", "phone", "email", "payment_plan", "booking_date", "co_applicant_email"):
        assert row[field] is None


def test_offline_buyer_comes_from_their_form_for_this_plot():
    row = _rows(project_name=SURAKSHA)["C-12"]
    assert row["client_name"] == "Mr. Dheeraj Nirwal"
    assert (row["phone"], row["alternative_number"]) == ("9876543210", "9812345678")
    assert row["email"] == "dheeraj@gmail.com"
    assert row["address"] == "H.No. 543, Sonipat"
    assert (row["co_applicant_email"], row["co_applicant_address"]) == ("pooja@gmail.com", "H.No. 543, Sonipat")
    assert row["plot_address"] == "Plot No -C-12, Suraksha Enclave Sec-15, Ganaur"
    assert row["payment_plan"] is None and row["booking_date"] is None


def test_website_booking_has_plan_booking_date_and_co_applicant():
    row = _rows(project_name=OPS)["A-7"]
    assert row["status"] == "pending_kyc_review"
    assert row["company_name"] == "KCG Resorts Pvt. Ltd."
    assert row["payment_plan"] == "PP2/CLP/10-40-50"
    assert row["client_name"] == "Neha Verma" and row["phone"] == "9000000601"
    assert row["address"] == "Flat 4, Model Town, Karnal"
    assert row["co_applicant_address"] == "12 Civil Lines, Panipat"
    # The booking row's created date, as YYYY-MM-DD (within a day of now, whatever the time zone).
    booked = datetime.fromisoformat(row["booking_date"]).date()
    assert abs((booked - datetime.now(timezone.utc).date()).days) <= 1
    assert row["plot_address"] == "Plot No A-7, OPS Divine Greens, Karnal"


def test_filters_and_paging_match_the_list():
    assert set(_rows(project_name=SURAKSHA, status="sold")) == {"C-12"}
    res = client.get("/admin/inventory/export", params={"project_name": SURAKSHA, "page_size": 1},
                     headers=_admin()).json()
    assert res["pagination"]["total_items"] == 2 and res["pagination"]["total_pages"] == 2
    assert client.get("/admin/inventory/export", params={"page_size": 101}, headers=_admin()).status_code == 422


def test_customer_account_fills_what_the_form_lacks():
    links = ExportLinks({"customers": [{"id": "C9", "first_name": "Aman", "last_name": "Gupta",
                                        "email": "aman@example.com", "phone": "9000000700"}]})
    fields = buyer_fields({"unit_number": "B-2", "project_name": OPS, "booked_by": "C9"}, links)
    assert (fields["client_name"], fields["email"], fields["phone"]) == ("Aman Gupta", "aman@example.com", "9000000700")


def test_unknown_project_gets_no_company_and_bad_form_json_is_ignored():
    assert company_for_project("Palm County") == ("", "")
    assert load_form("{not json") == {} and load_form('["a"]') == {}


# ---- review fixes -------------------------------------------------------------
def test_booking_date_is_the_payment_date_in_ist_like_the_letters():
    # 01:30 IST on 5 Nov is 20:00 UTC on 4 Nov - the letter says 05-11-2026.
    links = ExportLinks({"payments": [{"payment_id": "P1", "customer_id": "C1", "notes": None,
                                       "payment_created_date": datetime(2026, 11, 4, 20, 0)}]})
    unit = {"unit_number": "A-1", "project_name": OPS, "booked_payment_id": "P1",
            "booked_at": datetime(2026, 11, 9, 10, 0)}       # e.g. the form upload day
    assert buyer_fields(unit, links)["booking_date"] == "2026-11-05"
    # Same for a string timestamp (sqlite), with or without an offset.
    links.payments["P1"]["payment_created_date"] = "2026-11-04T20:00:00+00:00"
    assert buyer_fields(unit, links)["booking_date"] == "2026-11-05"


def test_paid_plot_without_a_booking_row_still_gets_buyer_and_payment_date():
    links = ExportLinks({
        "payments": [{"payment_id": "P2", "customer_id": None, "owner_id": "C7", "owner_role": "customer",
                      "notes": None, "payment_created_date": datetime(2026, 10, 1, 6, 0)}],
        "customers": [{"id": "C7", "first_name": "Isha", "last_name": "Rao", "email": "isha@example.com",
                       "phone": "9000000777"}],
    })
    fields = buyer_fields({"unit_number": "A-2", "project_name": OPS, "booked_payment_id": "P2"}, links)
    assert fields["client_name"] == "Isha Rao" and fields["booking_date"] == "2026-10-01"


def test_offline_buyer_date_comes_from_their_form_with_the_letter_keys():
    doc = {"owner_id": "C8", "project_id": "suraksha-enclave",
           "form_data": {"unitNo": "C-5", "date": "05.11.2026", "coApplicantCorrespondenceSameAsPermanent": "y",
                         "coApplicantPermanentAddress": "Perm", "coApplicantCorrespondenceAddress": "Corr"}}
    links = ExportLinks({"forms_by_owner": [doc]})
    fields = buyer_fields({"unit_number": "C-5", "project_name": SURAKSHA, "booked_by": "C8",
                           "booked_at": datetime(2026, 12, 1)}, links)
    assert fields["booking_date"] == "2026-11-05"           # 'date' is a demand-letter key
    assert fields["co_applicant_address"] == "Perm"         # 'y' is truthy, as on the letter


def test_form_matches_when_any_named_project_is_the_plots_project():
    from DivineService.inventory_export import form_matches_unit
    unit = {"unit_number": "A-7", "project_name": OPS}
    assert form_matches_unit({"project_id": "divine-greens", "form_data": {"unitNo": "A7"}}, unit)
    assert form_matches_unit({"project_id": "wrong-id", "form_data": {"unitNo": "A-7",
                                                                      "projectId": "ops-divine-greens"}}, unit)
    assert form_matches_unit({"project_id": None, "form_data": {"unitNo": "A-7"}}, unit)
    assert not form_matches_unit({"project_id": "suraksha-enclave", "form_data": {"unitNo": "A-7"}}, unit)
    assert not form_matches_unit({"project_id": "ops-divine-greens", "form_data": {"unitNo": "A-8"}}, unit)


def test_text_a_spreadsheet_would_run_is_made_safe():
    from DivineService.inventory_export import csv_safe, safe_row
    assert csv_safe('=HYPERLINK("http://evil","x")') == '\'=HYPERLINK("http://evil","x")'
    assert [csv_safe(v) for v in ("+919876543210", "-1", "@SUM(A1)", "\tx", "Neha")] == [
        "'+919876543210", "'-1", "'@SUM(A1)", "'\tx", "Neha"]
    assert safe_row({"a": 7.45, "b": None, "c": "=1+1"}) == {"a": 7.45, "b": None, "c": "'=1+1"}


def test_paging_across_projects_never_repeats_or_skips_a_plot():
    _unit(OPS, "C-12")                                        # same plot number as Suraksha's C-12
    first = client.get("/admin/inventory/export", params={"page_size": 100}, headers=_admin()).json()
    expected = [r["id"] for r in first["items"]]
    paged = []
    for page in range(1, first["pagination"]["total_items"] + 1):
        res = client.get("/admin/inventory/export", params={"page": page, "page_size": 1}, headers=_admin())
        paged.extend(r["id"] for r in res.json()["items"])
    assert paged == expected and len(set(paged)) == len(paged)


def test_csv_download_is_one_file_with_header_and_safe_cells():
    import csv as csv_module
    import io
    assert client.get("/admin/inventory/export.csv").status_code == 401
    res = client.get("/admin/inventory/export.csv", params={"project_name": SURAKSHA}, headers=_admin())
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("text/csv")
    assert res.headers["content-disposition"].startswith('attachment; filename="inventory-')
    body = res.content.decode("utf-8")
    assert body.startswith("﻿")
    lines = list(csv_module.reader(io.StringIO(body.lstrip("﻿"))))
    header, rows = lines[0], lines[1:]
    assert header[:4] == ["Plot ID", "Project", "Block", "Plot No"]
    by_plot = {r[header.index("Plot No")]: dict(zip(header, r)) for r in rows}
    assert set(by_plot) == {"C-1", "C-12"}
    assert by_plot["C-12"]["Client Name"] == "Mr. Dheeraj Nirwal"
    assert by_plot["C-1"]["Client Name"] == "" and by_plot["C-1"]["Width (m)"] == "7.45"
    assert by_plot["C-1"]["Company Name"] == "Rama Krishna Buildwell Pvt. Ltd."
