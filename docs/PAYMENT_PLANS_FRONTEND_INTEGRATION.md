# Payment Plans: Frontend Integration Guide

**Audience:** customer website team (React), plus the admin panel team (section 9 only).
**Backend base URL:** `https://divinevisioninfrabackend-301k.onrender.com` (the same `API_BASE_URL` as today).
**Scope:** new customers booking a **Suraksha Enclave** plot.

## 1. What changes for the customer

1. On the **Pricing** step, the customer picks a **payment plan** from a dropdown: PP1, PP2, PP3 or PP4. A 5th plan will appear later, with no frontend change, because the list comes from the API.
2. Under the dropdown, show the plan's details: its instalments, plus a worked example the API provides.
3. The website calculates **PLC, IFMS and CAM with its own text fields and formulas** (as today for PLC). It sends the resulting **amounts** in rupees with the Basic Sale Price, and the backend never recalculates the charges. Whenever any of these change, call **preview**. Show the exact instalments with due dates, the **total**, and the **booking amount**.
4. **The booking payment is the plan's first instalment, not a fixed 10%:**

   | Plan | Booking payment |
   |---|---|
   | PP1 | 10% |
   | PP2 | 10% |
   | PP3 | 50% |
   | PP4 | 100% plus all charges |

   The backend rejects any other amount.
5. The chosen plan is saved against that plot. From then on, the customer profile, the demand letter and the reminder emails all follow it.

### Plans currently loaded

| Code (send exactly this) | Short | Instalments (% of Basic Sale Price) | Charges added to |
|---|---|---|---|
| `PP1/FPP/10-15-25-25-15-10` | PP1 | 10 on booking · 15 @45 days · 25 @90 · 25 @180 · 15 @270 · 10 on **Offer of Possession** | PLC → 90-day row, IFMS → 180-day row, CAM → 270-day row |
| `PP2/FPP/10-90` | PP2 | 10 on booking · 90 @60 days | all charges → 60-day row |
| `PP3/FPP/50-25-15-10` | PP3 | 50 on booking · 25 @30 · 15 @60 · 10 @90 | PLC → 30-day, IFMS → 60-day, CAM → 90-day |
| `PP4/FPP/100` | PP4 | 100 on booking | all charges on booking |

**Never hard-code these.** Always render what `GET /payment-plans` returns. Admins can add more plans (PP5, PP6, …), change one (new version), or switch one off from the admin panel at any time; see `ADMIN_PAYMENT_PLANS_API.md`.

**Calculation (done by the backend; shown here for reference):**
- **Total** = Sale Price + PLC + IFMS + CAM.
- **Each row** = Sale Price × % (rounded to the rupee) + that row's charge. The last row absorbs the rounding difference.
- **Due date** = booking date + days. The "On Offer of Possession" row has **no date** (`due_date: null`) until the company announces possession.

## 2. End-to-end flow

```
Pricing step
  GET  /payment-plans?project_id=suraksha-enclave       -> dropdown + detail cards
  POST /payment-plans/preview   (on every price/plan change, debounce ~300 ms)
        -> rows, total, booking_amount
Payment step
  POST /payments/create-order   (Razorpay)        } send payment_plan_code + prices;
  POST /payments/cash           (cash/RTGS/cheque) } amount must == preview.booking_amount
Generate step
  POST /documents/project-booking-application   form_data.payment_plan_code + total_amount
Profile
  GET  /customer/profile    -> bookings[].payment_plan, bookings[].payment_schedule (with charges)
  GET  /documents/{id}/demand-letter
```

## 3. `GET /payment-plans`: dropdown and details (public, no login)

```
GET /payment-plans?project_id=suraksha-enclave
```

**Response 200** (one plan shown; the real response has all of them in display order):

