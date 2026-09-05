from types import SimpleNamespace
from unittest.mock import MagicMock

from DivineService.service_chatbot import serviceChatbot


class FakePersistence:
    def __init__(self, menu_state=None):
        self.session = SimpleNamespace(
            id="s1", lead_id="l1", callback_state=None, callback_name=None,
            callback_phone=None, callback_time=None, auth_state=None, auth_payload=None,
            menu_state=menu_state, menu_payload={}, loan_payload=None,
        )
        self.menu_state_updates = []

    def get_session_by_id(self, id):
        return self.session

    def touch_session(self, id):
        return self.session

    def update_session_menu_state(self, id, menu_state, menu_payload=None):
        self.menu_state_updates.append(menu_state)
        self.session.menu_state = menu_state
        self.session.menu_payload = menu_payload
        return self.session

    def update_session_callback_state(self, id, callback_state, callback_name=None, callback_phone=None, callback_time=None):
        self.session.callback_state = callback_state
        return self.session

    def update_session_loan_state(self, id, loan_payload=None):
        self.session.loan_payload = loan_payload
        return self.session

    def list_recent_messages(self, session_id, limit=20):
        return []

    def create_message(self, **kwargs):
        return SimpleNamespace(**kwargs)


def _make_service(persistence, gemini=None, groq=None):
    return serviceChatbot(
        persistence=persistence, gemini=gemini or MagicMock(), groq=groq or MagicMock(),
        zoho=None, loan_persistence=MagicMock(),
    )


def test_request_callback_mid_funnel_clears_menu_state():
    persistence = FakePersistence(menu_state="sales_track")
    svc = _make_service(persistence)

    result = svc.handle_message(session_id="s1", text="", intent="request_callback")

    assert result["reply"] == "Sure! May I know your name?"
    assert None in persistence.menu_state_updates
    assert persistence.session.menu_state is None


def test_request_callback_with_no_active_menu_state_is_unaffected():
    persistence = FakePersistence(menu_state=None)
    svc = _make_service(persistence)

    result = svc.handle_message(session_id="s1", text="", intent="request_callback")

    assert result["reply"] == "Sure! May I know your name?"
    assert persistence.menu_state_updates == []


def test_structured_result_survives_guardrail_path_when_kb_and_loan_tool_both_called():
    persistence = FakePersistence(menu_state=None)
    gemini = MagicMock()
    # Round 1: model calls the knowledge-base tool (forces the guardrail/used_kb path).
    # Round 2: model also calls calculate_emi in the same turn.
    # Round 3: model returns final text.
    gemini.generate.side_effect = [
        {"text": None, "function_call": {"name": "search_knowledge_base", "args": {"query": "RERA"}}},
        {"text": None, "function_call": {"name": "calculate_emi", "args": {"principal": 5000000, "annual_rate_pct": 8.5, "tenure_years": 20}}},
        {"text": "Here is the info and your EMI estimate.", "function_call": None},
    ]
    gemini.embed.return_value = [0.1, 0.2]
    groq = MagicMock()
    groq.judge.return_value = 0.9  # passes GUARDRAIL_THRESHOLD

    svc = _make_service(persistence, gemini=gemini, groq=groq)
    svc._persistence.search_kb_chunks = MagicMock(return_value=[])

    result = svc.handle_message(session_id="s1", text="Tell me about RERA and calculate my EMI for 50 lakh")

    assert result.get("structured_result") is not None
    assert result["structured_result"]["type"] == "emi_result"
    assert result["structured_result"]["data"]["emi"] > 0


# ---- eligibility report: absolute URL + clean reply ----

_RID = "fa7deb30-63a8-4c22-ae48-86e247941e2f"


def _svc_with_calc(persistence=None):
    persistence = persistence or FakePersistence()
    svc = _make_service(persistence)
    svc._get_loan_payload = MagicMock(return_value={
        "monthly_income": 120000,
        "existing_emi": 8000,
        "tenure_years": 20,
        "last_calculation": {
            "emi": {"emi": 34713, "annual_rate_pct": 8.5, "tenure_months": 240,
                    "total_interest": 4331120, "total_payment": 8331120,
                    "illustrative_rate_used": True},
            "eligibility": {"eligible_loan_range": {"low": 3600000, "high": 4250000},
                            "combined_income": 120000, "foir_cap": 0.5, "category": "comfortable"},
        },
    })
    svc._persistence.get_lead_by_id = MagicMock(return_value=SimpleNamespace(
        visitor_name="Vansh Duggal", visitor_phone="7276971875", visitor_email="v@example.com"))
    svc._loan_persistence.create_loan_report = MagicMock(
        return_value=SimpleNamespace(id=_RID, created_date=None))
    return svc


