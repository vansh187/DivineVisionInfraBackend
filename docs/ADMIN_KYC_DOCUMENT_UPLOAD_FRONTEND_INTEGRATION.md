# Admin KYC Document Upload: Frontend Integration Guide

**For:** admin panel frontend engineers
**Backend status:** implemented, deployed with the next backend release
**Last updated:** 2026-10-24

This guide covers everything the admin panel needs for **uploading or replacing a KYC document on
a booking** from the Booking Detail screen. It also covers the backend-wide changes that ship with it:
rate limits, the `X-Request-ID` header, and the version rules. Every request and response below was
captured from the real backend. Only IDs, timestamps and signed URLs were shortened.

---

## 1. Base URL

| Environment | Base URL |
|---|---|
| Production | `https://divinevisioninfrabackend-301k.onrender.com` |
| Local | `http://localhost:8000` |

> **There is no `/api/v1` prefix.** Routes start directly at `/admin/...`.
> If `VITE_API_BASE_URL` currently ends in `/api/v1`, drop that suffix for these calls.

All examples below use `{{BASE_URL}}` = the value above.

---

## 2. Authentication

Every endpoint in this guide needs the admin access token from `POST /admin/login`:

```http
Authorization: Bearer <admin_access_token>
```

The upload endpoint also checks, **on every request**, that the admin account is still approved
by a super admin. A token for an admin who has since been rejected gets `403 forbidden`.

---

## 3. The upload endpoint

```
POST {{BASE_URL}}/admin/bookings/{booking_id}/documents/{document_type}
```

### 3.1 Path parameters

| Param | Example | Notes |
|---|---|---|
| `booking_id` | `BKG-2026-000001` | The booking's `id` from `GET /admin/bookings/{id}` |
| `document_type` | `pan_card` | One of the five values below |

| `document_type` | Label shown in the checklist | Accepted files |
|---|---|---|
| `aadhaar_front` | Aadhaar Card - Front | JPG, JPEG, PNG |
| `aadhaar_back` | Aadhaar Card - Back | JPG, JPEG, PNG |
| `pan_card` | PAN Card | JPG, JPEG, PNG |
| `applicant_photo` | Applicant Photo | JPG, JPEG, PNG |
| `cancelled_cheque` | Cancelled Cheque | **PDF**, JPG, JPEG, PNG |

> PDF is accepted **only for `cancelled_cheque`**. Aadhaar, PAN and the photo are embedded as
> images into the generated booking application, so they must be images. Set the file picker's
> `accept` attribute to match:
> - `cancelled_cheque`: `accept=".pdf,.jpg,.jpeg,.png"`
> - the other four types: `accept=".jpg,.jpeg,.png"`

### 3.2 Headers

| Header | Required | Value |
|---|---|---|
| `Authorization` | yes | `Bearer <admin_access_token>` |
| `Idempotency-Key` | yes | A **UUID** (`crypto.randomUUID()`). See §5. |
| `Accept` | recommended | `application/json` |
| `Content-Type` | **do not set** | Let the browser set `multipart/form-data; boundary=...` from the `FormData` body. Setting it by hand breaks the boundary. |

Send the body as a normal `FormData`. Browsers always send a `Content-Length` for it, and the
backend **rejects requests without one** (chunked or streamed bodies). Don't stream the upload.

### 3.3 Body (`multipart/form-data`)

| Field | Required | Rules |
|---|---|---|
| `file` | yes | Max **10 MiB**, non-empty. The server reads the file's actual bytes: the file extension, the browser's MIME type and the real content must all match (a PNG renamed `.jpg` is rejected). Encrypted PDFs are rejected. |
| `expected_version` | yes | The booking `version` from the last `GET /admin/bookings/{id}`. Integer ≥ 1. |
| `note` | no | Plain text, max 500 characters, trimmed. Shown in Decision History. Newlines and tabs are fine; other control characters are rejected. |

### 3.4 Sample request

**curl**

