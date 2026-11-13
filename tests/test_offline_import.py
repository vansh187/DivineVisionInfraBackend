"""Prod data migration: client inventory sheet sync, offline-buyer import,
invites, website-booking cleanup, and the login / letter behaviour they rely on.
Each DB test runs on its own throwaway SQLite file."""
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_db.sqlite")
os.environ.setdefault("JWT_SECRET_KEY", "testsecret")

import jwt
import pytest
from fastapi import HTTPException
from passlib.context import CryptContext
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import Divinepersistence  # noqa: F401 - registers every model on Base
from Divinepersistence.persistence_db import Base
from DivineService.service_offline_import import (
    InventorySheetSync, OfflineBuyerImport, OfflineBuyerInvites, WebsiteBookingCleanup,
    clean_phone, correct_gmail_typo, generate_temp_password, group_buyers, parse_buyers, plot_key,
    split_salutation, strip_residence_prefix, suspicious_email_domain,
)

PROJECT = "Suraksha Enclave"
INV_HEADER = ["Plot No.", "Width(mtr.)", "Length(mtr.)", "Area in sq mtrs", "Area in sq yard", "Status",
              "Project ", "Company ", "Block"]
BUYER_HEADER = ["Plot No.", "Applicant", "1 Co_Applicant", "2 Co_applicant", "Contact No.",
                "Alternative Number", "Email Id", "Co_applicant Email", "Adders", "Address_1", "Plot_Address "]


def inv_row(plot, status="Sold", w="7.45", length="13.404", sqm="99.86", sqyd="119.43"):
    return [plot, w, length, sqm, sqyd, status, PROJECT, "RAMA KRISHNA BUILDWELL PVT. LTD.", "C-Block"]


def buyer_row(plot, name="Mr. Dheeraj Nirwal", phone="9.876543210E9", email="dheeraj@gmail.com",
              co1="", co2="", co_email="", address="R/o-H.No. 543, Sonipat", co_address="", alt=""):
    return [plot, name, co1, co2, phone, alt, email, co_email, address, co_address,
            f"Plot No -{plot},Suraksha Enclave Sec-15, Ganaur"]


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm.sqlite'}")
    Base.metadata.create_all(bind=eng)
    return eng


fast_hash = lambda pw: "hash:" + pw   # noqa: E731 - test double for bcrypt


def q(engine, sql, **params):
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(sql), params).mappings().all()]


def unit(engine, plot):
    rows = q(engine, "SELECT * FROM divine_project_inventory WHERE unit_number = :u", u=plot)
    return rows[0] if rows else None


def seed_unit(engine, plot, status="available", project=PROJECT, **extra):
    cols = {"id": f"id-{plot}-{project[:3]}", "project_name": project, "city": "Sonipat", "unit_number": plot,
            "unit_type": "plot", "area_sqmt": 99.86, "area_sqyd": 119.43, "status": status, **extra}
    with engine.begin() as c:
        c.execute(text(f"INSERT INTO divine_project_inventory({', '.join(cols)}) "
                       f"VALUES ({', '.join(':' + k for k in cols)})"), cols)
    return cols["id"]


def sync(engine, rows, apply=True, **kw):
    return InventorySheetSync(engine).run([INV_HEADER] + rows, PROJECT, "Sonipat", "Sector-15, Ganaur",
                                          apply=apply, **kw)


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("9.996031373E9", "9996031373"), ("9996031373.0", "9996031373"), ("+91 99960 31373", "9996031373"),
    ("09996031373", "9996031373"), ("", ""), (None, ""),
])
def test_clean_phone(raw, expected):
    assert clean_phone(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("Mrs. Aparna Yadav", ("Female", "Aparna Yadav")), ("Mrs.Poonam", ("Female", "Poonam")),
    ("MR. Sanjay Dhingra", ("Male", "Sanjay Dhingra")), ("Ms.Komal", ("Female", "Komal")),
    ("Mr Vikas", ("Male", "Vikas")), ("Deepak", (None, "Deepak")), ("Seema And Sujata", (None, "Seema And Sujata")),
    ("Mrinal Sen", (None, "Mrinal Sen")), ("Msraj Kumar", (None, "Msraj Kumar")), ("", (None, "")),
])
def test_split_salutation(raw, expected):
    assert split_salutation(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("R/o-H.No. 497/15, Ganaur", "H.No. 497/15, Ganaur"), ("R/o. Ahulana (121), Sonipat", "Ahulana (121), Sonipat"),
    ("R/o Sonipat", "Sonipat"), ("289/14, Ward 14", "289/14, Ward 14"), ("  R/O-  Model\nTown ", "Model Town"),
])
def test_strip_residence_prefix(raw, expected):
    assert strip_residence_prefix(raw) == expected


def test_plot_key_matches_both_sheet_spellings():
    assert plot_key("B1-B") == plot_key("B1B") == plot_key(" b1 b ") == "B1B"
    assert plot_key("C61") != plot_key("C6")


def test_email_typos_corrected_to_gmail_only():
    assert correct_gmail_typo("ritik@gamil.com") == ("ritik@gmail.com", "'ritik@gamil.com' corrected to 'ritik@gmail.com'")
    assert correct_gmail_typo("malik@gmaiul.com")[0] == "malik@gmail.com"
    assert correct_gmail_typo("x@gmail.com") == ("x@gmail.com", None)
    assert correct_gmail_typo("x@rediffmail.com") == ("x@rediffmail.com", None)
    assert suspicious_email_domain("x@yahooo.com") == "yahoo.com"
    assert correct_gmail_typo("x@yahooo.com")[0] == "x@yahooo.com"   # flagged, not changed


def test_temp_password_is_readable_and_strong():
    pws = {generate_temp_password() for _ in range(200)}
    assert len(pws) == 200
    for pw in pws:
        assert len(pw) == 10 and not set(pw) & set("0O1lI")
        assert any(c.isupper() for c in pw) and any(c.islower() for c in pw) and any(c.isdigit() for c in pw)


