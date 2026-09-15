# Cancel Booking & Refund Retry — Admin API

Frontend integration reference for two admin-only endpoints:

1. `POST /admin/bookings/{booking_id}/cancel` — cancel a booking (works at either
   the KYC-review stage or once the plot is already booked/approved), release the
   plot, start a refund, and email the customer.
2. `POST /admin/payments/{payment_id}/refund/retry` — resume a Razorpay refund
   that got stuck at `refund_status: "pending"` after its automatic attempts
   failed at the gateway.

Both require an admin JWT: `Authorization: Bearer <admin_jwt>`. A non-admin or
missing token gets `401`.

---

## 1. Cancel Booking

```
POST /admin/bookings/{booking_id}/cancel
```

Allowed while the booking's `status` is `pending_kyc_review` **or** `booked`.
Not allowed once it's already `rejected` or `cancelled`.

### Request

```http
POST /admin/bookings/BKG-2026-000042/cancel
Content-Type: application/json
Authorization: Bearer <admin_jwt>
```
```json
{
  "note": "Customer requested cancellation over phone",
  "version": 2
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `version` | int | yes | Must equal the booking's current `version`. Always re-`GET` the booking detail right before showing the Cancel action so the version used is fresh. |
| `note` | string | no | Max 2000 chars. Stored in the decision history and included in the customer's cancellation email. |

### Response — 200 OK

```json
{
  "id": "BKG-2026-000042",
  "status": "cancelled",
  "kyc_status": "verified",
  "version": 3,
  "admin_note": "Customer requested cancellation over phone",
  "customer_id": "C00019",
  "customer_name": "Rehan Sharma",
  "customer_email": "rehan@example.com",
  "customer_phone": "9999999998",
  "project_name": "Suraksha Enclave",
  "unit_number": "C-9",
  "amount": 900000,
  "payment_id": "b1e2b6b0-71b1-4e0a-9d3a-1a2b3c4d5e6f",
  "payment_method": "cash",
  "payment_status": "paid",
  "razorpay_payment_id": null,
  "utr_number": null,
  "documents": [
    { "document_type": "aadhaar_front", "label": "Aadhaar Card - Front", "uploaded": true,
      "preview_url": "https://...", "preview_url_expires_in": 900, "uploaded_at": "2026-01-01T09:00:00Z" }
  ],
  "decision_history": [
    { "actor": "system", "action": "payment_received", "note": null, "created_at": "2026-01-01T10:00:00Z" },
    { "actor": "DV1234", "action": "approved", "note": "All docs verified", "created_at": "2026-01-02T09:00:00Z" },
    { "actor": "DV1234", "action": "cancelled", "note": "Customer requested cancellation over phone", "created_at": "2026-09-16T11:20:00Z" }
  ],
  "created_at": "2026-01-01T10:00:00Z",
  "last_activity_at": "2026-09-16T11:20:00Z"
}
```

The plot is released back to `available` in the same call. A refund is started
automatically, matched to how the customer paid:

| `payment_method` | What happens |
|---|---|
| `razorpay` | Real refund triggered through Razorpay's API immediately. |
| `cash` | No gateway call — the confirmation email tells the customer which office to collect the cash refund from (OPS Divine Greens office, or the Ganaur site office for Suraksha Enclave). |
| `rtgs_neft` | No gateway call — the business processes the bank transfer manually; the email says the refund will clear in 5–7 business days. |

A "Booking Cancelled" email is sent to the customer **in every case above** —
nothing further for the frontend to trigger. If a Razorpay refund fails at the
gateway (both automatic attempts), it settles at `refund_status: "pending"` on
the payment — see §2 below for how to recover it.

### Errors

| Status | `detail` | Meaning / what the frontend should do |
|---|---|---|
| 401 | — | missing/invalid/non-admin token |
| 404 | `not_found` | unknown booking id |
| 409 | `version_conflict` | stale `version` — re-fetch `GET /admin/bookings/{id}` and show the latest state before retrying |
| 409 | `booking_not_cancellable` | booking is already `rejected`/`cancelled` — hide/disable the Cancel action and refresh |
| 422 | — | `version` missing or not a positive integer |
| 500 | `internal_error` | safe to retry |

---

## 2. Retry Razorpay Refund

```
POST /admin/payments/{payment_id}/refund/retry
```

Use this only for a **Razorpay** payment whose `refund_status` is `"pending"`
(shown on the Revenue tab / booking detail) after a Cancel/Reject — meaning the
automatic gateway attempt(s) already failed. It is safe to click more than
once: a second call while a first is still in flight, or once it's already
resolved, is refused rather than double-refunding.

### Request

```http
POST /admin/payments/b1e2b6b0-71b1-4e0a-9d3a-1a2b3c4d5e6f/refund/retry
Authorization: Bearer <admin_jwt>
```

No request body.

### Response — 200 OK

```json
{
  "id": "b1e2b6b0-71b1-4e0a-9d3a-1a2b3c4d5e6f",
  "method": "razorpay",
  "refund_status": "completed",
  "refund_amount": 750000,
  "razorpay_refund_id": "rfnd_abc123",
  "refund_initiated_date": "2026-09-16T11:05:00Z",
  "refund_completed_date": "2026-09-16T11:22:14Z",
  "refund_note": null
}
```

If the retry itself fails at the gateway again, the response still comes back
`200` with `"refund_status": "pending"` and a `refund_note` explaining the
failure — poll/re-fetch the payment, or let the admin retry again later; it's
not an error response.

### Errors

| Status | `detail` | Meaning |
|---|---|---|
| 401 | — | missing/invalid/non-admin token |
| 404 | `not_found` | unknown payment id |
| 409 | `payment_not_paid` | payment was never actually captured |
| 409 | `not_a_razorpay_refund` | payment method is `cash`/`rtgs_neft` — those are a manual payout owed by the business, not gateway-retryable; don't show the Retry button for these |
| 409 | `refund_not_retryable` | not currently stuck at `pending` — already completed, never started, or a concurrent retry just claimed it (double-click safe: only show a spinner/disable the button while a request is in flight) |
| 500 | `internal_error` | safe to retry |

### When to show the "Retry Refund" button

Show it only when, for a given payment:
- `method === "razorpay"`, **and**
- `refund_status === "pending"`

For `cash`/`rtgs_neft` refunds stuck at `"pending"`, that's the expected steady
state until ops manually pays out and marks it complete — there is currently no
"mark complete" endpoint for those; treat it as informational, not actionable
from this button.
