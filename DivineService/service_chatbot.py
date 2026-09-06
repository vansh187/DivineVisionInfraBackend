import os
import re
import json
import uuid
import logging
import ipaddress
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote_plus

from Divinepersistence import persistenceChatbot, persistenceInventory
from Divinepersistence.persistence_loan import persistenceLoan
from DivineDTO.models import UserCreateDTO
from DivineService.service_broker import serviceBroker
from DivineService.service_customer import serviceCustomer
from DivineService.llm_gemini import llmGemini, GeminiError
from DivineService.llm_groq import llmGroq, GroqError
from DivineService.service_zoho import serviceZoho
from DivineService.loan_utils import normalize_indian_amount
from DivineService.loan_report_data import (
    build_report_data, report_pdf_filename, report_download_url, report_data_url,
)
from DivineService.service_loan_calculator import calculate_emi, compare_tenures as _compare_tenures
from DivineService.service_loan_eligibility import (
    calculate_loan_eligibility, calculate_affordability, ILLUSTRATIVE_RATE_DEFAULT,
)
from DivineService.loan_knowledge import get_document_checklist

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = (
    "You are the AI concierge for Divine Vision Infratech, a real-estate developer. "
    "Answer visitor questions about projects, pricing, RERA, and specs ONLY using the "
    "search_knowledge_base tool's results - never invent figures or claims from your own "
    "general knowledge. If the knowledge base has no relevant information, say so plainly "
    "and offer a callback instead of guessing. "
    "For any question about unit sizes, plot sizes, plot dimensions, or plot area ('how "
    "big are the plots', 'what sizes do you have', 'kitne gaj', 'kitne size'), call "
    "get_plot_size_options and answer with how many distinct sizes are on offer and list "
    "them - give the area in sq. yards, plus the plot dimensions in metres where available, "
    "grouped by project. Do NOT ask for the visitor's name or phone number just to answer "
    "a sizing question. "
    "You are a knowledgeable, consultative sales assistant, not just an FAQ bot - when you "
    "don't have specific data, don't just say you don't know: acknowledge the question, "
    "share what you do know from the knowledge base, and route to a callback or site visit "
    "for the specifics you can't confirm. When a visitor raises a concern or objection, "
    "address it directly using knowledge base content before pivoting to a call-to-action. "
    "Keep replies short, warm, and in the visitor's own language/register (Hindi/Hinglish/"
    "English as they write). Use upsert_crm_lead when the visitor shares their name, phone "
    "number, or email address. Only ask for email if the visitor explicitly says they want "
    "to connect by email. Use extract_lead_signals after a few substantive turns to capture "
    "budget/timeline signals."
)

LOAN_SYSTEM_INSTRUCTION = (
    "\n\nYou are also the Home Loan Assistant for this real-estate site. You help visitors "
    "estimate EMI, home-loan eligibility, and property affordability using ONLY the loan "
    "calculation tools provided (update_loan_profile, calculate_emi, calculate_loan_eligibility, "
    "calculate_affordability, compare_tenures, get_document_checklist, "
    "generate_eligibility_report). You must NEVER perform this arithmetic yourself - always "
    "call the relevant tool and base your answer only on its returned numbers.\n"
    "Rules:\n"
    "- Ask only for the specific pieces of information a calculation actually needs, one or "
    "two questions at a time, conversationally - never present a long form.\n"
    "- Before asking for anything, call update_loan_profile with whatever the visitor already "
    "stated in their message, and check what's already known this session - never re-ask for a "
    "value already captured.\n"
    "- For eligibility, ALWAYS ask whether the visitor already has any ongoing EMIs or loan "
    "repayments (car loan, personal loan, another home loan, credit-card EMIs) and their total "
    "monthly amount - existing obligations directly reduce how much they can borrow. If they "
    "say they have none, call update_loan_profile with existing_emi: 0 so it is on record, "
    "then proceed. In the eligibility answer, briefly note how their existing EMIs affected the "
    "available capacity.\n"
    "- If the visitor doesn't give an interest rate, the calculation tools use an illustrative "
    "default and tell you what it was - always say plainly to the visitor: \"Illustrative rate "
    "used for calculation: X%\" - never invent or claim to know current bank rates.\n"
    "- NEVER say a loan is \"approved\" or guaranteed. Always use \"estimated\", \"indicative\", "
    "\"approximate\", or \"based on the information provided\".\n"
    "- NEVER state a specific bank's policy, a specific lender's approval odds, or a CIBIL/"
    "credit score the visitor did not themselves provide.\n"
    "- Every EMI or eligibility answer must end with: \"This is an indicative calculation and "
    "actual lender terms may differ.\"\n"
    "- When a calculation is complete, mention the visitor can download an eligibility report.\n"
    "- After calling generate_eligibility_report, reply with ONE short sentence saying the "
    "report is ready and downloading now. NEVER write out a URL, a link, or any "
    "'/loan/report' path - the download is handled for the visitor.\n"
    "- For document questions, call get_document_checklist rather than listing documents from "
    "memory, and note that exact requirements vary by lender.\n"
    "- Keep the tone warm, human, and consultative - like a knowledgeable loan advisor, not a form."
)
SYSTEM_INSTRUCTION = SYSTEM_INSTRUCTION + LOAN_SYSTEM_INSTRUCTION

SAFE_FALLBACK_REPLY = "I don't want to guess on that — let me get you an exact answer from our team. Would you like a callback?"
# Never phrased as a failure/apology - a customer must always be handed forward, not
# left at a dead end. Paired with deterministic buttons wherever it is returned.
DEGRADED_FALLBACK_REPLY = (
    "I want to make sure you get exact, up-to-date information on this. Our property "
    "team can help you directly - would you like to book a site visit or speak with a "
    "sales advisor?"
)
# Buttons attached to any degraded reply so the visitor always has a working next step.
DEGRADED_FALLBACK_BUTTONS = [
    {"label": "Book a site visit", "value": "Book a site visit", "action": "chatbot_message"},
    {"label": "Talk to a sales advisor", "value": "Talk to a sales advisor", "action": "chatbot_message"},
]

# ---- Best-effort recovery -------------------------------------------------
# The client's requirement: the bot must never dead-end with "I can't answer that". When the
# normal loop fails to land a grounded reply (tool budget spent, provider error, garbled text,
# or a draft the guardrail rejects), the recovery path re-reads the WHOLE conversation, runs a
# wider search across the knowledge base and inventory, and asks the model for its single best
# answer. If that answer checks out against retrieved data it goes as-is; if it can only be
# answered from general knowledge it still goes out, tagged as general guidance with a callback
# offer, rather than being withheld. The one remaining dead-end is a total LLM outage.
RECOVERY_HISTORY_TURN_LIMIT = 40
RECOVERY_KB_TOP_K = 10
RECOVERY_KB_MIN_SIMILARITY = 0.15
INDICATIVE_GENERAL_SUFFIX = (
    " (This is general guidance, not confirmed project detail — our team can give you exact, "
    "up-to-date figures. Would you like a callback or a site visit?)"
)
RECOVERY_DIRECTIVE = (
    "RECOVERY MODE. Earlier attempts to answer this visitor did not produce a confident reply, "
    "but you must not give up, stall, or tell them to try again later — give the single most "
    "useful answer you can from everything below. Ground every specific figure in the retrieved "
    "knowledge-base or inventory data. Where the data does not cover the question you may offer "
    "general real-estate guidance, but never state a specific price, plot number, RERA number, or "
    "legal guarantee that is not present in the data. Keep every home-loan rule already given: "
    "never say a loan is approved, never quote a specific bank's rate or policy, always keep the "
    "'indicative calculation' disclaimer. Match the visitor's language/register and keep it short "
    "and warm."
)

MAX_TOOL_ROUNDS = 3
HISTORY_TURN_LIMIT = 20
# After this many failed login attempts in one auth flow, abandon the flow and let the
# visitor keep chatting - otherwise a wrong password (or a stale auth_state that made a
# normal question get read as a password) traps them in a "invalid email or password" loop.
MAX_LOGIN_ATTEMPTS = 3
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
CHANNEL_PARTNER_LOGIN_BUTTON = {
    "label": "Login as Channel Partner",
    "value": "login_broker",
    "action": "chatbot_auth",
}
# A booking always needs an authenticated customer / channel partner - both buttons
# are offered so a visitor can pick the right account type.
BOOKING_LOGIN_BUTTONS = [CUSTOMER_LOGIN_BUTTON, CHANNEL_PARTNER_LOGIN_BUTTON]

# Frontend "browse & book plots" page. The chat hands over clickable plot rows
# pointing here; that page checks the auth token and, if absent, shows the login
# buttons above. Navigated to IN THE SAME TAB (action "navigate", target "_self") -
# it is a plain client-side route, nothing is proxied through this backend.
BOOK_PLOT_URL = (os.getenv("DIVINE_BOOK_PLOT_URL")
                 or "https://www.divinevisioninfra.com/customer/plots").rstrip("/")
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

# ---- Menu-driven sales flow (deterministic, no LLM) --------------------------
# Button copy below is a first pass and expected to be refined with exact wording
# from the business - the state sequencing/persistence is the stable part.
MAIN_MENU_BUTTONS = [
    {"label": "About Divine Vision", "value": "menu_about", "action": "chatbot_menu"},
    {"label": "Chat with Sales", "value": "menu_sales", "action": "chatbot_menu"},
    {"label": "Chat with Support", "value": "menu_support", "action": "chatbot_menu"},
    {"label": "Only Browsing", "value": "menu_browsing", "action": "chatbot_menu"},
    {"label": "Home Loan / EMI Help", "value": "menu_loan", "action": "chatbot_menu"},
]

LOAN_ENTRY_SEED_TEXT = "I want to check my home loan eligibility"

# Deterministic reply for the report-ready turn. Belt-and-suspenders: the model
# is not handed any URL to leak (see _model_facing_tool_result), but if it ever
# improvises a link this fixed line still replaces its prose.
REPORT_READY_REPLY = (
    "Your home loan eligibility report is ready and downloading now. If it does not "
    "start automatically, use the download button below. This is an indicative "
    "calculation and actual lender terms may differ."
)
LOAN_INITIAL_BUTTONS = [
    {"label": "Check Loan Eligibility", "value": "loan_eligibility", "action": "chatbot_message"},
    {"label": "Calculate EMI", "value": "loan_emi", "action": "chatbot_message"},
    {"label": "Check Property Affordability", "value": "loan_affordability", "action": "chatbot_message"},
    {"label": "Documents Required", "value": "loan_documents", "action": "chatbot_message"},
]