def test_parse_and_group_buyers_like_the_client_sheet():
    rows = [BUYER_HEADER,
            buyer_row("C61", email="vij@gmail.com"),
            buyer_row("C62", email="VIJ@gmail.com "),                          # same owner, 2 plots
            buyer_row("C15", name="Mrs. Priya Chaudhary", email="sat@gmail.com", phone="9.899927817E9"),
            buyer_row("C14", name="", phone="", email="sat@gmail.com", address="R/o-MD-11 Mapsko"),
            buyer_row("C54", name="MR. Sanjay Dhingra", email="", phone="9.416942655E9"),
            buyer_row("C80", name="Mrs.Poonam", co1="Mrs. Suman", co_address="R/o-917/29 Malviya Nagar",
                      email="p@gamil.com"),
            ["", "", "", "", "", "", "", "", "", "", ""]]
    buyers, warnings = parse_buyers(rows)
    assert len(buyers) == 6
    c80 = next(b for b in buyers if b.plot_raw == "C80")
    assert (c80.applicant, c80.gender, c80.co1, c80.co1_gender, c80.co_address, c80.email) == \
        ("Poonam", "Female", "Suman", "Female", "917/29 Malviya Nagar", "p@gmail.com")
    assert any("corrected to 'p@gmail.com'" in w for w in warnings)
    groups, gwarn = group_buyers(buyers)
    assert sorted(len(v) for v in groups.values()) == [1, 1, 2, 2]
    c14 = next(b for b in buyers if b.plot_raw == "C14")
    assert (c14.applicant, c14.gender, c14.phone, c14.address) == ("Priya Chaudhary", "Female", "9899927817",
                                                                   "MD-11 Mapsko")
    assert any("C54" in w and "no email" in w for w in gwarn)


# --------------------------------------------------------------------------
# A. Inventory sheet sync
# --------------------------------------------------------------------------

def test_sync_maps_statuses_and_inserts_new_plots(engine):
    report = sync(engine, [inv_row("C1"), inv_row("C2", "sold"), inv_row("C14", "Unsold"), inv_row("C28", "Hold")])
    assert not report.errors
    assert {p: unit(engine, p)["status"] for p in ("C1", "C2", "C14", "C28")} == \
        {"C1": "sold", "C2": "sold", "C14": "available", "C28": "held"}
    assert unit(engine, "C1")["sale_source"] == "offline" and unit(engine, "C14")["sale_source"] is None
    assert unit(engine, "C1")["block"] == "C"
    assert any("C2: status written as 'sold'" in w for w in report.warnings)


def test_sync_dry_run_writes_nothing(engine):
    report = sync(engine, [inv_row("C1")], apply=False)
    assert report.change_count == 1 and unit(engine, "C1") is None
    assert q(engine, "SELECT * FROM divine_inventory_status_history") == []


def test_sync_is_idempotent(engine):
    sync(engine, [inv_row("C1"), inv_row("C14", "Unsold"), inv_row("C28", "Hold")])
    again = sync(engine, [inv_row("C1"), inv_row("C14", "Unsold"), inv_row("C28", "Hold")], apply=False)
    assert again.change_count == 0


def test_sync_override(engine):
    report = sync(engine, [inv_row("B1-B", "Unsold")], overrides={"B1B": "sold"})
    assert unit(engine, "B1-B")["status"] == "sold"
    assert "Override" in unit(engine, "B1-B")["status_note"]
    assert any("overridden" in w for w in report.warnings)


@pytest.mark.parametrize("app_status", ["booked", "pending_kyc_review", "reserved"])
def test_sync_never_touches_app_activity(engine, app_status):
    seed_unit(engine, "C5", status=app_status, booked_payment_id="pay-1", booked_by="C00001")
    report = sync(engine, [inv_row("C5", "Unsold")])
    assert unit(engine, "C5")["status"] == app_status
    assert any(k == "CONFLICT" for k, _, _ in report.actions)


def test_sync_sold_on_app_booking_marks_it_app(engine):
    seed_unit(engine, "C5", status="booked", booked_payment_id="pay-1")
    sync(engine, [inv_row("C5", "Sold")])
    assert unit(engine, "C5")["status"] == "booked" and unit(engine, "C5")["sale_source"] == "app"


def test_sync_renames_db_spelling_and_keeps_booking_in_step(engine):
    inv_id = seed_unit(engine, "B-1-B", status="available")
    with engine.begin() as c:
        c.execute(text("INSERT INTO divine_bookings(id, payment_id, inventory_id, customer_id, project_name, "
                       "unit_number, status, kyc_status, version) VALUES ('BKG-1', 'p1', :i, 'C00001', :p, "
                       "'B-1-B', 'rejected', 'rejected', 1)"), {"i": inv_id, "p": PROJECT})
    sync(engine, [inv_row("B1-B", "Unsold")])
    assert unit(engine, "B1-B")["id"] == inv_id
    assert q(engine, "SELECT unit_number FROM divine_bookings")[0]["unit_number"] == "B1-B"


def test_sync_extra_project_plot_is_held_other_projects_untouched(engine):
    seed_unit(engine, "B420", status="available")
    seed_unit(engine, "A-1", status="available", project="OPS Divine Greens")
    sync(engine, [inv_row("C1")])
    assert unit(engine, "B420")["status"] == "held"
    assert unit(engine, "A-1")["status"] == "available"


def test_sync_keeps_existing_dimension_when_sheet_cell_blank(engine):
    seed_unit(engine, "C27", width_mtr=7.45, length_mtr=13.404)
    report = sync(engine, [inv_row("C27", w="")])
    assert float(unit(engine, "C27")["width_mtr"]) == 7.45
    assert any("C27: sheet has no width_mtr" in w for w in report.warnings)


def test_sync_flags_inconsistent_area(engine):
    report = sync(engine, [inv_row("C74", w="8.046")])
    assert any("C74: width x length" in w for w in report.warnings)


