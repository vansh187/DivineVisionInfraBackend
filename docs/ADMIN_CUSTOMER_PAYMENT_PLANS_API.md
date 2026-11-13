# Admin: customers' payment plans and demand letters

**For:** admin panel developers. **Backend base URL:** `https://divinevisioninfrabackend-301k.onrender.com`.
All endpoints need `Authorization: Bearer <admin token>`.

## What this adds

- The **Customers list** shows which payment plan each customer's plots are on.
- A **customer page** lists each of the customer's plots with its payment plan, booking date, prices, amount received, instalment schedule, and a **Demand Letter** button.
- For an **offline buyer** (a plot loaded from the client's sheets), the admin links the payment plan and booking date, then the prices and the amount received to date. A **website booking** already has these from its payment, so they can't be edited.

A plot moves through three states (`terms_status`):

| `terms_status` | Meaning | Demand letter |
|---|---|---|
| `plan_needed` | No payment plan linked yet. | Not available |
| `price_needed` | Plan and booking date linked; prices still missing. | Not available (409 `payment_details_pending`) |
| `complete` | Prices entered; schedule built and paid instalments marked. | Available |

## 1. Customers list: new field

`GET /admin/customers` (unchanged otherwise). Each item now has:

```json
{ "id": "C00123", "full_name": "Rohit Mehra", "...": "...", "payment_plans": ["PP1"] }
```

It also has `plot_plans`, one entry per plot the customer holds now, in plot-number order:

```json
"plot_plans": [
  { "project_name": "Suraksha Enclave", "unit_number": "D7", "payment_plan": "PP1", "applicant_name": "Mr. Rohit Mehra" },
  { "project_name": "Suraksha Enclave", "unit_number": "D8", "payment_plan": "PP3", "applicant_name": "Mr. Vikas Rao" },
  { "project_name": "Suraksha Enclave", "unit_number": "D9", "payment_plan": null, "applicant_name": "Mr. Rohit Mehra" }
]
```

- **One account per email.** Buyers who share an email share one login account, but **each plot keeps its own applicant**. Show `applicant_name` per plot (it can differ from the account's `full_name`). Each plot's demand letter is addressed to that plot's applicant.
- `plot_plans` tells you **which plot is on which plan**. Show it as e.g. "D7 · PP1, D8 · PP3, D9 · —". `payment_plan` is `null` while no plan is linked to that plot.
- `payment_plans` holds the distinct short codes across those plots (`["PP1", "PP3"]`). It's `[]` when no plot has a plan yet.

## 2. Customer page

`GET /admin/customers/{customer_id}` → `200`, or `404 not_found`.

```json
{
  "id": "C00123", "full_name": "Rohit Mehra", "email": null, "phone": "9000000102",
  "source": "OFFLINE_IMPORT",
  "plots": [{
    "document_id": "5b0c…",              // use in the URLs below
    "record_type": "offline",            // offline | website
    "project_name": "Suraksha Enclave", "unit_number": "D8", "applicant_name": "Mr. Rohit Mehra",
    "co_applicant_names": [],             // the plot's first and second co-applicant, when given
    "payment_plan": { "code": "PP1/FPP/10-15-25-25-15-10", "short_code": "PP1",
                      "name": "Flexible Payment Plan 10-15-25-25-15-10", "version": 1 },
    "booking_date": "2026-04-18",
    "sale_price": 5000000, "plc": 100000, "ifms": 50000, "cam": 75000, "total": 5225000,
    "amount_received": 1350000, "outstanding": 3875000,
    "terms_status": "complete",
    "terms_editable": true,              // false for a website booking
    "demand_letter_available": true,
    "schedule": [
      { "milestone_no": 1, "label": "On Booking", "percent": 10, "due_date": "2026-04-18",
        "amount": 500000, "status": "paid" },
      { "milestone_no": 6, "label": "On Offer of Possession", "percent": 10, "due_date": null,
        "amount": 500000, "status": "upcoming" }
    ]
  }]
}
```

- `status` is one of `paid`, `due`, `overdue` or `upcoming`.
- `due_date` is `null` for an instalment whose event (such as the offer of possession) has no date yet.
- Price fields, `amount_received`, `outstanding` and `schedule` are `null` or `[]` until prices are entered.

## 3. Link a plan, booking date and prices (offline plots only)

`PUT /admin/customers/{customer_id}/plots/{document_id}/payment-terms`

```json
{
  "payment_plan": "PP1",               // plan code, short code, or the client's label "PL-10-15-25-25-15-10"
  "booking_date": "2026-04-18",        // YYYY-MM-DD, not in the future
  "sale_price": 5000000,               // optional: Basic Sale Price (INR)
  "plc": 100000, "ifms": 50000, "cam": 75000,   // optional, default 0
  "amount_received": 1350000           // optional: total the buyer has paid to date
}
```

- **Without `sale_price`:** only the plan and booking date are saved, and the plot shows `price_needed`. Sending a later call with prices completes it.
- **With `sale_price`:** the schedule is built from the plan. `amount_received` marks instalments paid, oldest first. A remainder that only partly covers the next instalment is credited on the demand letter.
- **Sending it again** replaces the plot's terms.

Returns the updated plot (the same shape as one entry of `plots` above).

| Status | `detail` | Meaning, as you might word it for the admin |
|---|---|---|
| 404 | `plot_not_found` | This plot doesn't belong to this customer. |
| 409 | `terms_from_website_booking` | A website booking's plan comes from its payment and can't be changed here. |
| 409 | `schedule_has_payments` | An instalment of this plot was already paid in the app, so its terms are locked. |
| 400 | `payment_plan_not_found` | No active plan matches this name. |
| 400 | `payment_plan_ambiguous` | More than one plan matches these percentages. Choose the plan by its code. |
| 400 | `payment_plan_not_for_project` | This plan belongs to another project. |
| 400 | `booking_date_in_future` / `invalid_booking_date` | Check the booking date. |
| 400 | `sale_price_required` | Enter the sale price before PLC / IFMS / CAM or the amount received. |
| 400 | `amount_received_exceeds_total` | The amount received is more than the plot's total. |
| 400 | `sale_price_too_small` / `total_amount_too_large` / `invalid_sale_price` / `invalid_plc_amount` / `invalid_ifms_amount` / `invalid_cam_amount` / `invalid_amount_received` | Check the amounts. |

## 4. Demand letter

`GET /admin/customers/{customer_id}/plots/{document_id}/demand-letter` → `200 application/pdf` (download).

This is the same letter the customer downloads. It returns `409 payment_details_pending` while the plot has no prices, so show the button only when `demand_letter_available` is `true`.

## Notes

- **No reminder emails** go to these offline buyers. Admins send the letters themselves.
- **Admin Payment Plans list:** offline plots linked to a plan now count in its `plots_count`.
- **Bulk load:** the backend team can load the client's sheet with `scripts/link_offline_payment_plans.py`, which writes the same data as this endpoint.
