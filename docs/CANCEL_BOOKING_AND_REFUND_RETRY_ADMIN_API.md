# Cancel Booking & Refunds - Admin API

Frontend integration reference for admin-only refund workflows:

1. `POST /admin/bookings/{booking_id}/cancel` - cancel a booking, release the plot, start a refund, and email the customer.
2. `GET /admin/refunds` - list refund information for the admin panel.
3. `GET /admin/refunds/{payment_id}` - show one refund's latest status and references.
4. `POST /admin/refunds/{payment_id}/retry` - retry a Razorpay refund stuck in processing.
5. `POST /admin/refunds/{payment_id}/mark-collected` - mark a cash/RTGS/NEFT refund as paid out.

All endpoints require an admin JWT: `Authorization: Bearer <admin_jwt>`. A non-admin or missing token gets `401`.

---

## 1. Cancel Booking

```http
POST /admin/bookings/{booking_id}/cancel
Content-Type: application/json
Authorization: Bearer <admin_jwt>
```

Allowed while the booking's `status` is `pending_kyc_review` or `booked`. Not allowed once it is already `rejected` or `cancelled`.

### Request

```json
{
  "note": "Customer requested cancellation over phone",
  "version": 2
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `version` | int | yes | Must equal the booking's current `version`. Re-fetch booking detail before showing the Cancel action. |
| `note` | string | no | Max 2000 chars. Stored in decision history and included in the cancellation email. |

### Response - 200 OK

Returns the updated booking detail. Important refund fields:

```json
{
  "id": "BKG-2026-000042",
  "status": "cancelled",
  "version": 3,
  "payment_id": "b1e2b6b0-71b1-4e0a-9d3a-1a2b3c4d5e6f",
  "payment_method": "cash",
  "payment_status": "paid",
  "project_name": "Suraksha Enclave",
  "unit_number": "C-9",
  "amount": 900000
}
```

Refund behavior by payment method:

| `payment_method` | What happens |
|---|---|
| `razorpay` | Real refund is triggered through Razorpay immediately. If gateway retry attempts fail, the payment stays retryable from the Refunds tab. |
| `cash` | No gateway call. The customer is told where to collect the cash refund. Admin later confirms payout with `mark-collected`. |
| `rtgs_neft` | No gateway call. Business pays manually by bank transfer. Admin later confirms payout with `mark-collected`. |

### Errors

| Status | `detail` | Meaning |
|---|---|---|
| 401 | - | Missing/invalid/non-admin token |
| 404 | `not_found` | Unknown booking id |
| 409 | `version_conflict` | Stale `version`; re-fetch the booking |
| 409 | `booking_not_cancellable` | Booking is already rejected/cancelled |
| 422 | - | Invalid request body |
| 500 | `internal_error` | Safe to retry |

---

## 2. List Refunds

```http
GET /admin/refunds?page=1&page_size=20&search=Verma&status=processing&method=razorpay
Authorization: Bearer <admin_jwt>
```

Query params:

| Param | Type | Notes |
|---|---|---|
| `page` | int | Default `1`, min `1` |
| `page_size` | int | Default `20`, min `1`, max `100` |
| `search` | string | Matches payment id, booking id, project, unit, or customer name |
| `status` | enum | `processing`, `completed`, `failed`, `cash_refund_pending`, `cash_collected`, `bank_transfer_pending`, `bank_transfer_completed` |
| `method` | enum | `razorpay`, `cash`, `rtgs_neft` |

### Response - 200 OK

```json
{
  "items": [
    {
      "id": "b1e2b6b0-71b1-4e0a-9d3a-1a2b3c4d5e6f",
      "booking_id": "BKG-2026-000042",
      "customer_id": "C00019",
      "customer_name": "Rehan Sharma",
      "project_name": "Suraksha Enclave",
      "unit_number": "C-9",
      "amount": 900000,
      "currency": "INR",
      "method": "cash",
      "status": "cash_refund_pending",
      "refund_initiated_date": "2026-09-16T11:05:00Z",
      "refund_completed_date": null
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total_items": 1,
    "total_pages": 1
  }
}
```

Only payments where `refund_status != "none"` appear here.

---

## 3. Get Refund Detail

```http
GET /admin/refunds/{payment_id}
Authorization: Bearer <admin_jwt>
```

### Response - 200 OK

```json
{
  "id": "b1e2b6b0-71b1-4e0a-9d3a-1a2b3c4d5e6f",
  "booking_id": "BKG-2026-000042",
  "customer_id": "C00019",
  "customer_name": "Rehan Sharma",
  "project_name": "Suraksha Enclave",
  "unit_number": "C-9",
  "amount": 900000,
  "currency": "INR",
  "method": "razorpay",
  "status": "processing",
  "razorpay_payment_id": "pay_abc123",
  "razorpay_refund_id": null,
  "utr_number": null,
  "refund_note": "Automatic refund failed (gateway timeout) - needs manual retry.",
  "refund_initiated_date": "2026-09-16T11:05:00Z",
  "refund_completed_date": null,
  "created_at": "2026-01-01T10:00:00Z"
}
```

Unknown payments and payments with no refund return `404 not_found`.

---

## 4. Retry Razorpay Refund

```http
POST /admin/refunds/{payment_id}/retry
Authorization: Bearer <admin_jwt>
```

Use only when:

- `method === "razorpay"`
- `status === "processing"`

The retry is double-click safe. A concurrent or repeated retry is refused rather than double-refunding.

### Response - 200 OK

Returns the same shape as `GET /admin/refunds/{payment_id}` with the latest status. If Razorpay succeeds, `status` becomes `completed` and `razorpay_refund_id` is populated. If Razorpay fails again, the response can still be `200` with `status: "processing"` and an updated `refund_note`.

### Errors

| Status | `detail` | Meaning |
|---|---|---|
| 401 | - | Missing/invalid/non-admin token |
| 404 | `not_found` | Unknown payment id |
| 409 | `payment_not_paid` | Payment was never captured |
| 409 | `not_a_razorpay_refund` | Cash/RTGS/NEFT refunds are manual |
| 409 | `refund_not_retryable` | Not currently pending/retryable |
| 500 | `internal_error` | Safe to retry |

---

## 5. Mark Manual Refund Collected

```http
POST /admin/refunds/{payment_id}/mark-collected
Content-Type: application/json
Authorization: Bearer <admin_jwt>
```

Use for `cash` and `rtgs_neft` refunds once the business has actually paid the customer.

### Request

```json
{
  "note": "Cash paid from Ganaur site office"
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `note` | string | no | Max 2000 chars. Stored in `refund_note` with the confirming admin id. |

### Response - 200 OK

Returns the latest refund detail. `cash_refund_pending` becomes `cash_collected`; `bank_transfer_pending` becomes `bank_transfer_completed`.

### Errors

| Status | `detail` | Meaning |
|---|---|---|
| 401 | - | Missing/invalid/non-admin token |
| 404 | `not_found` | Unknown payment id |
| 409 | `not_a_manual_refund` | Razorpay refunds cannot be manually collected |
| 409 | `refund_not_pending` | Already completed, failed, or no refund is pending |
| 422 | - | Invalid request body |
| 500 | `internal_error` | Safe to retry |