def test_sync_rejects_duplicate_plots_and_wrong_project(engine):
    assert sync(engine, [inv_row("C1"), inv_row("C-1")], apply=False).errors
    bad = inv_row("C1")
    bad[6] = "OPS Divine Greens"
    assert sync(engine, [bad], apply=False).errors


def test_sync_rollback_restores_previous_state(engine):
    seed_unit(engine, "C1", status="available")
    report = sync(engine, [inv_row("C1", "Sold"), inv_row("C2")])
    assert unit(engine, "C1")["status"] == "sold" and unit(engine, "C2")
    InventorySheetSync(engine).rollback(report.batch_id)
    assert unit(engine, "C1")["status"] == "available" and unit(engine, "C1")["sale_source"] is None
    assert unit(engine, "C2") is None
    assert q(engine, "SELECT * FROM divine_inventory_status_history") == []


def test_sync_and_rollback_status_changes_stay_in_the_permanent_log(engine):
    seed_unit(engine, "C1", status="available")
    report = sync(engine, [inv_row("C1", "Sold")])
    InventorySheetSync(engine).rollback(report.batch_id)
    log = q(engine, "SELECT * FROM divine_inventory_status_log ORDER BY changed_at, id")
    assert [(r["old_status"], r["new_status"], r["source"]) for r in log] == [
        ("available", "sold", "sheet_sync"), ("sold", "available", "sync_rollback"),
    ]
    assert {r["changed_by_name"] for r in log} == {"Sheet sync"}
    assert all(r["unit_number"] == "C1" and r["project_name"] == PROJECT for r in log)


def test_sync_dry_run_writes_no_status_log(engine):
    seed_unit(engine, "C1", status="available")
    sync(engine, [inv_row("C1", "Sold")], apply=False)
    assert q(engine, "SELECT * FROM divine_inventory_status_log") == []


# --------------------------------------------------------------------------
# B. Offline buyer import
# --------------------------------------------------------------------------

def _load_inventory(engine, plots, status="Sold"):
    sync(engine, [inv_row(p, status) for p in plots])


def run_import(engine, rows, apply=True):
    return OfflineBuyerImport(engine, password_hasher=fast_hash).run([BUYER_HEADER] + rows, PROJECT, apply=apply)


def test_import_creates_one_account_per_email_with_plot_records(engine):
    _load_inventory(engine, ["C61", "C62", "C80"])
    report = run_import(engine, [
        buyer_row("C61", email="vij@gmail.com"), buyer_row("C62", email="vij@gmail.com"),
        buyer_row("C80", name="Mrs.Poonam", co1="Mrs. Suman", co2="Mr. Ravi", co_email="suman@gmail.com",
                  co_address="R/o-917/29 Malviya Nagar", email="poonam@gmail.com", phone="9.999910724E9"),
    ])
    assert not report.errors
    accounts = q(engine, "SELECT * FROM divine_customer_users ORDER BY email")
    assert [a["email"] for a in accounts] == ["poonam@gmail.com", "vij@gmail.com"]
    poonam = accounts[0]
    assert (poonam["username"], poonam["first_name"], poonam["phone"], poonam["source"], poonam["invite_status"]) == \
        ("poonam@gmail.com", "Poonam", "9999910724", "offline_import", "pending")
    assert poonam["password_hash"].startswith("hash:")
    docs = q(engine, "SELECT * FROM divine_documents WHERE owner_id = :o", o=poonam["id"])
    assert len(docs) == 1
    d = docs[0]
    assert (d["status"], d["document_type"], d["project_id"]) == ("imported", "project_booking_application",
                                                                  "suraksha-enclave")
    form = json.loads(d["form_data"]) if isinstance(d["form_data"], str) else d["form_data"]
    assert form["unitNo"] == "C80" and form["applicantName"] == "Poonam" and form["gender"] == "Female"
    assert form["coApplicantName"] == "Suman" and form["coApplicant2Name"] == "Ravi"
    assert form["coApplicantEmail"] == "suman@gmail.com" and form["coApplicantPermanentAddress"] == "917/29 Malviya Nagar"
    assert form["permanentAddress"] == "H.No. 543, Sonipat" and form["finance_status"] == "pending"
    assert form["plotAreaSqYd"] == "119.43"
    assert unit(engine, "C80")["booked_by"] == poonam["id"]
    vij = accounts[1]
    assert len(q(engine, "SELECT id FROM divine_documents WHERE owner_id = :o", o=vij["id"])) == 2


def test_import_never_creates_payments_bookings_or_milestones(engine):
    _load_inventory(engine, ["C61"])
    run_import(engine, [buyer_row("C61")])
    for table in ("divine_payments", "divine_bookings", "divine_payment_milestones", "divine_payment_reminders"):
        assert q(engine, f"SELECT * FROM {table}") == [], table


def test_import_matches_hyphenless_plot_numbers(engine):
    _load_inventory(engine, ["B1-D"])
    run_import(engine, [buyer_row("B1D")])
    doc = q(engine, "SELECT form_data FROM divine_documents")[0]["form_data"]
    assert json.loads(doc)["unitNo"] == "B1-D"


def test_import_fills_missing_name_from_same_email_owner(engine):
    _load_inventory(engine, ["C14", "C15"])
    run_import(engine, [buyer_row("C15", name="Mrs. Priya Chaudhary", email="sat@gmail.com"),
                        buyer_row("C14", name="", phone="", email="sat@gmail.com")])
    assert len(q(engine, "SELECT * FROM divine_customer_users")) == 1
    forms = [json.loads(r["form_data"]) for r in q(engine, "SELECT form_data FROM divine_documents")]
    assert {f["unitNo"]: f["applicantName"] for f in forms} == {"C14": "Priya Chaudhary", "C15": "Priya Chaudhary"}


