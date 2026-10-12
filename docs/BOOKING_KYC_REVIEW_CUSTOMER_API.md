# Booking KYC Review — API contract for the website frontend

**Status:** shipped on branch `DivineInfraBackend`. DB migrations already applied to production.

**What changed:** when a customer's plot-booking payment settles, the plot is no longer
booked immediately — it's held (`pending_kyc_review`) while an admin reviews their KYC
documents. Once Approved, the plot is `booked` and the receipt/PDF download unlocks. If
Rejected, the plot is released and a refund is started. This document covers what the
website needs to change.

---

## 1. Payment response now reflects a hold, not an immediate booking

`POST /payments/verify`, the Zoho Payments webhook, and `POST /payments/cash` all return
the same `PaymentOutDTO` shape as before, with **two changes**:

- `inventory_status` is now `"pending_kyc_review"` on success (previously `"booked"`).
  `"conflict"` (plot already taken) and `null` (non-booking payment) are unchanged.
- A new field, **`booking_id`**, is set alongside it — save this, it's what you use to
  poll status and fetch the receipt later (see §2).

**Sample response — plot-booking payment settled**
```json
{
  "id": "b7e2f1a3-...",
  "owner_id": "C45172",
  "owner_role": "customer",
  "amount": 2450000,
  "currency": "INR",
  "status": "paid",
  "method": "zoho",
  "verified": true,
  "zoho_payments_session_id": "1000000012345",
  "zoho_payment_id": "pay_QX...",
  "created_date": "2026-09-14T18:10:00Z",
  "inventory_id": "b0e1f2a3-4c5d-6e7f-8a9b-0c1d2e3f4a5b",
  "inventory_status": "pending_kyc_review",
  "inventory_conflict_reason": null,
  "booking_id": "BKG-2026-000001"
}
```

**What to show the customer immediately after payment:** "Payment received — we're
verifying your documents. You'll be notified once your booking is confirmed." Do **not**
show "Booked" yet; use the booking's actual status (§2) to decide what to display.

---

## 2. `GET /bookings/mine` — the customer's own bookings

Bearer-authed (customer token). Returns every booking the signed-in customer has, across
all statuses.

**Request**
```
GET /bookings/mine
Authorization: Bearer <customer_access_token>
```

**Response — 200**
```json
[
  {
    "id": "BKG-2026-000001",
    "project_name": "Green Meadows",
    "unit_number": "A-112",
    "amount": 2450000,
    "status": "pending_kyc_review",
    "kyc_status": "pending",
    "admin_note": null,
    "can_download_receipt": false,
    "created_at": "2026-09-14T18:10:00Z",
    "last_activity_at": "2026-09-14T18:10:00Z"
  }
]
```

| Field | Notes |
|---|---|
| `status` | `pending_kyc_review` \| `booked` \| `rejected` \| `cancelled` |
| `kyc_status` | `pending` \| `verified` \| `needs_resubmission` \| `rejected` |
| `admin_note` | the admin's note from their decision, once one exists — show this to the customer, especially on `rejected`, so they know why |
| **`can_download_receipt`** | **use this directly to enable/disable the Download Receipt / Generate PDF button** — `true` only once `status: "booked"`. Don't re-derive it from `status`/`kyc_status` yourself; the backend already did that. |

No matches → `200 []`, not a 404. `401` for a missing/invalid token.

---

## 3. `GET /bookings/{booking_id}/receipt` — download the receipt PDF

Bearer-authed, owner only. Enabled the moment `can_download_receipt` is `true`.

```
GET /bookings/BKG-2026-000001/receipt
Authorization: Bearer <customer_access_token>
```

- `200` — `application/pdf` body, `Content-Disposition: attachment; filename="..."`. Wire
  this directly to your Download Receipt / Generate PDF button.
- `400 {"detail":"kyc_not_approved"}` — booking isn't `booked` yet; button should be
  disabled before this can ever happen (see `can_download_receipt` above), but handle it
  gracefully anyway.
- `403 {"detail":"forbidden"}` — not this customer's booking.
- `404 {"detail":"not_found"}` — unknown booking id.

---

## 4. `POST /payments/cash` — new `method` + `utr_number` fields

If your "enter UTR number" payment page posts here (as an alternative to the Zoho Payments
hosted checkout), two fields were added. Fully backward compatible — omit both and it behaves
exactly as before (cash).

**Request**
```json
{
  "amount": 2450000,
  "method": "rtgs_neft",
  "utr_number": "UTR1234567890123",
  "purpose": "plot_booking",
  "inventory_id": "b0e1f2a3-4c5d-6e7f-8a9b-0c1d2e3f4a5b"
}
```

| Field | Type | Notes |
|---|---|---|
| `method` | enum | `"cash"` (default) \| `"rtgs_neft"` |
| `utr_number` | string, optional | **required when `method` is `"rtgs_neft"`** — the customer's bank transfer reference number |

Response is the same `PaymentOutDTO` as §1, `booking_id` included.

Errors (in addition to the existing ones): `400 {"detail":"invalid_method"}`,
`400 {"detail":"utr_number_required"}`.

---

## 5. Notification emails (nothing to build, for context only)

The customer automatically receives an email when an admin Approves or Rejects their
booking, including the admin's note and (on rejection) refund instructions specific to how
they paid. No frontend action needed — just know it happens, in case support asks "did the
customer get notified?" — yes, always, on every decision.

---

## Known limitations (phase 1)
- `admin_note` only appears after a decision is made — while `status` is
  `pending_kyc_review`, it's `null`. That's expected, not a missing-data bug.
- Refunds for `cash`/`rtgs_neft` payments are never automatic — if a customer asks "where's
  my refund," the 5–7 business day manual-process note in their rejection email is the
  accurate answer; there's no live refund-tracking status to show them yet.
