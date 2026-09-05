from types import SimpleNamespace

from DivineService.service_chatbot import serviceChatbot


class FakeMenuPersistence:
    def __init__(self):
        self.session = SimpleNamespace(
            id="session-1", lead_id="lead-1", callback_state=None,
            callback_name=None, callback_phone=None, callback_time=None,
            auth_state=None, auth_payload=None,
            menu_state="greeting_name", menu_payload=None,
        )
        self.lead = SimpleNamespace(
            id="lead-1", visitor_name=None, visitor_phone=None, visitor_email=None,
            lead_temperature="cold", notify_updates_opt_in=None,
        )
        self.lead_updates = []
        self.email_updates = []
        self.notify_updates = []
        self.messages = []
        self.callback_requests = []
        self.menu_qualifications = []
        self.callback_state_updates = []

    # ---- session ---------------------------------------------------
    def get_session_by_id(self, id):
        return self.session

    def touch_session(self, id):
        return self.session

    def update_session_menu_state(self, id, menu_state, menu_payload=None):
        self.session.menu_state = menu_state
        self.session.menu_payload = menu_payload
        return self.session

    def update_session_callback_state(self, id, callback_state, callback_name=None, callback_phone=None, callback_time=None):
        self.session.callback_state = callback_state
        if callback_name is not None:
            self.session.callback_name = callback_name
        if callback_phone is not None:
            self.session.callback_phone = callback_phone
        if callback_time is not None:
            self.session.callback_time = callback_time
        self.callback_state_updates.append(callback_state)
        return self.session

    def update_session_auth_state(self, id, auth_state, auth_payload=None):
        self.session.auth_state = auth_state
        self.session.auth_payload = auth_payload
        return self.session

    # ---- lead --------------------------------------------------------
    def get_lead_by_id(self, id):
        return self.lead

    def update_lead_fields(self, id, visitor_name=None, visitor_phone=None, lead_temperature=None):
        if visitor_name is not None:
            self.lead.visitor_name = visitor_name
        if visitor_phone is not None:
            self.lead.visitor_phone = visitor_phone
        if lead_temperature is not None:
            self.lead.lead_temperature = lead_temperature
        self.lead_updates.append({"visitor_name": visitor_name, "visitor_phone": visitor_phone})
        return self.lead

    def update_lead_email(self, id, visitor_email):
        self.lead.visitor_email = visitor_email
        self.email_updates.append(visitor_email)
        return self.lead

    def update_lead_notify_updates(self, id, notify_updates_opt_in):
        self.lead.notify_updates_opt_in = notify_updates_opt_in
        self.notify_updates.append(notify_updates_opt_in)
        return self.lead

    # ---- callback requests --------------------------------------------
    def create_callback_request(self, id, lead_id, visitor_name, phone, preferred_time, notes=None, request_type="callback"):
        record = {
            "id": id, "lead_id": lead_id, "visitor_name": visitor_name, "phone": phone,
            "preferred_time": preferred_time, "notes": notes, "request_type": request_type,
        }
        self.callback_requests.append(record)
        return SimpleNamespace(**record)

    # ---- qualification --------------------------------------------------
    def create_menu_qualification(self, **kwargs):
        self.menu_qualifications.append(kwargs)
        return SimpleNamespace(**kwargs)

    # ---- messages ---------------------------------------------------------
    def create_message(self, **kwargs):
        self.messages.append(kwargs)
        return SimpleNamespace(**kwargs)


class FakeZoho:
    def __init__(self):
        self.pushed = []

    def push_lead_async(self, **kwargs):
        self.pushed.append(kwargs)


def _service():
    persistence = FakeMenuPersistence()
    zoho = FakeZoho()
    service = serviceChatbot(persistence=persistence, gemini=object(), groq=object(), zoho=zoho)
    return service, persistence, zoho


def _complete_greeting(service, persistence):
    service.handle_message("session-1", text="")
    service.handle_message("session-1", text="Vansh Duggal")
    service.handle_message("session-1", text="9876543210")
    return service.handle_message("session-1", text="vansh@example.com")


def test_greeting_flow_captures_contact_then_shows_main_menu():
    service, persistence, zoho = _service()

    result = _complete_greeting(service, persistence)

    assert persistence.session.menu_state == "main_menu"
    assert persistence.lead.visitor_name == "Vansh Duggal"
    assert persistence.lead.visitor_phone == "9876543210"
    assert persistence.lead.visitor_email == "vansh@example.com"
    assert any(b["value"] == "menu_sales" for b in result["buttons"])
    assert zoho.pushed  # email capture triggers a zoho sync


