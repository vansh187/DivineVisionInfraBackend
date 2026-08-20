import os
import re
import json
import uuid
import logging
import ipaddress
import unicodedata
from datetime import datetime, timezone

from Divinepersistence import persistenceChatbot
from DivineDTO.models import UserCreateDTO
from DivineService.service_broker import serviceBroker
from DivineService.service_customer import serviceCustomer
from DivineService.llm_gemini import llmGemini, GeminiError
from DivineService.llm_groq import llmGroq, GroqError

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = (
    "You are the AI concierge for Divine Vision Infratech, a real-estate developer. "
    "Answer visitor questions about projects, pricing, RERA, and specs ONLY using the "
    "search_knowledge_base tool's results - never invent figures or claims from your own "
    "general knowledge. If the knowledge base has no relevant information, say so plainly "
    "and offer a callback instead of guessing. Keep replies short, warm, and in the "
    "visitor's own language/register (Hindi/Hinglish/English as they write). Use "
    "upsert_crm_lead when the visitor shares their name, phone number, or email address. "
    "Only ask for email if the visitor explicitly says they want to connect by email. Use "
    "extract_lead_signals after a few substantive turns to capture budget/timeline signals."
)

SAFE_FALLBACK_REPLY = "I don't want to guess on that — let me get you an exact answer from our team. Would you like a callback?"
DEGRADED_FALLBACK_REPLY = "Sorry, I'm having a little trouble right now. Could you try again in a moment, or would you like our team to call you back?"

MAX_TOOL_ROUNDS = 3
HISTORY_TURN_LIMIT = 20
GUARDRAIL_THRESHOLD = float(os.getenv("CHATBOT_GUARDRAIL_THRESHOLD", "0.5"))

BOOKING_PROJECT_BUTTONS = [
    {"label": "OPS Project", "value": "book_project_ops", "action": "select_booking_project"},
    {"label": "Suraksha Project", "value": "book_project_suraksha", "action": "select_booking_project"},
]
CUSTOMER_LOGIN_BUTTON = {
    "label": "Login as Customer",
    "value": "login_customer",
    "action": "chatbot_auth",
}
AUTH_FLOW_BUTTONS = [
    {"label": "Customer Signup", "value": "signup_customer", "action": "chatbot_auth"},
    {"label": "Customer Login", "value": "login_customer", "action": "chatbot_auth"},
    {"label": "Broker Signup", "value": "signup_broker", "action": "chatbot_auth"},
    {"label": "Broker Login", "value": "login_broker", "action": "chatbot_auth"},
]
AUTH_LOGIN_BUTTONS = {
    "customer": {"label": "Login as Customer", "value": "login_customer", "action": "chatbot_auth"},
    "broker": {"label": "Login as Broker", "value": "login_broker", "action": "chatbot_auth"},
}
AUTH_ROLE_ALIASES = {
    "customer": (
        "customer", "cust", "buyer", "client", "user", "member", "grahak", "customer account",
        "customer portal", "customer panel", "customer dashboard",
    ),
    "broker": (
        "broker", "agent", "channel partner", "cp", "dealer", "sales partner", "property dealer",
        "broker account", "broker portal", "broker panel", "broker dashboard",
    ),
}
AUTH_LOGIN_PHRASES = (
    "login", "log in", "signin", "sign in", "sign me in", "let me in", "open my account",
    "access account", "account access", "enter account", "portal login", "dashboard login",
    "login karna", "login karo", "login krna", "login krdo", "login kar do", "signin karna",
)
AUTH_SIGNUP_PHRASES = (
    "signup", "sign up", "register", "registration", "create account", "new account",
    "open account", "make account", "create my account", "register me", "join as", "create", "make",
    "signup karna", "sign up karna", "register karna", "account banana", "account banao",
    "naya account", "new registration",
)
AUTH_FLOW_VALUES = {
    "login_customer": ("login", "customer"),
    "customer_login": ("login", "customer"),
    "signin_customer": ("login", "customer"),
    "customer_signin": ("login", "customer"),
    "signup_customer": ("signup", "customer"),
    "customer_signup": ("signup", "customer"),
    "register_customer": ("signup", "customer"),
    "customer_register": ("signup", "customer"),
    "login_broker": ("login", "broker"),
    "broker_login": ("login", "broker"),
    "signin_broker": ("login", "broker"),
    "broker_signin": ("login", "broker"),
    "signup_broker": ("signup", "broker"),
    "broker_signup": ("signup", "broker"),
    "register_broker": ("signup", "broker"),
    "broker_register": ("signup", "broker"),
}

