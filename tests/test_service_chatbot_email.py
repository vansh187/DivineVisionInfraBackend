from types import SimpleNamespace

from DivineService.service_chatbot import (
    extract_phone,
    normalize_phone,
    selected_auth_flow,
    serviceChatbot,
    valid_phone,
    extract_email,
    wants_email_contact,
    wants_phone_contact,
    wants_plot_booking,
)


class FakeAuthService:
    def __init__(self, role):
        self.role = role
        self.created = []
        self.logins = []
        self.username_taken = False
        self.invalid_login = False

    def signup(self, dto, created_by=None):
        if self.username_taken:
            raise ValueError("username_taken")
        self.created.append(dto)
        prefix = "C" if self.role == "customer" else "B"
        return SimpleNamespace(id=f"{prefix}00001", username=dto.username)

    def login(self, dto):
        if self.invalid_login:
            raise ValueError("invalid_credentials")
        self.logins.append(dto)
        return f"{self.role}-token"

    def login_by_email(self, email, password):
        if self.invalid_login:
            raise ValueError("invalid_credentials")
        login = SimpleNamespace(email=email, password=password)
        self.logins.append(login)
        return f"{self.role}-token"


class FakeChatbotPersistence:
    def __init__(self):
        self.session = SimpleNamespace(
            id="session-1", lead_id="lead-1", callback_state=None,
            callback_name=None, callback_phone=None, callback_time=None,
            auth_state=None, auth_payload=None,
        )
        self.lead_updates = []
        self.email_updates = []
        self.state_updates = []
        self.messages = []
        self.fail_email_save = False
        self.fail_state_update = False
        self.fail_auth_state_update = False

    def get_session_by_id(self, id):
        return self.session

    def touch_session(self, id):
        return self.session

    def update_session_callback_state(self, id, callback_state, callback_name=None, callback_phone=None, callback_time=None):
        if self.fail_state_update:
            raise RuntimeError("state update failed")
        self.session.callback_state = callback_state
        if callback_name is not None:
            self.session.callback_name = callback_name
        if callback_phone is not None:
            self.session.callback_phone = callback_phone
        if callback_time is not None:
            self.session.callback_time = callback_time
        self.state_updates.append(callback_state)
        return self.session

    def update_session_auth_state(self, id, auth_state, auth_payload=None):
        if self.fail_auth_state_update:
            raise RuntimeError("auth state update failed")
        self.session.auth_state = auth_state
        self.session.auth_payload = auth_payload
        self.state_updates.append(auth_state)
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
    assert wants_email_contact("e mail pe contact karna") is True
    assert wants_email_contact("please reply through mail only") is True
    assert wants_email_contact("what is your email address?") is False
    assert wants_email_contact("do you have company email?") is False


def test_phone_contact_detection_handles_common_callback_phrases():
    assert wants_phone_contact("please call me") is True
    assert wants_phone_contact("mobile pe contact karna") is True
    assert wants_phone_contact("mujhe whatsapp par message karo") is True
    assert wants_phone_contact("what is your phone number?") is False
    assert wants_phone_contact("company number kya hai?") is False


def test_phone_normalization_handles_noisy_indian_mobile_strings():
    assert valid_phone("my number is +91 98765-43210") is True
    assert valid_phone("call me at 09876543210") is True
    assert valid_phone("2bhk query, phone 9876543210") is True
    assert extract_phone("2bhk query, phone 9876543210") == "9876543210"
    assert normalize_phone("my mobile: +91 98765 43210") == "9876543210"
    assert valid_phone("12345") is False


def test_plot_booking_request_returns_project_buttons():
    persistence = FakeChatbotPersistence()
    service = serviceChatbot(persistence=persistence, gemini=object(), groq=object())

    result = service.handle_message("session-1", text="i want book the 250 sq ft plot")

    assert wants_plot_booking("i want book the 250 sq ft plot") is True
    assert "which project" in result["reply"].lower()
    assert result["buttons"] == [
        {"label": "OPS Project", "value": "book_project_ops", "action": "select_booking_project"},
        {"label": "Suraksha Project", "value": "book_project_suraksha", "action": "select_booking_project"},
    ]