def test_greeting_rejects_invalid_phone_without_advancing():
    service, persistence, zoho = _service()

    service.handle_message("session-1", text="")
    service.handle_message("session-1", text="Vansh")
    result = service.handle_message("session-1", text="12345")

    assert "valid phone" in result["reply"].lower()
    assert persistence.session.menu_state == "greeting_phone"


def test_main_menu_about_exits_menu_state_for_llm_loop():
    service, persistence, zoho = _service()
    _complete_greeting(service, persistence)

    result = service.handle_message("session-1", text="menu_about")

    assert persistence.session.menu_state is None
    assert "ask me anything" in result["reply"].lower()


def test_main_menu_support_flow_creates_ticket_and_notify_optin():
    service, persistence, zoho = _service()
    _complete_greeting(service, persistence)

    service.handle_message("session-1", text="menu_support")
    result = service.handle_message("session-1", text="My booking amount was not refunded yet")

    assert persistence.callback_requests[-1]["request_type"] == "support"
    assert persistence.callback_requests[-1]["notes"] == "My booking amount was not refunded yet"
    assert "notified" in result["reply"].lower()

    final = service.handle_message("session-1", text="yes")

    assert persistence.notify_updates == [True]
    assert persistence.session.menu_state == "complete"
    assert "thank you" in final["reply"].lower()
    assert zoho.pushed


def test_sales_flow_walks_through_all_questions_and_saves_qualification():
    service, persistence, zoho = _service()
    _complete_greeting(service, persistence)

    service.handle_message("session-1", text="menu_sales")
    service.handle_message("session-1", text="sales_investor_dealer")
    service.handle_message("session-1", text="profile_individual_investor")
    service.handle_message("session-1", text="loc_ops_divine_greens")
    service.handle_message("session-1", text="opp_residential_plot")
    service.handle_message("session-1", text="size_20_50l")
    service.handle_message("session-1", text="goal_long_term")
    result = service.handle_message("session-1", text="proceed_register")

    assert persistence.session.menu_state == "complete"
    saved = persistence.menu_qualifications[-1]
    assert saved["buyer_type"] == "investor_dealer"
    assert saved["working_profile_type"] == "profile_individual_investor"
    assert saved["location_preference"] == "loc_ops_divine_greens"
    assert saved["opportunity_type"] == "opp_residential_plot"
    assert saved["investment_size_band"] == "size_20_50l"
    assert saved["investment_goal"] == "goal_long_term"
    assert saved["proceed_preference"] == "proceed_register"
    assert saved["source_flow"] == "menu_sales"
    assert "registered your interest" in result["reply"].lower()
    assert zoho.pushed


def test_sales_proceed_talk_now_routes_into_prefilled_callback():
    service, persistence, zoho = _service()
    _complete_greeting(service, persistence)

    service.handle_message("session-1", text="menu_sales")
    service.handle_message("session-1", text="sales_end_client")
    service.handle_message("session-1", text="profile_individual_buyer")
    service.handle_message("session-1", text="loc_suraksha_enclave")
    service.handle_message("session-1", text="opp_residential_unit")
    service.handle_message("session-1", text="size_50l_1cr")
    service.handle_message("session-1", text="goal_self_use")
    result = service.handle_message("session-1", text="proceed_talk_now")

    assert persistence.session.menu_state is None
    assert persistence.session.callback_state == "awaiting_time"
    assert persistence.session.callback_name == "Vansh Duggal"
    assert persistence.session.callback_phone == "9876543210"
    assert "time works best" in result["reply"].lower()
    assert persistence.menu_qualifications[-1]["source_flow"] == "menu_sales"


def test_browsing_flow_light_qualification_then_decision_proceed():
    service, persistence, zoho = _service()
    _complete_greeting(service, persistence)

    service.handle_message("session-1", text="menu_browsing")
    service.handle_message("session-1", text="first_time_yes")
    service.handle_message("session-1", text="loc_other")
    service.handle_message("session-1", text="size_under_20l")
    service.handle_message("session-1", text="sales_end_client")
    result = service.handle_message("session-1", text="decision_proceed")

    assert persistence.session.menu_state == "complete"
    saved = persistence.menu_qualifications[-1]
    assert saved["source_flow"] == "menu_browsing"
    assert saved["buyer_type"] == "end_client"
    assert saved["location_preference"] == "loc_other"
    assert saved["investment_size_band"] == "size_under_20l"
    assert "thank you" in result["reply"].lower()


