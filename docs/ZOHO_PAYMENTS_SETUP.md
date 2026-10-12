# Zoho Payments Setup (Production)

This backend takes payments through **Zoho Payments** - the payment gateway. This is a
**separate Zoho product/connected app from Zoho CRM** (see
[`ZOHO_CRM_SETUP.md`](ZOHO_CRM_SETUP.md)): different credentials, different scopes,
different dashboard. Do not reuse `ZOHO_CLIENT_KEY` / `ZOHO_SECRET_KEY` (those are the
CRM app's) for anything in this document.

See [`DivineService/service_payment_gateway.py`](../DivineService/service_payment_gateway.py)
for the client implementation and
[`DivineService/service_payment.py`](../DivineService/service_payment.py) for how it's
used (create order, verify, webhook, refunds).

---

## 1. Create a Server-based Application in the Zoho API Console

1. Go to https://api-console.zoho.in (use `.com` instead of `.in` if the Zoho Payments
   account is on the US/global data center - match whatever data center the account
   itself is on).
2. **Add Client** -> **Server-based Applications** (a Self Client has no browser
   consent/redirect flow; this integration's token refresh needs the standard OAuth
   grant/refresh-token flow instead).
3. Fill in Client Name / Homepage URL as you like. This is a fresh connected app -
   don't reuse the one registered for Zoho CRM.
4. Under **Authorized Redirect URIs**, add a redirect URI for the one-time authorization
   step (any reachable URL works, since the authorization code is only used once, by hand,
   to mint the refresh token - unlike the CRM integration, there is no ongoing
   `/admin/zoho/oauth/callback`-style route for Zoho Payments in this backend).
5. Save. Note the **Client ID** and **Client Secret** - these become
   `ZOHO_PAYMENTS_CLIENT_ID` / `ZOHO_PAYMENTS_CLIENT_SECRET`.
6. Generate a grant token / refresh token scoped to exactly:
   ```
   ZohoPay.payments.CREATE,ZohoPay.payments.READ,ZohoPay.refunds.CREATE,ZohoPay.refunds.READ
   ```
   Exchange the grant code for a refresh token the same way the Zoho OAuth docs describe
   for a Server-based Application (authorize URL -> user consent -> code -> token
   exchange). The resulting refresh token becomes `ZOHO_PAYMENTS_REFRESH_TOKEN`.

## 2. Find the account id, signing key, and webhook secret

- **`ZOHO_PAYMENTS_ACCOUNT_ID`** - Zoho Payments dashboard -> **Settings > Business
  Profile**. Every API call (session create, session retrieve, refund create/retrieve)
  is scoped to this account id as a query param.
- **`ZOHO_PAYMENTS_SIGNING_KEY`** - Zoho Payments dashboard -> **Settings > Developer
  Space > Authentication Keys**. This is a separate secret from the OAuth client
  secret - it's used only to verify the HMAC signature Zoho appends when it redirects
  the customer's browser back to `success_url`/`failure_url` after hosted checkout
  (see `verify_redirect_signature` in `service_payment_gateway.py`).
- **`ZOHO_PAYMENTS_WEBHOOK_SECRET`** - generated when you register the webhook (see
  step 3 below) in the Zoho Payments dashboard. Used to verify the
  `X-Zoho-Webhook-Signature` header on incoming webhook deliveries.

## 3. Register the webhook

In the Zoho Payments dashboard, register a webhook whose URL points at **this
backend's own** `POST /payments/webhook` endpoint - not the frontend, not a CRM
webhook. Registering it there is also what generates the webhook secret for step 2.

## 4. Set environment variables

All of these are documented in `.env.example`'s `ZOHO_PAYMENTS_*` block - keep this
list in sync with it if either changes.

