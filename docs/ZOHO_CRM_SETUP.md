# Zoho CRM Setup (Production)

This backend mirrors chatbot leads and customer/broker signups into Zoho CRM,
best-effort, alongside Postgres (Postgres is always the source of truth - see
[`DivineService/service_zoho.py`](../DivineService/service_zoho.py)). The account
currently connected is a **dummy/test Zoho account**. Follow these steps to point
production at the real Zoho CRM account instead.

## What syncs where

Per the client, signups and chatbot activity land in **Leads**. The one exception is
a customer's FIRST confirmed plot booking - the payment settles paid AND the unit
actually flips to 'booked' - made either via Razorpay or recorded manually as
cash/RTGS/cheque. That lands in **Contacts** instead. See
[`service_payment.py`](../DivineService/service_payment.py)
`_push_booking_contact_to_zoho` (called from `_apply_booking_to_inventory`).

| Event | Zoho module | Trigger |
|---|---|---|
| Customer signup (with email or phone) | **Leads** | `POST /customer/signup` |
| Broker signup (with email or phone) | **Leads** | `POST /broker/signup` |
| Chatbot callback request | **Leads** | Visitor completes "call me back" (name + phone) |
| Chatbot email capture | **Leads** | Visitor gives an email in chat |
| Plot booking confirmed (payment settled + unit flipped to `booked`) - Razorpay or cash/RTGS/cheque | **Contacts** | `POST /payments/verify`, the Razorpay webhook, or `POST /payments/cash` |

A later instalment payment on that same already-booked plot does **not** push again -
only the original booking event lands in Contacts.

A signup/lead with no email and no phone is skipped - there's no reliable field to
dedupe on. A Zoho outage or bad config never fails or slows down the underlying
signup/chat request; it just logs a warning and moves on.

## 1. Create/choose the production Zoho CRM account

Use the real business Zoho CRM account (not the dummy one used for testing) - the
account you log into during step 4 below is the one that ends up owning every synced
Contact/Lead.

## 2. Create a Server-based Application in the Zoho API Console

1. Go to https://api-console.zoho.in (use `.com` instead of `.in` if the account is on
   the US/global data center - match whatever data center the CRM account itself is on).
2. **Add Client** -> **Server-based Applications** (NOT "Self Client" - Self Client has
   no browser consent/redirect flow, which this integration relies on).
3. Fill in Client Name / Homepage URL as you like.
4. Under **Authorized Redirect URIs**, add exactly (must match character-for-character,
   no trailing slash):
   ```
   https://divinevisioninfrabackend.onrender.com/admin/zoho/oauth/callback
   ```
   If this backend's domain changes, update this here AND in `ZOHO_REDIRECT_URI` (step 3).
5. Save. Note the **Client ID** and **Client Secret** shown - these are
   `ZOHO_CLIENT_KEY` / `ZOHO_SECRET_KEY`.

## 3. Set environment variables on Render

Render dashboard -> this service -> **Environment**. Set/update:

| Key | Value |
|---|---|
| `ZOHO_CLIENT_KEY` | Client ID from step 2 |
| `ZOHO_SECRET_KEY` | Client Secret from step 2 |
| `ZOHO_ACCOUNTS_DOMAIN` | `accounts.zoho.in` (or `accounts.zoho.com` for US/global) |
| `ZOHO_API_DOMAIN` | `www.zohoapis.in` (or `www.zohoapis.com`) - self-corrects after the first token refresh, this is just the initial guess |
| `ZOHO_REDIRECT_URI` | `https://divinevisioninfrabackend.onrender.com/admin/zoho/oauth/callback` |
| `ZOHO_OAUTH_SETUP_TOKEN` | A random secret you generate (e.g. `openssl rand -base64 24`) - gates the admin OAuth routes below. **Rotate this** if it was ever shared/pasted somewhere, since it doubles as the bearer credential for those routes. |
| `ZOHO_REFRESH_TOKEN` | Leave **empty** - step 4 fills this in automatically |

Optional, for the refresh token to auto-persist across Render restarts instead of a
manual copy-paste (see [`DivineService/service_zoho.py`](../DivineService/service_zoho.py)
`_persist_refresh_token_to_render`):

| Key | Value |
|---|---|
| `RENDER_API_KEY` | Render dashboard -> Account Settings -> API Keys. **Note:** Render API keys are account-wide, not scoped to one service - this grants the running app (and anyone holding the key) access to every service in the Render account. Only set this if that tradeoff is acceptable. |
| `RENDER_SERVICE_ID` | This service's ID, visible in its Render dashboard URL/Settings (`srv-...`) |

Save and let Render redeploy with the new values.

## 4. Run the one-time OAuth bootstrap

This is the one step that must be done by a human in a browser - Zoho requires actual
user login + consent, which can't be scripted or automated away.

1. In a browser, visit:
   ```
   https://divinevisioninfrabackend.onrender.com/admin/zoho/oauth/start?key=<ZOHO_OAUTH_SETUP_TOKEN>
   ```
2. Log into the **production** Zoho account from step 1 (not the dummy one) and click
   **Approve**.
3. Zoho redirects back to `/admin/zoho/oauth/callback`, which exchanges the grant code
   for a refresh token and activates it immediately - no restart needed. The page shown
   tells you whether it was also auto-persisted to Render (if `RENDER_API_KEY`/
   `RENDER_SERVICE_ID` are set) or needs a manual copy-paste into `ZOHO_REFRESH_TOKEN`.

If you see **"Invalid Redirect Uri"**: the URI in step 2.4 doesn't exactly match
`ZOHO_REDIRECT_URI` - check for a trailing slash, http vs https, or a typo.

## 5. Verify

All three routes are gated by the same `ZOHO_OAUTH_SETUP_TOKEN`:

```bash
# Confirms the refresh token is actually set (no secret values exposed)
curl "https://divinevisioninfrabackend.onrender.com/admin/zoho/status?key=<TOKEN>"

# Writes one real dummy Lead (name "Zoho Integration Test") to confirm the full
# pipeline - token refresh, API domain resolution, upsert - works end-to-end
curl -X POST "https://divinevisioninfrabackend.onrender.com/admin/zoho/test-push?key=<TOKEN>"

# Checks whether a specific email exists in Zoho right now (queries Zoho directly,
# not this app's own DB) - useful for confirming/debugging a real signup or lead
curl "https://divinevisioninfrabackend.onrender.com/admin/zoho/lookup?key=<TOKEN>&email=someone@example.com&module=Leads"
# everything (signups + chatbot) syncs to Leads now; module=Contacts still works for
# looking up any legacy records written before that change of signups
```

Then do a real signup or chatbot callback/email capture on the live site and confirm
it shows up in Zoho CRM -> Leads under the production account - not the dummy one.

## Switching away from the dummy account later

Repeat steps 2-4 with the new/production Zoho account: create a new connected app (or
reuse the existing one and just re-run step 4 logged in as the production account -
either works, since step 4 always overwrites `ZOHO_REFRESH_TOKEN` with whichever
account approved last). There's no code change needed either way.
