"""Payment plans of customers' plots in the admin Customers section: linking
the client's plan + booking date (+ prices, amount received) to an offline
buyer's plot, showing it per customer, and the demand letter."""
import os
import uuid
from datetime import date, datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"
os.environ["ADMIN_JWT_SECRET_KEY"] = "admintestsecret"

from Divinepersistence.persistence_db import PersistenceDB, engine
from Divinepersistence.persistence_customer import persistenceCustomer
from Divinepersistence.persistence_document import persistenceDocument
from Divinepersistence.persistence_inventory import persistenceInventory
from DivineAPI.main import app, RateLimitMiddleware
from DivineService.service_demand_letter import demand_letter_figures
from DivineService.service_payment_plans import sync_plans
from DivineService.service_plot_payment_terms import parse_booking_date, servicePlotPaymentTerms

client = TestClient(app)
SURAKSHA = "Suraksha Enclave"
OPS = "OPS Divine Greens"
PP1 = "PP1/FPP/10-15-25-25-15-10"
CLIENT_LABEL = "PL-10-15 -25- 25 -15 -10"           # as written in the client's sheet
T = uuid.uuid4().hex[:6].upper()                     # unique plot numbers per run (shared test DB)
B = {}


def _buyer(name, phone, project, plot, applicant):
    first, *rest = name.split()
    cid = persistenceCustomer().create_user(
        username=f"buyer_{uuid.uuid4().hex[:8]}", password_hash="x", first_name=first,
        last_name=" ".join(rest) or None, phone=phone).id
    unit_id = persistenceInventory().upsert_unit(
        id=str(uuid.uuid4()), project_name=project, city="Ganaur", unit_number=plot, area_sqmt=99.86,
        area_sqyd=119.43, status="sold").id
    slug = "suraksha-enclave" if project == SURAKSHA else "ops-divine-greens"
    doc_id = str(uuid.uuid4())
    persistenceDocument().create_document(
        id=doc_id, owner_id=cid, owner_role="customer", document_type="project_booking_application",
        storage_path=f"imported://t/{plot}", status="imported", project_id=slug,
        form_data={"projectId": slug, "project_name": project, "unitNo": plot, "applicantName": applicant,
                   "phone": phone, "mobile": phone, "permanentAddress": "H.No. 1, Ganaur",
                   "source": "offline_import", "finance_status": "pending"})
    with engine.begin() as conn:
        conn.execute(text("UPDATE divine_customer_users SET source = 'offline_import' WHERE id = :c"), {"c": cid})
        conn.execute(text("UPDATE divine_project_inventory SET booked_by = :c, sale_source = 'offline' "
                          "WHERE id = :u"), {"c": cid, "u": unit_id})
    return {"customer": cid, "doc": doc_id, "unit": unit_id, "plot": plot}


def setup_module(module):
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()
    # Always load PP1-PP4: the shared test database may still hold other modules'
    # plans (it can't be deleted while open), and seeding only fills an empty catalogue.
    sync_plans()
    B["asha"] = _buyer("Asha Verma", "9000000101", SURAKSHA, f"D7{T}", "Mrs. Asha Verma")
    B["rohit"] = _buyer("Rohit Mehra", "9000000102", SURAKSHA, f"D8{T}", "Mr. Rohit Mehra")
    B["karan"] = _buyer("Karan Malhotra", "9000000103", SURAKSHA, f"D9{T}", "Mr. Karan")
    B["ops"] = _buyer("Ravi Ops", "9000000001", OPS, f"A1{T}", "Mr. Ravi")
    # A website booking: its terms come from its payment, not from here.
    B["web"] = _buyer("Web Buyer", "9000000002", SURAKSHA, f"C99{T}", "Ms. Web")
    with engine.begin() as conn:
        conn.execute(text("UPDATE divine_documents SET status = 'generated' WHERE id = :d"), {"d": B["web"]["doc"]})


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    RateLimitMiddleware.reset_all()