| Key | What it's for |
|---|---|
| `ZOHO_PAYMENTS_CLIENT_ID` | OAuth client id of the Zoho Payments connected app (step 1) |
| `ZOHO_PAYMENTS_CLIENT_SECRET` | OAuth client secret of the same connected app |
| `ZOHO_PAYMENTS_REFRESH_TOKEN` | Refresh token minted in step 1.6, scoped to the four `ZohoPay.*` scopes |
| `ZOHO_PAYMENTS_ACCOUNT_ID` | Zoho Payments account every API call is scoped to (step 2) |
| `ZOHO_PAYMENTS_ACCOUNTS_DOMAIN` | OAuth token-refresh domain, e.g. `accounts.zoho.in` (or `accounts.zoho.com` for US/global) |
| `ZOHO_PAYMENTS_API_DOMAIN` | Payments API domain, e.g. `payments.zoho.in` (or `payments.zoho.com`) |
| `ZOHO_PAYMENTS_SIGNING_KEY` | Verifies the hosted-checkout success/failure redirect signature (step 2) |
| `ZOHO_PAYMENTS_WEBHOOK_SECRET` | Verifies the `X-Zoho-Webhook-Signature` header on `POST /payments/webhook` (step 3) |
| `ZOHO_PAYMENTS_SUCCESS_URL` | Frontend page Zoho redirects the customer's browser to after a successful payment |
| `ZOHO_PAYMENTS_FAILURE_URL` | Frontend page Zoho redirects the customer's browser to after a failed/cancelled payment |

Without these set, `/payments/create-order`, `/payments/verify`, and
`/payments/webhook` all fail closed - the backend never silently accepts an
unverified payment.

---

## Frontend contract change

This is a **hard contract change** from the old Razorpay integration, not just a
rename - the frontend team needs to implement these changes in the **same release
window** as this backend's deploy. There is no dual-gateway fallback: once this
backend is deployed, the old Razorpay Checkout.js flow simply stops working.

1. **Checkout is now a full-page redirect, not an embedded JS modal.**
   `POST /payments/create-order` returns `checkout_url` (a full
   `https://payments.zoho.in/hostedcheckout/<access_key>` URL) and `access_key`
   instead of Razorpay's `razorpay_key_id`. The frontend must **navigate the
   customer's browser to `checkout_url`** (e.g. `window.location.href =
   checkout_url`) rather than opening an embedded Razorpay Checkout.js modal.
   `amount_paise` is gone too - Zoho Payments takes a decimal amount, not paise.

2. **`POST /payments/verify`'s request body changed.** It used to be
   `{razorpay_order_id, razorpay_payment_id, razorpay_signature}`. It is now:
   ```json
   {
     "payments_session_id": "...",
     "payment_id": "...",
     "payment_status": "...",
     "amount": "...",
     "signature": "...",
     "udf1": "...", "udf2": "...", "udf3": "...", "udf4": "...", "udf5": "..."
   }
   ```
   These are the exact query-string fields Zoho's hosted checkout appends when it
   redirects the customer's browser back to `ZOHO_PAYMENTS_SUCCESS_URL` /
   `ZOHO_PAYMENTS_FAILURE_URL`. The frontend's success/failure landing page just
   needs to read its own query string and forward those fields to `/payments/verify`
   unchanged - `udf1`..`udf5` are optional and only present if the backend set them
   when creating the order.

3. **Webhook header/event names changed** (backend-to-backend, no frontend action,
   listed here for completeness): `X-Razorpay-Signature` -> `X-Zoho-Webhook-Signature`
   (format `t=<timestamp>,v=<hmac>`), and event names `payment.captured`/
   `payment.failed` -> `payment.success`/`payment.failed`.

---

## This is a hard cutover

There is no dual-gateway support and no gradual rollout path. Any Razorpay order
that is left mid-checkout at the moment of deploy will be **orphaned** - the
customer's browser will be mid-flow against a checkout page whose backend no longer
recognizes the old request shape. **Recommend deploying in a low-traffic window** to
minimize how many in-flight checkouts get stranded.

Payments made before the cutover keep working as read-only history (`method:
"razorpay"` rows still display, refund via the manual mark-collected flow), but no
new Razorpay payment can ever be created again after this deploy.