# Re-offered whenever the visitor asks for the "Main Menu" - mirrors the widget's
# opening "Popular questions" chips. value == label so the frontend posts the plain
# text, which the quick-action intent detectors (wants_home_loan / wants_project_info
# / wants_site_visit / wants_sales_advisor) then route.
MAIN_MENU_BUTTON = {"label": "Main Menu", "value": "main_menu", "action": "chatbot_message"}
POPULAR_QUESTION_BUTTONS = [
    {"label": "Home loan / finance enquiry", "value": "Home loan / finance enquiry", "action": "chatbot_message"},
    {"label": "Pricing & payment plan", "value": "Pricing & payment plan", "action": "chatbot_message"},
    {"label": "Book a site visit", "value": "Book a site visit", "action": "chatbot_message"},
    {"label": "Show available plots", "value": "Show available plots", "action": "chatbot_message"},
    {"label": "Talk to a sales advisor", "value": "Talk to a sales advisor", "action": "chatbot_message"},
]
SALES_TRACK_BUTTONS = [
    {"label": "Buy a Property / End Client", "value": "sales_end_client", "action": "chatbot_menu"},
    {"label": "Investor / Dealer", "value": "sales_investor_dealer", "action": "chatbot_menu"},
]
PROFILE_TYPE_BUTTONS = [
    {"label": "Individual Buyer", "value": "profile_individual_buyer", "action": "chatbot_menu"},
    {"label": "Individual Investor", "value": "profile_individual_investor", "action": "chatbot_menu"},
    {"label": "Channel Partner / Broker", "value": "profile_channel_partner", "action": "chatbot_menu"},
    {"label": "Corporate / Institutional", "value": "profile_corporate", "action": "chatbot_menu"},
]
LOCATION_BUTTONS = [
    {"label": "OPS Divine Greens area", "value": "loc_ops_divine_greens", "action": "chatbot_menu"},
    {"label": "Suraksha Enclave area", "value": "loc_suraksha_enclave", "action": "chatbot_menu"},
    {"label": "Other / Not sure yet", "value": "loc_other", "action": "chatbot_menu"},
]
OPPORTUNITY_TYPE_BUTTONS = [
    {"label": "Residential Plot", "value": "opp_residential_plot", "action": "chatbot_menu"},
    {"label": "Residential Unit / Flat", "value": "opp_residential_unit", "action": "chatbot_menu"},
    {"label": "Commercial", "value": "opp_commercial", "action": "chatbot_menu"},
]
INVESTMENT_SIZE_BUTTONS = [
    {"label": "Under 20 Lakh", "value": "size_under_20l", "action": "chatbot_menu"},
    {"label": "20-50 Lakh", "value": "size_20_50l", "action": "chatbot_menu"},
    {"label": "50 Lakh - 1 Cr", "value": "size_50l_1cr", "action": "chatbot_menu"},
    {"label": "Above 1 Cr", "value": "size_above_1cr", "action": "chatbot_menu"},
]
INVESTMENT_GOAL_BUTTONS = [
    {"label": "Long-term Investment", "value": "goal_long_term", "action": "chatbot_menu"},
    {"label": "Short-term / Quick Resale", "value": "goal_short_term", "action": "chatbot_menu"},
    {"label": "Rental Yield", "value": "goal_rental_yield", "action": "chatbot_menu"},
    {"label": "Self Use / End Use", "value": "goal_self_use", "action": "chatbot_menu"},
]
PROCEED_BUTTONS = [
    {"label": "Register My Interest", "value": "proceed_register", "action": "chatbot_menu"},
    {"label": "Schedule a Site Visit", "value": "proceed_site_visit", "action": "chatbot_menu"},
    {"label": "Send Me Regular Updates", "value": "proceed_updates", "action": "chatbot_menu"},
    {"label": "Talk to Someone Now", "value": "proceed_talk_now", "action": "chatbot_menu"},
]
FIRST_TIME_BUTTONS = [
    {"label": "Yes, first time", "value": "first_time_yes", "action": "chatbot_menu"},
    {"label": "No, visited before", "value": "first_time_no", "action": "chatbot_menu"},
]
DECISION_BUTTONS = [
    {"label": "I've Decided, Proceed", "value": "decision_proceed", "action": "chatbot_menu"},
    {"label": "Call Me Back", "value": "decision_call_back", "action": "chatbot_menu"},
    {"label": "WhatsApp Me", "value": "decision_whatsapp", "action": "chatbot_menu"},
]
YES_NO_BUTTONS = [
    {"label": "Yes", "value": "yes", "action": "chatbot_menu"},
    {"label": "No", "value": "no", "action": "chatbot_menu"},
]

# state -> (reply text asked upon entering the state, buttons or None)
MENU_QUESTIONS = {
    "main_menu": ("How can I help you today?", MAIN_MENU_BUTTONS),
    "sales_track": ("Great! Are you looking to buy a property for yourself, or exploring as an investor/dealer?", SALES_TRACK_BUTTONS),
    "sales_profile_type": ("Please select your working profile type.", PROFILE_TYPE_BUTTONS),
    "sales_location": ("Which location are you currently exploring for investment?", LOCATION_BUTTONS),
    "sales_opportunity_type": ("What type of opportunities are you looking for?", OPPORTUNITY_TYPE_BUTTONS),
    "sales_investment_size": ("What is your typical investment size?", INVESTMENT_SIZE_BUTTONS),
    "sales_investment_goal": ("What is your investment goal?", INVESTMENT_GOAL_BUTTONS),
    "sales_proceed": ("How would you like to proceed?", PROCEED_BUTTONS),
    "browsing_first_time": ("No problem! Is this your first time exploring our projects?", FIRST_TIME_BUTTONS),
    "browsing_location": ("Which location are you exploring?", LOCATION_BUTTONS),
    "browsing_budget": ("What's your approximate budget range?", INVESTMENT_SIZE_BUTTONS),
    "browsing_investor_qual": ("Are you exploring this as an investor, or for personal/end use?", SALES_TRACK_BUTTONS),
    "browsing_decision": ("How would you like to proceed?", DECISION_BUTTONS),
    "support_updates_optin": ("Thanks, our team will get back to you shortly! Would you like to be notified about new updates and offers?", YES_NO_BUTTONS),
}

# state -> (menu_payload field the matched button value is stored under, next state)
# Only states whose answer is "pick one button, store it, move to the next question" -
# branching states (main_menu, support_*, browsing_decision, sales_proceed) are hand-written.
# The mandatory contact-capture steps at the very start of the funnel. A top
# quick-action chip clicked while in one of these skips straight to its action;
# every later state (main menu, sales/browsing/support flows) is left intact -
# there the visitor already has the relevant buttons.
_GREETING_STATES = ("greeting_name", "greeting_phone", "greeting_email")

MENU_LINEAR_TRANSITIONS = {
    "sales_track": ("buyer_type", "sales_profile_type"),
    "sales_profile_type": ("working_profile_type", "sales_location"),
    "sales_location": ("location_preference", "sales_opportunity_type"),
    "sales_opportunity_type": ("opportunity_type", "sales_investment_size"),
    "sales_investment_size": ("investment_size_band", "sales_investment_goal"),
    "sales_investment_goal": ("investment_goal", "sales_proceed"),
    "browsing_first_time": ("first_time_response", "browsing_location"),
    "browsing_location": ("location_preference", "browsing_budget"),
    "browsing_budget": ("investment_size_band", "browsing_investor_qual"),
    "browsing_investor_qual": ("buyer_type", "browsing_decision"),
}

# Both sales_track and browsing_investor_qual reuse SALES_TRACK_BUTTONS' values for the
# buyer_type signal - map them to the DB's CHECK-constrained enum.
BUYER_TYPE_VALUE_MAP = {"sales_end_client": "end_client", "sales_investor_dealer": "investor_dealer"}


def _tokens_contain_subsequence(haystack_tokens: list, needle_tokens: list) -> bool:
    # Whole-token containment, not raw substring containment - "no" must not match inside
    # "noon" or "not_sure" or "know", the way naive `"no" in "noon"` would.
    if not needle_tokens:
        return False
    n = len(needle_tokens)
    return any(haystack_tokens[i:i + n] == needle_tokens for i in range(len(haystack_tokens) - n + 1))


def _match_menu_button(raw: str, buttons: list):
    text = _contact_text(raw)
    if not text:
        return None
    normalized_value = re.sub(r"[\s-]+", "_", text)
    value_tokens = [t for t in normalized_value.split("_") if t]

    for b in buttons:
        if b["value"] == normalized_value:
            return b["value"]
    for b in buttons:
        needle = [t for t in b["value"].split("_") if t]
        if _tokens_contain_subsequence(value_tokens, needle):
            return b["value"]

    text_tokens = text.split()
    for b in buttons:
        label_norm = _contact_text(b["label"])
        if not label_norm:
            continue
        if label_norm == text:
            return b["value"]
        if _tokens_contain_subsequence(text_tokens, label_norm.split()):
            return b["value"]
    return None


