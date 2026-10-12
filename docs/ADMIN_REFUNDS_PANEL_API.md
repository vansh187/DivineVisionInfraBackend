# Admin Refunds Panel API Integration

Frontend integration guide for the admin panel Refunds screen.

All endpoints are admin-only and require:

```http
Authorization: Bearer <admin_access_token>
```

Missing, invalid, customer, or broker tokens return `401`.

## Screen Purpose

The Refunds screen shows payments where a refund has already been started or resolved. A payment appears in this screen only when:

```text
divine_payments.refund_status != "none"
```

Use this screen for:

- tracking Zoho Payments refunds while they are processing, completed, or failed
- retrying eligible Zoho Payments refunds
- tracking cash, RTGS/NEFT, and legacy Razorpay refunds that must be paid manually
- marking manual refunds as collected/completed after payout

> **Legacy Razorpay payments:** payments made before the Zoho Payments cutover have
> `method: "razorpay"`. That gateway's credentials were retired at cutover, so there is
> no automatic retry path for them anymore - they are refunded the same manual way as
> cash/RTGS-NEFT (via Mark Collected, section 4), and display under the
> `bank_transfer_pending` / `bank_transfer_completed` statuses below, not `processing`.

## API Base

```text
/admin/refunds
```

## 1. List Refunds

```http
GET /admin/refunds?page=1&page_size=20&search=Verma&status=processing&method=zoho
Authorization: Bearer <admin_access_token>
```

### Query Params

| Param | Type | Values | Default | Notes |
|---|---|---|---|---|
| `page` | int | `>= 1` | `1` | Current page |
| `page_size` | int | `1` to `100` | `20` | Rows per page |
| `search` | string | any text, max `200` chars | none | Matches payment id, booking id, project, unit, or customer name |
| `status` | enum | see status table below | none | Filters display status |
| `method` | enum | `zoho`, `cash`, `rtgs_neft`, `razorpay` | none | Filters payment method. `razorpay` is legacy-only (payments made before the Zoho cutover) |

### Response

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

### Table Column Mapping

| Admin UI column | API field | Display note |
|---|---|---|
| Refund ID / Payment ID | `id` | This is the payment id; refunds do not have a separate internal id |
| Booking | `booking_id` | Can be `null` |
| Customer | `customer_name` | Fallback to `customer_id` when null |
| Project | `project_name` | Can be `null` |
| Unit | `unit_number` | Can be `null` |
| Amount | `amount` + `currency` | Format as INR in the UI |
| Method | `method` | Render as Zoho, Cash, RTGS/NEFT, or Razorpay (legacy) |
| Status | `status` | Use the status vocabulary below |
| Initiated | `refund_initiated_date` | Can be `null` for old/imported rows |
| Completed | `refund_completed_date` | Usually null until final status |

## 2. Get Refund Detail

Use this when the admin opens a row drawer/detail modal.

```http
GET /admin/refunds/{payment_id}
Authorization: Bearer <admin_access_token>
```

