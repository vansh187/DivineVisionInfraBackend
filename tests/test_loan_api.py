import pytest
from fastapi.testclient import TestClient

from Divinepersistence.persistence_db import engine
from sqlalchemy import text
from DivineAPI.main import app

client = TestClient(app)


@pytest.fixture(autouse=True, scope="module")
def _ensure_loan_reports_table():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS divine_loan_reports (
              id varchar(36) PRIMARY KEY,
              session_id varchar(36),
              lead_id varchar(36),
              snapshot_json text NOT NULL,
              created_date timestamptz DEFAULT (datetime('now'))
            );
        """))


def test_emi_endpoint_happy_path():
    resp = client.post("/loan/emi", json={"principal": 5000000, "annual_rate_pct": 8.5, "tenure_years": 20})
    assert resp.status_code == 200
    body = resp.json()
    assert body["emi"] == pytest.approx(43391.16, abs=0.5)
    assert body["schedule"] is None


def test_emi_endpoint_with_schedule():
    resp = client.post("/loan/emi", json={
        "principal": 1000000, "annual_rate_pct": 8.5, "tenure_years": 5, "include_schedule": True,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["schedule"]) == 60


def test_emi_endpoint_rejects_invalid_input():
    resp = client.post("/loan/emi", json={"principal": -1, "annual_rate_pct": 8.5, "tenure_years": 20})
    assert resp.status_code == 422


def test_eligibility_endpoint_happy_path():
    resp = client.post("/loan/eligibility", json={"monthly_income": 180000, "existing_emi": 15000})
    assert resp.status_code == 200
    body = resp.json()
    assert body["category"] in ("Strong", "Moderate", "Low")
    assert body["illustrative_rate_used"] is True


def test_affordability_endpoint_happy_path():
    resp = client.post("/loan/affordability", json={
        "property_price": 12000000, "down_payment": 2500000, "monthly_income": 160000, "existing_emi": 12000,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["required_loan"] == 9500000
    assert "suggestions" in body


def test_affordability_endpoint_rejects_downpayment_exceeding_price():
    resp = client.post("/loan/affordability", json={
        "property_price": 5000000, "down_payment": 6000000, "monthly_income": 100000,
    })
    assert resp.status_code == 422


def test_compare_endpoint():
    resp = client.post("/loan/compare", json={
        "principal": 5000000, "annual_rate_pct": 8.5, "tenure_options_years": [15, 20, 25],
    })
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert [r["tenure_years"] for r in rows] == [15, 20, 25]


def test_report_generate_and_download_round_trip():
    payload = {
        "profile": {"monthly_income": 160000, "existing_emi": 12000, "employment_type": "salaried", "tenure_years": 20},
        "last_calculation": {"emi": {"principal": 8000000, "annual_rate_pct": 8.5, "tenure_years": 20,
                                      "emi": 69000, "total_interest": 8100000, "total_payment": 15600000}},
    }
    create_resp = client.post("/loan/report", json=payload)
    assert create_resp.status_code == 200
    report_id = create_resp.json()["report_id"]

    download_resp = client.get(f"/loan/report/{report_id}/download")
    assert download_resp.status_code == 200
    assert download_resp.headers["content-type"] == "application/pdf"
    assert download_resp.content[:4] == b"%PDF"


def test_report_download_missing_id_returns_404():
    resp = client.get("/loan/report/does-not-exist/download")
    assert resp.status_code == 404
