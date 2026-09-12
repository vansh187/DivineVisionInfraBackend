# Password Reset API

Implements the two-endpoint flow from the frontend handoff doc: request an OTP by
email, then verify that OTP and set the new password in one call. Both are scoped
per role, mirroring the existing `/customer/*` and `/broker/*` split used by signup
and login. See [`DivineService/service_password_reset.py`](../DivineService/service_password_reset.py)
for the implementation.

Base URL: `https://divinevisioninfrabackend.onrender.com`
Auth: none (pre-login flow) · Content-Type: `application/json`

## Flow

1. `POST /{role}/forgot-password` — customer/broker enters their email, taps "Send OTP".
2. `POST /{role}/reset-password` — enters the 6-digit code + a new password in one call.
3. `POST /{role}/login` — existing endpoint, unchanged.

## POST /{role}/forgot-password

Sends a one-time code to the account's email. Always returns 200 on a well-formed
request, whether or not that email is registered - the check never leaks account
existence to the caller.

Request:
```json
{ "email": "riya.sharma@example.com" }
```

Response `200`:
```json
{ "message": "If that email is registered, an OTP has been sent." }
```

| Status | detail | When |
|---|---|---|
| 422 | FastAPI validation array | Malformed email. |
| 429 | `"too_many_requests"` | Same email requested another OTP inside the 30s resend cooldown. |
| 500 | `"email_send_failed"` | Mail provider rejected or timed out. |
| 500 | `"internal_error"` | Unexpected server-side failure (DB, etc.) - not in the original spec table but the service never lets a raw exception escape. |

## POST /{role}/reset-password

Verifies the code and updates the password in the same call - there is no
separate "verify" step.

Request:
```json
{ "email": "riya.sharma@example.com", "otp": "482913", "new_password": "NewPass!234" }
```

Response `200`:
```json
{ "message": "Password has been reset." }
```

| Status | detail | When |
|---|---|---|
| 400 | `"otp_not_requested"` | No OTP on file for this email (never requested, already consumed by a prior successful reset, or the account no longer exists). |
| 400 | `"invalid_otp"` | Code doesn't match. |
| 400 | `"otp_expired"` | Code is older than the 10-minute expiry window. |
| 422 | FastAPI validation array | `otp` isn't exactly 6 digits, or `new_password` is under 8 characters. |
| 429 | `"too_many_attempts"` | 5 wrong codes in a row - locked for 15 minutes. |
| 500 | `"internal_error"` | Unexpected server-side failure. |

## OTP rules

| Rule | Value |
|---|---|
| Code | 6 digits, numeric, generated with `secrets` (not `random`) |
| Expiry | 10 minutes from send time |
| Resend cooldown | 30 seconds per email |
| Lockout | 5 wrong attempts -> 15-minute lockout |
| `new_password` | min 8 characters, same rule as signup |

A new `/forgot-password` request always replaces any OTP already on file for that
email (fresh code, attempts reset to 0, any lockout cleared) - only the most
recently sent code is ever valid. A successful reset deletes the OTP row outright,
so it cannot be replayed.

## Role scoping

The OTP is stored keyed by `(role, email)`, so a code sent via `/customer/forgot-password`
cannot be used against `/broker/reset-password` even if both accounts share the same
email address.

## Error format

Matches every other endpoint in this API: non-2xx responses return `{ "detail": ... }`,
where `detail` is either a plain string (a known error code) or a FastAPI validation
array for 422s.