```json
{
  "count": 4,
  "plans": [
    {
      "code": "PP1/FPP/10-15-25-25-15-10",
      "short_code": "PP1",
      "name": "Flexible Payment Plan 10-15-25-25-15-10",
      "description": "6 instalments over about a year: 10% on booking, then 15%, 25% (+PLC), 25% (+IFMS), 15% (+CAM), and the final 10% on Offer of Possession.",
      "project_id": "suraksha-enclave",
      "version": 1,
      "booking_percent": 10,
      "instalments": [
        {"seq": 1, "label": "On Booking", "percent": 10, "due_trigger": "booking", "due_days": 0, "due_text": "On booking", "charge_head": null},
        {"seq": 2, "label": "Within 45 days of booking", "percent": 15, "due_trigger": "booking", "due_days": 45, "due_text": "45 days after booking", "charge_head": null},
        {"seq": 3, "label": "Within 90 days of booking", "percent": 25, "due_trigger": "booking", "due_days": 90, "due_text": "90 days after booking", "charge_head": "PLC"},
        {"seq": 4, "label": "Within 180 days of booking", "percent": 25, "due_trigger": "booking", "due_days": 180, "due_text": "180 days after booking", "charge_head": "IFMS"},
        {"seq": 5, "label": "Within 270 days of booking", "percent": 15, "due_trigger": "booking", "due_days": 270, "due_text": "270 days after booking", "charge_head": "CAM"},
        {"seq": 6, "label": "On Offer of Possession", "percent": 10, "due_trigger": "possession", "due_days": 0, "due_text": "On Offer of Possession", "charge_head": null}
      ],
      "example": {
        "sale_price": 5000000, "plc": 100000, "ifms": 50000, "cam": 75000,
        "total": 5225000, "booking_amount": 500000,
        "rows": [
          {"seq": 1, "label": "On Booking", "percent": 10, "due_text": "On booking", "basic_amount": 500000, "charge_head": null, "charge_amount": 0, "amount": 500000},
          {"seq": 2, "label": "Within 45 days of booking", "percent": 15, "due_text": "45 days after booking", "basic_amount": 750000, "charge_head": null, "charge_amount": 0, "amount": 750000},
          {"seq": 3, "label": "Within 90 days of booking", "percent": 25, "due_text": "90 days after booking", "basic_amount": 1250000, "charge_head": "PLC", "charge_amount": 100000, "amount": 1350000},
          {"seq": 4, "label": "Within 180 days of booking", "percent": 25, "due_text": "180 days after booking", "basic_amount": 1250000, "charge_head": "IFMS", "charge_amount": 50000, "amount": 1300000},
          {"seq": 5, "label": "Within 270 days of booking", "percent": 15, "due_text": "270 days after booking", "basic_amount": 750000, "charge_head": "CAM", "charge_amount": 75000, "amount": 825000},
          {"seq": 6, "label": "On Offer of Possession", "percent": 10, "due_text": "On Offer of Possession", "basic_amount": 500000, "charge_head": null, "charge_amount": 0, "amount": 500000}
        ]
      }
    }
  ]
}
```

**UI:**
- Dropdown label: `short_code — name` (e.g. "PP1 — Flexible Payment Plan 10-15-25-25-15-10").
- Detail card: `description`, plus a table of `instalments` (Label | % | When = `due_text` | + Charge = `charge_head`).
- Add an "Example on ₹50,00,000" table from `example.rows`, with the line: *"Example: Sale price ₹50,00,000 + PLC ₹1,00,000 + IFMS ₹50,000 + CAM ₹75,000 = ₹52,25,000."*
- `charge_head` is `PLC`, `IFMS`, `CAM`, `ALL` (all three) or `null`.

### Single plan

```
GET /payment-plans/PP1/FPP/10-15-25-25-15-10
```

This returns the same object as one entry of `plans`. The slashes in the code are part of the URL path; **don't URL-encode them**.

The short code also works, in any letter case: `GET /payment-plans/pp1`, and `"plan_code": "PP1"` in preview and payments. The server always answers with, and stores, the full code.

## 4. `POST /payment-plans/preview`: exact instalments (public)

Call this whenever the plan or any price changes.

```http
POST /payment-plans/preview
Content-Type: application/json

{
  "plan_code": "PP1/FPP/10-15-25-25-15-10",
  "sale_price": 5000000,
  "plc_amount": 100000,
  "ifms_amount": 50000,
  "cam_amount": 75000,
  "project_id": "suraksha-enclave"
}
```

`booking_date` (`YYYY-MM-DD`) is optional and defaults to today in IST. The real booking date is fixed by the server when the booking payment is made.

**Response 200:**