### Response

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
  "method": "zoho",
  "status": "processing",
  "refund_initiated_date": "2026-09-16T11:05:00Z",
  "refund_completed_date": null,
  "gateway_payment_id": "pay_abc123",
  "zoho_refund_id": null,
  "utr_number": null,
  "refund_note": "Automatic refund failed (gateway timeout) - needs manual retry.",
  "created_at": "2026-01-01T10:00:00Z"
}
```

`gateway_payment_id` holds the `zoho_payment_id` for a `method: "zoho"` payment, or the legacy `razorpay_payment_id` for a pre-cutover `method: "razorpay"` payment - never both.

Unknown payment ids and payments with no refund return:

```json
{ "detail": "not_found" }
```

with HTTP `404`.

## 3. Retry Zoho Refund

Use this action only for Zoho Payments rows stuck in processing.

```http
POST /admin/refunds/{payment_id}/retry
Authorization: Bearer <admin_access_token>
```

Show the Retry action when:

```js
refund.method === "zoho" && refund.status === "processing"
```

Do **not** show Retry for `method === "razorpay"` - that gateway's credentials were
retired at the Zoho cutover, so a legacy Razorpay refund can never be retried
automatically. Use Mark Collected (section 4) for those instead.

The endpoint is double-click safe. It returns the latest refund detail.

### Success Response

```json
{
  "id": "b1e2b6b0-71b1-4e0a-9d3a-1a2b3c4d5e6f",
  "method": "zoho",
  "status": "completed",
  "gateway_payment_id": "pay_abc123",
  "zoho_refund_id": "rfnd_admin_refunds",
  "refund_note": "Refund retry completed.",
  "refund_completed_date": "2026-09-16T11:10:00Z"
}
```

The real response includes all fields from `GET /admin/refunds/{payment_id}`.

### Retry Errors

| HTTP | `detail` | Meaning |
|---|---|---|
| `404` | `not_found` | Unknown payment id |
| `409` | `payment_not_paid` | Payment was never captured |
| `409` | `not_a_zoho_refund` | Cash/RTGS/NEFT refunds cannot use gateway retry |
| `409` | `razorpay_gateway_retired` | Legacy Razorpay payment - that gateway has no live credentials anymore; use Mark Collected instead |
| `409` | `refund_not_retryable` | Refund is not in a retryable state |
| `500` | `internal_error` | Server-side failure |

## 4. Mark Manual Refund Collected

Use this after the business has actually paid a cash or RTGS/NEFT refund.

```http
POST /admin/refunds/{payment_id}/mark-collected
Content-Type: application/json
Authorization: Bearer <admin_access_token>
```

Show the Mark Collected action when:

```js
["cash", "rtgs_neft", "razorpay"].includes(refund.method) &&
["cash_refund_pending", "bank_transfer_pending"].includes(refund.status)
```

Note that `razorpay` (legacy, pre-cutover payments) now goes through this same manual
flow, not an automatic gateway retry - it has no live gateway credentials anymore.

### Request

```json
{
  "note": "NEFT completed from ops account"
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `note` | string | no | Max `2000` chars. Stored in `refund_note` with the confirming admin id |

### Response

Returns the latest refund detail. Status transitions:

| Before | After |
|---|---|
| `cash_refund_pending` | `cash_collected` |
| `bank_transfer_pending` | `bank_transfer_completed` |

### Manual Collection Errors

| HTTP | `detail` | Meaning |
|---|---|---|
| `404` | `not_found` | Unknown payment id |
| `409` | `not_a_manual_refund` | Zoho refunds cannot be manually marked collected (use Retry instead) |
| `409` | `refund_not_pending` | Already completed, failed, or not pending |
| `422` | standard FastAPI validation body | Invalid request body |
| `500` | `internal_error` | Server-side failure |

## Status Vocabulary

The backend returns display-ready status values. Do not derive status on the frontend from raw `refund_status`.

| `status` | Method | UI label | Suggested badge |
|---|---|---|---|
| `processing` | `zoho` | Processing | Amber |
| `completed` | `zoho` | Completed | Green |
| `failed` | `zoho` | Failed | Red |
| `cash_refund_pending` | `cash` | Cash Refund Pending | Amber |
| `cash_collected` | `cash` | Cash Collected | Green |
| `bank_transfer_pending` | `rtgs_neft` or `razorpay` (legacy) | Bank Transfer Pending | Amber |
| `bank_transfer_completed` | `rtgs_neft` or `razorpay` (legacy) | Bank Transfer Completed | Green |

## Frontend Integration Example

```ts
type RefundMethod = "zoho" | "cash" | "rtgs_neft" | "razorpay";

type RefundStatus =
  | "processing"
  | "completed"
  | "failed"
  | "cash_refund_pending"
  | "cash_collected"
  | "bank_transfer_pending"
  | "bank_transfer_completed";

type RefundListItem = {
  id: string;
  booking_id: string | null;
  customer_id: string;
  customer_name: string | null;
  project_name: string | null;
  unit_number: string | null;
  amount: number;
  currency: string;
  method: RefundMethod;
  status: RefundStatus;
  refund_initiated_date: string | null;
  refund_completed_date: string | null;
};

type RefundListResponse = {
  items: RefundListItem[];
  pagination: {
    page: number;
    page_size: number;
    total_items: number;
    total_pages: number;
  };
};

export async function fetchAdminRefunds(params: {
  token: string;
  page?: number;
  pageSize?: number;
  search?: string;
  status?: RefundStatus;
  method?: RefundMethod;
}) {
  const query = new URLSearchParams();
  query.set("page", String(params.page ?? 1));
  query.set("page_size", String(params.pageSize ?? 20));
  if (params.search) query.set("search", params.search);
  if (params.status) query.set("status", params.status);
  if (params.method) query.set("method", params.method);

  const res = await fetch(`/admin/refunds?${query.toString()}`, {
    headers: { Authorization: `Bearer ${params.token}` },
  });

  if (!res.ok) throw new Error(`Failed to load refunds: ${res.status}`);
  return (await res.json()) as RefundListResponse;
}

export async function retryRefund(token: string, paymentId: string) {
  const res = await fetch(`/admin/refunds/${paymentId}/retry`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
  });

  if (!res.ok) throw new Error(`Failed to retry refund: ${res.status}`);
  return res.json();
}

export async function markRefundCollected(token: string, paymentId: string, note?: string) {
  const res = await fetch(`/admin/refunds/${paymentId}/mark-collected`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ note }),
  });

  if (!res.ok) throw new Error(`Failed to mark refund collected: ${res.status}`);
  return res.json();
}
```

## Recommended UI Behavior

- Load `GET /admin/refunds` on screen mount with `page=1&page_size=20`.
- Debounce search input before calling the list API.
- Reset `page` to `1` when `search`, `status`, or `method` changes.
- Refresh the current page after `retry` or `mark-collected`.
- Open detail drawer with `GET /admin/refunds/{payment_id}` so the admin sees `refund_note`, gateway ids, and UTR.
- Disable action buttons while the request is in flight.
- For `409` responses, show the returned `detail` and refresh the row because another admin or gateway update may have changed the state.

## Error Handling Summary

| HTTP | Meaning | Frontend action |
|---|---|---|
| `401` | Admin auth missing/invalid | Redirect to login or show session expired |
| `404` | Refund/payment not found | Close detail view and refresh list |
| `409` | Business rule conflict | Show message and refresh row/list |
| `422` | Invalid query/body | Fix frontend request construction |
| `500` | Server error | Show retry option; do not parse internal details |

## Related Backend Files

- API routes: `DivineAPI/admin_refunds_api.py`
- Service logic: `DivineService/service_refund.py`
- SQL queries: `DivineDatabasequeries/admin_refunds_queries.yaml`
- DTOs: `DivineDTO/models.py`
- Existing combined cancellation/refund workflow doc: `docs/CANCEL_BOOKING_AND_REFUND_RETRY_ADMIN_API.md`