def test_generate_report_tool_returns_absolute_urls_and_data():
    svc = _svc_with_calc()
    out = svc._tool_generate_eligibility_report(svc._persistence.session)
    assert out["download_url"] == f"https://divinevisioninfrabackend.onrender.com/loan/report/{_RID}/download"
    assert out["download_path"] == f"/loan/report/{_RID}/download"
    # data_url carries the session id so the widget's own re-fetch gets the PII fields
    assert out["data_url"] == f"https://divinevisioninfrabackend.onrender.com/loan/report/{_RID}?session_id=s1"
    assert out["filename"] == f"home-loan-eligibility-{_RID}.pdf"
    assert out["auto_download"] is True

    report = out["report"]
    assert report["report_id"] == _RID
    assert report["eligible_amount"] == 4250000
    assert "Lakh" in report["eligible_amount_words"]
    assert report["rate_pct"] == 8.5
    assert report["rate_is_illustrative"] is True
    assert report["tenure_months"] == 240
    assert report["emi"] == 34713
    assert report["foir_pct"] == 50.0
    assert report["applicant"] == {"name": "Vansh Duggal", "phone": "7276971875", "email": "v@example.com"}


def test_generate_report_applicant_is_empty_when_lead_missing():
    svc = _svc_with_calc()
    svc._persistence.get_lead_by_id = MagicMock(side_effect=AttributeError)
    out = svc._tool_generate_eligibility_report(svc._persistence.session)
    assert out["report"]["applicant"] == {"name": None, "phone": None, "email": None}


def test_report_ready_reply_is_overridden_and_has_no_url():
    svc = _make_service(FakePersistence())
    leaked = "Here's your report: Download URL: [/loan/report/abc/download]"
    structured = {"type": "report_ready", "data": {
        "download_url": "https://divinevisioninfrabackend.onrender.com/loan/report/abc/download",
        "auto_download": True,
    }}
    result = svc._with_structured_result({"reply": leaked, "llm_provider": "gemini"}, structured)
    assert "/loan/report/" not in result["reply"]
    assert "http" not in result["reply"]
    assert "downloading now" in result["reply"].lower()
    assert result["structured_result"]["data"]["auto_download"] is True


def test_report_ready_button_carries_absolute_url_and_filename():
    svc = _make_service(FakePersistence())
    svc._get_loan_payload = MagicMock(return_value={"last_calculation": {"emi": 1}})
    structured = {"type": "report_ready", "data": {
        "download_url": "https://divinevisioninfrabackend.onrender.com/loan/report/abc/download",
        "filename": "home-loan-eligibility-abc.pdf",
    }}
    buttons = svc._loan_buttons_for_response(svc._persistence.session, structured)
    dl = next(b for b in buttons if b.get("action") == "download_link")
    assert dl["url"].startswith("https://")
    assert dl["filename"] == "home-loan-eligibility-abc.pdf"
    assert not any(b.get("value") == "loan_download_report" for b in buttons)


def test_model_facing_tool_result_strips_report_and_urls():
    from DivineService.service_chatbot import serviceChatbot
    full = {
        "report_id": _RID,
        "download_url": "https://x/loan/report/abc/download",
        "data_url": "https://x/loan/report/abc",
        "filename": "home-loan-eligibility-abc.pdf",
        "auto_download": True,
        "report": {"applicant": {"name": "Vansh", "phone": "72769", "email": "v@e.com"},
                   "eligible_amount": 4250000},
    }
    trimmed = serviceChatbot._model_facing_tool_result("generate_eligibility_report", full)
    assert trimmed == {"report_id": _RID, "status": "ready"}
    # non-report tools pass through untouched
    passthrough = {"emi": 34713, "annual_rate_pct": 8.5}
    assert serviceChatbot._model_facing_tool_result("calculate_emi", passthrough) is passthrough
    # an error result is not trimmed (the model needs to see the failure)
    err = {"error": "no_calculation_yet"}
    assert serviceChatbot._model_facing_tool_result("generate_eligibility_report", err) is err


# ---- deterministic EMI fallback when both LLM providers are down ----

def test_loan_math_fallback_computes_emi_from_payload():
    svc = _make_service(FakePersistence())
    svc._get_loan_payload = MagicMock(return_value={"requested_loan": "40 lakh", "tenure_years": 20})
    out = svc._loan_math_fallback(svc._persistence.session)
    assert out is not None
    assert out["structured_result"]["type"] == "emi_result"
    assert out["structured_result"]["data"]["emi"] > 0
    assert "indicative" in out["reply"].lower()
    assert "http" not in out["reply"]


def test_loan_math_fallback_returns_none_without_enough_numbers():
    svc = _make_service(FakePersistence())
    svc._get_loan_payload = MagicMock(return_value={"monthly_income": 120000})  # no loan amount / tenure
    assert svc._loan_math_fallback(svc._persistence.session) is None


def test_loan_math_fallback_never_raises_on_garbage_payload():
    svc = _make_service(FakePersistence())
    svc._get_loan_payload = MagicMock(return_value={"requested_loan": "abc", "tenure_years": "xyz"})
    assert svc._loan_math_fallback(svc._persistence.session) is None


def test_loan_math_fallback_sets_illustrative_rate_flag():
    svc = _make_service(FakePersistence())
    # no interest_rate in payload -> falls back to the illustrative default
    svc._get_loan_payload = MagicMock(return_value={"requested_loan": "40 lakh", "tenure_years": 20})
    out = svc._loan_math_fallback(svc._persistence.session)
    assert out["structured_result"]["data"]["illustrative_rate_used"] is True

    svc._get_loan_payload = MagicMock(return_value={"requested_loan": "40 lakh", "tenure_years": 20, "interest_rate": 9.1})
    out = svc._loan_math_fallback(svc._persistence.session)
    assert out["structured_result"]["data"]["illustrative_rate_used"] is False
