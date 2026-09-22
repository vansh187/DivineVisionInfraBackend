# Instalment payments, "Pay now" window & payment-due reminders — API contract

**Status:** shipped on branch `DivineInfraBackend`. DB migration + backfill applied to production.
**Builds on:** `PLOT_BOOKED_STATUS_API.md` (booking + `payment_plan`).

The frontend already renders the plan table with a per-row **Pay now** button and a
home "payment due" banner off `GET /customer/profile → booking.payment_schedule`.
This adds the backend it needs: accept an instalment payment, mark the milestone
paid, stamp per-row `status` / `pay_enabled_from`, run the reminder email, and
serve a receipt PDF.

---

## 1. Milestone model

On a confirmed booking the backend now materialises one **milestone** row per plan
line (`<booking_doc_id>-m<n>`, `milestone_no` 1..5). `On Booking` (m1) is `paid`
immediately (the 10% booking amount). Amounts follow the plan: `round(total × pct/100)`
with the last row absorbing rounding so the column foots exactly.

`status` (server-computed against **today**, Asia/Kolkata, +05:30):

| status | rule |
|--------|------|
| `paid` | milestone settled |
| `overdue` | not paid and `today > due_date` |
| `due` | not paid and `due_date − 20 days ≤ today ≤ due_date` |
| `upcoming` | not paid and `today < due_date − 20 days` |

`pay_enabled_from = due_date − 5 days`. Ordering is enforced: only the **earliest
unpaid** milestone is payable.

---

## 2. `POST /payments/create-order` — new `installment` purpose

Auth: customer bearer token.

### Request
```jsonc
{
  "amount": 443576,               // must equal the milestone amount (±1 for rounding)
  "purpose": "installment",       // NEW enum value
  "installment_no": 3,            // NEW — 1-based milestone position
  "due_date": "2025-04-10",       // NEW — that milestone's ISO due date (optional echo)
  "inventory_id": "b0e1f2a3-..."  // OPTIONAL — which plot this instalment is for.
                                  // Only needed when the customer holds more than
                                  // one plot; it disambiguates "milestone #N of
                                  // WHICH booking". Omitted, the server resolves
                                  // against the customer's only / most-behind plan
                                  // (unchanged single-booking behaviour). Accepted
                                  // on both /payments/create-order and /payments/cash.
}
```

### Response 200 — same purpose/installment fields, gateway fields changed for Zoho Payments
```jsonc
{
  "payment_id": "b7e2c1a0-...",
  "zoho_payments_session_id": "1000000012345",
  // Full hosted-checkout URL to redirect the customer's browser to - a
  // full-page redirect (NOT an embedded JS checkout modal like Razorpay's).
  "checkout_url": "https://payments.zoho.in/hostedcheckout/8f3a9b2c...",
  "access_key": "8f3a9b2c...",
  "amount": 443576, "currency": "INR", "status": "created"
}
```
`amount_paise` no longer exists - Zoho Payments takes a decimal amount, not paise.
There is no `razorpay_key_id` equivalent either: instead of an embedded JS checkout
modal, redirect the customer's browser to `checkout_url` (Zoho's hosted checkout,
a full-page redirect).

### Guard-rail errors — `400 { "detail": "<code>" }` (before the order is created)
| code | when |
|------|------|
| `no_booking` | customer has no booked plot / no plan |
| `installment_not_found` | no milestone at `installment_no` |
| `installment_already_paid` | that milestone is already `paid` |
| `installment_out_of_order` | an earlier milestone is still unpaid |
| `installment_not_payable` | `today < due_date − 5 days` (window not open) |
| `installment_amount_mismatch` | `amount` ≠ milestone amount (±1) |

The **same** checks re-run at settle time (order + settle can be minutes apart).

---

## 3. `POST /payments/verify` / `POST /payments/cash` — settle & mark paid

`/cash` takes the same three new fields as §2 in its body.

`/verify`'s body changed with the Zoho Payments cutover: it now takes the exact
query-string fields Zoho's hosted checkout appends when redirecting the customer's
browser back to `success_url`/`failure_url` — the frontend forwards these unchanged:
```jsonc
{
  "payments_session_id": "1000000012345",
  "payment_id": "pay_NQ...",
  "payment_status": "success",
  "amount": "443576.00",
  "signature": "…",
  "udf1": "…", "udf2": "…", "udf3": "…", "udf4": "…", "udf5": "…"  // optional
}
```

On a valid Zoho redirect signature (or a broker/customer cash record) the milestone is
flipped to `paid` (`paid_on`, `paid_payment_id`), `amount_received` is recomputed,
and sibling statuses refreshed — in the same step that settles the payment.

### Response 200 — instalment variant (adds 3 fields to `PaymentOut`)
```jsonc
{
  "id": "b7e2c1a0-...", "owner_id": "C00007", "owner_role": "customer",
  "amount": 443576, "currency": "INR", "status": "paid", "method": "zoho",
  "verified": true, "zoho_payments_session_id": "1000000012345", "zoho_payment_id": "pay_NQ...",
  "created_date": "2025-02-20T09:12:44Z",

  "purpose": "installment",        // NEW echo
  "installment_no": 3,             // NEW echo
  "installment_status": "paid"     // NEW — "paid" | "rejected" | null
}
```

### Settle-time guard failure — money is kept, **no 4xx**
```jsonc
{ "...": "...", "status": "paid", "installment_status": "rejected" }
```
The payment settles, `needs_manual_review` is set with `manual_review_reason` = the
guard code. **Frontend:** show "payment received, our team will reconcile it" — not
a payment error.