```bash
curl -X POST "https://divinevisioninfrabackend-301k.onrender.com/admin/bookings/BKG-2026-000001/documents/pan_card" \
  -H "Authorization: Bearer <admin_access_token>" \
  -H "Accept: application/json" \
  -H "Idempotency-Key: 2f105db8-7109-4b1a-8e44-22227a8c9181" \
  -F "file=@pan-card-replacement.png;type=image/png" \
  -F "expected_version=1" \
  -F "note=Replacement PAN supplied after name mismatch"
```

**Raw HTTP** (what the browser sends)

```http
POST /admin/bookings/BKG-2026-000001/documents/pan_card HTTP/1.1
Host: divinevisioninfrabackend-301k.onrender.com
Authorization: Bearer <admin_access_token>
Accept: application/json
Idempotency-Key: 2f105db8-7109-4b1a-8e44-22227a8c9181
Content-Type: multipart/form-data; boundary=----client-boundary
Content-Length: 48213

------client-boundary
Content-Disposition: form-data; name="file"; filename="pan-card-replacement.png"
Content-Type: image/png

<binary file bytes>
------client-boundary
Content-Disposition: form-data; name="expected_version"

1
------client-boundary
Content-Disposition: form-data; name="note"

Replacement PAN supplied after name mismatch
------client-boundary--
```

### 3.5 Success: `200 OK`

Response headers:

```http
Content-Type: application/json
X-Request-ID: 1e77b178-4fdd-403c-b39e-a60ebe97cec9
```

Body: the **refreshed Booking Detail**, the same shape `GET /admin/bookings/{id}` returns. Replace
your screen state with it. Note that `version` went up by 1.

```json
{
  "id": "BKG-2026-000001",
  "status": "pending_kyc_review",
  "kyc_status": "pending",
  "version": 2,
  "admin_note": null,
  "customer_id": "C17663",
  "customer_name": "Aarav Sharma",
  "customer_email": "aarav@example.com",
  "customer_phone": "+919876543210",
  "project_name": "Divine Meadows",
  "unit_number": "A-104",
  "amount": 250000.0,
  "payment_id": "8f103186-5447-49b4-a5c6-99d6ef9bf87e",
  "payment_method": "rtgs_neft",
  "payment_status": "paid",
  "razorpay_payment_id": null,
  "utr_number": "UTR000111222",
  "cheque_number": null,
  "documents": [
    {
      "document_type": "aadhaar_front",
      "label": "Aadhaar Card - Front",
      "uploaded": false,
      "preview_url": null,
      "preview_url_expires_in": null,
      "uploaded_at": null
    },
    {
      "document_type": "aadhaar_back",
      "label": "Aadhaar Card - Back",
      "uploaded": false,
      "preview_url": null,
      "preview_url_expires_in": null,
      "uploaded_at": null
    },
    {
      "document_type": "pan_card",
      "label": "PAN Card",
      "uploaded": true,
      "preview_url": "https://<project>.supabase.co/storage/v1/object/sign/documents/<path>?token=<signed-token>",
      "preview_url_expires_in": 3600,
      "uploaded_at": "2026-10-24T10:10:00.912923Z"
    },
    {
      "document_type": "applicant_photo",
      "label": "Applicant Photo",
      "uploaded": false,
      "preview_url": null,
      "preview_url_expires_in": null,
      "uploaded_at": null
    },
    {
      "document_type": "cancelled_cheque",
      "label": "Cancelled Cheque",
      "uploaded": false,
      "preview_url": null,
      "preview_url_expires_in": null,
      "uploaded_at": null
    }
  ],
  "decision_history": [
    {
      "actor": "system",
      "action": "payment_received",
      "note": null,
      "created_at": "2026-10-20T12:30:00.886781Z"
    },
    {
      "actor": "A64253",
      "action": "kyc_document_uploaded",
      "note": "Replacement PAN supplied after name mismatch",
      "created_at": "2026-10-24T10:10:00.910914Z"
    }
  ],
  "payment_schedule": [],
  "next_due": null,
  "amount_received": null,
  "created_at": "2026-10-20T12:30:00.886781Z",
  "last_activity_at": "2026-10-24T10:10:00.910914Z"
}
```