def test_plot_booking_detection_handles_any_size_and_common_phrases():
    booking_phrases = [
        "i want book the 250 sq ft plot",
        "book 500 sqft plot",
        "reserve 1200 square feet land",
        "I want to buy 900 sq. ft unit",
        "plot booking karni hai 100 gaj ki",
        "application form chahiye for 750 sq yd plot",
        "interested in 600sft property",
        "kharidna hai 1000 square foot plot",
        "book 5 marla plot",
        "10 marlas ki booking karni hai",
    ]

    for phrase in booking_phrases:
        assert wants_plot_booking(phrase) is True

    assert wants_plot_booking("I want to book a site visit") is False
    assert wants_plot_booking("what is the price of 250 sq ft plot?") is False


def test_booking_project_selection_returns_customer_login_button():
    for selected_project in ("book_project_ops", "Suraksha Project"):
        persistence = FakeChatbotPersistence()
        service = serviceChatbot(persistence=persistence, gemini=object(), groq=object())

        result = service.handle_message("session-1", text=selected_project)

        assert "login as a customer" in result["reply"].lower()
        assert "application form" in result["reply"].lower()
        assert result["buttons"] == [{
            "label": "Login as Customer",
            "value": "login_customer",
            "action": "chatbot_auth",
        }]


def test_customer_signup_flow_collects_form_fields_and_offers_login():
    persistence = FakeChatbotPersistence()
    customer_auth = FakeAuthService("customer")
    service = serviceChatbot(
        persistence=persistence, gemini=object(), groq=object(), customer_service=customer_auth,
    )

    assert "first name" in service.handle_message("session-1", text="signup as a customer")["reply"].lower()
    assert "last name" in service.handle_message("session-1", text="Vansh")["reply"].lower()
    assert "email" in service.handle_message("session-1", text="Sharma")["reply"].lower()
    assert "phone" in service.handle_message("session-1", text="vansh@example.com")["reply"].lower()
    assert "username" in service.handle_message("session-1", text="+91 98765 43210")["reply"].lower()
    assert "password" in service.handle_message("session-1", text="vansh_customer")["reply"].lower()
    result = service.handle_message("session-1", text="strongpassword")

    assert result["account_created"] == {"role": "customer", "username": "vansh_customer"}
    assert result["buttons"] == [{"label": "Login as Customer", "value": "login_customer", "action": "chatbot_auth"}]
    assert customer_auth.created[-1].first_name == "Vansh"
    assert customer_auth.created[-1].last_name == "Sharma"
    assert customer_auth.created[-1].email == "vansh@example.com"
    assert customer_auth.created[-1].phone == "9876543210"
    assert customer_auth.created[-1].password == "strongpassword"
    assert persistence.session.auth_state == "post_signup_customer_confirm"
    assert "[password hidden]" in [m["content"] for m in persistence.messages]
    assert "strongpassword" not in [m["content"] for m in persistence.messages]


def test_customer_can_login_by_replying_yes_after_signup():
    persistence = FakeChatbotPersistence()
    customer_auth = FakeAuthService("customer")
    service = serviceChatbot(
        persistence=persistence, gemini=object(), groq=object(), customer_service=customer_auth,
    )

    service.handle_message("session-1", text="signup as a customer")
    service.handle_message("session-1", text="Vansh")
    service.handle_message("session-1", text="Demo")
    service.handle_message("session-1", text="vansh@example.com")
    service.handle_message("session-1", text="9876543210")
    service.handle_message("session-1", text="vanshdemo")
    created = service.handle_message("session-1", text="vanshdemo123")
    prompt = service.handle_message("session-1", text="yes")
    result = service.handle_message("session-1", text="vanshdemo123")

    assert "do you want to login now" in created["reply"].lower()
    assert "password" in prompt["reply"].lower()
    assert result["auth_token"] == "customer-token"
    assert result["auth_role"] == "customer"
    assert customer_auth.logins[-1].email == "vansh@example.com"
    assert customer_auth.logins[-1].password == "vanshdemo123"
    assert persistence.session.auth_state is None
    assert "[password hidden]" in [m["content"] for m in persistence.messages]
    assert "vanshdemo123" not in [m["content"] for m in persistence.messages]


