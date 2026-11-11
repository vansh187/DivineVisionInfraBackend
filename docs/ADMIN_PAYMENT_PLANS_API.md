# Admin Panel: Payment Plan Management

**Audience:** admin panel team.
**Backend base URL:** `https://divinevisioninfrabackend-301k.onrender.com`

**Auth:** every endpoint needs an admin token (`Authorization: Bearer <admin access token>`, the same one the admin panel already uses). Without one you get 401; refresh tokens are refused.

Use these endpoints to build an **"Add payment plan"** screen, so the team can add **any** plan (PP5, PP6, …), change a plan, or switch one off, with no developer involved. New plans appear in the customer booking dropdown within about a minute.

## 1. Rules the screen must explain to the admin

| Rule | What it means for the form |
|---|---|
| Percentages add up to **exactly 100** | Show a live total under the rows; disable Save until it reads 100. Up to 3 decimals are allowed (e.g. 33.333 + 33.333 + 33.334). |
| The **first row is the booking payment** | The customer always pays row 1 to book, and it is marked paid at booking. Its "Counted from" and days can be anything; they only change the wording shown. |
| Each charge goes on **one row** | "Add charges" options: `PLC`, `IFMS`, `CAM`, `PLC+IFMS`, `PLC+CAM`, `IFMS+CAM`, `ALL` (all three). Each of PLC, IFMS and CAM may appear on at most one row, counting pairs and ALL (e.g. `PLC+IFMS` on one row and `PLC` on another is rejected). A charge not placed on any row is automatically added to the **last** row. |
| Due timing ("Counted from") | `booking` + days (0–3650); `possession` + days (normally 0 = "On Offer of Possession", no date until the project's possession date is set); or **`custom`**: any event the admin types (e.g. "Completion of plinth"), sent in `due_from` (1–100 characters). A custom row never gets a date: it shows as "30 days after Completion of plinth", is never overdue, gets no reminders and is never demanded in a letter. Instalments after it with a date are reminded and can be paid normally. |
| Up to 24 instalments | |
| A plan in use is never edited | To change instalments, save a **new version**: it becomes active, and customers who already booked keep the version they paid for. Name, description, display order and on/off can be edited at any time. |
| Codes | `code`: 2–80 characters, letters, digits and `/ _ . -`, e.g. `PP5/FPP/20-30-50`; it can't change later. `short_code`: 1–20 characters, letters, digits and `-`, e.g. `PP5`; it must be unique among active plans. |
| Project | `project_id` slug, e.g. `suraksha-enclave` or `ops-divine-greens`; leave empty to offer the plan for every project. |

**How amounts are calculated** (the backend does this; show the example the API returns):
- Row amount = Basic Sale Price × % (rounded to the rupee; the last row absorbs the rounding difference) + that row's charge(s).
- Total = Sale Price + PLC + IFMS + CAM.

## 2. Endpoints

| Method | URL | Purpose |
|---|---|---|
| GET | `/admin/payment-plans?project_id=` | All plans, including inactive and older versions |
| POST | `/admin/payment-plans/validate` | Check a plan and get its worked example; **saves nothing** ("Preview" button) |
| POST | `/admin/payment-plans` | Add a new plan |
| POST | `/admin/payment-plans/versions` | Change a plan's instalments (new version) |
| PATCH | `/admin/payment-plans` | Edit name/description/order, or switch on/off |
| PUT | `/admin/projects/{project_id}/possession-date` | Set the Offer of Possession date (see the frontend guide, section 9b) |

Plan codes contain `/`, so the **code is sent in the JSON body**, never in the URL.

## 3. Add a plan: `POST /admin/payment-plans`

Request:

```json
{
  "code": "PP5/FPP/20-30-50",
  "short_code": "PP5",
  "name": "Flexible Payment Plan 20-30-50",
  "description": "20% on booking, 30% in 60 days, 50% in 120 days.",
  "project_id": "suraksha-enclave",
  "sort_order": 5,
  "instalments": [
    {"label": "On Booking", "percent": 20, "due_trigger": "booking", "due_days": 0, "charge_head": null},
    {"label": "Within 60 days of booking", "percent": 30, "due_trigger": "booking", "due_days": 60, "charge_head": "PLC"},
    {"label": "Within 120 days of booking", "percent": 50, "due_trigger": "booking", "due_days": 120, "charge_head": "IFMS"}
  ]
}
```

| Instalment field | Values |
|---|---|
| `label` | 1–150 characters, shown to the customer |
| `percent` | > 0, ≤ 100, up to 3 decimals |
| `due_trigger` | `booking` (default), `possession` or `custom` |
| `due_days` | 0–3650 (default 0) |
| `due_from` | **required when `due_trigger` is `custom`**: the event name, 1–100 characters (e.g. "Completion of plinth"). Ignored for other triggers |
| `charge_head` | `null`, `"PLC"`, `"IFMS"`, `"CAM"`, `"PLC+IFMS"`, `"PLC+CAM"`, `"IFMS+CAM"` or `"ALL"`. Exact values only: pairs always in the order PLC, IFMS, CAM, joined by `+` with no spaces. `"CAM+PLC"`, `"PLC + IFMS"` or lower case → 422 |

Row order is the order you send; `seq` is assigned 1, 2, 3, ….

Response **201** (`POST /admin/payment-plans/validate` returns the same shape, without saving):

```json
{
  "code": "PP5/FPP/20-30-50",
  "short_code": "PP5",
  "name": "Flexible Payment Plan 20-30-50",
  "description": "20% on booking, 30% in 60 days, 50% in 120 days.",
  "project_id": "suraksha-enclave",
  "version": 1,
  "is_active": true,
  "sort_order": 5,
  "valid": true,
  "booking_percent": 20,
  "instalments": [
    {"seq": 1, "label": "On Booking", "percent": 20, "due_trigger": "booking", "due_days": 0, "due_text": "On booking", "charge_head": null},
    {"seq": 2, "label": "Within 60 days of booking", "percent": 30, "due_trigger": "booking", "due_days": 60, "due_text": "60 days after booking", "charge_head": "PLC"},
    {"seq": 3, "label": "Within 120 days of booking", "percent": 50, "due_trigger": "booking", "due_days": 120, "due_text": "120 days after booking", "charge_head": "IFMS"}
  ],
  "example": {
    "sale_price": 5000000, "plc": 100000, "ifms": 50000, "cam": 75000,
    "total": 5225000, "booking_amount": 1000000,
    "rows": [
      {"seq": 1, "label": "On Booking", "percent": 20, "due_text": "On booking", "basic_amount": 1000000, "charge_head": null, "charge_amount": 0, "amount": 1000000},
      {"seq": 2, "label": "Within 60 days of booking", "percent": 30, "due_text": "60 days after booking", "basic_amount": 1500000, "charge_head": "PLC", "charge_amount": 100000, "amount": 1600000},
      {"seq": 3, "label": "Within 120 days of booking", "percent": 50, "due_text": "120 days after booking", "basic_amount": 2500000, "charge_head": "IFMS+CAM", "charge_amount": 125000, "amount": 2625000}
    ]
  },
  "created_date": "2026-11-09T10:15:00+00:00",
  "last_updated_date": "2026-11-09T10:15:00+00:00"
}
```

Note the last row: CAM was not placed on any row, so it was added to the last row (`IFMS+CAM`). Show this example to the admin before they confirm.

### Two charges on one row ("PLC + IFMS")

```json
"instalments": [
  { "label": "Booking Amount",            "percent": 10, "due_trigger": "booking",    "due_days": 0,  "charge_head": null },
  { "label": "Within 45 days of booking", "percent": 40, "due_trigger": "booking",    "due_days": 45, "charge_head": "PLC+IFMS" },
  { "label": "On Offer of Possession",    "percent": 50, "due_trigger": "possession", "due_days": 0,  "charge_head": "CAM" }
]
```

Worked example in the response:

| Row | Basic | Charge | Amount |
|---|---|---|---|
| 1 | 5,00,000 | - | 5,00,000 |
| 2 | 20,00,000 | `PLC+IFMS` 1,50,000 | 21,50,000 |
| 3 | 25,00,000 | `CAM` 75,000 | 25,75,000 |

The same pattern applies to `PLC+CAM` (row 2 becomes 21,75,000) and `IFMS+CAM` (21,25,000). If the third charge isn't placed on any row, it goes to the last row. A pair on row 1 raises the booking amount (e.g. `PLC+IFMS` on a 10% row 1 gives 6,50,000).

### Custom "Counted from" event

Request row:

```json
{ "label": "On completion of plinth", "percent": 40, "due_trigger": "custom", "due_days": 30, "due_from": "Completion of plinth", "charge_head": null }
```

Returned row (`due_from` is `null` on booking and possession rows):

```json
{ "seq": 2, "label": "On completion of plinth", "percent": 40, "due_trigger": "custom", "due_days": 30, "due_from": "Completion of plinth", "due_text": "30 days after Completion of plinth", "charge_head": null }
```

With `due_days: 0` the wording is "On Completion of plinth".

## 4. Change a plan's instalments: `POST /admin/payment-plans/versions`

```json
{
  "code": "PP5/FPP/20-30-50",
  "instalments": [
    {"label": "On Booking", "percent": 20, "due_days": 0},
    {"label": "Within 90 days of booking", "percent": 80, "due_days": 90, "charge_head": "ALL"}
  ]
}
```

`short_code`, `name`, `description`, `project_id` and `sort_order` are optional; anything not sent is copied from the current version.

**Response 201** is the same shape as above, with `"version": 2, "is_active": true`. The previous version is now `is_active: false`.

## 5. Edit or switch off: `PATCH /admin/payment-plans`

```json
{"code": "PP5/FPP/20-30-50", "name": "Flexible Plan 20-30-50", "description": "…", "sort_order": 6, "is_active": false}
```

- Send only the fields you want to change.
- `is_active: false` removes the plan from the customer dropdown and from new bookings. Customers who already booked on it are not affected.
- `is_active: true` switches it back on, as long as its `short_code` isn't used by another active plan.

## 6. List: `GET /admin/payment-plans`

```json
{"count": 6, "plans": [ { …same shape as section 3… }, … ]}
```

- Every version is included, newest version first within each code.
- Suggested table columns: `short_code`, `name`, `version`, Active (`is_active`), `project_id`, booking % (`booking_percent`), last updated (`last_updated_date`).
- Group rows by `code`, showing older versions collapsed.
- `valid: false` is a safety flag: a stored plan that no longer passes validation. It shouldn't happen; if it does, report it to the developers.

## 7. Errors

| Status | `detail` | Show |
|---|---|---|
| 400 | `invalid_payment_plan` | "Percentages must add up to 100, and each charge (PLC, IFMS, CAM) can be on only one row." |
| 400 | `invalid_instalments` | "Check the instalment rows (label, percent up to 3 decimals, days 0–3650, and an event name for every custom 'Counted from')." |
| 400 | `invalid_plan_code` / `invalid_short_code` / `invalid_plan_name` / `invalid_project_id` / `invalid_sort_order` / `invalid_description` | Field-level message |
| 409 | `payment_plan_exists` | "A plan with this code already exists. Use 'New version' to change it." |
| 409 | `short_code_in_use` | "Another active plan already uses this short code." |
| 409 | `payment_plan_version_conflict` | "Someone saved a new version at the same time. Reload and try again." |
| 404 | `payment_plan_not_found` | "Plan not found." (PATCH / versions) |
| 422 | (validation list) | Missing or out-of-range field; show the field errors |
| 401 / 403 | | Session expired / not an admin |

## 8. Booking Detail rows and the bookings list

`GET /admin/bookings/{id}` → `payment_schedule[]` rows also carry `due_from` and `due_text`. Show `due_text` in the Due Date column whenever `due_date` is `null`:

```json
{ "id": "<booking-application-document-id>-m2", "label": "On completion of plinth", "percent": 40, "due_days": 30,
  "due_date": null, "due_trigger": "custom", "due_from": "Completion of plinth",
  "due_text": "30 days after Completion of plinth", "amount": 2000000, "basic_amount": 2000000,
  "charge_head": null, "charge_amount": 0, "status": "upcoming", "pay_enabled_from": "2026-11-11",
  "paid_on": null, "paid_payment_id": null }
```

`GET /admin/bookings` accepts **`customer_id`** for an exact match (e.g. `?customer_id=C12345&page_size=100`), alongside the existing `search`.

## 9. Suggested screen

1. **Plans list** (section 6), with buttons **Add plan**, **New version** and **Edit**.
2. **Add plan form:**
   - fields: code, short code, name, description, project (dropdown: Suraksha Enclave / OPS Divine Greens / All);
   - an instalment table with add/remove/reorder rows. Each row has: label, %, due (On booking + days | On possession + days) and charge (none / PLC / IFMS / CAM / ALL);
   - a live total of %.
3. **Preview** calls `/validate` and shows the `example` table. **Save** calls `POST /admin/payment-plans`.
