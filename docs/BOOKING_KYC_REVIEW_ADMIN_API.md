# Booking KYC Review — API contract for the admin frontend

**Status:** shipped on branch `DivineInfraBackend`. DB migrations already applied to production.

**What changed:** a plot-booking payment no longer books the plot immediately. It now
**holds** the unit (`status = "pending_kyc_review"`) and creates a **Booking** record
(`"BKG-2026-000001"`-style id) that an admin must review and Approve or Reject before the
plot is truly booked. Approve confirms the plot as `booked` and unlocks the customer's
receipt download. Reject releases the plot back to `available` and starts a refund.

This document covers the five admin-only endpoints behind the Booking Queue / Booking
Detail screens you were sent screenshots of. All require `Authorization: Bearer
<admin_access_token>`.

---

## 1. `GET /admin/bookings` — Booking Queue list

| Query param | Type | Values | Default |
|---|---|---|---|
| `page` | int | ≥1 | 1 |
| `page_size` | int | 1–100 | 20 |
| `search` | string | matches booking id, customer id, or project name | — |
| `status` | enum | `pending_kyc_review`, `booked`, `rejected`, `cancelled` | — (all) |
| `kyc_status` | enum | `pending`, `verified`, `needs_resubmission`, `rejected` | — (all) |

**Sample request**
```
GET /admin/bookings?status=pending_kyc_review&page=1&page_size=20
Authorization: Bearer eyJ...
```

**Sample response — 200**
```json
{
  "items": [
    {
      "id": "BKG-2026-000001",
      "customer_id": "C45172",
      "customer_name": "Rehan Sharma",
      "project_name": "Green Meadows",
      "unit_number": "A-112",
      "amount": 2450000,
      "status": "pending_kyc_review",
      "kyc_status": "pending",
      "version": 1,
      "created_at": "2026-09-14T18:10:00Z",
      "last_activity_at": "2026-09-14T18:10:00Z"
    }
  ],
  "pagination": { "page": 1, "page_size": 20, "total_items": 4, "total_pages": 1 }
}
```

`version` is the value you must echo back on Approve/Reject/Cancel (see §3) — always use
the one from the most recent GET, never a cached/stale one.

This endpoint is exempt from the app's normal per-IP rate limit (like the other admin list
screens), so it's safe to poll/refresh it frequently.

---

## 2. `GET /admin/bookings/{booking_id}` — Booking Detail

Everything the detail screen needs in one call: customer + payment details, the KYC
document checklist, and the full decision-history timeline.

**Sample response — 200**
```json
{
  "id": "BKG-2026-000001",
  "status": "pending_kyc_review",
  "kyc_status": "pending",
  "version": 1,
  "admin_note": null,
  "customer_id": "C45172",
  "customer_name": "Rehan Sharma",
  "customer_email": "rehan.sharma@example.com",
  "customer_phone": "+91 98765 43210",
  "project_name": "Green Meadows",
  "unit_number": "A-112",
  "amount": 2450000,
  "payment_id": "b7e2f1a3-...",
  "payment_method": "zoho",
  "payment_status": "paid",
  "gateway_payment_id": "pay_QX...",
  "utr_number": null,
  "documents": [
    { "document_type": "aadhaar_front", "label": "Aadhaar Card - Front", "uploaded": true,
      "preview_url": "https://.../sign/...?token=...", "preview_url_expires_in": 3600,
      "uploaded_at": "2026-09-08T10:12:00Z" },
    { "document_type": "aadhaar_back", "label": "Aadhaar Card - Back", "uploaded": true,
      "preview_url": "https://.../sign/...?token=...", "preview_url_expires_in": 3600,
      "uploaded_at": "2026-09-08T10:12:05Z" },
    { "document_type": "pan_card", "label": "PAN Card", "uploaded": true,
      "preview_url": "https://.../sign/...?token=...", "preview_url_expires_in": 3600,
      "uploaded_at": "2026-09-08T10:13:00Z" },
    { "document_type": "applicant_photo", "label": "Applicant Photo", "uploaded": true,
      "preview_url": "https://.../sign/...?token=...", "preview_url_expires_in": 3600,
      "uploaded_at": "2026-09-08T10:13:20Z" },
    { "document_type": "cancelled_cheque", "label": "Cancelled Cheque", "uploaded": false,
      "preview_url": null, "preview_url_expires_in": null, "uploaded_at": null }
  ],
  "decision_history": [
    { "actor": "system", "action": "payment_received", "note": null,
      "created_at": "2026-09-08T11:02:00Z" }
  ],
  "created_at": "2026-09-08T11:02:00Z",
  "last_activity_at": "2026-09-08T11:02:00Z"
}
```

Field notes:
- **`documents`** — always exactly these 5 types: `aadhaar_front`, `aadhaar_back`,
  `pan_card`, `applicant_photo`, `cancelled_cheque`. `uploaded: false` means the customer
  hasn't uploaded that document yet — show it as missing, not as an error. `preview_url` is
  a short-lived (1 hour) signed URL; re-fetch this endpoint to refresh it, don't cache it.
