# Customer Profile API — Frontend Integration

`GET /customer/profile` — returns the signed-in customer's identity + current
booking snapshot for the account screen.

- **Auth:** `Authorization: Bearer <access_token>` — the customer JWT from
  `POST /customer/login`.
- **Query params:** none.
- **Body:** none.
- **Method:** `GET` only.

Every field except `customer_id` and `booking.has_booking` is optional and is
**omitted entirely** when the server has no value for it (nulls are not sent).
A partial payload is normal and expected — render what you get.

---

## Request

```
GET /customer/profile
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

---

## Response `200 OK` — full payload

Shown with every field populated. In practice you will usually get a subset.

```json
{
  "customer_id": "C00042",
  "first_name": "Rahul",
  "last_name": "Sharma",
  "full_name": "Rahul Sharma",
  "email": "rahul.sharma@example.com",
  "phone": "+91 98765 43210",
  "gender": "male",
  "date_of_birth": "1990-04-12",
  "age": 35,
  "address": {
    "line1": "Kothi No. 11, Ganeshwar Dham Road",
    "line2": "Karol Bagh",
    "city": "New Delhi",
    "state": "Delhi",
    "pincode": "110005"
  },
  "address_text": "Kothi No. 11, Ganeshwar Dham Road, Karol Bagh, New Delhi, Delhi 110005",
  "booking": {
    "has_booking": true,
    "project_id": "ops-divine-greens",
    "project_name": "OPS Divine Greens",
    "township_label": "OPS Divine Greens · Karnal",
    "unit_number": "204",
    "plot_area_sq_yd": "131.43",
    "unit_type": "Residential Plot",
    "booking_date": "2025-01-10",
    "total_consideration": 1774305,
    "amount_received": 700000,
    "payment_schedule": [
      { "label": "On Booking",                "percent": 10, "due_days": 0,   "due_date": "2025-01-10", "amount": 177431, "status": "paid" },
      { "label": "Within 45 days of booking", "percent": 15, "due_days": 45,  "due_date": "2025-02-24", "amount": 266146, "status": "paid" },
      { "label": "Within 90 days of booking", "percent": 25, "due_days": 90,  "due_date": "2025-04-10", "amount": 443576, "status": "due" },
      { "label": "Within 180 days of booking","percent": 25, "due_days": 180, "due_date": "2025-07-09", "amount": 443576, "status": "due" },
      { "label": "Within 270 days of booking","percent": 25, "due_days": 270, "due_date": "2025-10-07", "amount": 443577, "status": "due" }
    ]
  }
}
```

---

## Response `200 OK` — customer with no KYC and no booking

The common case right after signup. Only what the account row holds is returned.

```json
{
  "customer_id": "C00042",
  "first_name": "Rahul",
  "last_name": "Sharma",
  "full_name": "Rahul Sharma",
  "email": "rahul.sharma@example.com",
  "phone": "+91 98765 43210",
  "booking": {
    "has_booking": false
  }
}
```

- `gender`, `date_of_birth`, `age`, `address`, `address_text` are absent because
  they come from the customer's completed Aadhaar KYC, which hasn't happened.
- `booking` is always present. `has_booking: false` → no booking on record; show
  the "no booking yet" state.

---

## Response `200 OK` — payment recorded but no booking form on file

`amount_received` is the sum of the customer's confirmed payments; it can be
present even when there is no booking document yet.

```json
{
  "customer_id": "C00042",
  "first_name": "Rahul",
  "last_name": "Sharma",
  "full_name": "Rahul Sharma",
  "booking": {
    "has_booking": false,
    "amount_received": 250000
  }
}
```

---

## Response `200 OK` — booking present, no server-side payment schedule

`payment_schedule` is only returned when it exists on the booking form. When it
is absent, fall back to your local 10 / 15 / 25 / 25 / 25 split of
`total_consideration`.

```json
{
  "customer_id": "C00042",
  "first_name": "Rahul",
  "last_name": "Sharma",
  "full_name": "Rahul Sharma",
  "email": "rahul.sharma@example.com",
  "gender": "male",
  "date_of_birth": "1990-04-12",
  "age": 35,
  "booking": {
    "has_booking": true,
    "project_id": "ops-divine-greens",
    "project_name": "OPS Divine Greens",
    "unit_number": "204",
    "plot_area_sq_yd": "131.43",
    "unit_type": "Residential Plot",
    "booking_date": "2025-01-10",
    "total_consideration": 1774305,
    "amount_received": 700000
  }
}
```

---

## Field reference

### Top level

| Field | Type | Notes |
|---|---|---|
| `customer_id` | string | Always present. Opaque account id, format `C#####` (e.g. `C00042`). |
| `first_name` | string? | From the account; if blank, derived from the KYC name. |
| `last_name` | string? | Same. |
| `full_name` | string? | `"first last"`, or the KYC name. Prefer this for display. |
| `email` | string? | From the account. |
| `phone` | string? | From the account. Free-form; display as-is. |
| `gender` | string? | Lower-case: `male` / `female` / `transgender` / `other`; otherwise the raw value. Title-case in the UI. |
| `date_of_birth` | string? | ISO `YYYY-MM-DD`. Absent if DOB is unknown or only a birth year is on file. |
| `age` | integer? | Server-computed. Prefer over recomputing from `date_of_birth`. Can be present when `date_of_birth` is not (year-only DOB). |
| `address` | object? | Absent if no address parts are known. See below. |
| `address_text` | string? | Pre-formatted single line. Use verbatim when present. |
| `booking` | object | **Always present.** The single most-recent / active booking. See below. |
| `bookings` | array | **Additive.** `[]` when the customer holds no plot; otherwise lists **every** booking, newest first, each entry the same shape as `booking`. `booking` is identical to `bookings[0]`. Old clients that read only `booking` are unaffected. Each entry's `amount_received` / `payment_schedule` / `next_due` are scoped to that plot alone — two plots never share a figure. |