def test_explicit_login_intent_during_greeting_interrupts_menu_flow_instead_of_being_captured_as_name():
    service, persistence, zoho = _service()

    service.handle_message("session-1", text="")
    result = service.handle_message("session-1", text="login as customer")

    assert persistence.session.menu_state is None
    assert persistence.session.auth_state == "login_customer_email"
    assert "email" in result["reply"].lower()
    # the phrase must not have been captured as the visitor's name
    assert persistence.lead_updates == []


def test_ambiguous_bare_auth_word_in_a_menu_button_value_does_not_interrupt_the_flow():
    # "proceed_register" contains the bare word "register", which selected_auth_flow
    # treats as a weak signup signal with no role - this must NOT be treated as an
    # auth interrupt, unlike an unambiguous "login as customer"/"signup as broker".
    service, persistence, zoho = _service()
    _complete_greeting(service, persistence)

    service.handle_message("session-1", text="menu_sales")
    service.handle_message("session-1", text="sales_investor_dealer")
    service.handle_message("session-1", text="profile_individual_investor")
    service.handle_message("session-1", text="loc_ops_divine_greens")
    service.handle_message("session-1", text="opp_residential_plot")
    service.handle_message("session-1", text="size_20_50l")
    service.handle_message("session-1", text="goal_long_term")
    result = service.handle_message("session-1", text="proceed_register")

    assert persistence.session.menu_state == "complete"
    assert persistence.session.auth_state is None
    assert "registered your interest" in result["reply"].lower()


def test_yes_no_button_matching_does_not_false_positive_on_substrings():
    service, persistence, zoho = _service()
    _complete_greeting(service, persistence)
    service.handle_message("session-1", text="menu_support")
    service.handle_message("session-1", text="the gate code does not work")

    result = service.handle_message("session-1", text="not sure, maybe later")

    # "no" must not falsely match inside "not sure" - the bot should re-ask, not
    # silently record notify_updates_opt_in=False.
    assert persistence.notify_updates == []
    assert persistence.session.menu_state == "support_updates_optin"
    assert "yes or no" in result["reply"].lower()


def test_menu_state_unaffected_when_never_set_old_session():
    # Regression guard: a session predating this feature (menu_state absent entirely,
    # like the shared FakeChatbotPersistence in test_service_chatbot_email.py) must
    # behave exactly as before - getattr(...) must not raise and must not route here.
    persistence = FakeMenuPersistence()
    persistence.session.menu_state = None
    service = serviceChatbot(persistence=persistence, gemini=object(), groq=object())

    result = service.handle_message("session-1", text="I want to connect via email")

    assert "email address" in result["reply"]
    assert persistence.session.menu_state is None


def test_greeting_reprompts_on_unclear_non_name_input():
    service, persistence, _ = _service()
    service.handle_message("session-1", text="")  # widget-open greeting

    # not a name (has digits) and not a recognised quick-action intent
    result = service.handle_message("session-1", text="abc123 xyz")

    assert persistence.session.menu_state == "greeting_name"  # not advanced
    assert (persistence.session.menu_payload or {}).get("name") is None
    assert "name" in result["reply"].lower()


def test_greeting_topic_chip_skips_funnel_and_is_answered(monkeypatch):
    service, persistence, _ = _service()
    service._run_agent_loop = MagicMock(return_value={"reply": "Here are our current plots...", "llm_provider": "test"})
    service.handle_message("session-1", text="")

    result = service.handle_message("session-1", text="Pricing & payment plan")

    assert persistence.session.menu_state is None                 # funnel dropped
    assert (persistence.session.menu_payload or {}).get("name") is None
    assert result["reply"] == "Here are our current plots..."     # routed to the answer, not a name prompt


def test_greeting_site_visit_chip_routes_to_callback_flow():
    service, persistence, _ = _service()
    service.handle_message("session-1", text="")

    result = service.handle_message("session-1", text="Book a site visit")

    assert persistence.session.menu_state is None
    assert persistence.session.callback_state == "awaiting_name"
    assert "name" in result["reply"].lower()  # asked as part of scheduling, not as a gate


def test_greeting_still_accepts_a_real_name():
    service, persistence, _ = _service()
    service.handle_message("session-1", text="")

    result = service.handle_message("session-1", text="Priya Sharma")

    assert persistence.session.menu_state == "greeting_phone"
    assert persistence.session.menu_payload["name"] == "Priya Sharma"
    assert "phone" in result["reply"].lower()


