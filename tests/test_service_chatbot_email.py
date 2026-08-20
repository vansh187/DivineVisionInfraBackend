from types import SimpleNamespace

from DivineService.service_chatbot import serviceChatbot, wants_email_contact


class FakeChatbotPersistence:
    def __init__(self):
        self.session = SimpleNamespace(id="session-1", lead_id="lead-1", callback_state=None)
        self.lead_updates = []
        self.email_updates = []
        self.state_updates = []
        self.messages = []
        self.fail_email_save = False
        self.fail_state_update = False

    def get_session_by_id(self, id):
        return self.session

    def touch_session(self, id):
        return self.session

    def update_session_callback_state(self, id, callback_state, callback_name=None, callback_phone=None, callback_time=None):
        if self.fail_state_update:
            raise RuntimeError("state update failed")
        self.session.callback_state = callback_state
        self.state_updates.append(callback_state)
        return self.session

    def update_lead_fields(self, id, visitor_name=None, visitor_phone=None, lead_temperature=None):
        update = {
            "id": id,
            "visitor_name": visitor_name,
            "visitor_phone": visitor_phone,
            "lead_temperature": lead_temperature,
        }
        self.lead_updates.append(update)
        return SimpleNamespace(**update)

    def update_lead_email(self, id, visitor_email):
        if self.fail_email_save:
            raise RuntimeError("email save failed")
        update = {"id": id, "visitor_email": visitor_email}
        self.email_updates.append(update)
        return SimpleNamespace(**update)

    def create_message(self, **kwargs):
        self.messages.append(kwargs)
        return SimpleNamespace(**kwargs)


def test_email_connect_request_asks_for_email_then_saves_to_lead():
    persistence = FakeChatbotPersistence()
    service = serviceChatbot(persistence=persistence, gemini=object(), groq=object())

    first = service.handle_message("session-1", text="I want to connect via email")

    assert "email address" in first["reply"]
    assert persistence.state_updates == ["awaiting_email"]
    assert persistence.lead_updates == []

    second = service.handle_message("session-1", text="my email is buyer@example.com")

    assert second["email_confirmed"]["email"] == "buyer@example.com"
    assert persistence.state_updates[-1] == "email_complete"
    assert persistence.email_updates[-1]["id"] == "lead-1"
    assert persistence.email_updates[-1]["visitor_email"] == "buyer@example.com"


def test_email_connect_request_with_email_saves_immediately():
    persistence = FakeChatbotPersistence()
    service = serviceChatbot(persistence=persistence, gemini=object(), groq=object())

    result = service.handle_message("session-1", text="Please connect with me by email at buyer@example.com")

    assert result["email_confirmed"]["email"] == "buyer@example.com"
    assert persistence.state_updates == ["email_complete"]
    assert persistence.email_updates[-1]["visitor_email"] == "buyer@example.com"


def test_email_capture_only_starts_for_email_connection_requests():
    assert wants_email_contact("I want to connect via email") is True
    assert wants_email_contact("can you email me the details") is True
    assert wants_email_contact("mujhe mail par details bhejo") is True
    assert wants_email_contact("what is your email address?") is False
    assert wants_email_contact("do you have company email?") is False


def test_invalid_email_keeps_asking_without_throwing():
    persistence = FakeChatbotPersistence()
    persistence.session.callback_state = "awaiting_email"
    service = serviceChatbot(persistence=persistence, gemini=object(), groq=object())

    result = service.handle_message("session-1", text="not-an-email")

    assert "valid email" in result["reply"]
    assert persistence.email_updates == []


def test_email_save_failure_returns_friendly_reply_without_throwing():
    persistence = FakeChatbotPersistence()
    persistence.fail_email_save = True
    service = serviceChatbot(persistence=persistence, gemini=object(), groq=object())

    result = service.handle_message("session-1", text="Please email me at buyer@example.com")

    assert "trouble saving your email" in result["reply"]
    assert persistence.state_updates == []


def test_email_state_failure_returns_friendly_reply_without_throwing():
    persistence = FakeChatbotPersistence()
    persistence.fail_state_update = True
    service = serviceChatbot(persistence=persistence, gemini=object(), groq=object())

    result = service.handle_message("session-1", text="I want to connect via email")

    assert "trouble starting the email request" in result["reply"]
    assert persistence.email_updates == []


def test_upsert_crm_lead_keeps_name_phone_path_separate_from_email():
    persistence = FakeChatbotPersistence()
    service = serviceChatbot(persistence=persistence, gemini=object(), groq=object())

    result = service._tool_upsert_crm_lead(persistence.session, "Asha", "9876543210")

    assert result == {"lead_id": "lead-1"}
    assert persistence.lead_updates[-1]["visitor_name"] == "Asha"
    assert persistence.lead_updates[-1]["visitor_phone"] == "9876543210"
    assert persistence.email_updates == []