def _admin():
    payload = {"sub": "DV9200", "username": "plans_admin@example.com", "role": "admin",
               "exp": datetime.now(timezone.utc) + timedelta(hours=1)}
    return {"Authorization": f"Bearer {jwt.encode(payload, 'admintestsecret', algorithm='HS256')}"}


def _put(buyer, **body):
    return client.put(f"/admin/customers/{buyer['customer']}/plots/{buyer['doc']}/payment-terms",
                      json=body, headers=_admin())


def _detail(buyer):
    res = client.get(f"/admin/customers/{buyer['customer']}", headers=_admin())
    assert res.status_code == 200, res.text
    return res.json()


def _milestones(doc_id):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(
            "SELECT * FROM divine_payment_milestones WHERE booking_id = :b ORDER BY milestone_no"),
            {"b": doc_id}).mappings()]


# ---- linking a plan ---------------------------------------------------------
def test_endpoints_are_admin_only():
    b = B["rohit"]
    assert client.get(f"/admin/customers/{b['customer']}").status_code == 401
    assert client.put(f"/admin/customers/{b['customer']}/plots/{b['doc']}/payment-terms",
                      json={"payment_plan": "PP1", "booking_date": "2026-04-18"}).status_code == 401


def test_plan_and_booking_date_without_price_wait_for_prices():
    b = B["rohit"]
    before = _detail(b)["plots"][0]
    assert before["terms_status"] == "plan_needed" and before["payment_plan"] is None

    res = _put(b, payment_plan=CLIENT_LABEL, booking_date="2026-04-18")
    assert res.status_code == 200, res.text
    plot = res.json()
    assert plot["payment_plan"]["code"] == PP1 and plot["payment_plan"]["short_code"] == "PP1"
    assert plot["booking_date"] == "2026-04-18"
    assert plot["terms_status"] == "price_needed"
    assert plot["demand_letter_available"] is False and plot["schedule"] == []
    assert _milestones(b["doc"]) == []

    letter = client.get(f"/admin/customers/{b['customer']}/plots/{b['doc']}/demand-letter", headers=_admin())
    assert letter.status_code == 409 and letter.json()["detail"] == "payment_details_pending"


def test_prices_build_the_schedule_and_mark_received_oldest_first():
    b = B["asha"]
    # Received: 10% + 15% of 50,00,000 in full, and 1,00,000 of the third instalment.
    res = _put(b, payment_plan="PP1", booking_date="2026-04-20", sale_price=5000000, plc=100000,
               ifms=50000, cam=75000, amount_received=1350000)
    assert res.status_code == 200, res.text
    plot = res.json()
    assert plot["terms_status"] == "complete" and plot["demand_letter_available"] is True
    assert plot["total"] == 5225000 and plot["amount_received"] == 1350000
    assert plot["outstanding"] == 5225000 - 1350000
    rows = plot["schedule"]
    assert [r["amount"] for r in rows] == [500000, 750000, 1350000, 1300000, 825000, 500000]
    assert [r["status"] for r in rows[:2]] == ["paid", "paid"]
    assert rows[2]["status"] == "overdue"
    assert rows[2]["due_date"] == (date(2026, 4, 20) + timedelta(days=90)).isoformat()   # 90 days after booking
    assert rows[5]["due_date"] is None and rows[5]["status"] == "upcoming"         # on offer of possession

    stored = _milestones(b["doc"])
    assert len(stored) == 6 and stored[0]["id"] == f"{b['doc']}-m1"
    assert [m["status"] for m in stored] == ["paid", "paid", "upcoming", "upcoming", "upcoming", "upcoming"]
    assert all(m["paid_payment_id"] is None for m in stored)
    assert stored[0]["inventory_id"] == b["unit"] and stored[0]["project_id"] == "suraksha-enclave"

    letter = client.get(f"/admin/customers/{b['customer']}/plots/{b['doc']}/demand-letter", headers=_admin())
    assert letter.status_code == 200 and letter.content.startswith(b"%PDF")

    with engine.connect() as conn:
        form = conn.execute(text("SELECT form_data FROM divine_documents WHERE id = :d"),
                            {"d": b["doc"]}).scalar()
    import json
    form = json.loads(form) if isinstance(form, str) else form
    assert form["source"] == "offline_import"            # reminder emails stay off
    assert form["received_unallocated"] == 100000 and form["payment_plan_code"] == PP1


