# Production security settings

## Admin signup

`POST /admin/signup` requires a verified admin access token. The created admin
still has to verify the email OTP before login. There is no public admin
self-registration path; bootstrap an initial admin through a controlled,
operator-only provisioning process.

## Scheduled jobs

Send `DIVINE_JOBS_TOKEN` in the `X-Divine-Jobs-Token` header to
`/jobs/payment-reminders`. Remove tokens from scheduled URLs. Query-string
support is limited to local development.

## Client IP and rate limits

Logged-in requests are rate-limited by their verified JWT identity (role + user
id), so they don't depend on proxy headers and can't be spoofed. Only anonymous
requests are keyed by IP: the first valid `X-Forwarded-For` entry, else the
direct peer. That value is caller-controllable, so the per-IP limit on login and
other credential endpoints is best-effort, not a hard brute-force boundary.

Limits (per caller, per exact path, per `RATE_LIMIT_WINDOW_SECONDS`, default 60):
`RATE_LIMIT_AUTH_CALLS` (default 10) for login, signup, password reset, signup
OTP and token refresh; `RATE_LIMIT_CALLS` (default 60) for everything else.

The current rate limiter is process-local. It does not coordinate limits across
multiple workers or service instances; use a shared rate-limit store or edge/WAF
limits before scaling out or relying on it as a hard abuse-control boundary.
