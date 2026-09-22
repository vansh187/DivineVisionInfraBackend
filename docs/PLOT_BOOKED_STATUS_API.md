# Plot "booked" status — API contract for the frontend

**Status:** shipped on branch `DivineInfraBackend`. DB migration already applied to production.

**What changed:** when a customer completes the booking payment, the backend now flips
that plot's inventory row to `status = "booked"` and it disappears from every
"available plots" feed for all other users. Previously the plot stayed `available`
and was shown to the next customer.

The only thing the frontend must do is **send `inventory_id` (+ `purpose`) on the
booking payment call**. Everything else is automatic.

---

## 1. Inventory status values

| status      | shown in `GET /inventory/search`? | meaning |
|-------------|-----------------------------------|---------|
| `available` | ✅ yes | free to book |
| `held`      | ❌ no | (reserved for a future soft-hold feature) |
| `reserved`  | ❌ no | channel-partner 3-day hold (existing) |
| `booked`    | ❌ no | **new** — customer paid the booking amount |
| `sold`      | ❌ no | fully sold / registered |

`GET /inventory/search` with **no `status` query param** now returns **only
`available`** units. Pass `?status=booked` (or `sold`, `held`) explicitly if an
internal screen needs those.

---

## 2. `POST /payments/create-order`  — add two fields

Send `purpose` + `inventory_id` when the payment is a plot booking. Both are
optional; omit them for any non-booking payment.

### Request
```jsonc
{
  "amount": 500000,
  "purpose": "plot_booking",          // NEW — "plot_booking" | "other" (default "other")
  "inventory_id": "b0e1f2a3-4c5d-6e7f-8a9b-0c1d2e3f4a5b"   // NEW — the inventory row id
}
```

### Response 200 — gateway fields changed for Zoho Payments
```jsonc
{
  "payment_id": "b7e2...",
  "zoho_payments_session_id": "1000000012345",
  // Full hosted-checkout URL to redirect the customer's browser to - a
  // full-page redirect (NOT an embedded JS checkout modal like Razorpay's).
  "checkout_url": "https://payments.zoho.in/hostedcheckout/8f3a9b2c...",
  "access_key": "8f3a9b2c...",
  "amount": 500000,
  "currency": "INR",
  "status": "created"
}
```
`amount_paise` no longer exists - Zoho Payments takes a decimal amount, not paise.
There is no `razorpay_key_id` equivalent: redirect the customer's browser to
`checkout_url` instead of opening an embedded JS checkout modal.

Errors:
- `400 {"detail":"invalid_purpose"}` — `purpose` not `plot_booking` / `other`.
- `409 {"detail":"unit_not_available"}` — the `inventory_id` is already
  `booked` / `sold` / `reserved`. Don't start the payment; refresh the plot list.
  (Best-effort pre-check — it is not a hold, so two customers can still both pass
  it and race; the loser is caught at `/verify` with `inventory_status:"conflict"`.)

---

## 3. `POST /payments/verify`  — response gains 3 fields

Body changed with the Zoho Payments cutover: it now takes the exact query-string
fields Zoho's hosted checkout appends when redirecting the customer's browser back
to `success_url`/`failure_url` - the frontend forwards these unchanged:
`payments_session_id`, `payment_id`, `payment_status`, `amount`, `signature`, and
optional `udf1`..`udf5`. On a **valid signature** the backend settles the payment
**and** flips the linked plot to `booked` in the same step.

### Response 200 — success (plot locked)
```jsonc
{
  "id": "b7e2...",
  "owner_id": "C00007",
  "owner_role": "customer",
  "amount": 500000,
  "currency": "INR",
  "status": "paid",
  "method": "zoho",
  "verified": true,
  "zoho_payments_session_id": "1000000012345",
  "zoho_payment_id": "pay_NQ...",
  "created_date": "2026-09-07T10:34:12Z",

  "inventory_id": "b0e1f2a3-...",     // NEW — echo of the linked unit
  "inventory_status": "booked",       // NEW — "booked" | "conflict" | null
  "inventory_conflict_reason": null   // NEW — string only when inventory_status == "conflict"
}
```

### Response 200 — conflict variant (payment still succeeded, plot was NOT free)
```jsonc
{
  "...": "... same as above ...",
  "status": "paid",
  "verified": true,
  "inventory_id": "b0e1f2a3-...",
  "inventory_status": "conflict",
  "inventory_conflict_reason": "unit_not_available"   // or "inventory_update_failed"
}
```

**Frontend handling of `"conflict"`:** the money went through, so do **not** show a
payment failure. Show a "this plot was just booked by someone else — our team will
call you to re-assign or refund" message. The payment is auto-flagged for manual
review on the backend.

`inventory_status` is `null` when `purpose` was not `plot_booking` (nothing to do).

---

## 4. `POST /payments/cash`  — add two fields, response gains the same 3

### Request
```jsonc
{
  "amount": 500000,
  "note": "Cash recorded from booking application final page.",
  "purpose": "plot_booking",          // NEW
  "inventory_id": "b0e1f2a3-..."       // NEW
}
```