def test_customer_list_and_detail_show_the_plan():
    res = client.get("/admin/customers", params={"search": "Asha Verma"}, headers=_admin())
    item = next(i for i in res.json()["items"] if i["id"] == B["asha"]["customer"])
    assert item["payment_plans"] == ["PP1"] and item["source"] == "OFFLINE_IMPORT"
    other = client.get("/admin/customers", params={"search": "Karan Malhotra"}, headers=_admin()).json()["items"]
    assert next(i for i in other if i["id"] == B["karan"]["customer"])["payment_plans"] == []
    detail = _detail(B["asha"])
    assert detail["source"] == "OFFLINE_IMPORT" and detail["plots"][0]["unit_number"] == B["asha"]["plot"]


def test_terms_can_be_replaced_until_an_instalment_is_paid_in_the_app():
    b = B["asha"]
    res = _put(b, payment_plan="PP1", booking_date="2026-04-20", sale_price=5000000, amount_received=500000)
    assert res.status_code == 200 and res.json()["amount_received"] == 500000
    assert [m["status"] for m in _milestones(b["doc"])][:2] == ["paid", "upcoming"]

    with engine.begin() as conn:
        conn.execute(text("UPDATE divine_payment_milestones SET status = 'paid', paid_payment_id = 'pay-x' "
                          "WHERE id = :m"), {"m": f"{b['doc']}-m2"})
    res = _put(b, payment_plan="PP1", booking_date="2026-04-20", sale_price=6000000)
    assert res.status_code == 409 and res.json()["detail"] == "schedule_has_payments"
    assert _milestones(b["doc"])[1]["paid_payment_id"] == "pay-x"          # untouched


def test_refusals():
    b = B["karan"]
    cases = [
        (B["web"], {"payment_plan": "PP1", "booking_date": "2026-04-02"}, 409, "terms_from_website_booking"),
        (B["ops"], {"payment_plan": "PP1", "booking_date": "2026-04-02"}, 400, "payment_plan_not_for_project"),
        (b, {"payment_plan": "PL-33-33-34", "booking_date": "2026-04-02"}, 400, "payment_plan_not_found"),
        (b, {"payment_plan": "PP1", "booking_date": (date.today() + timedelta(days=5)).isoformat()},
         400, "booking_date_in_future"),
        (b, {"payment_plan": "PP1", "booking_date": "2026-04-02", "amount_received": 1000}, 400,
         "sale_price_required"),
        (b, {"payment_plan": "PP1", "booking_date": "2026-04-02", "sale_price": 100000,
             "amount_received": 200000}, 400, "amount_received_exceeds_total"),
    ]
    for buyer, body, status, code in cases:
        res = _put(buyer, **body)
        assert (res.status_code, res.json().get("detail")) == (status, code), (body, res.text)
    # Someone else's plot record is not found.
    res = client.put(f"/admin/customers/{B['rohit']['customer']}/plots/{b['doc']}/payment-terms",
                     json={"payment_plan": "PP1", "booking_date": "2026-04-02"}, headers=_admin())
    assert res.status_code == 404


# ---- the client's sheet ------------------------------------------------------
def _sheet(*rows):
    return [["Plot No", "Payment Plan", "Client Name", "Phone", "Booking Date"], *rows]


def test_sheet_dry_run_then_apply():
    service = servicePlotPaymentTerms()
    sheet = _sheet([f"D9{T}", CLIENT_LABEL, "Mr. Karan", "9000000103", "02-04-2026"],
                   [f"D8{T}", CLIENT_LABEL, "Mr. Rohit Mehra", "9000000102", "18-04-2026"])
    dry = service.link_from_sheet(sheet, SURAKSHA)
    assert dry.errors == [] and [k for k, _, _ in dry.actions] == ["LINK", "LINK"]
    assert _detail(B["karan"])["plots"][0]["payment_plan"] is None          # dry run saved nothing

    done = service.link_from_sheet(sheet, SURAKSHA, apply=True)
    assert done.errors == []
    plot = _detail(B["karan"])["plots"][0]
    assert (plot["payment_plan"]["code"], plot["booking_date"]) == (PP1, "2026-04-02")


