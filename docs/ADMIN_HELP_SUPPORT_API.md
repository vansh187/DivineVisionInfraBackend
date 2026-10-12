# Admin Help & Support API Integration

Frontend integration guide for the admin panel's Help & Support form (`/admin/help`).

Requires:

```http
Authorization: Bearer <admin_access_token>
```

Missing, invalid, customer, or broker tokens return `401`.

## Submit a Ticket

```http
POST /admin/support-tickets
Content-Type: application/json
Authorization: Bearer <admin_access_token>
```

### Request Body

```json
{
  "subject": "Ui Issue",
  "description": "The sidebar overlaps the content on mobile widths below 400px."
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `subject` | string | yes | 1–150 chars. Blank/whitespace-only is rejected. |
| `description` | string | yes | 1–3000 chars. Blank/whitespace-only is rejected. |

### Response - 200 OK

```json
{
  "ticket_number": "TTK-099049",
  "subject": "Ui Issue",
  "description": "The sidebar overlaps the content on mobile widths below 400px.",
  "raised_by": "Vansh Duggal",
  "submitted_date": "2026-09-16T04:15:22.533340Z",
  "email_sent": true
}
```

| Field | Notes |
|---|---|
| `ticket_number` | Randomly generated per submission, format `TTK-XXXXXX`. Not stored/searchable server-side - it exists purely as a reference for the email thread. |
| `raised_by` | The submitting admin's full name, resolved server-side from their token. `null` if it couldn't be resolved (never blocks submission). |
| `submitted_date` | UTC ISO-8601 timestamp. |
| `email_sent` | Will always be `true` on a 200 response (see below) - kept in the payload in case the frontend wants to display it directly. |

There is nothing to poll or fetch afterwards - this is fire-and-confirm. On success, clear the form and show the `ticket_number` to the admin (e.g. "Ticket TTK-099049 submitted").

## What Happens Server-Side

The subject, description, and ticket number are emailed to the configured support inbox (`SUPPORT_TICKET_EMAIL`, set as a Render environment variable - not user-configurable from the frontend). The email is sent synchronously as part of this request, so a `200` response means the notification was actually delivered, not just queued.

## Errors

| HTTP | `detail` | Meaning | Frontend action |
|---|---|---|---|
| `401` | `missing_token`, `invalid_token`, `token_expired` | Admin token missing/invalid/expired | Redirect to login |
| `422` | standard FastAPI validation body | `subject`/`description` blank or over the length limit | Show field-level validation errors from `detail` (array of `{loc, msg, ...}`) |
| `502` | `ticket_email_failed` | Support inbox is unreachable or email delivery failed | Show "couldn't submit, please try again" and let the admin retry - nothing was saved, so retrying is always safe |
| `500` | `internal_error` | Server-side failure | Show retry option; do not parse internal details |

### Example 422 body

```json
{
  "detail": [
    {
      "type": "string_too_short",
      "loc": ["body", "subject"],
      "msg": "String should have at least 1 character",
      "input": ""
    }
  ]
}
```

## Frontend Integration Example

```ts
type SupportTicketPayload = {
  subject: string;
  description: string;
};

type SupportTicketResponse = {
  ticket_number: string;
  subject: string;
  description: string;
  raised_by: string | null;
  submitted_date: string;
  email_sent: boolean;
};

export async function submitSupportTicket(accessToken: string, payload: SupportTicketPayload) {
  const res = await fetch("/admin/support-tickets", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    if (res.status === 502) throw new Error("Couldn't deliver the ticket - please try again.");
    if (res.status === 422) throw new Error("Please check the subject and description.");
    throw new Error(`Failed to submit ticket: ${res.status}`);
  }
  return (await res.json()) as SupportTicketResponse;
}
```

## Recommended UI Behavior

- Disable Submit while the request is in flight (the email send happens synchronously, so this can take a moment longer than a typical save).
- On success, clear the form and show a confirmation toast with `ticket_number`.
- On `502`, keep the form filled in and let the admin retry immediately.
- On `422`, surface which field failed rather than a generic error.

## Related Backend Files

- API route: `DivineAPI/admin_help_support_api.py`
- Service logic: `DivineService/service_help_support.py`
- Email template: `DivineService/service_email.py` (`send_ticket_notification`)
- DTOs: `DivineDTO/models.py` (`SupportTicketRequestDTO`, `SupportTicketResponseDTO`)