TOOL_SCHEMAS = [
    {
        "name": "search_knowledge_base",
        "description": "Search the project knowledge base (pricing, RERA, specs, location) for information relevant to the visitor's question.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "search query"}}, "required": ["query"]},
    },
    {
        "name": "get_plot_size_options",
        "description": (
            "List the distinct plot/unit sizes on offer across the projects, with a count of "
            "how many distinct sizes there are. Use this for any question about unit sizes, "
            "plot sizes, plot dimensions, area, 'how big are the plots', or 'what sizes are "
            "available'. Returns area in sq. yards / sq. metres and plot dimensions in metres "
            "where recorded, grouped by project."
        ),
        "parameters": {"type": "object", "properties": {
            "project_name": {"type": "string", "description": "optional project filter, e.g. 'OPS Divine Greens' or 'Suraksha'"},
        }, "required": []},
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

LOAN_TOOL_SCHEMAS = [
    {
        "name": "update_loan_profile",
        "description": "Save any financial details the visitor just mentioned (income, EMI, age, property price, etc.) so they aren't asked again this session.",
        "parameters": {"type": "object", "properties": {
            "monthly_income": {"type": "string", "description": "e.g. '1.5 lakh', '150000'"},
            "co_applicant_income": {"type": "string"},
            "existing_emi": {"type": "string"},
            "age": {"type": "number"},
            "employment_type": {"type": "string", "description": "'salaried' or 'self_employed'"},
            "property_price": {"type": "string"},
            "down_payment": {"type": "string"},
            "requested_loan": {"type": "string"},
            "interest_rate": {"type": "number", "description": "annual %, only if visitor stated one"},
            "tenure_years": {"type": "number"},
            "credit_score_band": {"type": "string", "description": "e.g. 'poor','fair','good','excellent', or a raw score like 740"},
        }, "required": []},
    },
    {
        "name": "calculate_emi",
        "description": "Compute monthly EMI, total interest, and total repayment. Uses the visitor's saved loan profile fields (requested_loan, interest_rate, tenure_years) for any argument not explicitly given.",
        "parameters": {"type": "object", "properties": {
            "principal": {"type": "number"}, "annual_rate_pct": {"type": "number"}, "tenure_years": {"type": "number"},
        }, "required": []},
    },
    {
        "name": "calculate_loan_eligibility",
        "description": "Estimate an indicative home-loan eligibility range from the visitor's saved income/EMI/credit profile.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "calculate_affordability",
        "description": "Check whether the visitor's saved profile can comfortably afford a given property price, and by how much it falls short if not.",
        "parameters": {"type": "object", "properties": {
            "property_price": {"type": "string"},
        }, "required": []},
    },
    {
        "name": "compare_tenures",
        "description": "Return an EMI/interest/total-payment comparison across multiple tenure options for the visitor's loan amount.",
        "parameters": {"type": "object", "properties": {
            "tenure_options_years": {"type": "array", "items": {"type": "number"}},
        }, "required": []},
    },
    {
        "name": "get_document_checklist",
        "description": "Return the standard home-loan document checklist for a given employment type.",
        "parameters": {"type": "object", "properties": {
            "employment_type": {"type": "string", "description": "'salaried' or 'self_employed'"},
        }, "required": ["employment_type"]},
    },
    {
        "name": "generate_eligibility_report",
        "description": "Generate a downloadable PDF summary of the visitor's most recent loan calculation and profile.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]


def _make_optionals_nullable(schemas: list) -> list:
    """Both providers' models emit `null` for optional tool args they don't have a
    value for; strict server-side validation (Groq especially) then 400s the whole
    call. Declaring every NON-required property as nullable makes that a valid call
    - the tool handlers already treat None / missing as "not provided". Applied
    once at import so no individual schema has to remember to do it."""
    patched = []
    for tool in schemas:
        try:
            params = dict(tool.get("parameters") or {})
            required = set(params.get("required") or [])
            props = {}
            for name, spec in (params.get("properties") or {}).items():
                spec = dict(spec)
                declared = spec.get("type", "string")
                if name not in required and isinstance(declared, str) and declared != "null":
                    spec["type"] = [declared, "null"]
                props[name] = spec
            params["properties"] = props
            patched.append({**tool, "parameters": params})
        except Exception as e:  # a malformed schema must not break module import
            logger.warning("tool_schema_nullable_pass_failed name=%s error=%s", tool.get("name"), e)
            patched.append(tool)
    return patched


TOOL_SCHEMAS = _make_optionals_nullable(TOOL_SCHEMAS + LOAN_TOOL_SCHEMAS)

LOAN_STRUCTURED_RESULT_TYPES = {
    "calculate_emi": "emi_result",
    "calculate_loan_eligibility": "eligibility_result",
    "calculate_affordability": "affordability_result",
    "compare_tenures": "tenure_comparison",
    "generate_eligibility_report": "report_ready",
}


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


def _as_number(value):
    # Inventory area/dimension columns come back as Decimal on Postgres and float/str on
    # SQLite - normalise to a plain float (or None) so tool output is JSON-clean.
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


# Words that mark a message as a topic/command (usually a tapped suggestion chip like
# "Home loan / finance enquiry"), not the visitor's name. Whole-word match only, and
# deliberately limited to strongly-topical words - generic ones like "call"/"now"/
# "help" are omitted because they are also plausible given names or surnames, and the
# 2-strike escape hatch in the greeting step covers the rest.
_NOT_A_NAME_WORDS = frozenset({
    "loan", "loans", "emi", "finance", "enquiry", "inquiry",
    "pricing", "payment", "payments", "plot", "plots",
    "visit", "visits", "booking", "brochure", "rera", "callback",
    "availability", "budget", "affordability",
    "eligibility", "tenure", "documents",
})


def looks_like_name(text: str) -> bool:
    """True if `text` is plausibly a person's name rather than a tapped topic chip
    or a sentence. Deliberately lenient - the greeting step only re-prompts twice
    on a False before accepting whatever was typed, so a false negative costs one
    extra prompt, never a dead end."""
    t = (text or "").strip()
    if not t or len(t) > 80:
        return False
    if any(ch.isdigit() for ch in t):
        return False
    if any(ch in t for ch in "/?@#:;=_"):
        return False
    words = [w for w in re.split(r"\s+", t) if w]
    if not (1 <= len(words) <= 7):
        return False
    word_set = {w.strip(".,!'\"-").lower() for w in words}
    if word_set & _NOT_A_NAME_WORDS:
        return False
    # letters, spaces and the handful of punctuation real names use
    return all(ch.isalpha() or ch.isspace() or ch in ".-'" for ch in t)


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
    password_match = re.search(r'["\']?\bpassword\b["\']?\s*[:=]\s*["\']?([^"\'},\s]+)', text, re.IGNORECASE)
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


_PROJECT_INFO_KEYWORDS = (
    "plot", "plots", "property", "properties", "unit", "units", "flat", "flats",
    "price", "pricing", "rate", "rates", "cost", "budget", "brochure",
    "detail", "details", "information", "info",
    "size", "sizes", "area", "dimension", "dimensions",
    "available", "availability", "inventory", "option", "options",
    "location", "township", "rera", "possession", "amenities", "amenity",
    "payment plan", "installment", "instalment", "floor plan", "layout",
)


def wants_project_info(raw: str) -> bool:
    # A message that plainly reads as a projects/pricing/specs question rather than a
    # login credential. Used to break out of a stale auth flow so a logged-in visitor
    # isn't answered with "invalid email or password".
    text = _contact_text(raw)
    if not text:
        return False
    return any(kw in text for kw in _PROJECT_INFO_KEYWORDS)


# Phrases that unambiguously mean "take me to the home-loan assistant" - the quick
# suggestion chip ("Home loan / finance enquiry"), the menu button ("Home Loan /
# EMI Help" / value "menu_loan"), or clear free text. A bare "loan" is NOT enough.
_HOME_LOAN_PHRASES = (
    "home loan", "homeloan", "housing loan", "house loan", "home finance",
    "loan eligibility", "loan eligiblity", "emi help", "calculate emi", "emi calcul",
    "loan / finance", "loan/finance", "finance enquiry", "finance inquiry",
    "loan enquiry", "loan inquiry", "menu_loan", "loan_emi", "loan / emi",
)
_HOME_LOAN_SUPPORT_WORDS = (
    "emi", "finance", "eligib", "mortgage", "tenure", "interest rate",
    "installment", "instalment", "down payment", "affordab",
)


def wants_home_loan(raw: str) -> bool:
    text = _contact_text(raw)
    if not text:
        return False
    if any(p in text for p in _HOME_LOAN_PHRASES):
        return True
    return "loan" in text and any(w in text for w in _HOME_LOAN_SUPPORT_WORDS)


_SITE_VISIT_PHRASES = (
    "site visit", "site-visit", "book a site", "book a visit", "schedule a visit",
    "schedule a site", "visit the site", "visit the project", "project visit",
    "site tour", "proceed_site_visit",
)
_SALES_ADVISOR_PHRASES = (
    "sales advisor", "sales adviser", "talk to sales", "talk to a sales",
    "speak to sales", "speak with sales", "sales executive", "sales expert",
    "sales team", "connect me with sales", "chat with sales", "menu_sales",
    "talk to a sales advisor",
)


def wants_site_visit(raw: str) -> bool:
    text = _contact_text(raw)
    return bool(text) and any(p in text for p in _SITE_VISIT_PHRASES)


def wants_sales_advisor(raw: str) -> bool:
    text = _contact_text(raw)
    return bool(text) and any(p in text for p in _SALES_ADVISOR_PHRASES)


_MAIN_MENU_PHRASES = (
    "main menu", "main_menu", "back to menu", "back to the menu", "go to menu",
    "show menu", "open menu", "show me the options", "show the options",
    "show options", "other options", "start over", "go back to start",
)


def wants_main_menu(raw: str) -> bool:
    text = _contact_text(raw)
    if not text:
        return False
    return text in ("menu", "home") or any(p in text for p in _MAIN_MENU_PHRASES)


_PLOT_LISTING_PHRASES = (
    "show available plots", "available plots", "plots available", "show plots",
    "show me plots", "see plots", "list plots", "what plots", "which plots",
    "plot sizes", "plot size", "plot options", "available units", "show inventory",
    "available inventory", "what's available", "whats available",
)


def wants_plot_listing(raw: str) -> bool:
    text = _contact_text(raw)
    return bool(text) and any(p in text for p in _PLOT_LISTING_PHRASES)


_BROWSE_PLOTS_PHRASES = ("browse_plots", "browse plots", "browse and book plots",
                        "browse & book plots", "book_plots")


def wants_browse_plots(raw: str) -> bool:
    text = _contact_text(raw)
    return bool(text) and any(p in text for p in _BROWSE_PLOTS_PHRASES)


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
                 customer_service: serviceCustomer = None, broker_service: serviceBroker = None,
                 zoho: serviceZoho = None, loan_persistence: persistenceLoan = None,
                 inventory_persistence: persistenceInventory = None):
        self._persistence = persistence or persistenceChatbot()
        self._gemini = gemini or llmGemini()
        self._groq = groq or llmGroq()
        self._customer_service = customer_service
        self._broker_service = broker_service
        self._loan_persistence = loan_persistence or persistenceLoan()
        try:
            self._inventory = inventory_persistence or persistenceInventory()
        except Exception as e:
            # Inventory lookups are an enhancement (plot-size answers) - a failure here must
            # never stop the chatbot from starting; the tool degrades to "unavailable".
            logger.warning("inventory_persistence_init_failed: %s", e)
            self._inventory = None
        try:
            self._zoho = zoho or serviceZoho()
        except Exception as e:
            logger.warning("zoho_service_init_failed: %s", e)
            self._zoho = None

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
        try:
            self._persistence.update_session_menu_state(session.id, "greeting_name", {})
        except Exception as e:
            # Best-effort: if this fails, the visitor just lands in the old free-text
            # experience (menu_state stays NULL) instead of the guided funnel - never
            # fail session creation over it.
            logger.warning("menu_state_init_failed session_id=%s error=%s", session.id, e)
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

        # "I'm already logged in" from the Browse & Book Plots gate - bypass the
        # in-chat login and just route to the plots page. Checked before the
        # auth_state router so the pending "choose_login_for_plots" state is escaped.
        if text.strip().lower() in ("plots_already_logged_in", "i'm already logged in", "im already logged in"):
            self._safe_update_session_auth_state(session_id, None, None)
            self._persist_turn(session_id, "user", text)
            reply = "Great - taking you to the plots page."
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply,
                    "redirect_url": BOOK_PLOT_URL, "redirect_target": "_self"}

        auth_flow = selected_auth_flow(text)
        if auth_flow and getattr(session, "auth_state", None):
            self._persist_turn(session_id, "user", text)
            return self._start_auth_flow(session, auth_flow)

        if getattr(session, "auth_state", None):
            return self._advance_auth_flow(session, text)

        # "Main Menu" - from the loan assistant or anywhere - drops whatever flow the
        # visitor is in and re-offers the opening "Popular questions" choices so they
        # can pick again. Sits above the callback/menu/loan routing so it always wins.
        if wants_main_menu(text):
            self._safe_update_session_menu_state(session_id, None, None)
            for clear in (
                lambda: self._persistence.update_session_callback_state(session_id, None),
                lambda: self._persistence.update_session_loan_state(session_id, None),
            ):
                try:
                    clear()
                except Exception as e:
                    logger.warning("main_menu_state_clear_failed: %s", e)
            self._persist_turn(session_id, "user", text)
            reply = "Sure! Here's what I can help you with - pick an option:"
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply, "buttons": list(POPULAR_QUESTION_BUTTONS)}

        if intent == "request_callback" and not session.callback_state:
            if getattr(session, "menu_state", None):
                # A callback request can arrive mid-funnel (e.g. a persistent "Request a
                # Callback" widget separate from the menu buttons) - without clearing
                # menu_state here, _advance_callback_flow's "complete" branch recurses into
                # handle_message once the callback flow finishes, and that recursive call
                # would misinterpret the visitor's next free-text reply as an answer to the
                # abandoned menu question instead of routing it normally.
                self._safe_update_session_menu_state(session_id, None, None)
            session = self._persistence.update_session_callback_state(session_id, "awaiting_name")
            reply = "Sure! May I know your name?"
            self._persist_turn(session_id, "user", "[intent:request_callback]")
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}

        if session.callback_state:
            return self._advance_callback_flow(session, text)

        if auth_flow and auth_flow[1] and getattr(session, "menu_state", None):
            # Explicit, UNAMBIGUOUS auth intent (mode + role both known, e.g. "login as
            # customer") always wins over the menu funnel - without this, a fresh visitor
            # typing that during greeting/menu capture would have it swallowed as their
            # name or as an answer to the current menu question. Deliberately requires a
            # role (auth_flow[1]) rather than firing on the weaker ("choose", None) case -
            # selected_auth_flow does plain substring matching on bare words like "register"/
            # "create", which collide with menu button values (e.g. "proceed_register").
            self._safe_update_session_menu_state(session_id, None, None)
            self._persist_turn(session_id, "user", text)
            return self._start_auth_flow(session, auth_flow)

        # The widget's top quick-action chips must act immediately - the client does
        # not want name/phone/email captured as a gate before the visitor gets what
        # they clicked for. When one of those intents arrives during the initial
        # greeting capture, drop the funnel and let the routing below handle it
        # (home loan -> loan assistant, pricing/plots -> KB/agent answer, booking ->
        # project buttons, site visit / advisor -> callback flow, which asks for
        # name/phone inline as part of the action, not as a gate). Deeper menu flows
        # the visitor deliberately entered (sales/support/browsing) are left intact.
        if getattr(session, "menu_state", None) in _GREETING_STATES and (
            wants_home_loan(text) or wants_project_info(text) or wants_plot_booking(text)
            or selected_booking_project(text) or wants_site_visit(text)
            or wants_sales_advisor(text)
        ):
            self._safe_update_session_menu_state(session_id, None, None)
            session.menu_state = None

        # An explicit "home loan / finance / EMI" request jumps straight into the loan
        # assistant. Gated so it fires only on the deliberate intent (never a bare
        # "loan") and only when the visitor is not already inside the loan assistant.
        if wants_home_loan(text) and not self._get_loan_payload(session):
            if getattr(session, "menu_state", None):
                self._safe_update_session_menu_state(session_id, None, None)
            return self._enter_loan_assistant(session)

        if getattr(session, "menu_state", None):
            return self._advance_menu_flow(session, text)

        if not text:
            # The frontend calls /message once with empty text right after opening the
            # widget, expecting the welcome greeting back. That greeting normally comes
            # from the menu funnel's "greeting_name" state, which init_session arms. If
            # it isn't armed here - init_session's menu_state write failed, or this
            # integration (e.g. the customer portal) opened the chat without calling
            # /session/init - arm it now on this first, message-less turn and greet,
            # instead of returning a terse "didn't catch that". Guarded on the session
            # being fresh so a stray empty message mid-conversation doesn't restart the
            # funnel.
            if self._session_is_fresh(session_id):
                armed = self._safe_update_session_menu_state(session_id, "greeting_name", {})
                if armed is not None:
                    return self._advance_menu_flow(armed, "")
            return {"session_id": session_id, "reply": "Sorry, I didn't catch that — could you type your question?"}

        if auth_flow:
            self._persist_turn(session_id, "user", text)
            return self._start_auth_flow(session, auth_flow)

        booking_project = selected_booking_project(text)
        if booking_project:
            reply = (
                f"Great, you selected {booking_project}. To fill the plot booking application, "
                "please log in as a customer or a channel partner."
            )
            self._persist_turn(session_id, "user", text)
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply, "buttons": list(BOOKING_LOGIN_BUTTONS)}

        if wants_browse_plots(text):
            # "Browse & Book Plots" - the plots page needs an authenticated account.
            # Ask which login, remembering to route to the plots page once logged in.
            self._persist_turn(session_id, "user", text)
            self._safe_update_session_auth_state(
                session_id, "choose_login_for_plots", {"redirect": BOOK_PLOT_URL})
            reply = (
                "To browse and book plots you'll need to be logged in. How would you like "
                "to continue?"
            )
            self._persist_turn(session_id, "assistant", reply)
            return {
                "session_id": session_id, "reply": reply,
                "buttons": [
                    CUSTOMER_LOGIN_BUTTON, CHANNEL_PARTNER_LOGIN_BUTTON,
                    {"label": "I'm already logged in", "value": "plots_already_logged_in",
                     "action": "chatbot_message"},
                ],
            }

        if wants_plot_booking(text):
            reply = "Sure, which project would you like to book the plot in?"
            self._persist_turn(session_id, "user", text)
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply, "buttons": BOOKING_PROJECT_BUTTONS}

        if not session.callback_state and (wants_site_visit(text) or wants_sales_advisor(text)):
            # "Book a site visit" / "Talk to a sales advisor" - route into the callback
            # flow, which collects name/phone/time inline as part of scheduling rather
            # than as a gate. _start_callback_flow_prefilled self-heals to the full
            # name->phone->time flow when the lead has no contact on file yet.
            self._persist_turn(session_id, "user", text)
            return self._start_callback_flow_prefilled(session)

        if wants_plot_listing(text):
            # "Show available plots" - a deterministic data question. Answer straight
            # from inventory so it never depends on the LLM being up; only fall through
            # to the agent loop when inventory is genuinely empty/unavailable.
            options = self._available_plot_options()
            if options:
                self._persist_turn(session_id, "user", text)
                lines = "\n".join(
                    f"{i}. {o['project']} — ~{o['size_sqyd']} sq yd"
                    + (f" ({o['dimensions']})" if o.get("dimensions") else "")
                    + f" — {o['available']} available"
                    for i, o in enumerate(options, 1)
                )
                reply = (
                    "Here are the plot sizes currently available. Tap any size to start a "
                    "booking:\n\n" + lines +
                    "\n\nYou'll be asked to log in as a customer or channel partner before "
                    "confirming a booking."
                )
                self._persist_turn(session_id, "assistant", reply)
                return {
                    "session_id": session_id, "reply": reply,
                    # Frontend renders each plot as a clickable row -> book_url; that page
                    # checks auth and shows the login buttons if the visitor isn't signed in.
                    "structured_result": {
                        "type": "plot_list",
                        "data": {"book_url": BOOK_PLOT_URL, "plots": options},
                    },
                    "buttons": [
                        {"label": "Browse & Book Plots", "value": "browse_plots", "action": "chatbot_message"},
                        {"label": "Book a site visit", "value": "Book a site visit", "action": "chatbot_message"},
                        {"label": "Talk to a sales advisor", "value": "Talk to a sales advisor", "action": "chatbot_message"},
                        dict(MAIN_MENU_BUTTON),
                    ],
                }

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
        response = {"session_id": session_id, "reply": result["reply"], "llm_provider": result.get("llm_provider"),
                    "guardrail_passed": result.get("guardrail_passed")}
        structured_result = result.get("structured_result")
        if structured_result:
            response["structured_result"] = structured_result
        if structured_result or self._get_loan_payload(session):
            response["buttons"] = self._loan_buttons_for_response(session, structured_result)
        elif result.get("buttons"):
            # A degraded/handoff reply from _run_agent_loop carries its own working
            # buttons so the visitor is never left with just an apology and no route.
            response["buttons"] = result["buttons"]
        return response

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
            lead_row = self._persistence.update_lead_fields(
                session.lead_id, visitor_name=updated.callback_name, visitor_phone=updated.callback_phone,
            )
            try:
                if self._zoho:
                    # Push the full current lead row (not just the fields that just changed)
                    # so the Zoho upsert dedups correctly against a record created earlier
                    # from a different identifier (e.g. email captured before phone).
                    self._zoho.push_lead_async(
                        lead_id=session.lead_id,
                        visitor_name=getattr(lead_row, "visitor_name", None) or updated.callback_name,
                        visitor_phone=getattr(lead_row, "visitor_phone", None) or updated.callback_phone,
                        visitor_email=getattr(lead_row, "visitor_email", None),
                        lead_temperature=getattr(lead_row, "lead_temperature", None),
                    )
            except Exception as e:
                # Best-effort CRM sync - must never block the visitor's callback confirmation.
                logger.warning("zoho_lead_sync_failed lead_id=%s error=%s", session.lead_id, e)

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

    # ---- Menu-driven sales flow (deterministic, no LLM) ----------------------
    def _advance_menu_flow(self, session, text: str):
        session_id = session.id
        state = getattr(session, "menu_state", None)
        payload = self._menu_payload(session)

        if state == "complete":
            # Same convention as callback_state's "complete" - clear the state and hand
            # this message to the normal handler, which will persist it itself.
            self._safe_update_session_menu_state(session_id, None, None)
            return self.handle_message(session_id, text=text)

        if state == "greeting_name":
            if not text:
                # First turn of a fresh session (frontend calls /message once, even with
                # empty text, right after session init) - show the greeting, don't persist
                # an empty user turn, don't advance state yet.
                reply = (
                    "Hi! Welcome to Divine Vision Infratech. Hope you're doing well. I'd "
                    "love to assist you. Please share your full name, phone number, and "
                    "email id, so our project expert can assist you better. To start, "
                    "what's your name?"
                )
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            self._persist_turn(session_id, "user", text)
            if not text.strip():
                reply = "Please share your full name to continue."
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            name_attempts = self._as_attempt_count(payload.get("name_attempts"))
            if not looks_like_name(text) and name_attempts < 2:
                # A tapped suggestion chip (e.g. "Home loan / finance enquiry") or a
                # question landed on the name step - don't store it as the name, just
                # ask again. Bounded to 2 re-prompts so an unusual real name is never
                # a dead end: on the 3rd try we accept whatever was typed.
                payload["name_attempts"] = name_attempts + 1
                self._safe_update_session_menu_state(session_id, "greeting_name", payload)
                reply = "Sure, I can help with that. First, may I know your name?"
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            payload["name"] = text.strip()
            payload.pop("name_attempts", None)
            self._safe_update_session_menu_state(session_id, "greeting_phone", payload)
            reply = "Thanks! Please share your phone number."
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}

        if state == "greeting_phone":
            self._persist_turn(session_id, "user", text)
            if not valid_phone(text):
                reply = "That doesn't look like a valid phone number. Could you share a valid phone number?"
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            payload["phone"] = normalize_phone(text)
            self._safe_update_session_menu_state(session_id, "greeting_email", payload)
            reply = "Great! And your email address?"
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}

        if state == "greeting_email":
            self._persist_turn(session_id, "user", text)
            email = text.strip() if valid_email(text) else extract_email(text)
            if not email:
                reply = "Please share a valid email address so our project expert can reach you."
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            try:
                self._persistence.update_lead_fields(
                    session.lead_id, visitor_name=payload.get("name"), visitor_phone=payload.get("phone"),
                )
            except Exception as e:
                logger.warning("menu_greeting_lead_update_failed lead_id=%s error=%s", session.lead_id, e)
            if not self._save_lead_email(session.lead_id, email):
                reply = "Sorry, I'm having trouble saving your details right now. Could you share your email again in a moment?"
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            self._safe_update_session_menu_state(session_id, "main_menu", {})
            reply, buttons = MENU_QUESTIONS["main_menu"]
            reply = "Thanks! " + reply
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply, "buttons": buttons}

        if state == "main_menu":
            self._persist_turn(session_id, "user", text)
            matched = _match_menu_button(text, MAIN_MENU_BUTTONS)
            if matched == "menu_about":
                self._safe_update_session_menu_state(session_id, None, None)
                reply = "Sure! Ask me anything about Divine Vision Infratech - our projects, RERA approvals, or anything else."
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            if matched == "menu_sales":
                self._safe_update_session_menu_state(session_id, "sales_track", {})
                reply, buttons = MENU_QUESTIONS["sales_track"]
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply, "buttons": buttons}
            if matched == "menu_support":
                self._safe_update_session_menu_state(session_id, "support_concern", {})
                reply = "I'm sorry to hear that. Please share your concern and our support team will assist you shortly."
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            if matched == "menu_browsing":
                self._safe_update_session_menu_state(session_id, "browsing_first_time", {})
                reply, buttons = MENU_QUESTIONS["browsing_first_time"]
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply, "buttons": buttons}
            if matched == "menu_loan":
                self._safe_update_session_menu_state(session_id, None, None)
                return self._enter_loan_assistant(session)
            reply, buttons = MENU_QUESTIONS["main_menu"]
            reply = "Sorry, please choose one of the options below.\n" + reply
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply, "buttons": buttons}

        if state == "support_concern":
            self._persist_turn(session_id, "user", text)
            if not text.strip():
                reply = "Please share your concern so our support team can help."
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            payload["concern"] = text.strip()
            try:
                lead = self._persistence.get_lead_by_id(session.lead_id)
                self._persistence.create_callback_request(
                    id=str(uuid.uuid4()), lead_id=session.lead_id,
                    visitor_name=getattr(lead, "visitor_name", None) or "Visitor",
                    phone=getattr(lead, "visitor_phone", None) or "",
                    preferred_time="As soon as possible", notes=payload["concern"], request_type="support",
                )
            except Exception as e:
                logger.warning("menu_support_ticket_failed lead_id=%s error=%s", session.lead_id, e)
                reply = "Sorry, I'm having trouble saving this right now. Could you try again in a moment?"
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            self._safe_update_session_menu_state(session_id, "support_updates_optin", payload)
            reply, buttons = MENU_QUESTIONS["support_updates_optin"]
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply, "buttons": buttons}

        if state == "support_updates_optin":
            self._persist_turn(session_id, "user", text)
            opt_in = None
            if is_affirmative(text) or _match_menu_button(text, YES_NO_BUTTONS) == "yes":
                opt_in = True
            elif is_negative(text) or _match_menu_button(text, YES_NO_BUTTONS) == "no":
                opt_in = False
            if opt_in is None:
                reply, buttons = MENU_QUESTIONS["support_updates_optin"]
                reply = "Please reply yes or no - " + reply
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply, "buttons": buttons}
            try:
                self._persistence.update_lead_notify_updates(session.lead_id, opt_in)
            except Exception as e:
                logger.warning("menu_notify_updates_failed lead_id=%s error=%s", session.lead_id, e)
            self._sync_lead_to_zoho(session.lead_id)
            self._safe_update_session_menu_state(session_id, "complete", {})
            reply = "Thank you! Our team will reach out to you soon. Is there anything else I can help with?"
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}

        if state == "browsing_decision":
            self._persist_turn(session_id, "user", text)
            matched = _match_menu_button(text, DECISION_BUTTONS)
            if matched == "decision_call_back":
                return self._start_callback_flow_prefilled(session)
            if matched in ("decision_proceed", "decision_whatsapp"):
                payload["proceed_preference"] = "whatsapp" if matched == "decision_whatsapp" else "proceed"
                self._save_menu_qualification(session, payload, source_flow="menu_browsing")
                self._sync_lead_to_zoho(session.lead_id)
                self._safe_update_session_menu_state(session_id, "complete", {})
                if matched == "decision_whatsapp":
                    reply = "Great, our team will reach out to you on WhatsApp shortly!"
                else:
                    reply = "Thank you! Our project expert will reach out to you shortly."
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            reply, buttons = MENU_QUESTIONS["browsing_decision"]
            reply = "Sorry, please choose one of the options below.\n" + reply
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply, "buttons": buttons}

        if state == "sales_proceed":
            self._persist_turn(session_id, "user", text)
            matched = _match_menu_button(text, PROCEED_BUTTONS)
            if matched == "proceed_talk_now":
                payload["proceed_preference"] = matched
                self._save_menu_qualification(session, payload, source_flow="menu_sales")
                self._sync_lead_to_zoho(session.lead_id)
                return self._start_callback_flow_prefilled(session)
            if matched in ("proceed_register", "proceed_site_visit", "proceed_updates"):
                payload["proceed_preference"] = matched
                self._save_menu_qualification(session, payload, source_flow="menu_sales")
                self._sync_lead_to_zoho(session.lead_id)
                self._safe_update_session_menu_state(session_id, "complete", {})
                replies = {
                    "proceed_register": "Thanks! We've registered your interest - our team will be in touch soon.",
                    "proceed_site_visit": "Great! Our team will contact you to schedule a site visit.",
                    "proceed_updates": "You're all set! We'll keep you updated with the best matching opportunities.",
                }
                reply = replies[matched]
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            reply, buttons = MENU_QUESTIONS["sales_proceed"]
            reply = "Sorry, please choose one of the options below.\n" + reply
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply, "buttons": buttons}

        if state in MENU_LINEAR_TRANSITIONS:
            self._persist_turn(session_id, "user", text)
            return self._advance_linear_menu_step(session, state, text, payload)

        # Unknown/corrupt state - reset and fall back, mirroring _auth_error's approach.
        logger.warning("menu_state_unrecognized session_id=%s state=%s", session_id, state)
        self._safe_update_session_menu_state(session_id, None, None)
        reply = DEGRADED_FALLBACK_REPLY
        self._persist_turn(session_id, "assistant", reply)
        return {"session_id": session_id, "reply": reply}

    def _advance_linear_menu_step(self, session, state: str, text: str, payload: dict):
        session_id = session.id
        _, buttons = MENU_QUESTIONS[state]
        matched = _match_menu_button(text, buttons)
        if not matched:
            reply, buttons = MENU_QUESTIONS[state]
            reply = "Sorry, please choose one of the options below.\n" + reply
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply, "buttons": buttons}

        field, next_state = MENU_LINEAR_TRANSITIONS[state]
        payload[field] = matched
        self._safe_update_session_menu_state(session_id, next_state, payload)
        reply, next_buttons = MENU_QUESTIONS[next_state]
        self._persist_turn(session_id, "assistant", reply)
        return {"session_id": session_id, "reply": reply, "buttons": next_buttons}

    def _start_callback_flow_prefilled(self, session):
        # Used when a visitor picks "Call me back"/"Talk to someone now" mid-menu - we
        # already have their name/phone from the mandatory greeting capture, so skip
        # straight to asking preferred time instead of re-asking name/phone.
        session_id = session.id
        lead = None
        try:
            lead = self._persistence.get_lead_by_id(session.lead_id)
        except Exception as e:
            logger.warning("menu_callback_lead_lookup_failed lead_id=%s error=%s", session.lead_id, e)
        name = getattr(lead, "visitor_name", None) if lead else None
        phone = getattr(lead, "visitor_phone", None) if lead else None
        self._safe_update_session_menu_state(session_id, None, None)
        if not (name and phone):
            # Missing one somehow - fall back to the full callback flow rather than block.
            if not self._safe_update_session_callback_state(session_id, "awaiting_name"):
                reply = "Sorry, I'm having trouble starting this right now. Please try again in a moment."
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply}
            reply = "Sure! May I know your name?"
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}
        try:
            self._persistence.update_session_callback_state(
                session_id, "awaiting_time", callback_name=name, callback_phone=phone,
            )
        except Exception as e:
            logger.warning("menu_callback_prefill_failed session_id=%s error=%s", session_id, e)
            reply = "Sorry, I'm having trouble starting this right now. Please try again in a moment."
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}
        reply = "Sure! What time works best for you? Morning, afternoon, or evening?"
        self._persist_turn(session_id, "assistant", reply)
        return {"session_id": session_id, "reply": reply}

    def _save_menu_qualification(self, session, payload: dict, source_flow: str):
        try:
            buyer_type_raw = payload.get("buyer_type")
            self._persistence.create_menu_qualification(
                id=str(uuid.uuid4()), lead_id=session.lead_id,
                buyer_type=BUYER_TYPE_VALUE_MAP.get(buyer_type_raw, buyer_type_raw),
                working_profile_type=payload.get("working_profile_type"),
                location_preference=payload.get("location_preference"),
                opportunity_type=payload.get("opportunity_type"),
                investment_size_band=payload.get("investment_size_band"),
                investment_goal=payload.get("investment_goal"),
                proceed_preference=payload.get("proceed_preference"),
                source_flow=source_flow,
            )
        except Exception as e:
            # Best-effort qualification snapshot - must never block lead capture/thank-you.
            logger.warning("menu_qualification_save_failed lead_id=%s error=%s", session.lead_id, e)

    def _sync_lead_to_zoho(self, lead_id: str):
        try:
            if not self._zoho:
                return
            lead = self._persistence.get_lead_by_id(lead_id)
            if not lead:
                return
            self._zoho.push_lead_async(
                lead_id=lead_id,
                visitor_name=getattr(lead, "visitor_name", None),
                visitor_phone=getattr(lead, "visitor_phone", None),
                visitor_email=getattr(lead, "visitor_email", None),
                lead_temperature=getattr(lead, "lead_temperature", None),
            )
        except Exception as e:
            logger.warning("zoho_lead_sync_failed lead_id=%s error=%s", lead_id, e)

    def _menu_payload(self, session) -> dict:
        raw = getattr(session, "menu_payload", None)
        if isinstance(raw, dict):
            return dict(raw)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {}

    def _safe_update_session_menu_state(self, session_id: str, state, payload: dict = None):
        try:
            return self._persistence.update_session_menu_state(session_id, state, payload)
        except Exception as e:
            logger.warning("chatbot_menu_state_update_failed: %s", e)
            return None

    def _session_is_fresh(self, session_id: str) -> bool:
        # "Fresh" = no turns persisted yet, i.e. this is the widget-open ping. On any
        # lookup failure, assume fresh: greeting an empty message is the friendlier
        # default, and the guard only exists to avoid re-arming the funnel mid-chat.
        try:
            return not self._persistence.list_recent_messages(session_id, limit=1)
        except Exception as e:
            logger.warning("chatbot_session_freshness_check_failed: %s", e)
            return True

    def _already_logged_in_this_session(self, session_id: str) -> bool:
        # True once a prior turn confirmed a login. Used to recognise a lingering
        # auth_state (whose clear write didn't persist) so later messages aren't
        # treated as credential guesses.
        try:
            rows = self._persistence.list_recent_messages(session_id, limit=HISTORY_TURN_LIMIT)
        except Exception as e:
            logger.warning("chatbot_login_history_check_failed: %s", e)
            return False
        for row in rows:
            if (getattr(row, "role", None) == "assistant"
                    and str(getattr(row, "content", "")).startswith("You are logged in as ")):
                return True
        return False

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
        login_payload = {"mode": mode, "role": role}
        prior = self._auth_payload(session)
        if isinstance(prior, dict) and prior.get("redirect"):
            # carried from "Browse & Book Plots": route there after a successful login
            login_payload["redirect"] = prior["redirect"]
        if not self._safe_update_session_auth_state(session_id, f"login_{role}_email", login_payload):
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

        # Escape hatch for a stale auth_state - e.g. a login that succeeded ("You are
        # logged in as customer.") but whose state-clear write didn't stick, so the
        # visitor's next message ("giv details of plots") gets consumed as a password
        # and answered "invalid email or password". Bail out of the auth flow when the
        # message carries no credential, is more than one word, and EITHER reads as a
        # project-info / booking question OR this session has already completed a login.
        # A genuine re-login isn't hurt: its email step carries an address (a credential)
        # and its password step is normally a single token. Same recursion pattern as
        # the callback/menu "complete" states.
        if (not credentials.get("email") and not credentials.get("password")
                and len((text or "").split()) >= 2
                and (wants_project_info(text) or wants_plot_booking(text)
                     or self._already_logged_in_this_session(session_id))):
            if self._safe_update_session_auth_state(session_id, None, None) is not None:
                return self.handle_message(session_id, text=text)
            # DB clear failed - don't recurse (auth_state is still set in the DB and we'd
            # loop). Drop it in-memory and answer this turn directly.
            logger.error("chatbot_auth_state_clear_failed_on_escape session_id=%s", session_id)
            session.auth_state = None
            self._persist_turn(session_id, "user", text)
            result = self._run_agent_loop(session, text)
            self._persist_turn(
                session_id, "assistant", result["reply"], llm_provider=result.get("llm_provider"),
                guardrail_score=result.get("guardrail_score"), guardrail_passed=result.get("guardrail_passed"),
            )
            response = {"session_id": session_id, "reply": result["reply"],
                        "llm_provider": result.get("llm_provider"),
                        "guardrail_passed": result.get("guardrail_passed")}
            if result.get("structured_result"):
                response["structured_result"] = result["structured_result"]
            if result.get("buttons"):
                response["buttons"] = result["buttons"]
            return response

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
            attempts = self._as_attempt_count(payload.get("login_attempts")) + 1
            if attempts >= MAX_LOGIN_ATTEMPTS:
                self._safe_update_session_auth_state(session_id, None, None)
                reply = (
                    "I still couldn't verify those login details. No problem - you can keep "
                    "chatting, and use the Login button whenever you'd like to try again."
                )
                self._persist_turn(session_id, "assistant", reply)
                return {"session_id": session_id, "reply": reply, "buttons": [AUTH_LOGIN_BUTTONS[role]]}
            self._safe_update_session_auth_state(
                session_id, f"login_{role}_email",
                {"mode": "login", "role": role, "login_attempts": attempts},
            )
            reply = "Invalid email or password. Please enter your email address again."
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}
        except Exception as e:
            logger.warning("chatbot_auth_login_failed: %s", e)
            self._safe_update_session_auth_state(session_id, None, None)
            reply = "Sorry, I could not login right now. Please try again in a moment."
            self._persist_turn(session_id, "assistant", reply)
            return {"session_id": session_id, "reply": reply}

        # This clear MUST stick: if auth_state survives a successful login, the visitor's
        # very next message is consumed as a password and bounces them into a bogus
        # "invalid email or password" loop despite being logged in. Retry once, then log.
        if self._safe_update_session_auth_state(session_id, None, None) is None:
            if self._safe_update_session_auth_state(session_id, None, None) is None:
                logger.error("chatbot_auth_state_clear_failed_after_login session_id=%s", session_id)
        redirect = payload.get("redirect") if isinstance(payload, dict) else None
        reply = (
            f"You're logged in as {role}. Taking you to the plots page now."
            if redirect else f"You are logged in as {role}."
        )
        self._persist_turn(session_id, "assistant", reply)
        resp = {"session_id": session_id, "reply": reply, "auth_token": token, "auth_role": role}
        if redirect:
            resp["redirect_url"] = redirect
            resp["redirect_target"] = "_self"
        return resp

    def _as_attempt_count(self, raw) -> int:
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0

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
        structured_result = None

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
                if name in LOAN_STRUCTURED_RESULT_TYPES and "error" not in tool_result:
                    structured_result = {"type": LOAN_STRUCTURED_RESULT_TYPES[name], "data": tool_result}

                # The frontend gets the full tool_result (via structured_result above);
                # the model only ever sees the trimmed version.
                model_result = self._model_facing_tool_result(name, tool_result)
                if provider == "gemini":
                    history.append({"role": "model", "function_call": {
                        "name": name, "args": args, "thought_signature": step["function_call"].get("thought_signature"),
                    }})
                    history.append({"role": "tool", "function_response": {"name": name, "response": model_result}})
                else:
                    # A plain round_index-based id could collide with ids already assigned
                    # during _gemini_history_to_groq's conversion (it also numbers from 0) if
                    # the fallback happened mid-loop - a random suffix guarantees uniqueness
                    # within the conversation regardless of when the switch occurred.
                    call_id = f"call_{round_index}_{uuid.uuid4().hex[:8]}"
                    history.append({"role": "assistant", "content": None,
                                     "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]})
                    history.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": json.dumps(model_result)})
                continue

            draft_reply = step["text"] or ""
            if not draft_reply or looks_degenerate(draft_reply):
                logger.warning("degenerate_reply_detected: provider=%s", provider)
                return self._recover(session, provider, latest_text, retrieved_chunks, structured_result)
            if not used_kb:
                return self._with_structured_result({"reply": draft_reply, "llm_provider": provider}, structured_result)

            guarded = self._apply_guardrail(draft_reply, retrieved_chunks, provider)
            if guarded.get("guardrail_passed"):
                return self._with_structured_result(guarded, structured_result)
            # Guardrail rejected the draft as ungrounded - don't dead-end on the canned line.
            # Re-read the whole chat, search wider, and answer best-effort instead.
            return self._recover(session, provider, latest_text, retrieved_chunks, structured_result)

        # The tool-round budget ran out (or a provider genuinely failed) without ever landing
        # on a text reply. Rather than keep negotiating "please stop calling tools now" on the
        # same tool-call-laden conversation - a constraint this model does not reliably honor,
        # and which Groq hard-rejects outright when disobeyed - finish with one guaranteed-clean
        # call: no tools registered, no tool-call-shaped history, just the gathered facts. A
        # model with no tool schema in the request has no structural way to attempt a tool call.
        return self._recover(session, provider, latest_text, retrieved_chunks, structured_result)

    def _with_structured_result(self, result: dict, structured_result: dict) -> dict:
        # Centralizes attaching a loan-tool's structured payload (EMI/eligibility/affordability/
        # report-ready data) onto whichever return shape the caller produced - the agent loop has
        # several distinct return points (plain reply, guardrail-checked reply, final-text-only
        # fallback) and a calculation tool can be called in the same turn as a KB lookup, so every
        # one of those paths needs to carry it through rather than only the "no KB used" branch.
        if structured_result:
            result["structured_result"] = structured_result
            # The report-ready turn: never surface the model's prose (it leaks the raw
            # /loan/report path). Replace it with a fixed clean line; the absolute
            # download_url + auto_download flag travel in structured_result.data.
            if (structured_result.get("type") == "report_ready"
                    and (structured_result.get("data") or {}).get("download_url")):
                result["reply"] = REPORT_READY_REPLY
        return result

    def _recover(self, session, provider: str, latest_text: str, retrieved_chunks: list,
                  structured_result: dict = None):
        """Last line of defence - the normal loop failed to land a confident answer (tool
        budget spent, provider error, garbled text, or a draft the guardrail rejected). Never
        dead-end here: re-read the whole conversation, widen the data search across the KB and
        inventory, and get the model to synthesise its best possible reply. Sent as-is if the
        guardrail now finds it grounded; otherwise sent tagged as general guidance with a
        callback offer. Only a total LLM outage falls through to the degraded line."""
        extra_chunks, inventory_notes = self._broadened_data_search(session, latest_text)
        seen = {c["content"] for c in retrieved_chunks}
        all_chunks = retrieved_chunks + [c for c in extra_chunks if c["content"] not in seen]
        transcript = self._full_transcript(session.id)

        context_parts = []
        if all_chunks:
            context_parts.append("KNOWLEDGE BASE MATCHES:\n" + "\n---\n".join(c["content"] for c in all_chunks))
        if inventory_notes:
            context_parts.append("INVENTORY / PLOT SIZES:\n" + inventory_notes)
        context_note = "\n\n".join(context_parts) or "(no matching records found in the knowledge base or inventory)"

        prompt = (
            f"{RECOVERY_DIRECTIVE}\n\n"
            f"CONVERSATION SO FAR:\n{transcript or '(no earlier turns)'}\n\n"
            f"DATA RETRIEVED FROM OUR SYSTEMS:\n{context_note}\n\n"
            f"The visitor's latest message: \"{latest_text}\"\n\n"
            "Reply now, directly, in the visitor's own language/register, in plain text."
        )

        # Try the given provider first, then the other - a degenerate/garbled reply is treated
        # the same as a provider failure: never send it, just try the next option.
        for attempt_provider in ([provider, "groq"] if provider == "gemini" else ["groq", "gemini"]):
            try:
                if attempt_provider == "gemini":
                    step = self._gemini.generate(SYSTEM_INSTRUCTION, [{"role": "user", "text": prompt}])
                else:
                    step = self._groq.generate(SYSTEM_INSTRUCTION, [{"role": "user", "content": prompt}])
            except (GeminiError, GroqError):
                continue
            draft_reply = step["text"] or ""
            if not draft_reply or looks_degenerate(draft_reply):
                logger.warning("degenerate_reply_detected: provider=%s (recovery)", attempt_provider)
                continue

            grounded = False
            if all_chunks:
                try:
                    grounded = self._groq.judge(draft_reply, [c["content"] for c in all_chunks]) >= GUARDRAIL_THRESHOLD
                except Exception as e:
                    if not isinstance(e, GroqError):
                        logger.warning("recovery_guardrail_check_failed: %s", e)
                    grounded = False
            if not grounded:
                draft_reply = self._tag_general_guidance(draft_reply)
            return self._with_structured_result(
                {"reply": draft_reply, "llm_provider": attempt_provider, "guardrail_passed": grounded},
                structured_result,
            )

        # Both providers are down. If the visitor is mid loan-calculation and we
        # already hold enough numbers, do the arithmetic ourselves rather than
        # dead-ending on a real-estate math question the LLM was only orchestrating.
        loan_fallback = self._loan_math_fallback(session)
        if loan_fallback:
            return self._with_structured_result(
                {"reply": loan_fallback["reply"], "llm_provider": None, "guardrail_passed": False},
                structured_result or loan_fallback.get("structured_result"),
            )

        # We already pulled real inventory / KB data above - serve it directly rather
        # than the "having a little trouble" line for a question the data can answer
        # (e.g. "Show available plots"). No LLM needed to read a list back.
        data_reply = self._deterministic_data_reply(inventory_notes, all_chunks, latest_text)
        if data_reply:
            return self._with_structured_result(
                {"reply": data_reply, "llm_provider": None, "guardrail_passed": False}, structured_result,
            )

        return self._with_structured_result(
            {"reply": DEGRADED_FALLBACK_REPLY, "llm_provider": None, "guardrail_passed": False,
             "buttons": list(DEGRADED_FALLBACK_BUTTONS)}, structured_result,
        )

    def _deterministic_data_reply(self, inventory_notes: str, all_chunks: list, latest_text: str):
        """A plain, no-LLM answer built straight from the retrieved data, for when both
        providers are unreachable. Returns a string or None. Never raises."""
        try:
            if inventory_notes and inventory_notes.strip():
                return (
                    "Here's what's currently on offer from our inventory:\n\n"
                    f"{inventory_notes.strip()}\n\n"
                    "Our team can share exact pricing, layouts and the latest availability. "
                    "Would you like a callback or a site visit?"
                )
            if all_chunks:
                top = " ".join(c.get("content", "") for c in all_chunks[:2]).strip()
                if top:
                    return (
                        f"{top[:900].strip()}\n\n"
                        "This is from our project records - our team can confirm the current "
                        "details. Would you like a callback?"
                    )
        except Exception as e:
            logger.warning("deterministic_data_reply_failed: %s", e)
        return None

    def _loan_math_fallback(self, session):
        """Deterministic EMI computed without the LLM, for the recovery path only.
        Returns {"reply", "structured_result"} when principal + tenure are known,
        else None. Never raises."""
        try:
            payload = self._get_loan_payload(session)
            if not isinstance(payload, dict):
                return None
            last = payload.get("last_calculation") or {}
            principal = normalize_indian_amount(
                payload.get("requested_loan") or (last.get("emi") or {}).get("principal")
            )
            try:
                tenure = float(payload["tenure_years"]) if payload.get("tenure_years") is not None else None
            except (TypeError, ValueError):
                tenure = None
            if not principal or principal <= 0 or not tenure:
                return None
            try:
                rate = float(payload["interest_rate"]) if payload.get("interest_rate") else None
            except (TypeError, ValueError):
                rate = None
            illustrative = rate is None
            rate = rate or ILLUSTRATIVE_RATE_DEFAULT
            result = calculate_emi(principal, rate, tenure)
            # Match _tool_calculate_emi: downstream (build_report_data, the EMI card)
            # keys off this to show the "illustrative rate" disclaimer.
            result["illustrative_rate_used"] = illustrative
        except Exception as e:
            logger.warning("loan_math_fallback_failed: %s", e)
            return None
        try:
            self._merge_loan_payload(session, {"last_calculation": {**(payload.get("last_calculation") or {}), "emi": result}})
        except Exception as e:
            logger.warning("loan_math_fallback_persist_failed: %s", e)
        rate_note = f"an illustrative {rate}% p.a. rate" if illustrative else f"a {rate}% p.a. rate"
        reply = (
            f"Estimated EMI for a Rs. {principal:,.0f} loan over {tenure:g} years at {rate_note}: "
            f"about Rs. {result['emi']:,.0f} per month (total interest ~Rs. {result['total_interest']:,.0f}, "
            f"total repayment ~Rs. {result['total_payment']:,.0f}). "
            "This is an indicative calculation and actual lender terms may differ. "
            "Would you like our team to call you with exact, up-to-date figures?"
        )
        return {"reply": reply, "structured_result": {"type": "emi_result", "data": result}}

    def _tag_general_guidance(self, reply: str) -> str:
        # A recovery answer that isn't backed by retrieved data still goes out, but flagged as
        # general and pointed at the team - unless the model already hedged / offered a next step.
        lowered = reply.lower()
        if any(marker in lowered for marker in ("callback", "call you back", "site visit", "our team")):
            return reply
        return reply.rstrip() + INDICATIVE_GENERAL_SUFFIX

    def _full_transcript(self, session_id: str) -> str:
        try:
            rows = self._persistence.list_recent_messages(session_id, limit=RECOVERY_HISTORY_TURN_LIMIT)
        except Exception as e:
            logger.warning("recovery_transcript_load_failed: %s", e)
            return ""
        lines = []
        for row in rows:
            if row.content and row.role in ("user", "assistant"):
                who = "Visitor" if row.role == "user" else "Assistant"
                lines.append(f"{who}: {row.content}")
        return "\n".join(lines)

    def _broadened_data_search(self, session, latest_text: str):
        """Wider retrieval for the recovery path: more KB chunks at a lower similarity floor,
        plus a compact dump of inventory plot-size data. Returns (chunks, inventory_notes_text)."""
        chunks = []
        query = (latest_text or "").strip()
        if query:
            try:
                embedding = self._gemini.embed(query)
                rows = self._persistence.search_kb_chunks(embedding, top_k=RECOVERY_KB_TOP_K)
                chunks = [
                    {"content": r.content, "similarity": float(r.similarity)}
                    for r in rows if r.similarity and r.similarity > RECOVERY_KB_MIN_SIMILARITY
                ]
            except Exception as e:
                logger.warning("recovery_kb_search_failed: %s", e)

        return chunks, self._inventory_summary_text()

    def _inventory_summary_text(self) -> str:
        """A compact "- Project: plot ~X sq.yd, W x L m (N of M available)" dump of the
        distinct plot sizes on offer. "" when inventory is unavailable/empty. Never raises."""
        if not self._inventory:
            return ""
        notes = []
        try:
            for r in self._inventory.distinct_plot_sizes():
                area = _as_number(getattr(r, "area_sqyd", None))
                width = _as_number(getattr(r, "width_mtr", None))
                length = _as_number(getattr(r, "length_mtr", None))
                dims = f", {width} x {length} m" if width and length else ""
                notes.append(
                    f"- {getattr(r, 'project_name', None) or 'Project'}: "
                    f"{getattr(r, 'unit_type', None) or 'plot'} ~{area} sq.yd{dims} "
                    f"({int(getattr(r, 'available_count', 0) or 0)} of "
                    f"{int(getattr(r, 'unit_count', 0) or 0)} available)"
                )
        except Exception as e:
            logger.warning("inventory_summary_lookup_failed: %s", e)
            return ""
        return "\n".join(notes)

    def _available_plot_options(self) -> list:
        """Clean, size-bucketed plot list for the "Show available plots" reply. Merges
        the raw distinct_plot_sizes rows (which split on exact area+dimension) into one
        entry per rounded sq-yd, summing availability. Each entry carries a book_url
        that deep-links the frontend booking page. Never raises; [] on failure/empty."""
        if not self._inventory:
            return []
        buckets = {}
        try:
            for r in self._inventory.distinct_plot_sizes():
                area = _as_number(getattr(r, "area_sqyd", None))
                if not area or area <= 0:
                    continue
                project = (getattr(r, "project_name", None) or "").strip() or "Divine Vision Infra"
                key = (project, round(float(area)))
                avail = int(getattr(r, "available_count", 0) or 0)
                total = int(getattr(r, "unit_count", 0) or 0)
                w = _as_number(getattr(r, "width_mtr", None))
                length = _as_number(getattr(r, "length_mtr", None))
                b = buckets.get(key)
                if b is None:
                    buckets[key] = {
                        "project": project, "size_sqyd": round(float(area)),
                        "dimensions": (f"{w} x {length} m" if w and length else None),
                        "available": avail, "total": total,
                    }
                else:
                    b["available"] += avail
                    b["total"] += total
                    if not b["dimensions"] and w and length:
                        b["dimensions"] = f"{w} x {length} m"
        except Exception as e:
            logger.warning("available_plot_options_lookup_failed: %s", e)
            return []
        options = [b for b in buckets.values() if b["available"] > 0] or list(buckets.values())
        options.sort(key=lambda o: o["size_sqyd"])
        for o in options:
            o["book_url"] = (
                f"{BOOK_PLOT_URL}?project={quote_plus(o['project'])}&size={o['size_sqyd']}"
            )
        return options

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
            if name == "get_plot_size_options":
                return self._tool_get_plot_size_options(args)
            if name == "update_loan_profile":
                return self._tool_update_loan_profile(session, args)
            if name == "calculate_emi":
                return self._tool_calculate_emi(session, args)
            if name == "calculate_loan_eligibility":
                return self._tool_calculate_loan_eligibility(session, args)
            if name == "calculate_affordability":
                return self._tool_calculate_affordability(session, args)
            if name == "compare_tenures":
                return self._tool_compare_tenures(session, args)
            if name == "get_document_checklist":
                return self._tool_get_document_checklist(args.get("employment_type"))
            if name == "generate_eligibility_report":
                return self._tool_generate_eligibility_report(session)
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

    def _tool_get_plot_size_options(self, args: dict) -> dict:
        if not self._inventory:
            return {"error": "inventory_unavailable"}
        project_name = (args.get("project_name") or "").strip() or None
        try:
            rows = self._inventory.distinct_plot_sizes(project_name=project_name)
        except Exception as e:
            logger.warning("chatbot_plot_sizes_lookup_failed: %s", e)
            return {"error": "tool_failed"}

        projects = {}
        distinct_keys = set()
        for r in rows:
            pname = getattr(r, "project_name", None) or "Unknown project"
            entry = projects.setdefault(pname, {
                "project_name": pname, "city": getattr(r, "city", None), "sizes": [],
            })
            area_sqyd = _as_number(getattr(r, "area_sqyd", None))
            width = _as_number(getattr(r, "width_mtr", None))
            length = _as_number(getattr(r, "length_mtr", None))
            dimensions = f"{width} x {length} m" if width and length else None
            entry["sizes"].append({
                "unit_type": getattr(r, "unit_type", None),
                "area_sq_yd": area_sqyd,
                "area_sq_mtr": _as_number(getattr(r, "area_sqmt", None)),
                "dimensions_mtr": dimensions,
                "total_units": int(getattr(r, "unit_count", 0) or 0),
                "available_units": int(getattr(r, "available_count", 0) or 0),
            })
            distinct_keys.add((pname, area_sqyd, dimensions))

        result = {
            "distinct_size_count": len(distinct_keys),
            "projects": list(projects.values()),
        }
        if not distinct_keys:
            result["note"] = (
                "No plot-size data is loaded in inventory yet - offer a callback so the team "
                "can share exact sizes."
            )
        return result

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

    # ---- Home Loan Assistant --------------------------------------------
    def _enter_loan_assistant(self, session):
        session_id = session.id
        self._persist_turn(session_id, "user", "[Home Loan / EMI Help]")
        result = self._run_agent_loop(session, LOAN_ENTRY_SEED_TEXT)
        self._persist_turn(
            session_id, "assistant", result["reply"], llm_provider=result.get("llm_provider"),
        )
        response = {"session_id": session_id, "reply": result["reply"], "llm_provider": result.get("llm_provider")}
        if result.get("structured_result"):
            response["structured_result"] = result["structured_result"]
        # On a degraded entry (LLM down) offer the deterministic routes; otherwise the
        # normal loan action buttons.
        response["buttons"] = (
            list(DEGRADED_FALLBACK_BUTTONS) + [dict(MAIN_MENU_BUTTON)]
            if result.get("reply") == DEGRADED_FALLBACK_REPLY
            else self._loan_buttons_for_response(session, result.get("structured_result"))
        )
        return response

    def _get_loan_payload(self, session) -> dict:
        raw = getattr(session, "loan_payload", None)
        if isinstance(raw, dict):
            return dict(raw)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        # Stored JSON that isn't an object (a list / string / number) would break
        # every `.get(...)` downstream - normalise to an empty profile instead.
        return parsed if isinstance(parsed, dict) else {}

    def _merge_loan_payload(self, session, updates: dict) -> dict:
        payload = self._get_loan_payload(session)
        payload.update({k: v for k, v in updates.items() if v is not None})
        try:
            self._persistence.update_session_loan_state(session.id, payload)
        except Exception as e:
            logger.warning("chatbot_loan_state_update_failed: %s", e)
        session.loan_payload = payload
        return payload

    def _loan_dynamic_buttons(self, session) -> list:
        payload = self._get_loan_payload(session)
        if payload.get("last_calculation"):
            buttons = [
                {"label": "Download Report", "value": "loan_download_report", "action": "chatbot_message"},
                {"label": "Change Loan Amount", "value": "loan_change_amount", "action": "chatbot_message"},
                {"label": "Change Tenure", "value": "loan_change_tenure", "action": "chatbot_message"},
                {"label": "Check Another Property", "value": "loan_new_property", "action": "chatbot_message"},
            ]
            if not payload.get("co_applicant_income"):
                buttons.insert(3, {"label": "Add Co-Applicant", "value": "loan_add_co_applicant", "action": "chatbot_message"})
            buttons.append(dict(MAIN_MENU_BUTTON))
            return buttons
        return list(LOAN_INITIAL_BUTTONS) + [dict(MAIN_MENU_BUTTON)]

    def _loan_buttons_for_response(self, session, structured_result: dict = None) -> list:
        buttons = self._loan_dynamic_buttons(session)
        if structured_result and structured_result.get("type") == "report_ready":
            data = structured_result.get("data") or {}
            download_url = data.get("download_url")
            if download_url:
                buttons = [b for b in buttons if b.get("value") != "loan_download_report"]
                buttons.insert(0, {"label": "Download My Home Loan Eligibility Report",
                                    "value": "loan_report_ready", "action": "download_link",
                                    "url": download_url, "filename": data.get("filename")})
        return buttons

    def _tool_update_loan_profile(self, session, args: dict) -> dict:
        money_fields = (
            "monthly_income", "co_applicant_income", "existing_emi",
            "property_price", "down_payment", "requested_loan",
        )
        updates = {}
        for field in money_fields:
            if field in args and args[field] not in (None, ""):
                normalized = normalize_indian_amount(args[field])
                if normalized is not None:
                    updates[field] = normalized
        for field in ("age", "interest_rate", "tenure_years"):
            if field in args and args[field] is not None:
                updates[field] = args[field]
        if args.get("employment_type"):
            employment = args["employment_type"].strip().lower()
            updates["employment_type"] = "self_employed" if "self" in employment or "business" in employment else "salaried"
        if args.get("credit_score_band"):
            updates["credit_score_band"] = str(args["credit_score_band"]).strip()

        payload = self._merge_loan_payload(session, updates)
        return {k: v for k, v in payload.items() if k != "last_calculation"}

    def _tool_calculate_emi(self, session, args: dict) -> dict:
        payload = self._get_loan_payload(session)
        principal = args.get("principal") or payload.get("requested_loan")
        rate = args.get("annual_rate_pct") or payload.get("interest_rate")
        tenure = args.get("tenure_years") or payload.get("tenure_years")
        illustrative_rate_used = not rate
        rate = rate or ILLUSTRATIVE_RATE_DEFAULT
        tenure = tenure or 20

        if not principal:
            return {"error": "missing_fields", "fields": ["principal"]}
        try:
            result = calculate_emi(principal, rate, tenure)
        except ValueError as e:
            return {"error": "invalid_input", "reason": str(e)}
        result["illustrative_rate_used"] = illustrative_rate_used
        self._merge_loan_payload(session, {"last_calculation": {**payload.get("last_calculation", {}), "emi": result}})
        return result

    def _tool_calculate_loan_eligibility(self, session, args: dict) -> dict:
        payload = self._get_loan_payload(session)
        monthly_income = payload.get("monthly_income")
        missing = []
        if not monthly_income:
            missing.append("monthly_income")
        # Existing EMIs / loan obligations directly reduce eligible capacity - insist
        # the visitor is asked (a "none" answer must be saved as existing_emi: 0, which
        # then makes the key present so this check passes).
        if "existing_emi" not in payload:
            missing.append("existing_emi")
        if missing:
            return {"error": "missing_fields", "fields": missing}
        try:
            result = calculate_loan_eligibility(
                monthly_income=monthly_income, co_applicant_income=payload.get("co_applicant_income"),
                existing_emi=payload.get("existing_emi"), requested_loan=payload.get("requested_loan"),
                tenure_years=payload.get("tenure_years") or 20, interest_rate=payload.get("interest_rate"),
                credit_score_band=payload.get("credit_score_band"), age=payload.get("age"),
            )
        except ValueError as e:
            return {"error": "invalid_input", "reason": str(e)}
        self._merge_loan_payload(session, {"last_calculation": {**payload.get("last_calculation", {}), "eligibility": result}})
        return result

    def _tool_calculate_affordability(self, session, args: dict) -> dict:
        payload = self._get_loan_payload(session)
        property_price = normalize_indian_amount(args.get("property_price")) or payload.get("property_price")
        monthly_income = payload.get("monthly_income")
        if not property_price or not monthly_income:
            missing = [f for f, v in (("property_price", property_price), ("monthly_income", monthly_income)) if not v]
            return {"error": "missing_fields", "fields": missing}
        try:
            result = calculate_affordability(
                property_price=property_price, down_payment=payload.get("down_payment"),
                monthly_income=monthly_income, co_applicant_income=payload.get("co_applicant_income"),
                existing_emi=payload.get("existing_emi"), tenure_years=payload.get("tenure_years") or 20,
                interest_rate=payload.get("interest_rate"),
            )
        except ValueError as e:
            return {"error": "invalid_input", "reason": str(e)}
        updates = {"property_price": property_price, "last_calculation": {**payload.get("last_calculation", {}), "affordability": result}}
        self._merge_loan_payload(session, updates)
        return result

    def _tool_compare_tenures(self, session, args: dict) -> dict:
        payload = self._get_loan_payload(session)
        principal = payload.get("requested_loan")
        rate = payload.get("interest_rate") or ILLUSTRATIVE_RATE_DEFAULT
        tenure_options = args.get("tenure_options_years") or [15, 20, 25]
        if not principal:
            return {"error": "missing_fields", "fields": ["requested_loan"]}
        try:
            rows = _compare_tenures(principal, rate, tenure_options)
        except ValueError as e:
            return {"error": "invalid_input", "reason": str(e)}
        result = {"principal": principal, "annual_rate_pct": rate, "illustrative_rate_used": not payload.get("interest_rate"), "rows": rows}
        self._merge_loan_payload(session, {"last_calculation": {**payload.get("last_calculation", {}), "tenure_comparison": result}})
        return result

    def _tool_get_document_checklist(self, employment_type: str) -> dict:
        return get_document_checklist(employment_type)

    def _tool_generate_eligibility_report(self, session) -> dict:
        payload = self._get_loan_payload(session)
        if not payload.get("last_calculation"):
            return {"error": "no_calculation_yet"}
        snapshot = {
            "profile": {k: v for k, v in payload.items() if k != "last_calculation"},
            "last_calculation": payload.get("last_calculation"),
            # Stored in the snapshot so the report row is self-contained - the JSON
            # endpoint and PDF generator don't need to re-join to the lead later.
            "applicant": self._report_applicant(session),
        }
        try:
            row = self._loan_persistence.create_loan_report(
                id=str(uuid.uuid4()), snapshot=snapshot, session_id=session.id, lead_id=session.lead_id,
            )
        except Exception as e:
            logger.warning("loan_report_create_failed: %s", e)
            return {"error": "report_generation_failed"}
        # This dict is the FRONTEND-facing structured_result['data'] (URLs + the full
        # flattened report incl. the visitor's own contact details). It is NOT what
        # the LLM sees - _model_facing_tool_result strips it down before the tool
        # response is appended to the conversation history.
        return {
            "report_id": row.id,
            "download_url": report_download_url(row.id),
            "download_path": f"/loan/report/{row.id}/download",
            "data_url": report_data_url(row.id, session.id),
            "filename": report_pdf_filename(row.id),
            "auto_download": True,
            "report": build_report_data(snapshot, row.id, getattr(row, "created_date", None)),
        }

    @staticmethod
    def _model_facing_tool_result(name: str, tool_result: dict) -> dict:
        """What actually goes into the LLM conversation history for a tool call.
        For the eligibility report we hand the model only "it exists" - never the
        URLs (which it would echo into prose) or the report payload (which carries
        the visitor's name/phone/email the system prompt forbids it to use)."""
        if name == "generate_eligibility_report" and isinstance(tool_result, dict) and "error" not in tool_result:
            return {"report_id": tool_result.get("report_id"), "status": "ready"}
        return tool_result

    def _report_applicant(self, session) -> dict:
        try:
            lead = self._persistence.get_lead_by_id(session.lead_id)
        except Exception as e:
            logger.warning("report_applicant_lookup_failed: %s", e)
            lead = None
        if not lead:
            return {}
        return {
            "name": getattr(lead, "visitor_name", None),
            "phone": getattr(lead, "visitor_phone", None),
            "email": getattr(lead, "visitor_email", None),
        }

    def _save_lead_email(self, lead_id: str, email: str) -> bool:
        try:
            lead_row = self._persistence.update_lead_email(lead_id, email.strip())
        except Exception as e:
            logger.warning("chatbot_email_save_failed: %s", e)
            return False
        try:
            if self._zoho:
                # Push the full current lead row (not just the email that just changed)
                # so the Zoho upsert dedups correctly against a record created earlier
                # from a different identifier (e.g. phone captured before email).
                self._zoho.push_lead_async(
                    lead_id=lead_id,
                    visitor_name=getattr(lead_row, "visitor_name", None),
                    visitor_phone=getattr(lead_row, "visitor_phone", None),
                    visitor_email=(getattr(lead_row, "visitor_email", None) or email.strip()),
                    lead_temperature=getattr(lead_row, "lead_temperature", None),
                )
        except Exception as e:
            # Best-effort CRM sync - must never fail the visitor's email capture.
            logger.warning("zoho_lead_sync_failed lead_id=%s error=%s", lead_id, e)
        return True

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