What the response tells you:

- `documents` always contains **all five** checklist entries, including ones not uploaded yet.
- `uploaded: true` means **a file exists**, not that it's verified.
- `preview_url` is a private, signed link that expires after `preview_url_expires_in` seconds
  (3600). Don't cache it long-term; re-fetch the booking to get a fresh one. It can be
  `null` even when `uploaded: true` if signing failed for a moment; show "Preview unavailable,
  refresh" in that case.
- `actor` in `decision_history` is the admin's ID.
- **Uploading never approves anything.** `status` and `kyc_status` don't change. The admin still
  reviews the file and clicks **Approve** (§7).

### 3.6 Idempotent replay: `200 OK`

If you resend the **same request with the same `Idempotency-Key`** (for example, after a network
timeout), the backend returns the same result without uploading again, plus this header:

```http
Idempotent-Replayed: true
X-Request-ID: 99e11f2f-7daa-4dfd-bfcf-60cdaa05b8df
```

The body is the same Booking Detail as §3.5: `version` stays `2` and there's still only one
`kyc_document_uploaded` entry. Treat a replay as a normal success.

---

## 4. Errors

### 4.1 Error format for this endpoint

Errors from the upload endpoint return JSON with an **object** `detail`:

```json
{
  "detail": {
    "code": "version_conflict",
    "message": "Booking changed since it was loaded. Refresh and try again."
  }
}
```

- Switch your logic on **`detail.code`**. It's stable.
- `detail.message` is safe to show to the admin as-is.
- Some codes carry extra fields (`current_version`, `saved`), listed below.
- Every response, success or error, carries an **`X-Request-ID`** header. Show it in error toasts
  or log it so support can trace the request.

> ⚠️ The **older admin endpoints** (approve, reject, cancel, GET detail) and the **global rate
> limiter** still return a **string** `detail`, e.g. `{"detail": "version_conflict"}`. Use the
> helper in §9 to handle both shapes.

### 4.2 All error responses (captured from the real backend)

**400 `invalid_multipart`: `Idempotency-Key` isn't a UUID**
```json
{ "detail": { "code": "invalid_multipart", "message": "Idempotency-Key must be a UUID." } }
```
Same code, other messages:
- `"Request must be multipart/form-data."`: the body wasn't `FormData`.
- `"Malformed multipart request."`: the multipart body couldn't be parsed.
- `"Content-Length header is required."`: the body was streamed or chunked.

**401 `unauthorized`: missing, expired or invalid token**
```json
{ "detail": { "code": "unauthorized", "message": "Sign in again to continue." } }
```
Action: refresh the token (`POST /admin/refresh`) or send the admin to login.

**403 `forbidden`: admin isn't approved (pending or rejected by a super admin)**
```json
{ "detail": { "code": "forbidden", "message": "Your admin account is not permitted to perform this action." } }
```

**404 `booking_not_found`**
```json
{ "detail": { "code": "booking_not_found", "message": "Booking not found." } }
```

**409 `version_conflict`: the booking changed since you loaded it**
```json
{
  "detail": {
    "code": "version_conflict",
    "message": "Booking changed since it was loaded. Refresh and try again.",
    "current_version": 2
  }
}
```
Action: re-fetch `GET /admin/bookings/{id}`, show the latest state, and let the admin retry.
Use a **new** `Idempotency-Key` for the retry, because the request changed (new `expected_version`).

**409 `booking_not_uploadable`: the booking is no longer `pending_kyc_review`**
```json
{ "detail": { "code": "booking_not_uploadable", "message": "This booking cannot accept document uploads in its current state." } }
```
Action: re-fetch and hide the upload controls. Uploads are only allowed while the booking is
`pending_kyc_review`; booked, rejected and cancelled bookings are read-only.