```json
{
  "plan": {"code": "PP1/FPP/10-15-25-25-15-10", "short_code": "PP1", "name": "Flexible Payment Plan 10-15-25-25-15-10", "version": 1},
  "sale_price": 5000000, "plc": 100000, "ifms": 50000, "cam": 75000,
  "total": 5225000,
  "booking_amount": 500000,
  "booking_date": "2026-11-09",
  "rows": [
    {"seq": 1, "label": "On Booking", "percent": 10, "due_trigger": "booking", "due_days": 0, "due_text": "On booking", "due_date": "2026-11-09", "basic_amount": 500000, "charge_head": null, "charge_amount": 0, "amount": 500000},
    {"seq": 2, "label": "Within 45 days of booking", "percent": 15, "due_trigger": "booking", "due_days": 45, "due_text": "45 days after booking", "due_date": "2026-12-24", "basic_amount": 750000, "charge_head": null, "charge_amount": 0, "amount": 750000},
    {"seq": 3, "label": "Within 90 days of booking", "percent": 25, "due_trigger": "booking", "due_days": 90, "due_text": "90 days after booking", "due_date": "2027-02-07", "basic_amount": 1250000, "charge_head": "PLC", "charge_amount": 100000, "amount": 1350000},
    {"seq": 4, "label": "Within 180 days of booking", "percent": 25, "due_trigger": "booking", "due_days": 180, "due_text": "180 days after booking", "due_date": "2027-05-08", "basic_amount": 1250000, "charge_head": "IFMS", "charge_amount": 50000, "amount": 1300000},
    {"seq": 5, "label": "Within 270 days of booking", "percent": 15, "due_trigger": "booking", "due_days": 270, "due_text": "270 days after booking", "due_date": "2027-08-06", "basic_amount": 750000, "charge_head": "CAM", "charge_amount": 75000, "amount": 825000},
    {"seq": 6, "label": "On Offer of Possession", "percent": 10, "due_trigger": "possession", "due_days": 0, "due_text": "On Offer of Possession", "due_date": null, "basic_amount": 500000, "charge_head": null, "charge_amount": 0, "amount": 500000}
  ]
}
```

**What to use from the response:**

| Use | Field |
|---|---|
| Booking amount shown and charged | **`booking_amount`**. This replaces `BOOKING_SHARE = 0.1` and `bookingAmountFor()` in `CustomerApplicationPage.tsx` and `RecordBookingPaymentModal.tsx`. |
| Total Plot Amount | **`total`**. Set `form.totalPlotAmount` from it; don't let the user edit it separately any more. |
| Due-date column | `due_date`. When it is `null`, show `due_text` ("On Offer of Possession"). |
| Amount column | `amount`, with an optional breakdown "₹12,50,000 + PLC ₹1,00,000" from `basic_amount` + `charge_head` / `charge_amount`. |

## 5. Booking payment: send the plan with the payment

Add these fields to **both** payment calls when `purpose` is `plot_booking`:

| Field | Type | Notes |
|---|---|---|
| `payment_plan_code` | string | from the dropdown |
| `sale_price` | number > 0 | Basic Sale Price |
| `plc_amount` | number ≥ 0 | 0 if none |
| `ifms_amount` | number ≥ 0 | 0 if none (new form input) |
| `cam_amount` | number ≥ 0 | 0 if none (new form input) |

Rules:
- `amount` **must equal** `preview.booking_amount`. The server allows a difference of up to ₹1 for rounding.
- **`inventory_id` is required** whenever plan fields are sent. The plot decides which plans are allowed (its project) and bounds the price.
- The server checks the declared Basic Sale Price against the plot's area. The implied rate must be between ₹5,000 and ₹5,00,000 per sq. yd (configurable on the server); otherwise the request fails with `sale_price_out_of_range`. The admin also sees the declared plan and prices on the Booking Detail screen at KYC review.

### Razorpay

```http
POST /payments/create-order
Authorization: Bearer <customer token>

{
  "amount": 500000,
  "purpose": "plot_booking",
  "inventory_id": "e976d812-5e66-4482-b8e6-9afc5d67465d",
  "payment_plan_code": "PP1/FPP/10-15-25-25-15-10",
  "sale_price": 5000000,
  "plc_amount": 100000,
  "ifms_amount": 50000,
  "cam_amount": 75000
}
```

The response is unchanged:

```json
{"payment_id": "…", "razorpay_order_id": "order_…", "razorpay_key_id": "rzp_…", "amount": 500000.0, "amount_paise": 50000000, "currency": "INR", "status": "created"}
```

### Cash / RTGS-NEFT / Cheque

`POST /payments/cash` takes the same plan fields, plus the existing `method`, `utr_number`, `cheque_number` and `note`.

> **Important: cheque / DD.** The wizard currently has a "manual offline" path (Cheque / DD / Online transfer / Other) that never calls the backend, so the plan never reaches the server. For cheque, call `POST /payments/cash` with `method: "cheque"` and `cheque_number`, so the plan is recorded. RTGS works the same way with `method: "rtgs_neft"`.