def test_sheet_flags_mismatches_and_saves_nothing_on_an_error():
    service = servicePlotPaymentTerms()
    sheet = _sheet([f"D9{T}", "PP1", "Mr. Someone Else", "9999999999", "03-04-2026"],
                   [f"D404{T}", "PP1", "Nobody", "", "03-04-2026"])
    report = service.link_from_sheet(sheet, SURAKSHA, apply=True)
    assert any("not a plot of" in e for e in report.errors)
    assert any("differs from the record" in w for w in report.warnings)
    assert any("is not on the record" in w for w in report.warnings)
    assert _detail(B["karan"])["plots"][0]["booking_date"] == "2026-04-02"   # unchanged


# ---- pieces ------------------------------------------------------------------
def test_booking_date_formats():
    assert parse_booking_date("20-04-2026") == date(2026, 4, 20)
    assert parse_booking_date("2026-04-20") == date(2026, 4, 20)
    assert parse_booking_date("20/04/2026") == date(2026, 4, 20)
    assert parse_booking_date("46132") == date(2026, 4, 20)                  # Excel date cell
    with pytest.raises(ValueError):
        parse_booking_date("April 20")


def test_letter_credits_a_part_paid_instalment():
    rows = [{"label": "On Booking", "amount": 100, "status": "paid", "due_date": "2026-01-01"},
            {"label": "45 days", "amount": 200, "status": "overdue", "due_date": "2026-02-15"},
            {"label": "90 days", "amount": 300, "status": "upcoming", "due_date": "2027-01-01"}]
    form = {"total_consideration": 600, "amount_received": 150, "received_unallocated": 50,
            "payment_schedule": rows}
    fig = demand_letter_figures(form, date(2026, 6, 1))
    assert fig["outstanding"] == 450 and fig["overdue"] == 150 and fig["demand"] == 150
    # Without the offline credit, nothing changes for other letters.
    form.pop("received_unallocated")
    assert demand_letter_figures(form, date(2026, 6, 1))["overdue"] == 200


def test_offline_plots_count_under_their_plan_in_the_admin_plan_list():
    plans = client.get("/admin/payment-plans", headers=_admin()).json()["plans"]
    pp1 = next(p for p in plans if p["code"] == PP1 and p["is_active"])
    linked = {B[k]["unit"] for k in ("asha", "rohit", "karan")}
    assert pp1["plots_count"] >= len(linked)
    from Divinepersistence import persistencePaymentPlan
    counts = persistencePaymentPlan().count_plots_by_plan([PP1])
    assert counts[(PP1, pp1["version"])] >= 3


def test_a_customer_with_several_plots_sees_each_plots_own_plan():
    # One buyer, three plots: PP1 on one, PP3 on another, the third not linked yet.
    owner = B["rohit"]["customer"]
    extra = {}
    for key in ("second", "third"):
        plot = f"E{key[0].upper()}{T}"
        unit_id = persistenceInventory().upsert_unit(
            id=str(uuid.uuid4()), project_name=SURAKSHA, city="Ganaur", unit_number=plot,
            area_sqmt=99.86, area_sqyd=119.43, status="sold").id
        doc_id = str(uuid.uuid4())
        persistenceDocument().create_document(
            id=doc_id, owner_id=owner, owner_role="customer", document_type="project_booking_application",
            storage_path=f"imported://t/{plot}", status="imported", project_id="suraksha-enclave",
            form_data={"projectId": "suraksha-enclave", "project_name": SURAKSHA, "unitNo": plot,
                       "applicantName": "Mr. Rohit Mehra", "source": "offline_import"})
        with engine.begin() as conn:
            conn.execute(text("UPDATE divine_project_inventory SET booked_by = :c WHERE id = :u"),
                         {"c": owner, "u": unit_id})
        extra[key] = {"customer": owner, "doc": doc_id, "plot": plot}
    assert _put(extra["second"], payment_plan="PP3", booking_date="2026-04-10").status_code == 200

    res = client.get("/admin/customers", params={"search": "Rohit Mehra"}, headers=_admin())
    item = next(i for i in res.json()["items"] if i["id"] == owner)
    by_plot = {p["unit_number"]: p["payment_plan"] for p in item["plot_plans"]}
    assert by_plot == {B["rohit"]["plot"]: "PP1", extra["second"]["plot"]: "PP3", extra["third"]["plot"]: None}
    assert sorted(item["payment_plans"]) == ["PP1", "PP3"]

    plots = {p["unit_number"]: p for p in _detail(B["rohit"])["plots"]}
    assert plots[extra["second"]["plot"]]["payment_plan"]["short_code"] == "PP3"
    assert plots[extra["third"]["plot"]]["terms_status"] == "plan_needed"