**409 `idempotency_conflict`: the key was reused for a different upload**
```json
{ "detail": { "code": "idempotency_conflict", "message": "Idempotency key was already used for a different request." } }
```
This is a frontend bug. Generate a new key for every new upload (§5).

**413 `file_too_large`**
```json
{ "detail": { "code": "file_too_large", "message": "File exceeds the 10 MiB limit." } }
```
Check `file.size <= 10 * 1024 * 1024` before uploading.

**415 `unsupported_media_type`**
```json
{ "detail": { "code": "unsupported_media_type", "message": "Only JPG/JPEG or PNG images are accepted for this document." } }
```
Other messages for the same code:
- `"Only PDF, JPG/JPEG, or PNG files are accepted."`: for `cancelled_cheque`.
- `"Encrypted PDFs are not supported."`

**422 `validation_error`**
```json
{ "detail": { "code": "validation_error", "message": "expected_version must be an integer >= 1." } }
```
Other messages:
- `"Unsupported document type."`
- `"A file is required."`
- `"The uploaded file is empty."`
- `"Idempotency-Key header is required."`
- `"Note must be at most 500 characters."`
- `"Note contains unsupported characters."`
- `"note must be plain text."`

**429 `rate_limited`: upload limit**
```http
Retry-After: 60
```
```json
{ "detail": { "code": "rate_limited", "message": "Too many document uploads. Try again shortly." } }
```
Per-booking variant (`Retry-After: 3600`):
`"Too many document uploads for this booking. Try again later."`
Action: disable the upload button for `Retry-After` seconds.

**500 `internal_error`**
```json
{ "detail": { "code": "internal_error", "message": "Unable to upload document. Contact support with the request ID." } }
```
Action: safe to retry with the **same** `Idempotency-Key`. Show the `X-Request-ID`.

**503 `document_storage_unavailable`: storage is down, nothing was saved**
```json
{ "detail": { "code": "document_storage_unavailable", "message": "Document service is temporarily unavailable." } }
```
Action: retry later with the **same** `Idempotency-Key`.

**503 `document_saved_refresh_failed`: the file WAS saved, but the booking couldn't be reloaded**
```json
{
  "detail": {
    "code": "document_saved_refresh_failed",
    "message": "The document was saved, but the booking could not be reloaded. Refresh the page to see it.",
    "saved": true
  }
}
```
Action: **don't tell the admin the upload failed.** Either re-fetch `GET /admin/bookings/{id}`, or
retry with the **same** `Idempotency-Key`, which replays the result without uploading again.

### 4.3 Quick reference

| Status | `detail.code` | Retry? | Key for retry |
|---|---|---|---|
| 400 | `invalid_multipart` | fix the request | new |
| 401 | `unauthorized` | after re-login | same |
| 403 | `forbidden` | no | — |
| 404 | `booking_not_found` | no | — |
| 409 | `version_conflict` | after re-fetch | **new** |
| 409 | `booking_not_uploadable` | no | — |
| 409 | `idempotency_conflict` | fix the bug | new |
| 413 | `file_too_large` | different file | new |
| 415 | `unsupported_media_type` | different file | new |
| 422 | `validation_error` | fix input | new |
| 429 | `rate_limited` | after `Retry-After` | same |
| 500 | `internal_error` | yes | **same** |
| 503 | `document_storage_unavailable` | yes | **same** |
| 503 | `document_saved_refresh_failed` | re-fetch, or retry | **same** |

---

## 5. Idempotency-Key rules

1. Generate **one key per upload attempt** when the admin picks a file and clicks Upload:
   `const key = crypto.randomUUID()`.
2. **Reuse the same key** only when retrying **that exact upload** (same booking, document type,
   file, `expected_version` and note) after a network error, timeout, 500, 503 or 429.
3. Generate a **new key** whenever anything in the request changes: a different file, an edited
   note, or a new `expected_version` after a 409.
4. Keys are per admin. Two admins can't collide.

---

## 6. Versions and the "other bookings" rule

Every booking has a `version`. Any change to the booking (an upload, approve, reject or cancel)
increments it. Always send the `version` from your **latest** fetch:

- Upload → `expected_version` form field.
- Approve, Reject, Cancel → `version` in the JSON body.
- After a successful upload, use the `version` **from the upload response** for the next action.
  It went up by 1.

**Documents belong to the customer, not to a single booking.** If a customer has two bookings
under KYC review (A and B) and an admin replaces the PAN via booking A, then booking B now shows
the new PAN as well. Because of that, B's `version` is also increased, and B's Decision History
gets an entry like:

```json
{
  "actor": "A64253",
  "action": "kyc_document_uploaded",
  "note": "Shared pan_card document updated via booking BKG-2026-000001: Replacement PAN supplied after name mismatch",
  "created_at": "2026-10-24T10:10:00.910914Z"
}
```

So an admin with booking B open from before the upload gets `409 version_conflict` on Approve.
That's intended: they would otherwise approve documents they never looked at. Handle it like any
409: re-fetch and re-render.

---

## 7. Related endpoints you'll use with uploads

### 7.1 `GET {{BASE_URL}}/admin/bookings/{booking_id}`: load the screen

```bash
curl "https://divinevisioninfrabackend-301k.onrender.com/admin/bookings/BKG-2026-000001" \
  -H "Authorization: Bearer <admin_access_token>"
```
`200` → same body as §3.5.
`404` → `{"detail": "not_found"}` (string format).

### 7.2 `POST {{BASE_URL}}/admin/bookings/{booking_id}/approve`

```bash
curl -X POST "https://divinevisioninfrabackend-301k.onrender.com/admin/bookings/BKG-2026-000001/approve" \
  -H "Authorization: Bearer <admin_access_token>" \
  -H "Content-Type: application/json" \
  -d '{ "note": "KYC verified", "version": 2 }'
```

| Field | Rules |
|---|---|
| `version` | required, integer ≥ 1, the booking's current version |
| `note` | optional, max 2000 characters |

- `200` → Booking Detail with `"status": "booked"` and `"kyc_status": "verified"`.
- Stale version (captured):
  ```json
  { "detail": "version_conflict" }
  ```
  with status `409`.

Reject and Cancel take the same body at `/reject` and `/cancel`.

---

## 8. Rate limits (all endpoints)

| Endpoints | Limit |
|---|---|
| `POST /admin/login`, `/customer/login`, `/broker/login`, all `/signup`, `/forgot-password`, `/reset-password`, `/admin/verify-signup`, `/admin/resend-signup-otp`, `/admin/refresh` | **10 requests / minute** |
| Everything else | **60 requests / minute** |
| KYC document upload (on top of the above) | 10 / minute per admin, 20 / hour per booking |

- Limits apply **per logged-in user** (or per IP when not logged in) and **per exact URL path**.
  For example, `/admin/bookings/BKG-1` and `/admin/bookings/BKG-2` have separate budgets.
- Query strings don't count as separate paths: `/inventory/search?city=A` and `?city=B` share one
  budget. Debounce search and filter inputs.
- Global limiter response (string `detail`, no `Retry-After`; wait about 60 seconds):
  ```http
  HTTP/1.1 429 Too Many Requests
  ```
  ```json
  { "detail": "rate_limited" }
  ```

---

## 9. Reference implementation (TypeScript)