**Errors (400 unless noted):**

| `detail` | Meaning / UI message |
|---|---|
| `booking_amount_mismatch` | Amount isn't the plan's first instalment. Re-run preview and use `booking_amount`. |
| `payment_plan_not_found` | Unknown or retired plan code. Reload the plans list. |
| `payment_plan_not_for_project` | That plan isn't offered for this plot's project. |
| `inventory_id_required` | Plan fields sent without `inventory_id`. |
| `unit_not_found` | The `inventory_id` doesn't exist. |
| `plot_lookup_failed` | The server couldn't read the plot right now. Ask the customer to retry. |
| `sale_price_out_of_range` | "Please check the Basic Sale Price for this plot." |
| `sale_price_too_small` | The price is so small an instalment rounds to ₹0. Same message as above. |
| `total_amount_too_large` | Total above ₹50 crore (a typo). |
| `invalid_sale_price` / `invalid_plc_amount` / `invalid_ifms_amount` / `invalid_cam_amount` | Bad price input. |
| `payment_plan_only_for_booking` | Plan fields sent on a non-booking payment. |
| `unit_not_available` (409) | Plot no longer available (unchanged). |
| `amount_too_large` | A single payment above ₹1 crore (existing limit). Can happen with PP4 on an expensive plot; book with another plan or pay offline via `/payments/cash`. |

If `payment_plan_code` is omitted, the old behaviour applies (no plan). Always send it for new bookings.

## 6. Upload the booking application

There's no new request field beyond `form_data`. Put the chosen plan and the preview total into `form_data`, which `serializeFormData` already sends:

```json
"form_data": {
  "...existing keys...": "...",
  "payment_plan_code": "PP1/FPP/10-15-25-25-15-10",
  "basicSalePrice": "5000000",
  "plcPrice": "100000",
  "ifmsAmount": "50000",
  "camAmount": "75000",
  "total_amount": 5225000
}
```

The backend takes the plan and prices **from the booking payment** (what was paid against), not from `form_data`. If `form_data.payment_plan_code` or `total_amount` disagree with the payment, the upload is refused with `plan_mismatch`.

**Response.** `payment_plan` now carries the plan schedule; this replaces the old 10/15/25/25/25 split:

```json
{
  "id": "4b0f…", "document_type": "project_booking_application", "status": "generated",
  "payment_plan": {
    "total_receivable": 5225000, "total_received": 500000, "total_outstanding": 4725000,
    "total_outstanding_words": "Forty-Seven Lakh Twenty-Five Thousand Rupees",
    "booking_date": "2026-11-09",
    "rows": [
      {"seq": 1, "label": "On Booking", "percent": 10, "due_date": "2026-11-09", "basic_amount": 500000, "charge_head": null, "charge_amount": 0, "amount": 500000, "due_trigger": "booking", "due_text": "On booking", "due_days": 0, "status": "paid"},
      {"seq": 3, "label": "Within 90 days of booking", "percent": 25, "due_date": "2027-02-07", "basic_amount": 1250000, "charge_head": "PLC", "charge_amount": 100000, "amount": 1350000, "due_trigger": "booking", "due_text": "90 days after booking", "due_days": 90, "status": "due"},
      {"seq": 6, "label": "On Offer of Possession", "percent": 10, "due_date": null, "basic_amount": 500000, "charge_head": null, "charge_amount": 0, "amount": 500000, "due_trigger": "possession", "due_text": "On Offer of Possession", "due_days": 0, "status": "due"}
    ],
    "payment_plan": {"code": "PP1/FPP/10-15-25-25-15-10", "short_code": "PP1", "name": "Flexible Payment Plan 10-15-25-25-15-10", "version": 1,
                     "sale_price": 5000000, "plc": 100000, "ifms": 50000, "cam": 75000, "total": 5225000, "booking_amount": 500000}
  }
}
```

(Rows abbreviated.)

**New upload errors:**

| Status | `detail` | Meaning |
|---|---|---|
| 409 | `booking_application_exists` | This booking payment already has an application. **Treat as success**, e.g. on a retry, then reload `GET /customer/profile`. |
| 400 | `plan_mismatch` | `form_data` plan/total differs from what was paid. |
| 400 | `payment_not_for_booking` | The `payment_id` is an instalment payment. |

## 7. Customer profile

`GET /customer/profile` changes:

- `bookings[]`:
  - `payment_plan` **new**: `{code, short_code, name, version, sale_price, plc, ifms, cam, total, booking_amount}`
  - `total_consideration`: the plan total (sale price + charges)
- `payment_schedule[]` rows **new fields**:
  - `basic_amount`
  - `charge_head` (`PLC` | `IFMS` | `CAM` | `PLC+IFMS+CAM` | null)
  - `charge_amount`
  - `due_trigger` (`booking` | `possession`)

Example row:

```json
{"id": "4b0f…-m3", "label": "Within 90 days of booking", "percent": 25.0, "due_days": 90, "due_date": "2027-02-07",
 "amount": 1350000, "basic_amount": 1250000, "charge_head": "PLC", "charge_amount": 100000, "due_trigger": "booking",
 "status": "upcoming", "pay_enabled_from": "2026-11-09", "paid_on": null, "paid_payment_id": null}
```

**UI rules:**
- Show the plan name above the schedule: `payment_plan.short_code — payment_plan.name`.
- If `due_date` is `null` and `due_trigger` is `"possession"`, show **"On Offer of Possession"** and **don't offer a Pay button** for that row until it has a date.
- Show `amount` as the payable figure. Optionally add a breakdown line: "incl. PLC ₹1,00,000".
- Instalment payments are unchanged (`purpose: "installment"`, `installment_no`, `due_date`). The amount must equal the row's `amount`.
- Remove the local fallbacks that assume 10/15/25/25/25 (`DEFAULT_SPLIT`, `PAYMENT_SCHEDULE`) for plan bookings; always use the server rows.

## 8. Demand letter

This endpoint is unchanged:

```
GET /documents/{document_id}/demand-letter
Authorization: Bearer <customer token>
```

It returns a PDF.

- **What the letter shows now:**
  - **Head** column: "Basic Price" or "BSP + PLC/IFMS/CAM".
  - **Installment** = share of the sale price; **Total Installment** = with charges.
  - "On Possession" for the undated row.
  - Paid instalments are hidden. The overdue total counts only past-due unpaid instalments.
- **Caching:** the response carries `Cache-Control: no-store, private`, so don't cache it in a service worker.
- **Errors:**

  | Status | `detail` |
  |---|---|
  | 403 | `forbidden` (not your booking) |
  | 404 | `not_found` |
  | 409 | `payment_details_pending` |
  | 500 | `demand_letter_failed` |

## 9. Admin panel

### 9a. Booking Detail shows the chosen plan

`GET /admin/bookings/{booking_id}` now includes:
- `payment_plan`: `{code, short_code, name, version, sale_price, plc, ifms, cam, total, booking_amount}`, or `null` for older bookings without a plan;
- per `payment_schedule` row: `basic_amount`, `charge_head`, `charge_amount`, `due_trigger`.

Show the plan and the declared Sale Price / PLC / IFMS / CAM in the KYC review panel, so the admin can verify the price before approving.

### 9b. Offer of Possession date

When the company offers possession, an admin sets the date once per project. Every unpaid "On Offer of Possession" instalment of that project then gets that due date and appears in the letters and reminders.

```http
PUT /admin/projects/suraksha-enclave/possession-date
Authorization: Bearer <admin token>

{"possession_offer_date": "2028-03-01"}
```

**Response 200:**

```json
{"project_id": "suraksha-enclave", "possession_offer_date": "2028-03-01", "instalments_dated": 42}
```

| Status | Meaning |
|---|---|
| 401 / 403 | Not an admin |
| 400 | `invalid_possession_date` / `invalid_project_id` |

## 10. Rules and corner cases (please implement all)