TOOL_SCHEMAS = [
    {
        "name": "search_knowledge_base",
        "description": "Search the project knowledge base (pricing, RERA, specs, location) for information relevant to the visitor's question.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "search query"}}, "required": ["query"]},
    },
    {
        "name": "upsert_crm_lead",
        "description": "Save or update the visitor's name, phone number, and/or email address on their lead record.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "visitor's name, if known"},
            "phone": {"type": "string", "description": "visitor's phone number, if known"},
            "email": {"type": "string", "description": "visitor's email address, if known"},
        }, "required": []},
    },
    {
        "name": "extract_lead_signals",
        "description": "Extract budget, unit type, timeline, and intent signals from the conversation so far.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]


def looks_degenerate(text: str) -> bool:
    # Reasoning models occasionally degenerate into repetitive punctuation/filler ("...",
    # "**", em-dashes) on low-information turns (observed in production on a plain "ok"
    # reply) instead of raising a clean error - nothing in the API contract flags this, so
    # it has to be caught by inspecting the text itself before it ever reaches a visitor.
    # Word tokens with no Latin or Devanagari alphanumeric character count as "junk"; a
    # short reply naturally has a few (an emoji, a dash) but a degenerate one is mostly junk.
    if not text:
        return False
    words = text.split()
    if len(words) < 8:
        return False
    junk = sum(1 for w in words if not re.search(r"[A-Za-z0-9ऀ-ॿ]", w))
    return (junk / len(words)) > 0.35


def _digits_only(raw: str) -> str:
    digits = []
    for char in raw or "":
        try:
            digits.append(str(unicodedata.digit(char)))
        except (TypeError, ValueError):
            continue
    return "".join(digits)


def extract_phone(raw: str) -> str:
    digits = _digits_only(raw)
    if len(digits) >= 12:
        for i in range(len(digits) - 11):
            candidate = digits[i:i + 12]
            if candidate.startswith("91") and candidate[2] in "6789":
                return candidate[2:]
    if len(digits) >= 10:
        for i in range(len(digits) - 9):
            candidate = digits[i:i + 10]
            if candidate[0] in "6789":
                return candidate
    return None


def valid_phone(raw: str) -> bool:
    digits = _digits_only(raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 10:
        return digits[0] in "6789"
    return extract_phone(raw) is not None


def normalize_phone(raw: str) -> str:
    extracted = extract_phone(raw)
    if extracted:
        return extracted
    digits = _digits_only(raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits


EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)


def valid_email(raw: str) -> bool:
    if not raw:
        return False
    return bool(EMAIL_RE.fullmatch(raw.strip().replace("\\@", "@")))


def extract_email(raw: str) -> str:
    match = EMAIL_RE.search((raw or "").replace("\\@", "@"))
    return match.group(0).strip() if match else None


def extract_auth_credentials(raw: str) -> dict:
    text = raw or ""
    email = extract_email(text)
    password = None
    password_match = re.search(r'\bpassword\b\s*[:=]\s*["\']?([^"\'},\s]+)', text, re.IGNORECASE)
    if password_match:
        password = password_match.group(1).strip()
    return {"email": email, "password": password}


def _contact_text(raw: str) -> str:
    text = (raw or "").lower()
    text = re.sub(r"\be\s*[-_.]?\s*mail\b", "email", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def wants_email_contact(raw: str) -> bool:
    text = _contact_text(raw)
    if not text:
        return False
    question_markers = (
        "what is your", "what's your", "tell me your", "do you have", "company email",
        "your email", "aapka email", "apka email",
    )
    if any(marker in text for marker in question_markers):
        return False
    email_words = ("email", "mail")
    connect_words = (
        "connect", "contact", "reach", "message", "send", "share", "talk", "reply", "ping",
        "communicate", "dm", "bhejo", "bhej", "karo", "karna", "sampark", "baat",
    )
    first_person_markers = (
        "i ", "i'", "me", "my", "mujhe", "mujko", "mere", "meri", "mera", "humko", "hamko",
    )
    if not any(marker in text for marker in first_person_markers):
        preference_phrases = (
            "via email", "by email", "through email", "on email", "over email",
            "email par", "email pe", "email se", "mail par", "mail pe", "mail se",
            "email only", "mail only", "no phone", "dont call", "don't call",
        )
        return any(phrase in text for phrase in preference_phrases) and any(word in text for word in email_words)
    if "email me" in text or "mail me" in text:
        return True
    return any(word in text for word in email_words) and any(word in text for word in connect_words)


def wants_phone_contact(raw: str) -> bool:
    text = _contact_text(raw)
    if not text:
        return False
    question_markers = (
        "what is your phone", "what is your number", "what's your phone", "what's your number",
        "company phone", "company number", "your mobile", "aapka number", "apka number",
    )
    if any(marker in text for marker in question_markers):
        return False
    phone_words = ("phone", "number", "mobile", "call", "whatsapp", "fone")
    connect_words = (
        "connect", "contact", "reach", "message", "send", "share", "talk", "reply", "call",
        "karo", "karna", "sampark", "baat",
    )
    first_person_markers = (
        "i ", "i'", "me", "my", "mujhe", "mujko", "mere", "meri", "mera", "humko", "hamko",
    )
    preference_phrases = (
        "via phone", "by phone", "through phone", "on phone", "over phone",
        "phone par", "phone pe", "mobile par", "mobile pe", "call me", "call back",
        "whatsapp me", "whatsapp par", "whatsapp pe",
    )
    if any(phrase in text for phrase in preference_phrases):
        return True
    return (
        any(marker in text for marker in first_person_markers)
        and any(word in text for word in phone_words)
        and any(word in text for word in connect_words)
    )


def wants_plot_booking(raw: str) -> bool:
    text = _contact_text(raw)
    if not text:
        return False
    if "site visit" in text:
        return False
    booking_words = (
        "book", "booking", "reserve", "apply", "application", "purchase", "buy",
        "allot", "allotment", "interested", "lena", "len", "kharid", "kharidna",
    )
    property_words = (
        "plot", "plots", "land", "property", "unit", "sq ft", "sqft", "sq. ft",
        "square feet", "square foot", "sq feet", "sq foot", "sft", "sf",
        "sq yd", "sqyd", "sq. yd", "square yard", "square yards", "yard", "yards", "gaj",
        "marla", "marlas",
    )
    area_pattern = re.compile(
        r"\b\d+(?:\.\d+)?\s*(?:sq\.?\s*ft|sqft|square\s*(?:feet|foot)|sq\.?\s*yd|sqyd|"
        r"square\s*yards?|sft|sf|yards?|gaj|marlas?)\b"
    )
    has_booking_intent = any(word in text for word in booking_words)
    has_plot_or_size = any(word in text for word in property_words) or bool(area_pattern.search(text))
    return has_booking_intent and has_plot_or_size


def selected_booking_project(raw: str) -> str:
    text = _contact_text(raw)
    if not text:
        return None
    if "book_project_ops" in text or "ops project" in text or "ops divine" in text:
        return "OPS Project"
    if "book_project_suraksha" in text or "suraksha project" in text or "suraksha" in text:
        return "Suraksha Project"
    return None


def selected_auth_flow(raw: str):
    text = _contact_text(raw)
    if not text:
        return None
    normalized_value = re.sub(r"[\s-]+", "_", text)
    if normalized_value in AUTH_FLOW_VALUES:
        return AUTH_FLOW_VALUES[normalized_value]
    for value, flow in AUTH_FLOW_VALUES.items():
        if value in normalized_value:
            return flow

    role = None
    for candidate_role, aliases in AUTH_ROLE_ALIASES.items():
        if any(alias in text for alias in aliases):
            role = candidate_role
            break

    wants_signup = any(phrase in text for phrase in AUTH_SIGNUP_PHRASES)
    wants_login = any(phrase in text for phrase in AUTH_LOGIN_PHRASES)
    if wants_signup and role:
        return ("signup", role)
    if wants_login and role:
        return ("login", role)
    if wants_signup or wants_login:
        return ("choose", None)
    return None


def is_affirmative(raw: str) -> bool:
    text = _contact_text(raw)
    return text in ("yes", "y", "yeah", "yep", "sure", "ok", "okay", "haan", "ha", "han", "ji", "yes login")


def is_negative(raw: str) -> bool:
    text = _contact_text(raw)
    return text in ("no", "n", "nope", "nah", "nahi", "na", "not now", "later")


class serviceChatbot:
    def __init__(self, persistence: persistenceChatbot = None, gemini: llmGemini = None, groq: llmGroq = None,
                 customer_service: serviceCustomer = None, broker_service: serviceBroker = None):
        self._persistence = persistence or persistenceChatbot()
        self._gemini = gemini or llmGemini()
        self._groq = groq or llmGroq()
        self._customer_service = customer_service
        self._broker_service = broker_service

    # ---- Session init ---------------------------------------------------
    def init_session(self, referrer: str = None, utm_source: str = None, utm_medium: str = None,
                      utm_campaign: str = None, device_type: str = None, ip_address: str = None):
        lead = self._persistence.create_lead(id=str(uuid.uuid4()))
        self._persistence.create_lead_source(
            id=str(uuid.uuid4()), lead_id=lead.id, ip_address=self._safe_ip(ip_address),
            referrer=referrer, utm_source=utm_source, utm_medium=utm_medium,
            utm_campaign=utm_campaign, device_type=device_type,
        )
        session = self._persistence.create_session(id=str(uuid.uuid4()), lead_id=lead.id)
        return {"session_id": session.id, "lead_id": lead.id}

    # ---- Main entry point -------------------------------------------------
    def handle_message(self, session_id: str, text: str = None, audio_bytes: bytes = None,
                        intent: str = None, precise_lat: float = None, precise_long: float = None):
        session = self._persistence.get_session_by_id(session_id)
        if not session:
            raise ValueError("session_not_found")
        try:
            self._persistence.touch_session(session_id)
        except Exception as e:
            # Best-effort activity timestamp - must never block the actual conversation turn.
            logger.warning("touch_session_failed: %s", e)

        if precise_lat is not None and precise_long is not None:
            try:
                maps_link = f"https://maps.google.com/?q={precise_lat},{precise_long}"
                self._persistence.update_lead_source_location(session.lead_id, precise_lat, precise_long, maps_link)
            except Exception as e:
                # Best-effort: losing precise geo must never block the visitor's actual message.
                logger.warning("update_lead_source_location_failed: %s", e)

        if audio_bytes:
            try:
                text = self._groq.transcribe(audio_bytes)
            except GroqError:
                return {"session_id": session_id, "reply": None, "error": "stt_failed"}

        text = (text or "").strip()

        auth_flow = selected_auth_flow(text)
        if auth_flow and getattr(session, "auth_state", None):
            self._persist_turn(session_id, "user", text)
            return self._start_auth_flow(session, auth_flow)

        if getattr(session, "auth_state", None):
            return self._advance_auth_flow(session, text)

        if intent == "request_callback" and not session.callback_state:
            session = self._persistence.update_session_callback_state(session_id, "awaiting_name")
            reply = "Sure! May I know your name?"
            self._persist_turn(session_id, "user", "[intent:request_callback]")
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}

        if session.callback_state:
            return self._advance_callback_flow(session, text)

        if not text:
            return {"session_id": session_id, "reply": "Sorry, I didn't catch that — could you type your question?"}

        if auth_flow:
            self._persist_turn(session_id, "user", text)
            return self._start_auth_flow(session, auth_flow)

        booking_project = selected_booking_project(text)
        if booking_project:
            reply = (
                f"Great, you selected {booking_project}. Please login as a customer so you can fill the "
                "plot booking application form."
            )
            self._persist_turn(session_id, "user", text)
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply, "buttons": [CUSTOMER_LOGIN_BUTTON]}

        if wants_plot_booking(text):
            reply = "Sure, which project would you like to book the plot in?"
            self._persist_turn(session_id, "user", text)
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply, "buttons": BOOKING_PROJECT_BUTTONS}

        if wants_email_contact(text):
            email = extract_email(text)
            self._persist_turn(session_id, "user", text)
            if email:
                if not self._save_lead_email(session.lead_id, email):
                    reply = "Sorry, I'm having trouble saving your email right now. Could you try again in a moment?"
                    self._persist_turn(session_id, "assistant", reply)
                    return {"session_id": session_id, "reply": reply}
                self._safe_update_session_callback_state(session_id, "email_complete")
                reply = f"Thanks! I have saved {email}. Our team will connect with you by email shortly."
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply, "email_confirmed": {"email": email}}
            if not self._safe_update_session_callback_state(session_id, "awaiting_email"):
                reply = "Sorry, I'm having trouble starting the email request right now. Could you try again in a moment?"
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            reply = "Sure, please share your email address and our team will connect with you there."
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}

        self._persist_turn(session_id, "user", text)
        result = self._run_agent_loop(session, text)
        self._persist_turn(
            session_id, "assistant", result["reply"], llm_provider=result.get("llm_provider"),
            guardrail_score=result.get("guardrail_score"), guardrail_passed=result.get("guardrail_passed"),
        )
        return {"session_id": session_id, "reply": result["reply"], "llm_provider": result.get("llm_provider"),
                "guardrail_passed": result.get("guardrail_passed")}

    # ---- Callback state machine (deterministic, no LLM) --------------------
    def _advance_callback_flow(self, session, text: str):
        session_id = session.id
        state = session.callback_state

        if state == "complete":
            # Callback flow already finished on a prior turn - clear the state and hand this
            # message to the normal agent loop instead, which will persist it itself. Persisting
            # it here too (before the recursive call below) would double-write the same user turn.
            self._persistence.update_session_callback_state(session_id, None)
            return self.handle_message(session_id, text=text)

        if state == "email_complete":
            self._safe_update_session_callback_state(session_id, None)
            return self.handle_message(session_id, text=text)

        self._persist_turn(session_id, "user", text)

        if state == "awaiting_email":
            if wants_phone_contact(text):
                phone = extract_phone(text)
                if phone:
                    self._persistence.update_session_callback_state(session_id, "awaiting_time", callback_phone=phone)
                    reply = "What time works best for you? Morning, afternoon, or evening?"
                    self._persist_turn(session_id, "assistant", reply)
                    return {"session_id": session_id, "reply": reply}
                self._persistence.update_session_callback_state(session_id, "awaiting_phone")
                reply = "Sure, please share your phone number and our team will call you."
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            email = extract_email(text)
            if not email or not valid_email(email):
                reply = "Could you share a valid email address?"
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            if not self._save_lead_email(session.lead_id, email):
                reply = "Sorry, I'm having trouble saving your email right now. Could you try again in a moment?"
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            self._safe_update_session_callback_state(session_id, "email_complete")
            reply = f"Thanks! I have saved {email}. Our team will connect with you by email shortly."
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply, "email_confirmed": {"email": email}}

        if state == "awaiting_name":
            if not text:
                reply = "Could you share your name?"
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            self._persistence.update_session_callback_state(session_id, "awaiting_phone", callback_name=text)
            reply = "And your phone number?"
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}

        if state == "awaiting_phone":
            if wants_email_contact(text):
                email = extract_email(text)
                if email:
                    if not self._save_lead_email(session.lead_id, email):
                        reply = "Sorry, I'm having trouble saving your email right now. Could you try again in a moment?"
                        self._persist_turn(session_id, "assistant", reply)
                        return {"session_id": session_id, "reply": reply}
                    self._safe_update_session_callback_state(session_id, "email_complete")
                    if getattr(session, "callback_name", None):
                        self._persistence.update_lead_fields(session.lead_id, visitor_name=session.callback_name)
                    reply = f"Thanks! I have saved {email}. Our team will connect with you by email shortly."
                    self._persist_turn(session_id, "assistant", reply)
                    return {"session_id": session_id, "reply": reply, "email_confirmed": {"email": email}}
                if not self._safe_update_session_callback_state(session_id, "awaiting_email"):
                    reply = "Sorry, I'm having trouble starting the email request right now. Could you try again in a moment?"
                    self._persist_turn(session_id, "assistant", reply)
                    return {"session_id": session_id, "reply": reply}
                reply = "Sure, please share your email address and our team will connect with you there."
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            if not valid_phone(text):
                reply = "Could you share a valid 10-digit number?"
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            self._persistence.update_session_callback_state(session_id, "awaiting_time", callback_phone=normalize_phone(text))
            reply = "What time works best for you? Morning, afternoon, or evening?"
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}

        if state == "awaiting_time":
            if not text:
                reply = "What time works best — morning, afternoon, or evening?"
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            updated = self._persistence.update_session_callback_state(session_id, "complete", callback_time=text)

            self._persistence.create_callback_request(
                id=str(uuid.uuid4()), lead_id=session.lead_id,
                visitor_name=updated.callback_name, phone=updated.callback_phone, preferred_time=updated.callback_time,
            )
            self._persistence.update_lead_fields(session.lead_id, visitor_name=updated.callback_name, visitor_phone=updated.callback_phone)

            reply = (f"Got it, {updated.callback_name}! We'll call you at {updated.callback_phone} around "
                     f"{updated.callback_time}. Our team will be in touch shortly.")
            self._persist_turn(session_id, "assistant", reply)
            return {
                "session_id": session_id, "reply": reply,
                "callback_confirmed": {"name": updated.callback_name, "phone": updated.callback_phone, "preferred_time": updated.callback_time},
            }

        # Unreachable in practice - callback_state is DB-constrained to the four known
        # values and "complete" is handled above before any persistence happens.
        return {"session_id": session_id, "reply": DEGRADED_FALLBACK_REPLY}

    # ---- Auth state machine (deterministic, no LLM) -------------------------
    def _start_auth_flow(self, session, auth_flow):
        mode, role = auth_flow
        session_id = session.id
        if mode == "choose":
            reply = "Sure, please choose what you want to do."
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply, "buttons": AUTH_FLOW_BUTTONS}
        if mode == "signup":
            if not self._safe_update_session_auth_state(session_id, f"signup_{role}_first_name", {"mode": mode, "role": role}):
                reply = "Sorry, I'm having trouble starting signup right now. Please try again in a moment."
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            reply = f"Sure, let's create your {role} account. What is your first name?"
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}
        if not self._safe_update_session_auth_state(session_id, f"login_{role}_email", {"mode": mode, "role": role}):
            reply = "Sorry, I'm having trouble starting login right now. Please try again in a moment."
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}
        reply = f"Sure, please enter your {role} email address."
        self._persist_turn(session_id, "assistant", reply)
        return {"session_id": session_id, "reply": reply}

    def _advance_auth_flow(self, session, text: str):
        session_id = session.id
        state = session.auth_state
        payload = self._auth_payload(session)
        credentials = extract_auth_credentials(text)

        if state.endswith("_password") or credentials.get("password"):
            self._persist_turn(session_id, "user", "[password hidden]")
        else:
            self._persist_turn(session_id, "user", text)

        if state.startswith("post_signup_"):
            return self._advance_post_signup_flow(session, text, payload)

        parts = state.split("_")
        if len(parts) < 3 or parts[0] not in ("signup", "login") or parts[1] not in ("customer", "broker"):
            return self._auth_error(session_id)
        mode, role, field = parts[0], parts[1], "_".join(parts[2:])

        if mode == "signup":
            return self._advance_signup_flow(session, text, role, field, payload)
        if mode == "login":
            return self._advance_login_flow(session, text, role, field, payload, credentials)

        self._safe_update_session_auth_state(session_id, None, None)
        reply = DEGRADED_FALLBACK_REPLY
        self._persist_turn(session_id, "assistant", reply)
        return {"session_id": session_id, "reply": reply}

    def _advance_post_signup_flow(self, session, text: str, payload: dict):
        session_id = session.id
        role = payload.get("role")
        email = payload.get("email")
        if role not in ("customer", "broker") or not email:
            return self._auth_error(session_id)
        if is_affirmative(text):
            if not self._safe_update_session_auth_state(
                session_id, f"login_{role}_password", {"mode": "login", "role": role, "email": email}
            ):
                reply = "Sorry, I'm having trouble starting login right now. Please try again in a moment."
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            reply = f"Great, please enter the password for {email}."
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}
        if is_negative(text):
            self._safe_update_session_auth_state(session_id, None, None)
            reply = "No problem. You can login anytime from this chat."
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}
        reply = "Please reply yes to login now, or no to do it later."
        self._persist_turn(session_id, "assistant", reply)
        return {"session_id": session_id, "reply": reply, "buttons": [AUTH_LOGIN_BUTTONS[role]]}

    def _advance_signup_flow(self, session, text: str, role: str, field: str, payload: dict):
        session_id = session.id
        value = (text or "").strip()
        if field in ("first_name", "last_name", "email", "phone") and value.lower() in ("skip", "na", "n/a", "none", "no"):
            value = None

        if field == "first_name":
            if not value:
                reply = "Please enter your first name, or type skip."
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            payload["first_name"] = value
            self._safe_update_session_auth_state(session_id, f"signup_{role}_last_name", payload)
            reply = "What is your last name? You can type skip if you don't want to add it."
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}

        if field == "last_name":
            payload["last_name"] = value
            self._safe_update_session_auth_state(session_id, f"signup_{role}_email", payload)
            reply = "Please enter your email address. You can type skip if you don't want to add it."
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}

        if field == "email":
            if value and not valid_email(value):
                reply = "Please enter a valid email address, or type skip."
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            payload["email"] = value
            self._safe_update_session_auth_state(session_id, f"signup_{role}_phone", payload)
            reply = "Please enter your phone number. You can type skip if you don't want to add it."
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}

        if field == "phone":
            if value:
                phone = extract_phone(value)
                if not phone:
                    reply = "Please enter a valid phone number, or type skip."
                    self._persist_turn(session_id, "assistant", reply)
                    return {"session_id": session_id, "reply": reply}
                payload["phone"] = phone
            else:
                payload["phone"] = None
            self._safe_update_session_auth_state(session_id, f"signup_{role}_username", payload)
            reply = "Please choose a username."
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}

        if field == "username":
            if len(value) < 3:
                reply = "Username should be at least 3 characters."
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            payload["username"] = value
            self._safe_update_session_auth_state(session_id, f"signup_{role}_password", payload)
            reply = "Please enter a password with at least 8 characters."
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}

        if field == "password":
            if len(value) < 8:
                reply = "Password should be at least 8 characters. Please enter a stronger password."
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            payload["password"] = value
            return self._create_auth_account(session, role, payload)

        return self._auth_error(session_id)

    def _advance_login_flow(self, session, text: str, role: str, field: str, payload: dict, credentials: dict = None):
        session_id = session.id
        value = (text or "").strip()
        credentials = credentials or {}
        if credentials.get("email") and credentials.get("password"):
            payload["email"] = credentials["email"]
            payload["password"] = credentials["password"]
            return self._login_auth_account(session, role, payload)
        if field == "email":
            email = credentials.get("email") or extract_email(value)
            if not email or not valid_email(email):
                reply = "Please enter a valid email address."
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            payload["email"] = email
            self._safe_update_session_auth_state(session_id, f"login_{role}_password", payload)
            reply = "Please enter your password."
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}
        if field == "password":
            if not value:
                reply = "Please enter your password."
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            payload["password"] = value
            return self._login_auth_account(session, role, payload)
        return self._auth_error(session_id)

    def _create_auth_account(self, session, role: str, payload: dict):
        session_id = session.id
        try:
            dto = UserCreateDTO(**payload)
            user = self._auth_service(role).signup(dto)
        except ValueError as e:
            if str(e) == "username_taken":
                self._safe_update_session_auth_state(session_id, f"signup_{role}_username", {
                    k: v for k, v in payload.items() if k not in ("username", "password")
                })
                reply = "That username is already taken. Please choose another username."
            else:
                self._safe_update_session_auth_state(session_id, None, None)
                reply = "Sorry, I could not create the account. Please try again."
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}
        except Exception as e:
            logger.warning("chatbot_auth_signup_failed: %s", e)
            self._safe_update_session_auth_state(session_id, None, None)
            reply = "Sorry, I could not create the account right now. Please try again in a moment."
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}

        if not payload.get("email"):
            self._safe_update_session_auth_state(session_id, None, None)
            reply = f"Your {role} account has been created. To login from chatbot next time, please use an account with an email address."
            self._persist_turn(session_id, "assistant", reply)
            return {
                "session_id": session_id,
                "reply": reply,
                "account_created": {"role": role, "username": user.username},
            }

        self._safe_update_session_auth_state(
            session_id, f"post_signup_{role}_confirm", {
                "mode": "post_signup", "role": role, "username": user.username, "email": payload.get("email"),
            }
        )
        reply = f"Your {role} account has been created. Do you want to login now?"
        self._persist_turn(session_id, "assistant", reply)
        return {
            "session_id": session_id,
            "reply": reply,
            "account_created": {"role": role, "username": user.username},
            "buttons": [AUTH_LOGIN_BUTTONS[role]],
        }

    def _login_auth_account(self, session, role: str, payload: dict):
        session_id = session.id
        try:
            token = self._auth_service(role).login_by_email(payload.get("email"), payload.get("password"))
        except ValueError:
            self._safe_update_session_auth_state(session_id, f"login_{role}_email", {"mode": "login", "role": role})
            reply = "Invalid email or password. Please enter your email address again."
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}
        except Exception as e:
            logger.warning("chatbot_auth_login_failed: %s", e)
            self._safe_update_session_auth_state(session_id, None, None)
            reply = "Sorry, I could not login right now. Please try again in a moment."
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}

        self._safe_update_session_auth_state(session_id, None, None)
        reply = f"You are logged in as {role}."
        self._persist_turn(session_id, "assistant", reply)
        return {"session_id": session_id, "reply": reply, "auth_token": token, "auth_role": role}

    def _auth_service(self, role: str):
        if role == "customer":
            if not self._customer_service:
                self._customer_service = serviceCustomer()
            return self._customer_service
        if not self._broker_service:
            self._broker_service = serviceBroker()
        return self._broker_service

    def _auth_payload(self, session) -> dict:
        raw = getattr(session, "auth_payload", None)
        if isinstance(raw, dict):
            return dict(raw)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {}

    def _auth_error(self, session_id: str):
        self._safe_update_session_auth_state(session_id, None, None)
        reply = DEGRADED_FALLBACK_REPLY
        self._persist_turn(session_id, "assistant", reply)
        return {"session_id": session_id, "reply": reply}

    # ---- Agent loop (Gemini primary, Groq fallback) -------------------------
    def _run_agent_loop(self, session, latest_text: str):
        history = self._load_gemini_history(session.id)
        history.append({"role": "user", "text": latest_text})

        provider = "gemini"
        retrieved_chunks = []
        used_kb = False

        for round_index in range(MAX_TOOL_ROUNDS):
            if provider == "gemini":
                try:
                    step = self._gemini.generate(SYSTEM_INSTRUCTION, history, tools=TOOL_SCHEMAS)
                except GeminiError:
                    # Gemini can fail on ANY round (observed in practice: transient 503s / slow
                    # responses under load), not just the first - so the fallback must be able to
                    # trigger mid-loop too, converting whatever history has accumulated so far.
                    provider = "groq"
                    history = self._gemini_history_to_groq(history)
                    try:
                        step = self._groq.generate(SYSTEM_INSTRUCTION, history, tools=TOOL_SCHEMAS)
                    except GroqError:
                        break
            else:
                try:
                    step = self._groq.generate(SYSTEM_INSTRUCTION, history, tools=TOOL_SCHEMAS)
                except GroqError:
                    break

            if step["function_call"]:
                name = step["function_call"]["name"]
                args = step["function_call"]["args"]
                tool_result = self._execute_tool(session, name, args)
                if name == "search_knowledge_base":
                    used_kb = True
                    retrieved_chunks = tool_result.get("chunks", [])

                if provider == "gemini":
                    history.append({"role": "model", "function_call": {
                        "name": name, "args": args, "thought_signature": step["function_call"].get("thought_signature"),
                    }})
                    history.append({"role": "tool", "function_response": {"name": name, "response": tool_result}})
                else:
                    # A plain round_index-based id could collide with ids already assigned
                    # during _gemini_history_to_groq's conversion (it also numbers from 0) if
                    # the fallback happened mid-loop - a random suffix guarantees uniqueness
                    # within the conversation regardless of when the switch occurred.
                    call_id = f"call_{round_index}_{uuid.uuid4().hex[:8]}"
                    history.append({"role": "assistant", "content": None,
                                     "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]})
                    history.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": json.dumps(tool_result)})
                continue

            draft_reply = step["text"] or SAFE_FALLBACK_REPLY
            if looks_degenerate(draft_reply):
                logger.warning("degenerate_reply_detected: provider=%s", provider)
                return self._final_text_only(provider, latest_text, retrieved_chunks, used_kb)
            if not used_kb:
                return {"reply": draft_reply, "llm_provider": provider}

            return self._apply_guardrail(draft_reply, retrieved_chunks, provider)

        # The tool-round budget ran out (or a provider genuinely failed) without ever landing
        # on a text reply. Rather than keep negotiating "please stop calling tools now" on the
        # same tool-call-laden conversation - a constraint this model does not reliably honor,
        # and which Groq hard-rejects outright when disobeyed - finish with one guaranteed-clean
        # call: no tools registered, no tool-call-shaped history, just the gathered facts. A
        # model with no tool schema in the request has no structural way to attempt a tool call.
        return self._final_text_only(provider, latest_text, retrieved_chunks, used_kb)

    def _final_text_only(self, provider: str, latest_text: str, retrieved_chunks: list, used_kb: bool):
        context_note = ""
        if retrieved_chunks:
            joined = "\n".join(c["content"] for c in retrieved_chunks)
            context_note = f"\n\nRelevant knowledge base info you already looked up:\n{joined}"
        prompt = f"The visitor just said: \"{latest_text}\"{context_note}\n\nReply now, directly, in plain text."

        # Try the given provider first, then the other one - a degenerate/garbled reply is
        # treated the same as a provider failure here: never send it, just try the next option.
        for attempt_provider in ([provider, "groq"] if provider == "gemini" else ["groq", "gemini"]):
            try:
                if attempt_provider == "gemini":
                    step = self._gemini.generate(SYSTEM_INSTRUCTION, [{"role": "user", "text": prompt}])
                else:
                    step = self._groq.generate(SYSTEM_INSTRUCTION, [{"role": "user", "content": prompt}])
                draft_reply = step["text"] or SAFE_FALLBACK_REPLY
                if looks_degenerate(draft_reply):
                    logger.warning("degenerate_reply_detected: provider=%s (final_text_only)", attempt_provider)
                    continue
                if not used_kb:
                    return {"reply": draft_reply, "llm_provider": attempt_provider}
                return self._apply_guardrail(draft_reply, retrieved_chunks, attempt_provider)
            except (GeminiError, GroqError):
                continue

        return {"reply": SAFE_FALLBACK_REPLY if used_kb else DEGRADED_FALLBACK_REPLY, "llm_provider": None}

        return {"reply": DEGRADED_FALLBACK_REPLY, "llm_provider": None}

    def _apply_guardrail(self, draft_reply: str, retrieved_chunks: list, provider: str):
        try:
            score = self._groq.judge(draft_reply, [c["content"] for c in retrieved_chunks])
        except Exception as e:
            # Fails closed on ANY failure here (transport, or malformed chunk data) - never
            # send an unverified draft just because the judge call itself broke.
            if not isinstance(e, GroqError):
                logger.warning("guardrail_check_failed: %s", e)
            return {"reply": SAFE_FALLBACK_REPLY, "llm_provider": provider, "guardrail_score": None, "guardrail_passed": False}
        passed = score >= GUARDRAIL_THRESHOLD
        return {
            "reply": draft_reply if passed else SAFE_FALLBACK_REPLY,
            "llm_provider": provider, "guardrail_score": score, "guardrail_passed": passed,
        }

    # ---- Tool execution -----------------------------------------------------
    def _execute_tool(self, session, name: str, args: dict) -> dict:
        try:
            if name == "search_knowledge_base":
                return self._tool_search_knowledge_base(args.get("query", ""))
            if name == "upsert_crm_lead":
                return self._tool_upsert_crm_lead(session, args.get("name"), args.get("phone"), args.get("email"))
            if name == "extract_lead_signals":
                return self._tool_extract_lead_signals(session)
        except Exception as e:
            logger.warning("chatbot_tool_failed: %s %s", name, e)
            return {"error": "tool_failed"}
        return {"error": "unknown_tool"}

    def _tool_search_knowledge_base(self, query: str) -> dict:
        if not query.strip():
            return {"chunks": [], "sources": []}
        try:
            embedding = self._gemini.embed(query)
        except GeminiError:
            return {"chunks": [], "sources": []}
        rows = self._persistence.search_kb_chunks(embedding, top_k=5)
        chunks = [{"content": r.content, "similarity": float(r.similarity)} for r in rows if r.similarity and r.similarity > 0.3]
        sources = [r.title for r in rows if r.similarity and r.similarity > 0.3]
        return {"chunks": chunks, "sources": sources}

    def _tool_upsert_crm_lead(self, session, name: str, phone: str, email: str = None) -> dict:
        if phone and not valid_phone(phone):
            return {"error": "invalid_phone"}
        if email and not valid_email(email):
            return {"error": "invalid_email"}
        if name or phone:
            self._persistence.update_lead_fields(
                session.lead_id, visitor_name=name, visitor_phone=normalize_phone(phone) if phone else None,
            )
        if email and not self._save_lead_email(session.lead_id, email.strip()):
            return {"error": "email_save_failed"}
        return {"lead_id": session.lead_id}

    def _tool_extract_lead_signals(self, session) -> dict:
        recent = self._persistence.list_recent_messages(session.id, limit=HISTORY_TURN_LIMIT)
        transcript = "\n".join(f"{m.role}: {m.content}" for m in recent if m.content)
        if not transcript.strip():
            return {}
        try:
            data = self._groq.generate_json(
                system_instruction=(
                    "Extract lead qualification signals from this real-estate chat transcript. "
                    "Return ONLY JSON: {\"budget_min\": number|null, \"budget_max\": number|null, "
                    "\"unit_type\": string|null, \"timeline_days\": number|null, \"intent_signal\": string|null, "
                    "\"temperature\": \"hot\"|\"warm\"|\"cold\"}"
                ),
                user_content=transcript,
            )
        except GroqError:
            return {}
        self._persistence.upsert_qualification(
            id=str(uuid.uuid4()), lead_id=session.lead_id,
            budget_min=data.get("budget_min"), budget_max=data.get("budget_max"),
            unit_type=data.get("unit_type"), timeline_days=data.get("timeline_days"),
            intent_signal=data.get("intent_signal"), temperature=data.get("temperature"),
        )
        if data.get("temperature"):
            self._persistence.update_lead_fields(session.lead_id, lead_temperature=data["temperature"])
        return data

    def _save_lead_email(self, lead_id: str, email: str) -> bool:
        try:
            self._persistence.update_lead_email(lead_id, email.strip())
            return True
        except Exception as e:
            logger.warning("chatbot_email_save_failed: %s", e)
            return False

    def _safe_update_session_callback_state(self, session_id: str, state):
        try:
            return self._persistence.update_session_callback_state(session_id, state)
        except Exception as e:
            logger.warning("chatbot_email_state_update_failed: %s", e)
            return None

    def _safe_update_session_auth_state(self, session_id: str, state, payload: dict = None):
        try:
            return self._persistence.update_session_auth_state(session_id, state, payload)
        except Exception as e:
            logger.warning("chatbot_auth_state_update_failed: %s", e)
            return None

    # ---- Callback requests (broker-facing) -----------------------------
    def list_callback_requests(self, limit: int = 100):
        return self._persistence.list_callback_requests(limit=limit)

    def _safe_ip(self, raw: str):
        # Postgres's `inet` column rejects anything that isn't a valid IP outright (a lone
        # malformed X-Forwarded-For entry, or a proxy/test client that isn't a real address,
        # would otherwise crash session creation - the single highest-value moment to capture
        # a lead). Store NULL instead of failing the whole request when the value is unusable.
        if not raw:
            return None
        try:
            ipaddress.ip_address(raw)
            return raw
        except ValueError:
            return None

    # ---- Helpers -------------------------------------------------------
    def _persist_turn(self, session_id: str, role: str, content: str, tool_name: str = None,
                       llm_provider: str = None, guardrail_score: float = None, guardrail_passed: bool = None):
        # Best-effort: a transcript-logging failure must never discard an otherwise-successful
        # reply that's already been computed (and for the pre-agent-loop user-turn write, must
        # never block the turn from proceeding either - conversation history is a convenience
        # for future context, not a precondition for answering this message).
        try:
            self._persistence.create_message(
                id=str(uuid.uuid4()), session_id=session_id, role=role, content=content or "",
                tool_name=tool_name, llm_provider=llm_provider, guardrail_score=guardrail_score, guardrail_passed=guardrail_passed,
            )
        except Exception as e:
            logger.warning("persist_turn_failed: %s", e)

    def _load_gemini_history(self, session_id: str) -> list:
        try:
            rows = self._persistence.list_recent_messages(session_id, limit=HISTORY_TURN_LIMIT)
        except Exception as e:
            # Losing prior context is degraded, not fatal - the visitor's current message still
            # deserves an attempt at an answer, just without conversation memory this turn.
            logger.warning("load_history_failed: %s", e)
            return []
        history = []
        for row in rows:
            if row.role == "user":
                history.append({"role": "user", "text": row.content})
            elif row.role == "assistant":
                history.append({"role": "model", "text": row.content})
        return history

    def _gemini_history_to_groq(self, gemini_history: list) -> list:
        """Converts accumulated Gemini-shaped turns (including function_call/function_response
        entries from mid-loop tool rounds) into OpenAI-shaped messages Groq expects, so a
        Gemini failure partway through a tool-calling round can still hand off to Groq instead
        of losing everything gathered so far."""
        converted = []
        last_call_id = None
        for m in gemini_history:
            if m["role"] == "user" and "text" in m:
                converted.append({"role": "user", "content": m["text"]})
            elif m["role"] == "model" and "text" in m:
                converted.append({"role": "assistant", "content": m["text"]})
            elif m["role"] == "model" and "function_call" in m:
                last_call_id = f"call_{uuid.uuid4().hex[:8]}"
                fc = m["function_call"]
                converted.append({"role": "assistant", "content": None,
                                   "tool_calls": [{"id": last_call_id, "type": "function",
                                                    "function": {"name": fc["name"], "arguments": json.dumps(fc["args"])}}]})
            elif m["role"] == "tool" and "function_response" in m:
                fr = m["function_response"]
                converted.append({"role": "tool", "tool_call_id": last_call_id, "name": fr["name"], "content": json.dumps(fr["response"])})
        return converted
