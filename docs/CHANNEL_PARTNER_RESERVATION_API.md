# Channel Partner Inventory Reservation — Sample Requests/Responses

Four broker-only endpoints let a Channel Partner lock a plot/unit exclusively
for 3 days. All require `Authorization: Bearer <broker JWT>` (obtained from
`POST /broker/login`) — a customer token or no token is rejected before the
reservation logic ever runs. While a unit's `status` is `"reserved"`, it is
silently absent from `GET /inventory/search` for every other broker and every
customer — it never appears as "held" or with a placeholder, it just isn't in
the results until it's released, sold, or the 3-day window lapses.

All examples below were captured from a real run against the live database.

## 1. Reserve a unit

**Request**
```
POST /inventory/59b97e32-abb6-4e12-92e2-cb234f9083c7/reserve
Authorization: Bearer <broker A's JWT>
```

**Response — 200**
```json
{
  "id": "59b97e32-abb6-4e12-92e2-cb234f9083c7",
  "project_name": "Suraksha Enclave",
  "city": "Sonipat",
  "locality": "Sector-15, Ganaur",
  "block": "B",
  "unit_number": "B46B",
  "unit_type": "plot",
  "width_mtr": 7.312,
  "length_mtr": 13.0,
  "area_sqmt": 95.056,
  "area_sqyd": 113.69,
  "status": "reserved",
  "estimated_price": null,
  "reserved_at": "2026-08-26T21:11:36.498814+00:00",
  "reserved_until": "2026-08-29T21:11:36.498814+00:00"
}
```

## 2. Another broker tries to reserve the same unit

**Request**
```
POST /inventory/59b97e32-abb6-4e12-92e2-cb234f9083c7/reserve
Authorization: Bearer <broker B's JWT>
```

**Response — 409**
```json
{ "detail": "unit_not_available" }
```

## 3. Broker A checks their own reservations

**Request**
```
GET /inventory/reserved/mine
Authorization: Bearer <broker A's JWT>
```

**Response — 200**
```json
{
  "count": 1,
  "reservations": [
    {
      "id": "59b97e32-abb6-4e12-92e2-cb234f9083c7",
      "project_name": "Suraksha Enclave",
      "city": "Sonipat",
      "locality": "Sector-15, Ganaur",
      "block": "B",
      "unit_number": "B46B",
      "unit_type": "plot",
      "width_mtr": 7.312,
      "length_mtr": 13.0,
      "area_sqmt": 95.056,
      "area_sqyd": 113.69,
      "status": "reserved",
      "estimated_price": null,
      "reserved_at": "2026-08-26T21:11:36.498814+00:00",
      "reserved_until": "2026-08-29T21:11:36.498814+00:00"
    }
  ]
}
```

Broker B's `GET /inventory/reserved/mine` at this point returns
`{"count": 0, "reservations": []}` — they never see this row.

## 4. Broker B tries to release/touch A's reservation

**Request**
```
POST /inventory/59b97e32-abb6-4e12-92e2-cb234f9083c7/release
Authorization: Bearer <broker B's JWT>
```

**Response — 409** (same error whether it's not reserved at all, expired, or
held by someone else — never reveals who holds it)
```json
{ "detail": "not_reserved_by_you" }
```

## 5. Broker A releases their own reservation early

**Request**
```
POST /inventory/59b97e32-abb6-4e12-92e2-cb234f9083c7/release
Authorization: Bearer <broker A's JWT>
```

**Response — 200**
```json
{
  "id": "59b97e32-abb6-4e12-92e2-cb234f9083c7",
  "project_name": "Suraksha Enclave",
  "city": "Sonipat",
  "locality": "Sector-15, Ganaur",
  "block": "B",
  "unit_number": "B46B",
  "unit_type": "plot",
  "width_mtr": 7.312,
  "length_mtr": 13.0,
  "area_sqmt": 95.056,
  "area_sqyd": 113.69,
  "status": "available",
  "estimated_price": null
}
```

No `reserved_at`/`reserved_until` fields — those only ever appear on the
reserved-state response shape (`/reserve` and `/reserved/mine`).

## 6. Broker A marks a (still) reserved unit sold

**Request**
```
POST /inventory/59b97e32-abb6-4e12-92e2-cb234f9083c7/mark-sold
Authorization: Bearer <broker A's JWT>
```

**Response — 200**
```json
{
  "id": "59b97e32-abb6-4e12-92e2-cb234f9083c7",
  "project_name": "Suraksha Enclave",
  "city": "Sonipat",
  "locality": "Sector-15, Ganaur",
  "block": "B",
  "unit_number": "B46B",
  "unit_type": "plot",
  "width_mtr": 7.312,
  "length_mtr": 13.0,
  "area_sqmt": 95.056,
  "area_sqyd": 113.69,
  "status": "sold",
  "estimated_price": null
}
```

## 7. Error cases common to all four endpoints

| Scenario | Status | Body |
|---|---|---|
| No `Authorization` header | 401 | `{"detail": "missing_token"}` |
| Token belongs to a customer, not a broker | 403 | `{"detail": "inventory_reservation_broker_only"}` |
| Expired token | 401 | `{"detail": "token_expired"}` |
| Invalid/malformed token | 401 | `{"detail": "invalid_token"}` |

## Auto-expiry (3-day lock)

There is no background job — this repo has no scheduler anywhere. Expiry is
lazy: every inventory read (`search`, `recommendations`, `reserved/mine`,
etc.) first runs a cheap `UPDATE ... WHERE status='reserved' AND
reserved_until < now()` that flips any past-due reservation back to
`available`. A stale reservation self-corrects the moment anyone next
queries the table — no manual cleanup needed.
