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