def test_import_buyer_without_email_gets_phone_account_flagged(engine):
    _load_inventory(engine, ["C54"])
    report = run_import(engine, [buyer_row("C54", name="MR. Sanjay Dhingra", email="", phone="9.416942655E9")])
    acct = q(engine, "SELECT * FROM divine_customer_users")[0]
    assert (acct["username"], acct["email"], acct["invite_status"]) == ("9416942655", None, "no_email")
    assert any("no email" in w for w in report.warnings)


def test_import_reuses_existing_website_account_and_keeps_password(engine):
    _load_inventory(engine, ["C61"])
    with engine.begin() as c:
        c.execute(text("INSERT INTO divine_customer_users(id, username, email, phone, password_hash, source, "
                       "must_change_password) VALUES ('C11111', 'dheeraj', 'Dheeraj@Gmail.com', '9876543210', "
                       "'own-hash', 'website', 0)"))
    report = run_import(engine, [buyer_row("C61", email="dheeraj@gmail.com")])
    assert any(k == "REUSE" for k, _, _ in report.actions)
    acct = q(engine, "SELECT * FROM divine_customer_users")
    assert len(acct) == 1 and acct[0]["password_hash"] == "own-hash"
    assert acct[0]["invite_status"] == "plot_added_pending"
    assert q(engine, "SELECT owner_id FROM divine_documents")[0]["owner_id"] == "C11111"


@pytest.mark.parametrize("status", ["Unsold", "Hold"])
def test_import_refuses_plot_that_is_not_sold(engine, status):
    _load_inventory(engine, ["C14"], status=status)
    report = run_import(engine, [buyer_row("C14")])
    assert report.errors and q(engine, "SELECT * FROM divine_customer_users") == []


def test_import_refuses_unknown_plot(engine):
    _load_inventory(engine, ["C1"])
    assert run_import(engine, [buyer_row("Z99")]).errors


def test_import_is_idempotent_and_does_not_re_flag_accounts(engine):
    _load_inventory(engine, ["C61", "C62"])
    run_import(engine, [buyer_row("C61"), buyer_row("C62")])
    with engine.begin() as c:
        c.execute(text("UPDATE divine_customer_users SET invite_status = 'sent'"))
    again = run_import(engine, [buyer_row("C61"), buyer_row("C62")])
    assert again.change_count == 0
    assert len(q(engine, "SELECT * FROM divine_documents")) == 2
    assert q(engine, "SELECT invite_status FROM divine_customer_users")[0]["invite_status"] == "sent"


def test_import_dry_run_writes_nothing(engine):
    _load_inventory(engine, ["C61"])
    report = run_import(engine, [buyer_row("C61")], apply=False)
    assert report.change_count == 2   # account + plot
    assert q(engine, "SELECT * FROM divine_customer_users") == [] and q(engine, "SELECT * FROM divine_documents") == []


def test_import_rollback_removes_only_that_batch(engine):
    _load_inventory(engine, ["C61", "C62"])
    first = run_import(engine, [buyer_row("C61", email="a@gmail.com")])
    run_import(engine, [buyer_row("C61", email="a@gmail.com"), buyer_row("C62", email="b@gmail.com")])
    OfflineBuyerImport(engine, password_hasher=fast_hash).rollback(first.batch_id)
    assert [a["email"] for a in q(engine, "SELECT email FROM divine_customer_users")] == ["b@gmail.com"]
    assert unit(engine, "C61")["booked_by"] is None and unit(engine, "C62")["booked_by"]


# --------------------------------------------------------------------------
# C. Invites
# --------------------------------------------------------------------------

class FakeEmail:
    def __init__(self, ok=True):
        self.ok, self.invites, self.added = ok, [], []

    def send_offline_buyer_invite(self, email, **kw):
        self.invites.append((email, kw))
        return self.ok

    def send_plot_added_to_account(self, email, **kw):
        self.added.append((email, kw))
        return self.ok


def invites(engine, email):
    return OfflineBuyerInvites(engine, email_service=email, password_hasher=fast_hash, sleep=lambda s: None)


def test_invite_sends_one_email_per_account_listing_all_plots(engine, caplog):
    _load_inventory(engine, ["C61", "C62"])
    run_import(engine, [buyer_row("C61"), buyer_row("C62")])
    mail = FakeEmail()
    report = invites(engine, mail).run(apply=True)
    assert len(mail.invites) == 1
    to, kw = mail.invites[0]
    assert to == "dheeraj@gmail.com" and kw["plots"] == ["C61", "C62"] and kw["project_name"] == PROJECT
    pw = kw["temp_password"]
    acct = q(engine, "SELECT * FROM divine_customer_users")[0]
    assert acct["password_hash"] == "hash:" + pw and acct["invite_status"] == "sent"
    assert bool(acct["must_change_password"]) and acct["temp_password_expires_at"]
    # the password is nowhere but in the email
    assert pw not in report.render() and pw not in str(report.extra["csv_rows"]) and pw not in caplog.text


def test_invite_failure_is_retried_with_a_new_password(engine):
    _load_inventory(engine, ["C61"])
    run_import(engine, [buyer_row("C61")])
    failing = FakeEmail(ok=False)
    invites(engine, failing).run(apply=True)
    undelivered = failing.invites[0][1]["temp_password"]
    acct = q(engine, "SELECT * FROM divine_customer_users")[0]
    assert acct["invite_status"] == "failed" and acct["invite_sent_at"] is None
    mail = FakeEmail()
    invites(engine, mail).run(apply=True)          # failed ones are retried
    assert len(mail.invites) == 1
    delivered = mail.invites[0][1]["temp_password"]
    assert delivered != undelivered
    assert q(engine, "SELECT password_hash FROM divine_customer_users")[0]["password_hash"] == "hash:" + delivered