### Response 200
Same `PaymentOut` shape as §3 — includes `inventory_id`, `inventory_status`,
`inventory_conflict_reason`.

**Important — who can lock a plot with cash:** the plot is flipped to `booked`
**only when a broker/staff token records the cash** (staff confirming money they
physically collected). A **customer's** own cash entry still creates the payment
row but returns `inventory_status: null` and does **not** lock the plot — an
unverified self-report can't remove a unit from the pool. Staff then confirm it
with `POST /inventory/{id}/book` (§6), or the customer pays via Zoho Payments (§2–3),
which locks it immediately.

---

## 5. `POST /documents/project-booking-application`  — optional safety-net field

Add `inventory_id` as a **multipart form field**. It is only a backstop and is
**echo-only** — the backend re-confirms the plot **from the payment itself**
(`payment.purpose == "plot_booking"` + `payment.inventory_id`), never from this
form field, so it can't be used to attach an unrelated plot to a paid payment. If
the payment already locked the plot (§3/§4) this is a harmless no-op.

```
Content-Type: multipart/form-data

file:                <signed PDF>
document_type:       project_booking_application
project_id:          ops-divine-greens
payment_id:          b7e2...
inventory_id:               b0e1f2a3-4c5d-6e7f-8a9b-0c1d2e3f4a5b  <-- NEW (optional but send it)
zoho_payments_session_id:   session_NQ...   (online payments only - renamed from
zoho_payment_id:            pay_NQ...       razorpay_order_id/razorpay_payment_id at the
                                             Zoho Payments cutover; echo-only, see note
                                             above, so send the values you have or omit
                                             both - it's a harmless no-op either way)
form_data:           {...JSON...}       (must still include the TOTAL plot amount)
```

### Response 200 — existing shape + 2 fields
```jsonc
{
  "id": "d3c4...",
  "owner_id": "C00007",
  "owner_role": "customer",
  "document_type": "project_booking_application",
  "status": "generated",
  "created_date": "2026-09-07T10:36:40Z",
  "signed_url": "https://storage.../d3c4.pdf?sig=...",
  "signed_url_expires_in": 3600,
  "payment_plan": { "...": "..." },

  "inventory_id": "b0e1f2a3-...",     // NEW (nullable echo)
  "inventory_status": "booked"        // NEW — "booked" | "conflict" | null
}
```

No new error codes — this endpoint never fails because of the inventory step.

---

## 6. Staff repair endpoints (broker bearer token)

Not for the customer app. Use from an internal/ops screen. This backend has no
`admin` role, so these require a **broker** token.

### `POST /inventory/{inventory_id}/book`
Force an `available` / `held` unit to `booked` (manual reconciliation).
```jsonc
// Request
{ "payment_id": "b7e2...", "reason": "manual reconciliation" }   // both optional
// Response 200
{ "id": "b0e1f2a3-...", "status": "booked",
  "booked_at": "2026-09-07T10:40:00Z", "booked_payment_id": "b7e2...", "booked_by": null }
```
`409 {"detail":"unit_not_available"}` if it is already booked by a different
payment, or is `sold` / `reserved`.

### `POST /inventory/{inventory_id}/unbook`
Reverse a booking (cancellation / refund): `booked → available`.
```jsonc
// Request
{ "reason": "booking cancelled, refund processed" }   // optional
// Response 200
{ "id": "b0e1f2a3-...", "status": "available",
  "booked_at": null, "booked_payment_id": null, "booked_by": null }
```
`409 {"detail":"unit_not_booked"}` if the unit is not currently `booked`.

Both return `403` for a customer token, `401` with no token.

---

## 7. Race safety

The flip is a single guarded `UPDATE ... WHERE status IN ('available','held')`, so
two customers paying for the same plot in the same second cannot both win — the
second one settles and comes back with `inventory_status: "conflict"` (§3). The
booking `UPDATE` is idempotent for the same `payment_id`, so `/verify` after the
webhook (or the document safety-net) is always safe to call.

---

## 8. Frontend checklist

1. Booking payment → send `purpose: "plot_booking"` + `inventory_id` on
   `POST /payments/create-order` **and** `POST /payments/cash`.
2. `POST /payments/create-order` may now return `409 unit_not_available` → the plot
   was taken before checkout started; refresh the list, don't redirect to Zoho's
   hosted checkout.
3. After `POST /payments/verify` / `/cash`: read `inventory_status`.
   - `"booked"` → proceed as normal.
   - `"conflict"` → payment succeeded but plot was taken; show the re-assign/refund
     message, don't show a payment error.
   - `null` on a customer **cash** booking is expected — staff confirm the plot
     separately; treat the booking as pending-confirmation, not failed.
4. `POST /documents/project-booking-application` → also send `inventory_id` (form
   field, echo-only backstop).
5. The "available plots" list already only shows `available` — no change needed, the
   booked plot just stops appearing.