### `address`

| Field | Type | Notes |
|---|---|---|
| `line1` | string? | House / building + street. |
| `line2` | string? | Landmark / locality. |
| `city` | string? | |
| `state` | string? | |
| `pincode` | string? | 6-digit string. |

### `booking`

| Field | Type | Notes |
|---|---|---|
| `has_booking` | boolean | **Always present.** `false` → all other fields except possibly `amount_received` are absent. |
| `id` | string? | Stable booking selection id. Currently the booking application document id. |
| `document_id` | string? | Booking application document id; use for allotment / demand / booking PDF endpoints. |
| `inventory_id` | string? | Plot inventory row id; use to scope instalment payment actions to the selected plot. |
| `project_id` | string? | Machine id, e.g. `ops-divine-greens`. |
| `project_name` | string? | Display name. |
| `township_label` | string? | e.g. `"OPS Divine Greens · Karnal"`. |
| `unit_number` | string? | Plot / unit number. |
| `plot_area_sq_yd` | string? | **String**, keeps decimals, e.g. `"131.43"`. |
| `unit_type` | string? | e.g. `"Residential Plot"`. |
| `booking_date` | string? | ISO `YYYY-MM-DD`. Falls back to the booking document's creation date. |
| `total_consideration` | integer? | Whole rupees (server parses `₹` / commas / `lakh` / `crore` from the source form). |
| `amount_received` | integer? | Whole rupees. Confirmed payments to date. For a customer with a single booking this is their full paid balance; with multiple bookings it is scoped to **this booking's own** payment + paid milestones, never another plot's. |
| `payment_id` | string? | Original plot-booking payment id linked to the booking document. |
| `booking_payment_amount` | integer? | Original plot-booking payment amount in whole rupees. |
| `payment_method` | string? | e.g. `razorpay` / `cash`. |
| `razorpay_order_id` | string? | Present for Razorpay-backed booking payments. |
| `razorpay_payment_id` | string? | Present after Razorpay settlement. |
| `payment_created_date` | string? | ISO `YYYY-MM-DD` date of the linked booking payment. |
| `payment_schedule` | array? | Absent unless present on the booking form — then fall back to the local split. |

### `payment_schedule[]` row

| Field | Type | Notes |
|---|---|---|
| `label` | string? | e.g. `"On Booking"`. |
| `percent` | number? | Percent of `total_consideration`. |
| `due_days` | integer? | Days from booking date. |
| `due_date` | string? | ISO date. |
| `amount` | integer? | Whole rupees. |
| `status` | string? | e.g. `paid` / `due` / `overdue` — free text, don't hard-code. |

---

## Errors

Body is always `{ "detail": "<code>" }`.

| Status | `detail` | When | Suggested UI |
|---|---|---|---|
| 401 | `missing_token` | No / malformed `Authorization` header | Prompt re-login |
| 401 | `token_expired` | JWT expired | Prompt re-login |
| 401 | `invalid_token` | Bad signature / not a customer-or-broker token | Prompt re-login |
| 403 | `customer_only` | Valid token but not a customer (e.g. a broker token) | Show access message |
| 404 | `profile_not_found` | No account matches the token's subject | Treat as "not ready" → local fallback |
| 429 | `rate_limited` | > 10 requests/60s from the same IP to this path | Back off and retry |
| 500 | `internal_error` | Unexpected server error | Generic error / local fallback |

```json
{ "detail": "token_expired" }
```

---

## Notes for integration

- **Don't send a body or query params** — `GET` only.
- **Treat missing fields as "unknown", not "empty"** — the server omits nulls;
  a field you saw yesterday may be gone today (e.g. before vs. after KYC).
- `customer_id` here is the real account id (`C#####`); the `ODG-####` style id
  in earlier mocks was placeholder data.
- `plot_area_sq_yd` is a **string** on purpose — don't `parseFloat` it for
  storage, only for display math.
- Rupee amounts (`total_consideration`, `amount_received`, schedule `amount`) are
  **whole integers**, no paise.
- `payment_schedule` is pass-through: render its own `due_date` / `status` per
  row when present; otherwise use the local 10 / 15 / 25 / 25 / 25 split.
```
