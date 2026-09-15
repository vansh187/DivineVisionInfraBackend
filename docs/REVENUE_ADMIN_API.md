# Revenue tab — API contract for the admin frontend

**Status:** shipped on branch `DivineInfraBackend`. No DB migration needed — this feature
only *reads* the existing `divine_payments` / `divine_bookings` / `divine_customer_users`
tables, it writes nothing.

**What this is:** the three admin-only, read-only endpoints behind the Revenue tab
(stat cards + the transactions table you were sent a screenshot of — TRANSACTION / BOOKING /
CUSTOMER / PROJECT / AMOUNT / METHOD / STATUS / DATE columns). All three require
`Authorization: Bearer <admin_access_token>` and return `401` without one, and `401` for a
customer or broker token too — nothing here is reachable by any role but admin.

A "transaction" is one **settled** payment (`divine_payments.status = "paid"`). A payment
that's still `created` or ended up `failed` never happened financially, so it never appears
in any of these three endpoints — there's no way to page/filter/search your way to it.

---

## Status vocabulary

Every transaction is bucketed into exactly one of these four values — this is what the
`status` field in every response below is:

| `status` value | Meaning | Screenshot label |
|---|---|---|
| `captured` | Settled online (Razorpay), no refund in flight | **Captured** |
| `cash_recorded` | Settled as cash (or RTGS/NEFT) self-reported, no refund in flight | **Cash Recorded** |
| `refund_pending` | Settled, but a refund has been initiated and hasn't completed yet | *(not in the screenshot — treat like "Refunded" with a "pending" qualifier if you want to distinguish it in the UI; safe to render as an amber "Refund Pending" badge)* |
| `refunded` | Settled, and the refund has completed | **Refunded** |

`method` is the underlying payment method and is independent of `status`: `razorpay`,
`cash`, or `rtgs_neft`.

---

## 1. `GET /admin/revenue/summary` — stat cards

| Query param | Type | Values | Default |
|---|---|---|---|
| `date_from` | string | `YYYY-MM-DD`, inclusive | — (no lower bound) |
| `date_to` | string | `YYYY-MM-DD`, inclusive | — (no upper bound) |

**Sample request**
```
GET /admin/revenue/summary?date_from=2026-09-01&date_to=2026-09-30
Authorization: Bearer eyJ...
```

**Sample response — 200**
```json
{
  "total_transactions": 5,
  "gross_amount": 10680000,
  "net_amount": 9660000,
  "captured_amount": 5570000,
  "cash_amount": 980000,
  "refund_pending_amount": 3110000,
  "refunded_amount": 2100000
}
```

Field meanings:
- `total_transactions` — count of every settled payment in range.
- `gross_amount` — sum of every settled payment's amount, refunds included.
- `net_amount` — `gross_amount` minus anything already **refunded** (`refund_pending` money
  is still counted as net revenue until the refund actually completes).
- `captured_amount` / `cash_amount` — the two "money actually kept, no refund in flight"
  buckets, split by method, for the Captured / Cash Recorded stat cards.
- `refund_pending_amount` / `refunded_amount` — for the refund-related stat card(s).

A bad `date_from`/`date_to` (not `YYYY-MM-DD`) is rejected with `422` before it reaches the
service. `date_from` after `date_to` is rejected with `400 {"detail": "date_from_after_date_to"}`.

---

## 2. `GET /admin/revenue/transactions` — the transactions table

| Query param | Type | Values | Default |
|---|---|---|---|
| `page` | int | ≥1 | 1 |
| `page_size` | int | 1–100 | 20 |
| `search` | string | matches booking id, customer name, or project name | — |
| `status` | enum | `captured`, `cash_recorded`, `refund_pending`, `refunded` | — (all) |
| `method` | enum | `razorpay`, `cash`, `rtgs_neft` | — (all) |
| `date_from` | string | `YYYY-MM-DD`, inclusive | — |
| `date_to` | string | `YYYY-MM-DD`, inclusive | — |

This is the search box in the screenshot ("Search by booking ID, customer or project") —
`search` is one field that matches all three.

**Sample request**
```
GET /admin/revenue/transactions?page=1&page_size=20&status=captured
Authorization: Bearer eyJ...
```

