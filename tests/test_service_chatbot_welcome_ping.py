from types import SimpleNamespace

from DivineService.service_chatbot import serviceChatbot


class FakePersistence:
    """Minimal chatbot persistence for the widget-open ping path, with menu_state
    starting unset (as it would be if init_session's menu_state write failed or the
    integration skipped /session/init)."""

    def __init__(self, messages=None, list_messages_raises=False):
        self.session = SimpleNamespace(
            id="s1", lead_id="l1", callback_state=None, callback_name=None,
            callback_phone=None, callback_time=None, auth_state=None, auth_payload=None,
            menu_state=None, menu_payload=None, loan_payload=None,
        )
        self.messages = list(messages or [])
        self.menu_state_updates = []
        self._list_messages_raises = list_messages_raises

    def get_session_by_id(self, id):
        return self.session

    def touch_session(self, id):
        return self.session

    def list_recent_messages(self, session_id, limit=20):
        if self._list_messages_raises:
            raise RuntimeError("db down")
        return self.messages[-limit:]

    def update_session_menu_state(self, id, menu_state, menu_payload=None):
        self.menu_state_updates.append(menu_state)
        self.session.menu_state = menu_state
        self.session.menu_payload = menu_payload
        return self.session

    def create_message(self, **kwargs):
        self.messages.append(SimpleNamespace(**kwargs))
        return SimpleNamespace(**kwargs)


def _service(persistence):
    return serviceChatbot(persistence=persistence, gemini=object(), groq=object(), zoho=None)


def test_empty_ping_on_fresh_unarmed_session_returns_welcome_and_arms_funnel():
    persistence = FakePersistence()
    service = _service(persistence)

    result = service.handle_message("s1", text="")

    assert "welcome to divine vision" in result["reply"].lower()
    assert "didn't catch that" not in result["reply"].lower()
    assert persistence.session.menu_state == "greeting_name"


def test_empty_ping_mid_conversation_still_returns_didnt_catch_that():
    persistence = FakePersistence(messages=[SimpleNamespace(role="user", content="hi")])
    service = _service(persistence)

    result = service.handle_message("s1", text="")

    assert "didn't catch that" in result["reply"].lower()
    assert persistence.session.menu_state is None


def test_empty_ping_greets_when_message_lookup_fails():
    persistence = FakePersistence(list_messages_raises=True)
    service = _service(persistence)

    result = service.handle_message("s1", text="")

    assert "welcome to divine vision" in result["reply"].lower()
    assert persistence.session.menu_state == "greeting_name"


def test_non_empty_message_on_unarmed_session_is_unaffected():
    persistence = FakePersistence()
    service = _service(persistence)

    # A real question with no funnel armed should NOT get the greeting - it falls
    # through to the normal agent loop (which errors out here with no real LLM).
    try:
        result = service.handle_message("s1", text="what plot sizes do you have?")
    except Exception:
        result = None

    if result is not None:
        assert "welcome to divine vision" not in result["reply"].lower()
    assert "greeting_name" not in persistence.menu_state_updates