| # | Situation | What happens / what to do |
|---|---|---|
| 1 | Customer changes plan or any price **before paying** | Call preview again. Use the new `booking_amount` and `total`. Nothing is stored until payment. |
| 2 | Customer wants to change plan **after the booking payment** | Not supported. The plan and prices are locked by the payment. Hide the plan selector once a booking payment exists for this plot; changes go through the office. |
| 3 | Preview fails or is still loading | Disable the payment buttons. Never fall back to 10%. |
| 4 | Paise in prices (e.g. 25,00,000.50) | Server rounds each price half-up to the rupee. Send numbers; show `preview` values, not your own maths. |
| 5 | All charges 0 | Fine: `charge_head` is `null` on every row and `total` = sale price. |
| 6 | PP4 (100%) | Booking amount = full total including charges. If it exceeds ₹1 crore, an online order fails (`amount_too_large`); offer cash/RTGS/cheque via `/payments/cash`. |
| 7 | Plan list changes (new plan / new version) | Always load from the API. Plans the customer already paid against keep their original terms (versioned). The list may take up to 60 s to refresh after an update. |
| 8 | Plan retired between preview and payment | Order returns `payment_plan_not_found`. Reload plans and ask the customer to choose again. |
| 9 | Upload retried after a network error | A 409 `booking_application_exists` means it already succeeded. Reload the profile. |
| 10 | `form_data.total_amount` ≠ the total paid against | 400 `plan_mismatch`. Always send `preview.total` from the same preview used for payment. |
| 11 | "On Offer of Possession" row | `due_date: null`, no Pay button, never overdue, no reminder. It gets a date (and then reminders) once the admin sets the project's possession date. |
| 12 | Instalment payments | Unchanged: pay rows in order; amount = that row's `amount` (charges included). |
| 13 | Temporary-password customers (imported buyers) | `POST /customer/login` returns `must_change_password: true`, and the chatbot login response also carries `must_change_password: true`. Show the "set new password" screen before anything else. |
| 14 | Legacy bookings (made before plans) | No `payment_plan` on the booking; the schedule shows as before. Don't show plan UI for them. |
| 15 | PLC / IFMS / CAM formulas | Calculated on the website. Round each to whole rupees before sending, and send 0 (not empty) when not applicable. Send the same values to preview, to the payment call and in `form_data`. |
| 16 | Plan switched off by an admin while the customer is on the form | Preview or payment returns `payment_plan_not_found`. Reload the dropdown and ask the customer to pick again. |

## 11. TypeScript types (suggested)

```ts
export interface PaymentPlanInstalment {
  seq: number; label: string; percent: number;
  due_trigger: 'booking' | 'possession'; due_days: number | null; due_text: string;
  charge_head: 'PLC' | 'IFMS' | 'CAM' | 'ALL' | null;
}
export interface PlanScheduleRow {
  seq: number; label: string; percent: number;
  due_trigger: 'booking' | 'possession'; due_days: number | null; due_text: string;
  due_date: string | null; basic_amount: number; charge_head: string | null;
  charge_amount: number; amount: number; status?: 'paid' | 'due';
}
export interface PaymentPlan {
  code: string; short_code: string; name: string; description: string | null;
  project_id: string | null; version: number; booking_percent: number;
  instalments: PaymentPlanInstalment[];
  example: { sale_price: number; plc: number; ifms: number; cam: number; total: number;
             booking_amount: number; rows: Omit<PlanScheduleRow, 'due_trigger' | 'due_days' | 'due_date'>[] };
}
export interface PaymentPlanPreview {
  plan: { code: string; short_code: string; name: string; version: number };
  sale_price: number; plc: number; ifms: number; cam: number;
  total: number; booking_amount: number; booking_date: string | null; rows: PlanScheduleRow[];
}
```

## 12. Checklist for the frontend team

**Booking form state and Pricing step**
- [ ] `documentStore.ts`: add `paymentPlanCode`, `ifmsAmount` and `camAmount` to `BookingApplicationFormData` (default `''`).
- [ ] Pricing step: plan dropdown + detail card (section 3); IFMS and CAM inputs; call preview on change (section 4).
- [ ] Replace the fixed "Construction Linked Plan" banner and the fixed plan list (wizard page 10 and `applicationPdf.ts` `paymentPlanRows`) with the selected plan's preview rows.
- [ ] Booking amount = `preview.booking_amount` everywhere (wizard, `RecordBookingPaymentModal`, "10%" wording).

**Payments and upload**
- [ ] Send plan fields on `/payments/create-order` and `/payments/cash` (section 5). Route cheque through `/payments/cash`.
- [ ] Block the payment buttons until a plan is chosen and a preview has loaded.
- [ ] Upload: put `payment_plan_code` and `total_amount = preview.total` in `form_data`; treat a 409 `booking_application_exists` as success.

**Profile and errors**
- [ ] Profile: plan name, charge breakdown, "On Offer of Possession" with no Pay button while undated (section 7).
- [ ] Map the new error codes (sections 5, 6 and 8) to the messages above.
- [ ] Send `inventory_id` with every plan booking payment.
- [ ] Lock the plan selector once the booking payment is made (section 10, row 2).
- [ ] Admin panel: show `payment_plan` and the declared prices on Booking Detail (section 9a).