def test_invite_password_is_stored_before_the_email_goes_out(engine):
    _load_inventory(engine, ["C61"])
    run_import(engine, [buyer_row("C61")])
    seen = {}

    class CheckingEmail(FakeEmail):
        def send_offline_buyer_invite(self, email, **kw):
            seen["hash_at_send"] = q(engine, "SELECT password_hash, invite_status FROM divine_customer_users")[0]
            return super().send_offline_buyer_invite(email, **kw)

    mail = CheckingEmail()
    invites(engine, mail).run(apply=True)
    pw = mail.invites[0][1]["temp_password"]
    assert seen["hash_at_send"] == {"password_hash": "hash:" + pw, "invite_status": "sending"}
    assert q(engine, "SELECT invite_status FROM divine_customer_users")[0]["invite_status"] == "sent"


def test_invite_dry_run_sends_nothing_and_already_sent_is_skipped(engine):
    _load_inventory(engine, ["C61"])
    run_import(engine, [buyer_row("C61")])
    mail = FakeEmail()
    report = invites(engine, mail).run(apply=False)
    assert mail.invites == [] and [a[0] for a in report.actions] == ["INVITE"]
    invites(engine, mail).run(apply=True)
    invites(engine, mail).run(apply=True)
    assert len(mail.invites) == 1


def test_resend_gives_a_new_password(engine):
    _load_inventory(engine, ["C61"])
    run_import(engine, [buyer_row("C61")])
    mail = FakeEmail()
    invites(engine, mail).run(apply=True)
    cid = q(engine, "SELECT id FROM divine_customer_users")[0]["id"]
    with engine.begin() as c:
        c.execute(text("UPDATE divine_customer_users SET invite_status = 'pending' WHERE id = :i"), {"i": cid})
    invites(engine, mail).run(apply=True, customer_ids=[cid])
    first, second = (kw["temp_password"] for _, kw in mail.invites)
    assert first != second
    assert q(engine, "SELECT password_hash FROM divine_customer_users")[0]["password_hash"] == "hash:" + second


def test_existing_account_gets_plot_added_email_not_a_password(engine):
    _load_inventory(engine, ["C61"])
    with engine.begin() as c:
        c.execute(text("INSERT INTO divine_customer_users(id, username, email, password_hash, source, "
                       "must_change_password) VALUES ('C11111', 'dheeraj@gmail.com', 'dheeraj@gmail.com', "
                       "'own-hash', 'website', 0)"))
    run_import(engine, [buyer_row("C61")])
    mail = FakeEmail()
    invites(engine, mail).run(apply=True)
    assert mail.invites == [] and mail.added[0][0] == "dheeraj@gmail.com"
    acct = q(engine, "SELECT * FROM divine_customer_users")[0]
    assert acct["password_hash"] == "own-hash" and acct["invite_status"] == "plot_added_sent"


def test_account_without_email_is_listed_not_sent(engine):
    _load_inventory(engine, ["C54"])
    run_import(engine, [buyer_row("C54", email="")])
    mail = FakeEmail()
    report = invites(engine, mail).run(apply=True)
    assert mail.invites == [] and report.actions[0][0] == "NO_EMAIL"


# --------------------------------------------------------------------------
# 0. Website booking cleanup
# --------------------------------------------------------------------------

def test_cleanup_removes_booking_data_and_frees_plots(engine):
    inv_id = seed_unit(engine, "C9", status="pending_kyc_review", booked_payment_id="pay-1", booked_by="C00001")
    seed_unit(engine, "C10", status="sold", sale_source="offline", booked_by="C22222")
    with engine.begin() as c:
        c.execute(text("INSERT INTO divine_customer_users(id, username, password_hash) VALUES ('C00001', 'u', 'h')"))
        c.execute(text("INSERT INTO divine_payments(id, owner_id, owner_role, amount, currency, status, method, "
                       "purpose, inventory_id) VALUES ('pay-1', 'C00001', 'customer', 100, 'INR', 'paid', 'cash', "
                       "'plot_booking', :i)"), {"i": inv_id})
        c.execute(text("INSERT INTO divine_payments(id, owner_id, owner_role, amount, currency, status, method, "
                       "purpose) VALUES ('pay-2', 'C00001', 'customer', 5, 'INR', 'paid', 'cash', 'other')"))
        c.execute(text("INSERT INTO divine_bookings(id, payment_id, inventory_id, customer_id, status, kyc_status, "
                       "version) VALUES ('BKG-1', 'pay-1', :i, 'C00001', 'pending_kyc_review', 'pending', 1)"),
                  {"i": inv_id})
        for doc_id, dtype, status in (("d1", "project_booking_application", "uploaded"),
                                      ("d2", "aadhaar_front", "uploaded"),
                                      ("d3", "project_booking_application", "imported")):
            c.execute(text("INSERT INTO divine_documents(id, owner_id, owner_role, document_type, form_data, "
                           "storage_path, status) VALUES (:id, 'C00001', 'customer', :t, '{}', 'x', :s)"),
                      {"id": doc_id, "t": dtype, "s": status})
    dry = WebsiteBookingCleanup(engine).run(apply=False)
    assert dry.extra["divine_bookings"] == 1 and q(engine, "SELECT * FROM divine_bookings")
    report = WebsiteBookingCleanup(engine).run(apply=True)
    assert report.extra["backup"]["divine_bookings"][0]["id"] == "BKG-1"
    assert q(engine, "SELECT * FROM divine_bookings") == []
    assert [p["id"] for p in q(engine, "SELECT id FROM divine_payments")] == ["pay-2"]
    assert sorted(d["id"] for d in q(engine, "SELECT id FROM divine_documents")) == ["d2", "d3"]
    assert q(engine, "SELECT id FROM divine_customer_users")       # accounts kept
    c9 = unit(engine, "C9")
    assert c9["status"] == "available" and c9["booked_by"] is None and c9["booked_payment_id"] is None
    assert unit(engine, "C10")["booked_by"] == "C22222"            # offline sale untouched
    log = q(engine, "SELECT * FROM divine_inventory_status_log WHERE inventory_id = :i", i=inv_id)
    assert [(r["old_status"], r["new_status"], r["source"], r["changed_by_name"]) for r in log] == [
        ("pending_kyc_review", "available", "test_booking_cleanup", "Website booking cleanup")]