def test_greeting_accepts_unusual_name_after_two_reprompts():
    service, persistence, _ = _service()
    service.handle_message("session-1", text="")
    # "Call" happens to be a surname; guard re-prompts twice, then accepts
    service.handle_message("session-1", text="Call")
    service.handle_message("session-1", text="Call")
    result = service.handle_message("session-1", text="Call")
    assert persistence.session.menu_state == "greeting_phone"
    assert persistence.session.menu_payload["name"] == "Call"
    assert "name_attempts" not in persistence.session.menu_payload
    assert "phone" in result["reply"].lower()


def test_greeting_accepts_long_multiword_name():
    service, persistence, _ = _service()
    service.handle_message("session-1", text="")
    result = service.handle_message("session-1", text="Sri Venkata Naga Sai Krishna Reddy")
    assert persistence.session.menu_state == "greeting_phone"
    assert persistence.session.menu_payload["name"] == "Sri Venkata Naga Sai Krishna Reddy"


from unittest.mock import MagicMock
from DivineService.service_chatbot import wants_home_loan


def test_wants_home_loan_matches_chip_and_menu_labels_but_not_bare_loan():
    assert wants_home_loan("Home loan / finance enquiry")
    assert wants_home_loan("Home Loan / EMI Help")
    assert wants_home_loan("menu_loan")
    assert wants_home_loan("check my loan eligibility")
    assert wants_home_loan("i need help with emi and finance for a loan")
    # not a home-loan intent
    assert not wants_home_loan("")
    assert not wants_home_loan("what plots do you have")
    assert not wants_home_loan("i want a loan")  # bare "loan", no finance word
    assert not wants_home_loan("what is the payment plan / installment for plots")


def test_home_loan_chip_skips_greeting_and_enters_loan_assistant():
    service, persistence, _ = _service()
    service._enter_loan_assistant = MagicMock(return_value={"session_id": "session-1", "reply": "Let's check your eligibility."})
    service.handle_message("session-1", text="")  # widget-open, arms greeting_name

    result = service.handle_message("session-1", text="Home loan / finance enquiry")

    service._enter_loan_assistant.assert_called_once()
    assert persistence.session.menu_state is None            # greeting funnel cleared
    assert (persistence.session.menu_payload or {}).get("name") is None
    assert result["reply"] == "Let's check your eligibility."


def test_real_name_at_greeting_does_not_trigger_loan_assistant():
    service, persistence, _ = _service()
    service._enter_loan_assistant = MagicMock()
    service.handle_message("session-1", text="")

    service.handle_message("session-1", text="Priya Sharma")

    service._enter_loan_assistant.assert_not_called()
    assert persistence.session.menu_state == "greeting_phone"


import pytest
from DivineService.service_chatbot import (
    wants_home_loan, wants_project_info, wants_site_visit, wants_sales_advisor,
)


@pytest.mark.parametrize("label", [
    "Home loan / finance enquiry",
    "Pricing & payment plan",
    "Book a site visit",
    "Show available plots",
    "Talk to a sales advisor",
])
def test_every_popular_question_chip_is_recognised_as_a_quick_action(label):
    assert (wants_home_loan(label) or wants_project_info(label)
            or wants_site_visit(label) or wants_sales_advisor(label)), label


@pytest.mark.parametrize("label,expect_state", [
    ("Home loan / finance enquiry", None),      # -> loan assistant
    ("Pricing & payment plan", None),           # -> KB/agent answer
    ("Show available plots", None),             # -> KB/agent answer
    ("Book a site visit", "awaiting_name"),     # -> callback flow (callback_state)
    ("Talk to a sales advisor", "awaiting_name"),
])
def test_every_popular_question_chip_skips_the_greeting_funnel(label, expect_state):
    service, persistence, _ = _service()
    service._run_agent_loop = MagicMock(return_value={"reply": "ok", "llm_provider": "test"})
    service._enter_loan_assistant = MagicMock(return_value={"session_id": "session-1", "reply": "loan"})
    service.handle_message("session-1", text="")  # arm greeting_name

    service.handle_message("session-1", text=label)

    assert persistence.session.menu_state is None                     # never stuck in the funnel
    assert (persistence.session.menu_payload or {}).get("name") is None
    assert persistence.session.callback_state == expect_state