def test_post_signup_no_clears_login_prompt_state():
    persistence = FakeChatbotPersistence()
    persistence.session.auth_state = "post_signup_customer_confirm"
    persistence.session.auth_payload = {"mode": "post_signup", "role": "customer", "email": "vansh@example.com"}
    service = serviceChatbot(persistence=persistence, gemini=object(), groq=object())

    result = service.handle_message("session-1", text="no")

    assert "no problem" in result["reply"].lower()
    assert persistence.session.auth_state is None


def test_post_signup_unclear_reply_keeps_yes_no_prompt():
    persistence = FakeChatbotPersistence()
    persistence.session.auth_state = "post_signup_broker_confirm"
    persistence.session.auth_payload = {"mode": "post_signup", "role": "broker", "email": "broker@example.com"}
    service = serviceChatbot(persistence=persistence, gemini=object(), groq=object())

    result = service.handle_message("session-1", text="maybe later maybe now")

    assert "reply yes" in result["reply"].lower()
    assert result["buttons"] == [{"label": "Login as Broker", "value": "login_broker", "action": "chatbot_auth"}]
    assert persistence.session.auth_state == "post_signup_broker_confirm"


def test_post_signup_missing_email_returns_fallback_without_throwing():
    persistence = FakeChatbotPersistence()
    persistence.session.auth_state = "post_signup_customer_confirm"
    persistence.session.auth_payload = {"mode": "post_signup", "role": "customer"}
    service = serviceChatbot(persistence=persistence, gemini=object(), groq=object())

    result = service.handle_message("session-1", text="yes")

    assert result["reply"]
    assert persistence.session.auth_state is None


def test_broker_signup_flow_accepts_skipped_optional_fields():
    persistence = FakeChatbotPersistence()
    broker_auth = FakeAuthService("broker")
    service = serviceChatbot(
        persistence=persistence, gemini=object(), groq=object(), broker_service=broker_auth,
    )

    service.handle_message("session-1", text="broker signup")
    service.handle_message("session-1", text="Ravi")
    service.handle_message("session-1", text="skip")
    service.handle_message("session-1", text="skip")
    service.handle_message("session-1", text="skip")
    service.handle_message("session-1", text="ravi_broker")
    result = service.handle_message("session-1", text="anotherstrongpass")

    assert result["account_created"] == {"role": "broker", "username": "ravi_broker"}
    assert "email address" in result["reply"].lower()
    assert "buttons" not in result
    assert persistence.session.auth_state is None
    assert broker_auth.created[-1].first_name == "Ravi"
    assert broker_auth.created[-1].last_name is None
    assert broker_auth.created[-1].email is None
    assert broker_auth.created[-1].phone is None


def test_broker_login_flow_returns_token_and_masks_password():
    persistence = FakeChatbotPersistence()
    broker_auth = FakeAuthService("broker")
    service = serviceChatbot(
        persistence=persistence, gemini=object(), groq=object(), broker_service=broker_auth,
    )

    assert "email" in service.handle_message("session-1", text="login as broker")["reply"].lower()
    assert "password" in service.handle_message("session-1", text="broker1@example.com")["reply"].lower()
    result = service.handle_message("session-1", text="correct-password")

    assert result["auth_token"] == "broker-token"
    assert result["auth_role"] == "broker"
    assert broker_auth.logins[-1].email == "broker1@example.com"
    assert broker_auth.logins[-1].password == "correct-password"
    assert persistence.session.auth_state is None
    assert "[password hidden]" in [m["content"] for m in persistence.messages]
    assert "correct-password" not in [m["content"] for m in persistence.messages]