# --------------------------------------------------------------------------
# Inventory safety (persistence SQL)
# --------------------------------------------------------------------------

@pytest.fixture
def inventory(engine):
    from Divinepersistence.persistence_inventory import persistenceInventory
    return persistenceInventory(session_factory=sessionmaker(bind=engine))


def test_held_unit_can_never_be_booked_or_held_for_kyc(engine, inventory):
    uid = seed_unit(engine, "C28", status="held")
    assert inventory.book_unit(id=uid, payment_id="p1", customer_id="C00001") is None
    assert inventory.hold_for_kyc_review(id=uid, payment_id="p1", customer_id="C00001") is None
    assert unit(engine, "C28")["status"] == "held"


def test_available_unit_still_books(engine, inventory):
    uid = seed_unit(engine, "C14", status="available")
    assert inventory.hold_for_kyc_review(id=uid, payment_id="p1", customer_id="C00001").status == "pending_kyc_review"


@pytest.mark.parametrize("status", ["booked", "pending_kyc_review", "reserved"])
def test_reingest_never_downgrades_app_activity(engine, inventory, status):
    seed_unit(engine, "C5", status=status)
    inventory.upsert_unit(id="new", project_name=PROJECT, city="Sonipat", unit_number="C5",
                          area_sqmt=99.86, area_sqyd=119.43, status="available")
    assert unit(engine, "C5")["status"] == status


@pytest.mark.parametrize("status", ["sold", "held"])
def test_reingest_keeps_offline_sales_and_client_holds(engine, inventory, status):
    seed_unit(engine, "C5", status=status, sale_source="offline" if status == "sold" else None)
    inventory.upsert_unit(id="new", project_name=PROJECT, city="Sonipat", unit_number="C5",
                          area_sqmt=99.86, area_sqyd=119.43, status="available")
    assert unit(engine, "C5")["status"] == status


def test_reingest_still_sets_status_of_available_plots(engine, inventory):
    seed_unit(engine, "C5", status="available")
    inventory.upsert_unit(id="new", project_name=PROJECT, city="Sonipat", unit_number="C5",
                          area_sqmt=99.86, area_sqyd=119.43, status="sold")
    assert unit(engine, "C5")["status"] == "sold"


@pytest.mark.parametrize("status", ["held", "sold", "booked"])
def test_online_order_refused_for_unbookable_plots(status):
    from DivineService.service_payment import servicePayment
    inv = MagicMock()
    inv.get_by_id.return_value = SimpleNamespace(id="INV-1", status=status)
    svc = servicePayment(MagicMock(), inv, MagicMock())
    with pytest.raises(ValueError, match="unit_not_available"):
        svc._guard_unit_bookable("INV-1")


def test_cash_already_collected_for_a_held_plot_is_still_recorded_and_flagged():
    """Real money is never refused: the payment is saved, the hold fails, and it
    goes to manual review as an inventory conflict."""
    from DivineService.service_payment import servicePayment
    payments, inv = MagicMock(), MagicMock()
    payments.create_payment.return_value = SimpleNamespace(
        id="pay1", owner_id="C00001", owner_role="customer", amount=500000, currency="INR", status="paid",
        method="cash", purpose="plot_booking", inventory_id="INV-1", installment_no=None, due_date=None,
        needs_manual_review=False, manual_review_reason=None, razorpay_order_id=None, razorpay_payment_id=None,
        notes={}, utr_number=None, cheque_number=None, recorded_by_admin_id=None, refund_status="none",
        created_date=None, last_updated_date=None)
    inv.get_by_id.return_value = SimpleNamespace(id="INV-1", status="held")
    inv.hold_for_kyc_review.return_value = None           # held -> not holdable
    svc = servicePayment(payments, inv, MagicMock())
    record = svc.record_cash_payment(500000, owner_id="C00001", owner_role="customer",
                                     purpose="plot_booking", inventory_id="INV-1")
    payments.create_payment.assert_called_once()
    assert record.inventory_status == "conflict"
    payments.flag_manual_review.assert_called()


# --------------------------------------------------------------------------
# Login with a temporary password / change password
# --------------------------------------------------------------------------

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _customer_service(user):
    from DivineService.service_customer import serviceCustomer
    persistence = MagicMock()
    persistence.get_by_email.return_value = user
    persistence.get_by_username.return_value = user
    persistence.get_by_id.return_value = user
    persistence.update_password.side_effect = lambda cid, h: SimpleNamespace(**{**vars(user), "password_hash": h})
    return serviceCustomer(persistence=persistence, secret_key="testsecret", zoho=MagicMock(), email=MagicMock())


def _user(must=True, expires_in=timedelta(days=7), password="Temp3pwXyz"):
    return SimpleNamespace(id="C12345", username="a@gmail.com", email="a@gmail.com",
                           password_hash=_pwd.hash(password), must_change_password=must,
                           temp_password_expires_at=(datetime.now(timezone.utc) + expires_in) if expires_in else None)


def test_login_with_temp_password_requires_change():
    from DivineDTO.models import UserLoginDTO
    svc = _customer_service(_user())
    token, must = svc.login_with_status(UserLoginDTO(username="a@gmail.com", password="Temp3pwXyz"))
    assert must is True
    assert jwt.decode(token, "testsecret", algorithms=["HS256"])["pwd_change"] is True


def test_expired_temp_password_is_refused():
    from DivineDTO.models import UserLoginDTO
    svc = _customer_service(_user(expires_in=timedelta(seconds=-1)))
    with pytest.raises(ValueError, match="temp_password_expired"):
        svc.login_with_status(UserLoginDTO(username="a@gmail.com", password="Temp3pwXyz"))


def test_normal_login_unchanged():
    from DivineDTO.models import UserLoginDTO
    svc = _customer_service(_user(must=False, expires_in=None))
    token, must = svc.login_with_status(UserLoginDTO(username="a@gmail.com", password="Temp3pwXyz"))
    assert must is False and "pwd_change" not in jwt.decode(token, "testsecret", algorithms=["HS256"])


