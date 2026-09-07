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

### Response 200 — unchanged
```jsonc
{
  "payment_id": "b7e2...",
  "razorpay_order_id": "order_NQ...",
  "razorpay_key_id": "rzp_test_xxx",
  "amount": 500000,
  "amount_paise": 50000000,
  "currency": "INR",
  "status": "created"
}
```

`400 {"detail":"invalid_purpose"}` if `purpose` is anything other than
`plot_booking` / `other`.

---

## 3. `POST /payments/verify`  — response gains 3 fields

Body unchanged (`razorpay_order_id`, `razorpay_payment_id`, `razorpay_signature`).
On a **valid signature** the backend settles the payment **and** flips the linked
plot to `booked` in the same step.

### Response 200 — success (plot locked)
```jsonc
{
  "id": "b7e2...",
  "owner_id": "C00007",
  "owner_role": "customer",
  "amount": 500000,
  "currency": "INR",
  "status": "paid",
  "method": "razorpay",
  "verified": true,
  "razorpay_order_id": "order_NQ...",
  "razorpay_payment_id": "pay_NQ...",
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
Same `PaymentOut` shape as §3 — includes `inventory_id`, `inventory_status`
(`"booked"` / `"conflict"`), `inventory_conflict_reason`. Cash settles immediately,
so the plot is locked on this call.

---

## 5. `POST /documents/project-booking-application`  — optional safety-net field

Add `inventory_id` as a **multipart form field**. It is only a backstop: if the
payment already locked the plot (§3/§4) this is a harmless no-op. It covers older
payments made before `purpose`/`inventory_id` existed.

```
Content-Type: multipart/form-data

file:                <signed PDF>
document_type:       project_booking_application
project_id:          ops-divine-greens
payment_id:          b7e2...
inventory_id:        b0e1f2a3-4c5d-6e7f-8a9b-0c1d2e3f4a5b     <-- NEW (optional but send it)
razorpay_order_id:   order_NQ...        (online payments only)
razorpay_payment_id: pay_NQ...          (online payments only)
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
2. After `POST /payments/verify` / `/cash`: read `inventory_status`.
   - `"booked"` → proceed as normal.
   - `"conflict"` → payment succeeded but plot was taken; show the re-assign/refund
     message, don't show a payment error.
3. `POST /documents/project-booking-application` → also send `inventory_id` (form field).
4. The "available plots" list already only shows `available` — no change needed, the
   booked plot just stops appearing.