def test_customer_login_accepts_single_payload_with_email_and_password():
    persistence = FakeChatbotPersistence()
    customer_auth = FakeAuthService("customer")
    service = serviceChatbot(
        persistence=persistence, gemini=object(), groq=object(), customer_service=customer_auth,
    )

    service.handle_message("session-1", text="login as customer")
    result = service.handle_message(
        "session-1",
        text='{email: "vansh.duggal\\@gmail.com", password: "vanshduggal123"}',
    )

    assert result["auth_token"] == "customer-token"
    assert result["auth_role"] == "customer"
    assert customer_auth.logins[-1].email == "vansh.duggal@gmail.com"
    assert customer_auth.logins[-1].password == "vanshduggal123"
    assert persistence.session.auth_state is None
    assert "[password hidden]" in [m["content"] for m in persistence.messages]
    assert "vanshduggal123" not in [m["content"] for m in persistence.messages]


def test_customer_login_accepts_legacy_frontend_username_key_when_value_is_email():
    persistence = FakeChatbotPersistence()
    customer_auth = FakeAuthService("customer")
    service = serviceChatbot(
        persistence=persistence, gemini=object(), groq=object(), customer_service=customer_auth,
    )

    service.handle_message("session-1", text="login as customer")
    result = service.handle_message(
        "session-1",
        text='{"username":"vansh.demo\\@gmail.com","password":"vanshdemo123"}',
    )

    assert result["auth_token"] == "customer-token"
    assert result["auth_role"] == "customer"
    assert customer_auth.logins[-1].email == "vansh.demo@gmail.com"
    assert customer_auth.logins[-1].password == "vanshdemo123"
    assert "[password hidden]" in [m["content"] for m in persistence.messages]
    assert "vanshdemo123" not in [m["content"] for m in persistence.messages]


def test_extract_email_accepts_escaped_at_symbol():
    assert extract_email("vansh.duggal\\@gmail.com") == "vansh.duggal@gmail.com"


def test_login_flow_rejects_invalid_email_without_advancing():
    persistence = FakeChatbotPersistence()
    service = serviceChatbot(persistence=persistence, gemini=object(), groq=object())

    service.handle_message("session-1", text="login as customer")
    result = service.handle_message("session-1", text="not-an-email")

    assert "valid email" in result["reply"].lower()
    assert persistence.session.auth_state == "login_customer_email"


def test_login_flow_invalid_credentials_restarts_at_email_and_masks_password():
    persistence = FakeChatbotPersistence()
    customer_auth = FakeAuthService("customer")
    customer_auth.invalid_login = True
    service = serviceChatbot(
        persistence=persistence, gemini=object(), groq=object(), customer_service=customer_auth,
    )

    service.handle_message("session-1", text="login as customer")
    service.handle_message("session-1", text="buyer@example.com")
    result = service.handle_message("session-1", text="wrong-password")

    assert "invalid email or password" in result["reply"].lower()
    assert persistence.session.auth_state == "login_customer_email"
    assert persistence.session.auth_payload == {"mode": "login", "role": "customer", "login_attempts": 1}
    assert "[password hidden]" in [m["content"] for m in persistence.messages]
    assert "wrong-password" not in [m["content"] for m in persistence.messages]


def test_explicit_customer_login_restarts_stale_auth_state():
    persistence = FakeChatbotPersistence()
    persistence.session.auth_state = "login_customer_password"
    persistence.session.auth_payload = {"mode": "login", "role": "customer", "email": "old@example.com"}
    customer_auth = FakeAuthService("customer")
    service = serviceChatbot(
        persistence=persistence, gemini=object(), groq=object(), customer_service=customer_auth,
    )

    result = service.handle_message("session-1", text="login as customer")

    assert "email" in result["reply"].lower()
    assert persistence.session.auth_state == "login_customer_email"
    assert persistence.session.auth_payload == {"mode": "login", "role": "customer"}
    assert customer_auth.logins == []