def test_change_password_clears_restriction():
    svc = _customer_service(_user())
    token = svc.change_password("C12345", "Temp3pwXyz", "MyNewPass123")
    assert "pwd_change" not in jwt.decode(token, "testsecret", algorithms=["HS256"])
    svc._persistence.update_password.assert_called_once()
    for bad, code in ((("wrong", "MyNewPass123")), "invalid_credentials"), \
                     ((("Temp3pwXyz", "short")), "password_too_short"), \
                     ((("Temp3pwXyz", "Temp3pwXyz")), "password_unchanged"):
        with pytest.raises(ValueError, match=code):
            svc.change_password("C12345", *bad)


def test_password_update_query_clears_temp_password_flags():
    from Divinepersistence.persistence_customer import persistenceCustomer
    sql = persistenceCustomer()._queries["update_password"]
    assert "must_change_password = false" in sql and "temp_password_expires_at = NULL" in sql


def test_optional_customer_treats_temp_password_token_as_anonymous():
    from DivineService.auth import get_optional_customer
    exp = (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
    restricted = jwt.encode({"sub": "C12345", "role": "customer", "exp": exp, "pwd_change": True},
                            os.environ["JWT_SECRET_KEY"], algorithm="HS256")
    normal = jwt.encode({"sub": "C12345", "role": "customer", "exp": exp},
                        os.environ["JWT_SECRET_KEY"], algorithm="HS256")
    assert get_optional_customer(f"Bearer {restricted}") is None
    assert get_optional_customer(f"Bearer {normal}")["sub"] == "C12345"


def test_change_password_api_wrong_current_password_is_400_not_401(monkeypatch):
    import DivineAPI.customer_api as api
    from DivineDTO.models import ChangePasswordDTO
    svc = MagicMock()
    for code, expected in (("invalid_credentials", "invalid_current_password"),
                           ("password_too_short", "password_too_short")):
        svc.change_password.side_effect = ValueError(code)
        monkeypatch.setattr(api, "_cust_service", svc)
        with pytest.raises(HTTPException) as e:
            api.customer_change_password(ChangePasswordDTO(current_password="x", new_password="short"),
                                         current_user={"sub": "C12345", "role": "customer"})
        assert e.value.status_code == 400 and e.value.detail == expected


def test_restricted_token_only_reaches_allowed_endpoints():
    from DivineService.auth import get_current_user, get_current_user_pending_password_change
    exp = (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
    token = jwt.encode({"sub": "C12345", "role": "customer", "exp": exp, "pwd_change": True},
                       os.environ["JWT_SECRET_KEY"], algorithm="HS256")
    with pytest.raises(HTTPException) as e:
        get_current_user(f"Bearer {token}")
    assert e.value.status_code == 403 and e.value.detail == "password_change_required"
    assert get_current_user_pending_password_change(f"Bearer {token}")["sub"] == "C12345"


# --------------------------------------------------------------------------
# Imported plot records in the app
# --------------------------------------------------------------------------

_IMPORTED_FORM = {"projectId": "suraksha-enclave", "project_name": PROJECT, "unitNo": "C61",
                  "applicantName": "Dheeraj Nirwal", "coApplicantName": "Asha", "finance_status": "pending",
                  "plot_address": "Plot No -C61,Suraksha Enclave"}


def _doc_service(status="imported", form=None):
    from DivineService.service_document import serviceDocument
    doc = MagicMock(id="d1", owner_id="C1", owner_role="customer", payment_id=None, status=status,
                    document_type="project_booking_application", form_data=json.dumps(form or _IMPORTED_FORM))
    persistence = MagicMock()
    persistence.get_by_id.return_value = doc
    persistence.get_latest_by_owner_and_type.return_value = doc
    return serviceDocument(persistence)


def test_imported_record_has_no_file_to_sign():
    svc = _doc_service()
    with pytest.raises(ValueError, match="no_file_for_imported_record"):
        svc.get("d1", requester_id="C1", requester_role="customer")
    with pytest.raises(ValueError, match="no_file_for_imported_record"):
        svc.get_latest("project_booking_application", requester_id="C1", requester_role="customer")


def test_demand_letter_waits_for_payment_details():
    with pytest.raises(ValueError, match="payment_details_pending"):
        _doc_service().get_demand_letter("d1", requester_id="C1", requester_role="customer")


def test_finance_pending_clears_itself_once_price_data_exists():
    from DivineService.service_demand_letter import finance_pending
    assert finance_pending(_IMPORTED_FORM)
    assert not finance_pending({**_IMPORTED_FORM, "totalPrice": 2500000})
    assert not finance_pending({**_IMPORTED_FORM, "saleValue": "25,00,000"})
    assert not finance_pending({**_IMPORTED_FORM, "payment_schedule": [{"label": "On Booking", "amount": 1}]})


def test_demand_letter_uses_live_milestones_even_without_form_schedule():
    """A booking whose schedule lives only in the milestones table is not 'pending'."""
    svc = _doc_service(status="generated", form={"projectId": "suraksha-enclave", "unitNo": "C61"})
    svc._milestone_persistence = MagicMock()
    svc._milestone_persistence.list_for_booking.return_value = [
        SimpleNamespace(label="On Booking", percent=10, due_date=None, amount=100000, status="paid"),
        SimpleNamespace(label="Within 45 days", percent=90, due_date=None, amount=900000, status="due"),
    ]
    pdf, _ = svc.get_demand_letter("d1", requester_id="C1", requester_role="customer")
    assert pdf[:5] == b"%PDF-"


def test_demand_letter_api_returns_409_for_pending_finance(monkeypatch):
    import DivineAPI.documents_api as api
    monkeypatch.setattr(api, "_doc_service", _doc_service())
    with pytest.raises(HTTPException) as e:
        api.get_demand_letter("d1", current_user={"sub": "C1", "role": "customer"})
    assert e.value.status_code == 409 and e.value.detail == "payment_details_pending"


def test_profile_marks_imported_plot_finance_pending():
    from DivineService.service_customer_profile import serviceCustomerProfile
    svc = serviceCustomerProfile(persistence=MagicMock())
    svc._resolve_amount_received = lambda *a, **k: None
    booking = svc._booking_from_record({"id": "d1", "form_data": _IMPORTED_FORM, "created_date": None}, "C1",
                                       multi_booking=False, all_rows=[])
    assert booking.finance_pending is True and booking.unit_number == "C61"
    assert booking.co_applicants == ["Asha"] and booking.applicant_name == "Dheeraj Nirwal"
    assert booking.plot_address.startswith("Plot No -C61")
    assert not booking.payment_schedule


# --------------------------------------------------------------------------
# Permanent status log: admin changes, creations, deletions, missing table
# --------------------------------------------------------------------------

def _admin_sets(engine, inv_id, old, new):
    """What POST /admin/inventory/{id}/status does: the status plus its log row."""
    from Divinepersistence.persistence_inventory import record_status_change, status_actor
    with engine.begin() as c:
        c.execute(text("UPDATE divine_project_inventory SET status = :s WHERE id = :i"), {"s": new, "i": inv_id})
        record_status_change(c, inventory_id=inv_id, old_status=old, new_status=new, source="admin_panel",
                             actor=status_actor("admin", "A00001", "Ritika Sharma"),
                             changed_at=datetime.now(timezone.utc))


def test_sync_keeps_a_status_an_admin_set_by_hand(engine):
    on_sheet = seed_unit(engine, "C1", status="available")
    off_sheet = seed_unit(engine, "C2", status="available")
    _admin_sets(engine, on_sheet, "available", "held")
    _admin_sets(engine, off_sheet, "available", "sold")
    report = sync(engine, [inv_row("C1", "Available")])
    conflicts = [(plot, msg) for kind, plot, msg in report.actions if kind == "CONFLICT"]
    assert any(plot == "C1" and "admin panel" in msg for plot, msg in conflicts)
    assert any(plot == "C2" and "admin panel" in msg for plot, msg in conflicts)
    assert unit(engine, "C1")["status"] == "held" and unit(engine, "C2")["status"] == "sold"


def test_rollback_never_overwrites_a_later_admin_change(engine):
    inv_id = seed_unit(engine, "C1", status="available")
    report = sync(engine, [inv_row("C1", "Sold")])
    _admin_sets(engine, inv_id, "sold", "held")
    rolled = InventorySheetSync(engine).rollback(report.batch_id)
    assert any(kind == "KEPT" and "now 'held'" in msg for kind, _, msg in rolled.actions)
    assert unit(engine, "C1")["status"] == "held"


def test_sync_insert_and_its_rollback_are_both_in_the_log(engine):
    report = sync(engine, [inv_row("C7", "Sold")])
    inv_id = unit(engine, "C7")["id"]
    InventorySheetSync(engine).rollback(report.batch_id)
    assert unit(engine, "C7") is None
    log = q(engine, "SELECT * FROM divine_inventory_status_log WHERE inventory_id = :i ORDER BY changed_at, id",
            i=inv_id)
    assert [(r["old_status"], r["new_status"], r["source"]) for r in log] == [
        (None, "sold", "sheet_sync"), ("sold", "removed", "sync_rollback")]


def test_ingest_creates_the_log_table_when_missing_and_logs_new_plots(tmp_path):
    from Divinepersistence.persistence_inventory import InventoryStatusLogModel, persistenceInventory
    eng = create_engine(f"sqlite:///{tmp_path / 'no_log.sqlite'}")
    Base.metadata.create_all(bind=eng)
    InventoryStatusLogModel.__table__.drop(bind=eng)
    inv = persistenceInventory(session_factory=sessionmaker(bind=eng))
    inv.upsert_unit(id="u1", project_name=PROJECT, city="Sonipat", unit_number="C1", status="available",
                    area_sqmt=99.86, area_sqyd=119.43)
    log = q(eng, "SELECT old_status, new_status, source FROM divine_inventory_status_log")
    assert log == [{"old_status": None, "new_status": "available", "source": "inventory_ingest"}]


def test_log_table_created_in_a_rolled_back_transaction_is_created_again(tmp_path):
    from Divinepersistence.persistence_inventory import InventoryStatusLogModel, record_status_change
    eng = create_engine(f"sqlite:///{tmp_path / 'ddl.sqlite'}")
    Base.metadata.create_all(bind=eng)
    InventoryStatusLogModel.__table__.drop(bind=eng)
    change = dict(inventory_id="u1", old_status="available", new_status="held", source="sheet_sync")
    with pytest.raises(RuntimeError):
        with eng.begin() as c:
            record_status_change(c, **change)
            raise RuntimeError("batch failed later")
    # SQLite DDL is transactional too: the table went with the rollback.
    with eng.begin() as c:
        record_status_change(c, **change)
    assert len(q(eng, "SELECT * FROM divine_inventory_status_log")) == 1


def test_book_and_hold_reruns_by_the_same_payment_log_nothing(engine, inventory):
    booked = seed_unit(engine, "C40", status="available")
    held = seed_unit(engine, "C41", status="available")
    for _ in range(2):
        assert inventory.book_unit(id=booked, payment_id="p-book", customer_id="C00001")
        assert inventory.hold_for_kyc_review(id=held, payment_id="p-hold", customer_id="C00001")
    log = q(engine, "SELECT inventory_id, old_status, new_status FROM divine_inventory_status_log "
                    "WHERE inventory_id IN (:a, :b) ORDER BY inventory_id", a=booked, b=held)
    assert [(r["old_status"], r["new_status"]) for r in log] == [
        ("available", "booked"), ("available", "pending_kyc_review")]
    # Another payment still can't take either plot.
    assert inventory.book_unit(id=booked, payment_id="p-other") is None
    assert inventory.hold_for_kyc_review(id=held, payment_id="p-other") is None