- **`gateway_payment_id`** — the `zoho_payment_id` for a payment made via Zoho Payments
  (`payment_method: "zoho"`), or the legacy `razorpay_payment_id` for a payment made
  before the Zoho Payments cutover (`payment_method: "razorpay"`) — never both.
- **`utr_number`** — only populated when `payment_method` is `"rtgs_neft"`; `null`
  otherwise.
- **`decision_history`** — grows by one entry every time the booking is created or
  decided (`payment_received` → `approved` | `rejected` | `cancelled`). This is exactly
  what the "Decision History" timeline in the screenshot shows.

Errors: `404 {"detail":"not_found"}` for an unknown booking id.

---

## 3. Approve / Reject / Cancel

All three share one request shape and are **optimistic-locked** on `version` — this is
what the "Entity Version: v7" field in the detail screenshot is for.

```
POST /admin/bookings/{booking_id}/approve
POST /admin/bookings/{booking_id}/reject
POST /admin/bookings/{booking_id}/cancel
```

**Request** (same shape for all three)
```json
{
  "note": "All KYC documents verified against the uploaded photos.",
  "version": 1
}
```
`note` is optional but strongly recommended — it's what gets emailed to the customer (see
below) and stored permanently in the decision history. `version` is **required** and must
match the booking's current version exactly.

**Response — 200** (same `BookingDetailDTO` shape as §2, reflecting the new state)

### What each one does

| Action | Effect |
|---|---|
| **Approve** | KYC verified. Plot flips to `booked`. Customer's receipt/PDF download unlocks. Customer gets a "KYC Verified — Booking Confirmed" email with your note. |
| **Reject** | KYC failed. Plot releases back to `available`. A refund is started (see below). Customer gets a "KYC Review — Action Needed" email with your note and refund instructions. |
| **Cancel** | Admin cancels the booking outright (e.g. customer asked to cancel) — allowed both while still `pending_kyc_review` **and** once already `booked` (post-approval). Releases/unbooks the plot, starts a refund (see below), and **always** emails the customer with refund instructions, recorded as a distinct `cancelled` action in the timeline so it's distinguishable from a KYC rejection. |

### Refund behavior on Reject/Cancel (automatic, nothing the frontend needs to send)
- **Paid via Zoho Payments (online):** a real refund is triggered through Zoho's Payments
  API immediately, and the customer is emailed that it's on its way to their original
  payment method.
- **Paid via cash:** no gateway involved — the customer is emailed to collect the cash
  refund from the project's site office within 5–7 working days. The office address in the
  email is project-specific: OPS Divine Greens customers are pointed to the OPS Divine
  Greens office, Suraksha Enclave customers to the Ganaur site office.
- **Paid via RTGS/NEFT (UTR):** no gateway involved — the admin processes the transfer
  manually, and the customer is emailed that the refund will be credited within 5–7 working
  days.
- **Paid via legacy Razorpay (pre-cutover):** that gateway's credentials were retired at
  the Zoho Payments cutover, so there's no automatic gateway call anymore — it's refunded
  the same manual way as cash/RTGS-NEFT, and the customer is emailed accordingly.

A refund-notification email is sent to the customer in every case above — a Zoho, cash,
RTGS/NEFT, or legacy Razorpay payment all result in an email, even if the automatic
refund/gateway call itself fails (that failure is logged for manual follow-up, never
surfaced as an error on this endpoint — the booking decision and plot release have
already succeeded).

### Errors (all three endpoints)
| Status | `detail` | Meaning |
|---|---|---|
| 401 | — | missing/invalid/non-admin token |
| 404 | `not_found` | unknown booking id |
| 409 | `version_conflict` | your `version` doesn't match the booking's current version — **re-fetch the detail and show the latest state**; someone else (or you, in another tab) already decided it, or your copy is stale. |
| 409 | `booking_not_reviewable` | (Approve/Reject only) the booking isn't `pending_kyc_review` — same remedy as above |
| 409 | `booking_not_cancellable` | (Cancel only) the booking is already `rejected`/`cancelled` — same remedy as above |
| 409 | `inventory_confirm_failed` | (Approve only, rare) the plot could not be confirmed at the DB level — re-fetch and retry |
| 422 | — | `version` missing or not a positive integer |
| 500 | `internal_error` | unexpected server error — safe to retry |

**Recommended frontend pattern:** on a `409`, always re-fetch `GET
/admin/bookings/{id}` and re-render the detail screen (updated status, version, decision
history) rather than assuming your action failed for no reason — this is the "someone else
already acted" case surfacing correctly, not a bug.

---

## Known limitations (phase 1 — flag to your PM, not bugs to chase)
- There is no per-document "Verified" / "Needs Resubmission" flag yet — the admin makes
  **one** overall Approve/Reject decision per booking after reviewing the document set.
  `kyc_status` reflects that overall decision (`pending` → `verified` | `rejected`);
  `needs_resubmission` is a reserved value for a future phase, nothing produces it yet.
  The document checklist itself (`uploaded: true/false` + preview) is fully live today.