def test_auth_flow_detection_handles_common_login_and_signup_variants():
    cases = {
        "login as customer": ("login", "customer"),
        "customer login": ("login", "customer"),
        "login-customer": ("login", "customer"),
        "customer_login": ("login", "customer"),
        "sign me in as customer": ("login", "customer"),
        "customer portal login": ("login", "customer"),
        "mujhe customer login karna hai": ("login", "customer"),
        "login as broker": ("login", "broker"),
        "broker login": ("login", "broker"),
        "broker_login": ("login", "broker"),
        "agent sign in": ("login", "broker"),
        "channel partner portal login": ("login", "broker"),
        "mujhe broker login karna hai": ("login", "broker"),
        "signup as customer": ("signup", "customer"),
        "customer signup": ("signup", "customer"),
        "register customer": ("signup", "customer"),
        "create customer account": ("signup", "customer"),
        "buyer registration": ("signup", "customer"),
        "customer account banana hai": ("signup", "customer"),
        "signup as broker": ("signup", "broker"),
        "broker signup": ("signup", "broker"),
        "register broker": ("signup", "broker"),
        "create broker account": ("signup", "broker"),
        "agent registration": ("signup", "broker"),
        "broker account banana hai": ("signup", "broker"),
    }

    for phrase, expected in cases.items():
        assert selected_auth_flow(phrase) == expected


def test_generic_login_request_returns_auth_choice_buttons():
    persistence = FakeChatbotPersistence()
    service = serviceChatbot(persistence=persistence, gemini=object(), groq=object())

    result = service.handle_message("session-1", text="I want to login")

    assert "choose" in result["reply"].lower()
    assert {"label": "Customer Login", "value": "login_customer", "action": "chatbot_auth"} in result["buttons"]
    assert {"label": "Broker Login", "value": "login_broker", "action": "chatbot_auth"} in result["buttons"]


def test_auth_state_update_failure_returns_reply_without_throwing():
    persistence = FakeChatbotPersistence()
    persistence.fail_auth_state_update = True
    service = serviceChatbot(persistence=persistence, gemini=object(), groq=object())

    result = service.handle_message("session-1", text="signup as customer")

    assert "trouble starting signup" in result["reply"].lower()
    assert persistence.session.auth_state is None


def test_corrupt_auth_state_returns_fallback_without_throwing():
    persistence = FakeChatbotPersistence()
    persistence.session.auth_state = "broken"
    service = serviceChatbot(persistence=persistence, gemini=object(), groq=object())

    result = service.handle_message("session-1", text="anything")

    assert result["reply"]
    assert persistence.session.auth_state is None


def test_invalid_email_keeps_asking_without_throwing():
    persistence = FakeChatbotPersistence()
    persistence.session.callback_state = "awaiting_email"
    service = serviceChatbot(persistence=persistence, gemini=object(), groq=object())

    result = service.handle_message("session-1", text="not-an-email")

    assert "valid email" in result["reply"]
    assert persistence.email_updates == []


def test_callback_phone_step_switches_to_email_when_visitor_prefers_email():
    persistence = FakeChatbotPersistence()
    service = serviceChatbot(persistence=persistence, gemini=object(), groq=object())

    service.handle_message("session-1", intent="request_callback")
    service.handle_message("session-1", text="vansh")
    result = service.handle_message("session-1", text="i want to contact via email")

    assert "email address" in result["reply"]
    assert persistence.state_updates == ["awaiting_name", "awaiting_phone", "awaiting_email"]
    assert persistence.email_updates == []


def test_callback_phone_step_saves_email_when_provided_with_channel_switch():
    persistence = FakeChatbotPersistence()
    service = serviceChatbot(persistence=persistence, gemini=object(), groq=object())

    service.handle_message("session-1", intent="request_callback")
    service.handle_message("session-1", text="vansh")
    result = service.handle_message("session-1", text="please contact me via email at vansh@example.com")

    assert result["email_confirmed"]["email"] == "vansh@example.com"
    assert persistence.state_updates == ["awaiting_name", "awaiting_phone", "email_complete"]
    assert persistence.email_updates[-1]["visitor_email"] == "vansh@example.com"
    assert persistence.lead_updates[-1]["visitor_name"] == "vansh"


def test_callback_email_step_switches_to_phone_when_visitor_prefers_call():
    persistence = FakeChatbotPersistence()
    service = serviceChatbot(persistence=persistence, gemini=object(), groq=object())

    service.handle_message("session-1", text="I want to connect via email")
    result = service.handle_message("session-1", text="actually call me on +91 98765 43210")

    assert "time works best" in result["reply"]
    assert persistence.state_updates == ["awaiting_email", "awaiting_time"]
    assert persistence.session.callback_phone == "9876543210"


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