def test_sheet_plot_value_becomes_the_sale_price_and_received_is_marked():
    service = servicePlotPaymentTerms()
    sheet = [["Plot No", "Payment Plan", "Client Name", "Phone", "Booking Date", "Plot Value", "Amount Received"],
             [f"D9{T}", "PP1", "Mr. Karan", "9000000103", "02-04-2026", "26,27,460", "15,74,870"]]
    report = service.link_from_sheet(sheet, SURAKSHA, apply=True)
    assert report.errors == [], report.render()
    plot = _detail(B["karan"])["plots"][0]
    assert (plot["sale_price"], plot["total"], plot["amount_received"]) == (2627460, 2627460, 1574870)
    assert [r["status"] for r in plot["schedule"]][:3] == ["paid", "paid", "paid"]   # 50% = 13,13,730
    assert plot["schedule"][3]["status"] != "paid"                                   # 2,61,140 of it paid
    assert plot["outstanding"] == 2627460 - 1574870 and plot["demand_letter_available"] is True

    bad = [sheet[0], [f"D9{T}", "PP1", "Mr. Karan", "9000000103", "02-04-2026", "1,000", "5,000"]]
    assert any("amount_received_exceeds_total" in e for e in service.link_from_sheet(bad, SURAKSHA).errors)


def test_plots_sharing_an_account_keep_their_own_applicants():
    # Two buyers with one email share an account; each plot keeps its applicant.
    owner = B["asha"]["customer"]
    plot = f"F1{T}"
    unit_id = persistenceInventory().upsert_unit(
        id=str(uuid.uuid4()), project_name=SURAKSHA, city="Ganaur", unit_number=plot,
        area_sqmt=99.86, area_sqyd=119.43, status="sold").id
    persistenceDocument().create_document(
        id=str(uuid.uuid4()), owner_id=owner, owner_role="customer", document_type="project_booking_application",
        storage_path=f"imported://t/{plot}", status="imported", project_id="suraksha-enclave",
        form_data={"projectId": "suraksha-enclave", "project_name": SURAKSHA, "unitNo": plot,
                   "applicantName": "Mr. Vikas Rao", "coApplicantName": "Mrs. Meena Rao",
                   "coApplicant2Name": "Mr. Ajay Rao",
                   "source": "offline_import"})
    with engine.begin() as conn:
        conn.execute(text("UPDATE divine_project_inventory SET booked_by = :c WHERE id = :u"),
                     {"c": owner, "u": unit_id})

    res = client.get("/admin/customers", params={"search": "Asha Verma"}, headers=_admin())
    item = next(i for i in res.json()["items"] if i["id"] == owner)
    applicants = {p["unit_number"]: p["applicant_name"] for p in item["plot_plans"]}
    assert applicants == {B["asha"]["plot"]: "Mrs. Asha Verma", plot: "Mr. Vikas Rao"}

    plots = {p["unit_number"]: p for p in _detail(B["asha"])["plots"]}
    assert plots[plot]["applicant_name"] == "Mr. Vikas Rao"
    assert plots[plot]["co_applicant_names"] == ["Mrs. Meena Rao", "Mr. Ajay Rao"]   # both co-applicants
    assert plots[B["asha"]["plot"]]["applicant_name"] == "Mrs. Asha Verma"