A "duplicate settle" (webhook arriving after `/verify`) is tolerated and returns
`installment_status: "paid"`.

---

## 4. `GET /customer/profile` → `booking.payment_schedule[]` + `booking.next_due`

> **Multi-plot:** a customer with more than one plot now also gets `bookings[]`
> (additive, newest first) — each entry a full booking object with its **own**
> `payment_schedule` / `next_due` / `amount_received`, scoped to that plot. `booking`
> stays the single most-recent one (`= bookings[0]`). See `CUSTOMER_PROFILE_API.md`.

Each schedule row now carries:
```jsonc
{
  "id": "doc-abc123-m3",            // NEW — stable milestone id
  "label": "Within 90 days of booking",
  "percent": 25, "due_days": 90,
  "due_date": "2025-04-10",
  "amount": 443576,
  "status": "due",                  // NEW — paid | due | overdue | upcoming
  "pay_enabled_from": "2025-04-05", // NEW — due_date − 5 days
  "paid_on": null,                  // NEW — ISO timestamp when paid
  "paid_payment_id": null           // NEW
}
```

`booking.amount_received` is the **sum of paid milestone amounts** (consistent with
the per-row `status`).

New optional `booking.next_due` (the earliest unpaid milestone; the FE may keep
computing its banner from the schedule and ignore this):
```jsonc
"next_due": {
  "milestone_id": "doc-abc123-m3", "label": "Within 90 days of booking",
  "amount": 443576, "due_date": "2025-04-10",
  "days_until_due": 12,             // negative when overdue
  "status": "due",
  "last_reminder_kind": "T_MINUS_20",
  "last_reminder_at": "2025-03-29T04:30:00Z"
}
```

Partial rollout is safe — the FE re-derives the same status/windows from `due_date`
when these fields are absent.

---

## 5. `GET /payments/{payment_id}/receipt` — payment slip PDF

Auth: customer bearer token (owner only). Server-rendered PDF (`application/pdf`,
`Content-Disposition: attachment`). Works for the booking amount and every
instalment.

| response | when |
|----------|------|
| `200` PDF | settled payment, caller owns it |
| `400 payment_not_paid` | payment exists but not `status: "paid"` |
| `403 forbidden` | not the caller's payment |
| `404 not_found` | no such payment |

The same PDF is **also emailed** as an attachment on a successful instalment settle
(see §6), so the slip is available on the site *and* in email.

---

## 6. Emails (premium template, brand logo, Pay-now button)

### Payment received (on settle)
Sent to `customer.email` right after an instalment is marked paid — a "Payment
Received" email with the **receipt PDF attached** and a *View My Payments* button →
`https://www.divinevisioninfra.com/customer/profile#payments`. Best-effort; a mail
failure never affects the settled payment.

### Payment due (scheduled)
`GET` **or** `POST` `/jobs/payment-reminders?key=<DIVINE_JOBS_TOKEN>` — call **once
daily** from cron-job.org (same as the `/health` keep-alive). Both methods hit the
same handler, so the cron only needs to store the URL — no request method or body
to configure. The token travels in the query string; there is no request body.

- No token configured → `503 jobs_not_configured`. Wrong/missing key → `401`.
- `200` → summary `{ "scanned_customers", "sent": {"T_MINUS_20": n, ...}, "skipped", "errors", "no_email" }`.
- Safe to fire twice a day — reminders are de-duplicated, so a repeat run sends nothing extra.

For each customer with a booked plot and an unpaid **earliest** milestone `M`, it
sends **one** email per run — the most urgent kind not already sent:

| kind | trigger | dedupe |
|------|---------|--------|
| `OVERDUE` | `today > M.due_date` | once per ISO week |
| `DUE_TODAY` | `today == M.due_date` | once |
| `T_MINUS_5` | `1 ≤ days_until_due ≤ 5` | once |
| `T_MINUS_20` | `5 < days_until_due ≤ 20` | once |

Content is all plan-derived: customer name, project + plot, milestone label,
amount (`₹ 4,43,576`), due date, days remaining / overdue-by, outstanding after
this milestone, and a **Pay Now** button → `/customer/profile#payments`.
Subject: `Payment due - ₹4,43,576 for OPS Divine Greens Plot 204 by 10 Apr 2025`.

**Recommended cron-job.org config:** URL
`https://<backend>/jobs/payment-reminders?key=<DIVINE_JOBS_TOKEN>`, schedule
`0 3 * * *` (08:30 IST ≈ 03:00 UTC), timeout 60s. Leave the method as the default
(GET) or set POST — either works.

---

## 7. Frontend checklist

1. **Pay now** → `POST /payments/create-order` with `purpose: "installment"`,
   `installment_no`, `amount` (exactly the row `amount`), `due_date`. Handle the
   `400` guard codes (§2) — most map to "refresh the plan".
2. On `verify` / `cash` success read `installment_status`:
   - `"paid"` → done, refresh the schedule.
   - `"rejected"` → payment received, reconciliation pending — **not** an error.
3. Read the new per-row `status` / `pay_enabled_from` (or keep deriving them from
   `due_date`). Enable **Pay now** only on the earliest row whose status is
   `due` / `overdue` and `today ≥ pay_enabled_from`.
4. Offer the receipt from `GET /payments/{payment_id}/receipt` (opens/downloads a PDF).
5. `booking.next_due` is available if you want it for the banner; not required.