```ts
const BASE_URL = import.meta.env.VITE_API_BASE_URL; // no trailing /api/v1
const MAX_BYTES = 10 * 1024 * 1024;

export type KycDocType =
  | "aadhaar_front" | "aadhaar_back" | "pan_card" | "applicant_photo" | "cancelled_cheque";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public requestId: string | null,
    public extra: Record<string, unknown> = {},
    public retryAfter: number | null = null,
  ) { super(message); }
}

/** Normalises both error shapes: {"detail": {"code","message",...}} and {"detail": "code"}. */
async function toApiError(res: Response): Promise<ApiError> {
  const requestId = res.headers.get("X-Request-ID");
  const retryAfter = Number(res.headers.get("Retry-After")) || null;
  let body: any = null;
  try { body = await res.json(); } catch { /* non-JSON */ }
  const d = body?.detail;
  if (d && typeof d === "object" && !Array.isArray(d)) {
    const { code, message, ...extra } = d;
    return new ApiError(res.status, code, message, requestId, extra, retryAfter);
  }
  const code = typeof d === "string" ? d : "internal_error";
  return new ApiError(res.status, code, "Something went wrong. Please try again.", requestId, {}, retryAfter);
}

export function acceptFor(type: KycDocType): string {
  return type === "cancelled_cheque" ? ".pdf,.jpg,.jpeg,.png" : ".jpg,.jpeg,.png";
}

export async function uploadBookingDocument(opts: {
  token: string;
  bookingId: string;
  documentType: KycDocType;
  file: File;
  expectedVersion: number;
  note?: string;
  idempotencyKey: string; // crypto.randomUUID() per upload attempt; reuse ONLY for retries
}) {
  if (opts.file.size === 0) throw new ApiError(422, "validation_error", "The selected file is empty.", null);
  if (opts.file.size > MAX_BYTES) throw new ApiError(413, "file_too_large", "File exceeds the 10 MiB limit.", null);

  const form = new FormData();
  form.append("file", opts.file);
  form.append("expected_version", String(opts.expectedVersion));
  if (opts.note?.trim()) form.append("note", opts.note.trim());

  const res = await fetch(
    `${BASE_URL}/admin/bookings/${encodeURIComponent(opts.bookingId)}/documents/${opts.documentType}`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${opts.token}`,
        Accept: "application/json",
        "Idempotency-Key": opts.idempotencyKey,
        // NO Content-Type - the browser adds the multipart boundary.
      },
      body: form,
    },
  );
  if (!res.ok) throw await toApiError(res);
  return {
    booking: await res.json(),                     // BookingDetail - replace screen state
    replayed: res.headers.get("Idempotent-Replayed") === "true",
    requestId: res.headers.get("X-Request-ID"),
  };
}
```

Suggested handling in the screen:

```ts
const key = crypto.randomUUID();          // once per Upload click
try {
  const { booking } = await uploadBookingDocument({ token, bookingId, documentType, file,
                                                     expectedVersion: booking.version, note,
                                                     idempotencyKey: key });
  setBooking(booking);                    // version is now +1
  toast.success("Document uploaded");
} catch (e) {
  if (!(e instanceof ApiError)) { /* network error: retry with the SAME key */ return; }
  switch (e.code) {
    case "version_conflict":
    case "booking_not_uploadable":
      setBooking(await fetchBooking(bookingId)); toast.info(e.message); break;
    case "document_saved_refresh_failed":
      setBooking(await fetchBooking(bookingId)); toast.success("Document uploaded"); break;
    case "rate_limited":
      disableUploadFor(e.retryAfter ?? 60); toast.warn(e.message); break;
    case "unauthorized":
      redirectToLogin(); break;
    default:
      toast.error(`${e.message} (Ref: ${e.requestId ?? "n/a"})`);
  }
}
```

---

## 10. Checklist for the frontend

- [ ] Base URL has no `/api/v1`.
- [ ] Upload button shown only when `status === "pending_kyc_review"`.
- [ ] File picker `accept` per document type (PDF only for the cancelled cheque).
- [ ] 10 MiB size check before upload.
- [ ] `FormData` body; no manual `Content-Type`.
- [ ] New `Idempotency-Key` per upload; same key only for retries of that exact upload.
- [ ] `expected_version` from the latest booking state; replace state with the response.
- [ ] Handle both error shapes (object `detail` here, string `detail` elsewhere).
- [ ] 409 → re-fetch the booking. 429 → wait `Retry-After`. 503 `document_saved_refresh_failed` → show success and re-fetch.
- [ ] Show `X-Request-ID` in error messages.
- [ ] Re-fetch the booking for fresh preview links (they expire after 1 hour).
- [ ] Debounce search and filter calls (60 per minute per path).