**Sample response — 200**
```json
{
  "items": [
    {
      "transaction_id": "3f7e2b1a-9c44-4e1d-8a20-1f9b6c5d0a11",
      "booking_id": "BKG-2026-001040",
      "customer_id": "C45172",
      "customer_name": "Meera Pillai",
      "project_name": "Palm County",
      "unit_number": "A-112",
      "amount": 3120000,
      "currency": "INR",
      "method": "razorpay",
      "status": "captured",
      "created_at": "2026-09-14T18:10:00Z"
    }
  ],
  "pagination": { "page": 1, "page_size": 20, "total_items": 5, "total_pages": 1 }
}
```

Map straight onto the screenshot's columns:

| Screenshot column | Response field |
|---|---|
| TRANSACTION | `transaction_id` (display as `TXN-...` if you want a shorter prefix — the raw value is the payment's internal id) |
| BOOKING | `booking_id` (can be `null` — see note below) |
| CUSTOMER | `customer_name` (falls back to `customer_id` if `null`) |
| PROJECT | `project_name` (can be `null`) |
| AMOUNT | `amount` + `currency` |
| METHOD | `method`, upper-cased for display |
| STATUS | `status` — see the vocabulary table above for the badge label/color |
| DATE | `created_at` |

**`booking_id` / `project_name` / `unit_number` can be `null`.** Not every settled payment is
tied to a plot booking — a customer/broker can record a cash payment for `purpose: "other"`
that has nothing to do with a specific unit. Render those rows with the Booking/Project cells
blank rather than assuming they're always populated.

A bad `status`/`method`/date param is rejected with `422`. `page=0`, `page_size=0`, or
`page_size>100` are also `422`. `date_from` after `date_to` is `400 {"detail":
"date_from_after_date_to"}`.

This endpoint is exempt from the app's normal per-IP rate limit (like the other admin list
screens), so it's safe to poll/refresh it frequently.

---

## 3. `GET /admin/revenue/transactions/{transaction_id}` — row detail

For a "view details" affordance on a row (receipt reference numbers, etc.) without having to
carry extra fields on every list row.

**Sample request**
```
GET /admin/revenue/transactions/3f7e2b1a-9c44-4e1d-8a20-1f9b6c5d0a11
Authorization: Bearer eyJ...
```

**Sample response — 200**
```json
{
  "transaction_id": "3f7e2b1a-9c44-4e1d-8a20-1f9b6c5d0a11",
  "booking_id": "BKG-2026-001040",
  "customer_id": "C45172",
  "customer_name": "Meera Pillai",
  "project_name": "Palm County",
  "unit_number": "A-112",
  "amount": 3120000,
  "currency": "INR",
  "method": "razorpay",
  "status": "captured",
  "created_at": "2026-09-14T18:10:00Z",
  "razorpay_payment_id": "pay_Nc9k2xLmQaZ1Yv",
  "utr_number": null
}
```

`404 {"detail": "not_found"}` for an unknown id **or** for a payment id that exists but never
actually settled (a `created`/`failed` payment) — deliberately the same response either way,
so this endpoint can't be used to probe for the existence of a payment that isn't revenue.

---

## Error shape (all three endpoints)

- `401` — missing/invalid/non-admin token. No body detail beyond `{"detail": "..."}"` is
  ever more specific than that for an auth failure.
- `422` — a query param failed validation (wrong enum value, bad date format, `page`/`page_size`
  out of range). FastAPI's standard `{"detail": [{"loc": [...], "msg": "...", ...}]}` shape.
- `400` — a filter combination that's individually well-formed but semantically invalid, e.g.
  `date_from` after `date_to`: `{"detail": "date_from_after_date_to"}`.
- `404` — (detail endpoint only) unknown or never-settled transaction id: `{"detail": "not_found"}`.
- `500` — `{"detail": "internal_error"}`. No stack trace, table name, or query text is ever
  included in this response, by design — treat any `500` as "something broke server-side,
  nothing actionable in the body," and check with backend rather than trying to parse it.

---

## What's intentionally not covered yet

- No CSV/Excel export endpoint. If the frontend needs a "Download" button on this tab, that's
  a follow-up ask, not something already built.
- No write endpoints (no manual "mark as refunded" from this tab) — refund status is only
  ever changed by the existing Booking KYC reject/cancel flow (see
  `BOOKING_KYC_REVIEW_ADMIN_API.md`), never directly from the Revenue tab.
