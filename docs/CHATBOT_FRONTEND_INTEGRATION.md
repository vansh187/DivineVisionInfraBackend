# Chatbot Frontend Integration — Menu-Driven Sales Flow

The backend now drives a structured greeting → main menu → sales/support/
browsing funnel (see `DivineService/service_chatbot.py`), instead of relying
purely on free-text LLM replies. **No API endpoints changed and no request/
response schema fields were added or removed** — this is a heads up on new
*behavior* through the existing contract, plus one required change to how the
widget opens a session.

## 1. Required change: send an empty first message right after session init

`POST /chatbot/session/init` still returns only `{session_id, lead_id}` — it
does **not** carry the greeting text. The greeting/name-prompt is delivered
through the normal message endpoint instead.

**After calling `/chatbot/session/init`, immediately call `/chatbot/message`
once with `text` empty (or omitted) and the returned `session_id`:**

```
POST /chatbot/session/init  -> { "session_id": "...", "lead_id": "..." }
POST /chatbot/message  { "session_id": "...", "text": "" }
  -> { "session_id": "...", "reply": "Hi! Welcome to Divine Vision Infratech. ... what's your name?" }
```

If the widget currently only calls `/chatbot/message` on the visitor's first
typed input, add this one extra call on session open so the greeting appears
before the visitor types anything.

## 2. New button action type: `"chatbot_menu"`

Buttons already work exactly as before — `ChatMessageResponseDTO.buttons` is
unchanged (`{label, value, action, url?}`), and existing `action` values
(`"chatbot_auth"`, `"select_booking_project"`) behave identically. There's one
new `action` value: **`"chatbot_menu"`**.

**Handle it exactly like `"chatbot_auth"` already is** — when the visitor taps
a button with `action: "chatbot_menu"`, send its `value` back as the `text` of
the next `/chatbot/message` call. No special-casing needed beyond whatever
generic "render buttons, POST the tapped one's value as text" logic already
exists for the auth buttons.

```json
{
  "session_id": "sess-123",
  "reply": "How can I help you today?",
  "buttons": [
    {"label": "About Divine Vision", "value": "menu_about", "action": "chatbot_menu"},
    {"label": "Chat with Sales", "value": "menu_sales", "action": "chatbot_menu"},
    {"label": "Chat with Support", "value": "menu_support", "action": "chatbot_menu"},
    {"label": "Only Browsing", "value": "menu_browsing", "action": "chatbot_menu"}
  ]
}
```

If the frontend also supports free-text instead of tapping a button, that
keeps working too — the backend matches typed text against the button
labels/values, not just literal postbacks.

## 3. New conversation flow (what the visitor experiences)

1. Greeting → asks for full name → phone number → email (three separate
   turns, plain text input, no buttons) — **mandatory** before the main menu
   unlocks.
2. Main menu: **About Divine Vision** / **Chat with Sales** / **Chat with
   Support** / **Only Browsing** (buttons).
3. **About Divine Vision** → drops into normal open-ended Q&A (same as
   today's free-text chat experience).
4. **Chat with Sales** → buyer-type → working-profile-type → location →
   opportunity-type → investment-size → investment-goal → "how would you like
   to proceed" (each step is one question, one button set).
5. **Only Browsing** → first-time? → location → budget → investor y/n → a
   lighter "how would you like to proceed" (fewer questions than Sales).
6. **Chat with Support** → free-text concern → yes/no "notify me of updates"
   button.
7. All paths end with a thank-you message; the next message after that drops
   back into normal free-text chat.

No new UI components should be needed — every step reuses the button
mechanism already built for the existing auth/booking flows.

## 4. Button copy is still placeholder — expect it to change

All labels/values above (and the full sets below) are backend-side
placeholders pending business sign-off on exact wording. **Don't hardcode
button `value`s into frontend logic beyond "render label, POST value, handle
action type"** — the values may still be renamed once copy is finalized, but
the *shape* (`{label, value, action}`) and the generic handling won't change.

Current placeholder button sets, for reference/testing:

| Step | Values |
|---|---|
| Main menu | `menu_about`, `menu_sales`, `menu_support`, `menu_browsing` |
| Buyer type (Sales) | `sales_end_client`, `sales_investor_dealer` |
| Working profile | `profile_individual_buyer`, `profile_individual_investor`, `profile_channel_partner`, `profile_corporate` |
| Location | `loc_ops_divine_greens`, `loc_suraksha_enclave`, `loc_other` |
| Opportunity type | `opp_residential_plot`, `opp_residential_unit`, `opp_commercial` |
| Investment size | `size_under_20l`, `size_20_50l`, `size_50l_1cr`, `size_above_1cr` |
| Investment goal | `goal_long_term`, `goal_short_term`, `goal_rental_yield`, `goal_self_use` |
| Proceed (Sales) | `proceed_register`, `proceed_site_visit`, `proceed_updates`, `proceed_talk_now` |
| First time? (Browsing) | `first_time_yes`, `first_time_no` |
| Decision (Browsing) | `decision_proceed`, `decision_call_back`, `decision_whatsapp` |
| Notify updates (Support) | `yes`, `no` |

## 5. Nothing else changes

- `POST /chatbot/message` request/response shape: unchanged.
- Voice input (`audio_b64`), location sharing (`precise_lat`/`precise_long`),
  `intent: "request_callback"`: unchanged, still work exactly as before.
- Existing auth (login/signup) and plot-booking flows: unchanged, still work
  exactly as before, and can even interrupt the new menu flow mid-conversation
  if a visitor explicitly types something like "login as customer".
