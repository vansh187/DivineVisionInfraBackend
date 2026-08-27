from types import SimpleNamespace

from DivineService.service_chatbot import serviceChatbot, MAX_LOGIN_ATTEMPTS


class FakeAuth:
    def __init__(self, ok=True):
        self.ok = ok
        self.logins = []

    def login_by_email(self, email, password):
        if not self.ok:
            raise ValueError("invalid_credentials")
        self.logins.append((email, password))
        return "customer-token"


class FakePersistence:
    def __init__(self, clear_fails_times=0):
        self.session = SimpleNamespace(
            id="s1", lead_id="l1", callback_state=None, callback_name=None,
            callback_phone=None, callback_time=None, auth_state=None, auth_payload=None,
            menu_state=None, menu_payload=None, loan_payload=None,
        )
        self.messages = []
        self._clear_fails_times = clear_fails_times  # times a clear-to-None write should raise

    def get_session_by_id(self, id):
        return self.session

    def touch_session(self, id):
        return self.session

    def list_recent_messages(self, session_id, limit=20):
        return list(self.messages[-limit:])

    def update_session_auth_state(self, id, auth_state, auth_payload=None):
        if auth_state is None and self._clear_fails_times > 0:
            self._clear_fails_times -= 1
            raise RuntimeError("transient write failure")
        self.session.auth_state = auth_state
        self.session.auth_payload = auth_payload
        return self.session

    def update_session_menu_state(self, id, menu_state, menu_payload=None):
        self.session.menu_state = menu_state
        self.session.menu_payload = menu_payload
        return self.session

    def create_message(self, **kwargs):
        self.messages.append(SimpleNamespace(**kwargs))
        return SimpleNamespace(**kwargs)


def _service(persistence, auth, gemini=None):
    return serviceChatbot(
        persistence=persistence, gemini=gemini or object(), groq=object(),
        zoho=None, customer_service=auth,
    )


def _login(service, bad_password="whatever"):
    service.handle_message("s1", text="login as customer")
    service.handle_message("s1", text="user@example.com")
    return service.handle_message("s1", text=bad_password)


def test_successful_login_clears_auth_state():
    persistence = FakePersistence()
    result = _login(_service(persistence, FakeAuth(ok=True)))
    assert result["auth_token"] == "customer-token"
    assert persistence.session.auth_state is None


def test_login_clear_retries_when_first_write_fails():
    persistence = FakePersistence(clear_fails_times=1)
    result = _login(_service(persistence, FakeAuth(ok=True)))
    assert result["auth_token"] == "customer-token"
    # second clear attempt succeeded -> state really is cleared
    assert persistence.session.auth_state is None


def test_repeated_bad_password_abandons_auth_flow_after_max_attempts():
    persistence = FakePersistence()
    service = _service(persistence, FakeAuth(ok=False))

    service.handle_message("s1", text="login as customer")
    last = None
    for _ in range(MAX_LOGIN_ATTEMPTS):
        service.handle_message("s1", text="user@example.com")
        last = service.handle_message("s1", text="badpass")

    assert "keep chatting" in last["reply"].lower()
    assert last["buttons"] == [{"label": "Login as Customer", "value": "login_customer", "action": "chatbot_auth"}]
    assert persistence.session.auth_state is None


def test_message_after_successful_login_is_not_consumed_as_password():
    persistence = FakePersistence()
    gemini = SimpleNamespace(generate=lambda *a, **k: {"function_call": None, "text": "Here are the plot details."})
    service = _service(persistence, FakeAuth(ok=True), gemini=gemini)

    _login(service)
    result = service.handle_message("s1", text="give details of plots")

    assert "invalid email or password" not in result["reply"].lower()
    assert result["reply"] == "Here are the plot details."
    assert persistence.session.auth_state is None


def _gemini_text(reply):
    return SimpleNamespace(generate=lambda *a, **k: {"function_call": None, "text": reply})


def test_stale_password_state_does_not_swallow_a_plot_question():
    # Simulate the reported bug's precondition: auth_state survived a prior successful
    # login. A plain project question must break out of it, not get read as a password.
    persistence = FakePersistence()
    persistence.session.auth_state = "login_customer_password"
    persistence.session.auth_payload = {"mode": "login", "role": "customer", "email": "u@example.com"}
    service = _service(persistence, FakeAuth(ok=True), gemini=_gemini_text("Available plots: ..."))

    result = service.handle_message("s1", text="give details of plots")

    assert "invalid email or password" not in result["reply"].lower()
    assert result["reply"] == "Available plots: ..."
    assert persistence.session.auth_state is None


def test_escape_hatch_falls_back_without_recursing_when_clear_write_keeps_failing():
    persistence = FakePersistence(clear_fails_times=99)  # every clear-to-None write raises
    persistence.session.auth_state = "login_customer_password"
    persistence.session.auth_payload = {"mode": "login", "role": "customer", "email": "u@example.com"}
    service = _service(persistence, FakeAuth(ok=True), gemini=_gemini_text("Plot info here"))

    result = service.handle_message("s1", text="what plot sizes are available")

    assert result["reply"] == "Plot info here"
    assert "invalid email or password" not in result["reply"].lower()


def test_single_word_during_password_entry_is_still_treated_as_password():
    persistence = FakePersistence()
    persistence.session.auth_state = "login_customer_password"
    persistence.session.auth_payload = {"mode": "login", "role": "customer", "email": "u@example.com"}
    service = _service(persistence, FakeAuth(ok=False))

    result = service.handle_message("s1", text="plots")

    assert "invalid email or password" in result["reply"].lower()