# ---- review fixes: which record is a plot's, and the list's plot order ----------
def _plot_for(owner, project, plot, status="sold"):
    unit_id = persistenceInventory().upsert_unit(
        id=str(uuid.uuid4()), project_name=project, city="Ganaur", unit_number=plot,
        area_sqmt=99.86, area_sqyd=119.43, status=status).id
    with engine.begin() as conn:
        conn.execute(text("UPDATE divine_project_inventory SET booked_by = :c WHERE id = :u"),
                     {"c": owner, "u": unit_id})
    return unit_id


def _record(owner, form, status="imported", project_id=None, payment_id=None, created=None):
    doc_id = str(uuid.uuid4())
    persistenceDocument().create_document(
        id=doc_id, owner_id=owner, owner_role="customer", document_type="project_booking_application",
        storage_path=f"t://{doc_id}", status=status, project_id=project_id, payment_id=payment_id, form_data=form)
    if created:
        with engine.begin() as conn:
            conn.execute(text("UPDATE divine_documents SET created_date = :d WHERE id = :i"), {"d": created, "i": doc_id})
    return doc_id


def _list_item(owner, search):
    res = client.get("/admin/customers", params={"search": search}, headers=_admin())
    return next(i for i in res.json()["items"] if i["id"] == owner)


def test_website_booking_without_a_linked_buyer_keeps_its_plan_in_the_list():
    owner = persistenceCustomer().create_user(username=f"w_{uuid.uuid4().hex[:8]}", password_hash="x",
                                              first_name="Web", last_name=f"Repair{T}").id
    plot = f"G1{T}"
    unit_id = persistenceInventory().upsert_unit(          # booked, but booked_by never set
        id=str(uuid.uuid4()), project_name=SURAKSHA, city="Ganaur", unit_number=plot,
        area_sqmt=99.86, area_sqyd=119.43, status="booked").id
    pay = f"pay-{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO divine_bookings(id, payment_id, inventory_id, customer_id, project_name, "
                          "unit_number, status, kyc_status, version) VALUES (:id, :p, :u, :c, :proj, :plot, "
                          "'booked', 'verified', 1)"),
                     {"id": f"BKG-{T}-1", "p": pay, "u": unit_id, "c": owner, "proj": SURAKSHA, "plot": plot})
    _record(owner, {"unitNo": plot, "projectId": "suraksha-enclave", "applicantName": "Web Buyer",
                    "payment_plan": {"code": "PP2/FPP/10-90", "short_code": "PP2"}},
            status="generated", payment_id=pay)
    item = _list_item(owner, f"Repair{T}")
    assert item["plot_plans"] == [{"project_name": SURAKSHA, "unit_number": plot, "payment_plan": "PP2",
                                   "applicant_name": "Web Buyer"}]
    assert item["payment_plans"] == ["PP2"]


def test_a_released_website_booking_is_not_listed():
    owner = persistenceCustomer().create_user(username=f"w_{uuid.uuid4().hex[:8]}", password_hash="x",
                                              first_name="Web", last_name=f"Released{T}").id
    pay = f"pay-{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO divine_bookings(id, payment_id, inventory_id, customer_id, project_name, "
                          "unit_number, status, kyc_status, version) VALUES (:id, :p, 'x', :c, :proj, 'G2', "
                          "'cancelled', 'verified', 1)"),
                     {"id": f"BKG-{T}-2", "p": pay, "c": owner, "proj": SURAKSHA})
    _record(owner, {"unitNo": "G2", "payment_plan": {"code": "PP2/FPP/10-90", "short_code": "PP2"}},
            status="generated", payment_id=pay)
    item = _list_item(owner, f"Released{T}")
    assert item["plot_plans"] == [] and item["payment_plans"] == []


def test_the_sheet_and_the_list_use_the_same_newest_record():
    owner = persistenceCustomer().create_user(username=f"o_{uuid.uuid4().hex[:8]}", password_hash="x",
                                              first_name="Dup", last_name=f"Records{T}", phone="9000000201").id
    plot = f"H1{T}"
    _plot_for(owner, SURAKSHA, plot)
    base = {"projectId": "suraksha-enclave", "project_name": SURAKSHA, "unitNo": plot,
            "applicantName": "Mr. Dup", "phone": "9000000201", "source": "offline_import"}
    _record(owner, base, project_id="suraksha-enclave", created=datetime(2026, 1, 1))
    newest = _record(owner, {**base, "payment_plan": {"code": PP1, "short_code": "PP1"}, "payment_plan_code": PP1},
                     project_id="suraksha-enclave", created=datetime(2026, 2, 1))
    sheet = [["Plot No", "Payment Plan", "Client Name", "Phone", "Booking Date"],
             [plot, "PL-50-25-15-10", "Mr. Dup", "9000000201", "01-04-2026"]]
    report = servicePlotPaymentTerms().link_from_sheet(sheet, SURAKSHA, apply=True)
    assert report.errors == [], report.render()
    assert _list_item(owner, f"Records{T}")["plot_plans"][0]["payment_plan"] == "PP3"
    with engine.connect() as conn:
        code = conn.execute(text("SELECT form_data FROM divine_documents WHERE id = :i"), {"i": newest}).scalar()
    assert "PP3/FPP/50-25-15-10" in code                    # the sheet wrote to the newest record


def test_same_plot_number_in_two_projects_keeps_each_plan_on_its_own_plot():
    owner = persistenceCustomer().create_user(username=f"o_{uuid.uuid4().hex[:8]}", password_hash="x",
                                              first_name="Two", last_name=f"Projects{T}").id
    plot = f"K1{T}"
    _plot_for(owner, SURAKSHA, plot)
    _plot_for(owner, OPS, plot)
    # The OPS record names its project only in projectId (no project_name, no document project_id).
    _record(owner, {"unitNo": plot, "projectId": "ops-divine-greens", "applicantName": "OPS Buyer",
                    "payment_plan": {"code": "PP9", "short_code": "PP9"}})
    _record(owner, {"unitNo": plot, "project_name": SURAKSHA, "applicantName": "Suraksha Buyer",
                    "payment_plan": {"code": PP1, "short_code": "PP1"}}, project_id="suraksha-enclave")
    plots = {p["project_name"]: p for p in _list_item(owner, f"Projects{T}")["plot_plans"]}
    assert (plots[SURAKSHA]["payment_plan"], plots[SURAKSHA]["applicant_name"]) == ("PP1", "Suraksha Buyer")
    assert (plots[OPS]["payment_plan"], plots[OPS]["applicant_name"]) == ("PP9", "OPS Buyer")


def test_list_plots_are_in_natural_plot_order():
    owner = persistenceCustomer().create_user(username=f"o_{uuid.uuid4().hex[:8]}", password_hash="x",
                                              first_name="Many", last_name=f"Plots{T}").id
    for plot in ("D7", "C100", "C16"):
        _plot_for(owner, f"Natural Order {T}", plot)
    order = [p["unit_number"] for p in _list_item(owner, f"Plots{T}")["plot_plans"]]
    assert order == ["C16", "C100", "D7"]


def test_plot_value_is_taken_in_whole_rupees_so_the_total_matches():
    service = servicePlotPaymentTerms()
    rec = {"plot_value": "10,00,000.50", "plc": "1,000.40"}
    price = service._sale_price_from_plot_value(rec, {"plc": "1,000.40"})
    assert price == "999001"                                # 10,00,001 - 1,000 (each rounded first)
    from DivineService.service_payment_plans import compute_schedule, load_plan_file
    pp1 = next(p for p in load_plan_file() if p["code"] == PP1)
    assert compute_schedule(pp1, price, plc=rec["plc"])["total"] == 1000001
