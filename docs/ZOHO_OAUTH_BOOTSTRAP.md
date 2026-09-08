# Zoho OAuth Bootstrap — Login & Generate Refresh / Auth Token

One-time human-in-the-browser step to connect this backend to a Zoho CRM account.
Zoho requires a real user login + consent, so this cannot be scripted.

## 1. Callback / Redirect URL

Register this **exactly** (character-for-character, no trailing slash) under
**Authorized Redirect URIs** in the Zoho API Console (Server-based Application),
and set it as the `ZOHO_REDIRECT_URI` env var on Render:

```
https://divinevisioninfrabackend.onrender.com/admin/zoho/oauth/callback
```

If this backend's domain ever changes, update it in **both** places.

## 2. Start the login + approval flow

Open this URL in a browser:

```
https://divinevisioninfrabackend.onrender.com/admin/zoho/oauth/start?key=<ZOHO_OAUTH_SETUP_TOKEN>
```

- `<ZOHO_OAUTH_SETUP_TOKEN>` = the secret set in Render env vars (gates the admin OAuth routes).
- This route builds the Zoho authorize URL and redirects you to the consent screen.

Params it requests (`DivineAPI/zoho_admin_api.py`):

| Param | Value |
|---|---|
| `scope` | `ZohoCRM.modules.ALL` |
| `response_type` | `code` |
| `access_type` | `offline` (so a refresh token is issued) |
| `prompt` | `consent` |
| `redirect_uri` | `ZOHO_REDIRECT_URI` |
| `state` | the setup token |

## 3. Approve

Log into the **production** Zoho account (not the dummy/test one) and click **Approve**.

Zoho redirects to `/admin/zoho/oauth/callback`, which:

1. Exchanges the one-time grant `code` for a **refresh token**.
2. Activates it immediately for the running process (no restart needed).
3. Persists it — auto-saves to Render env vars if `RENDER_API_KEY` + `RENDER_SERVICE_ID`
   are set; otherwise the page shows the `ZOHO_REFRESH_TOKEN` value for you to paste
   into the Render dashboard manually.

## 4. Data center note

Config here defaults to the Zoho India DC (`.in`):

- API Console: `https://api-console.zoho.in`
- `ZOHO_ACCOUNTS_DOMAIN` = `accounts.zoho.in`
- `ZOHO_API_DOMAIN` = `www.zohoapis.in`

If the real CRM account is on the US/global DC, use `.com` for all three.

## 5. Verify (all gated by `ZOHO_OAUTH_SETUP_TOKEN`)

```bash
# Refresh token actually set? (no secret values exposed)
curl "https://divinevisioninfrabackend.onrender.com/admin/zoho/status?key=<TOKEN>"

# Write one dummy Lead end-to-end (token refresh + API domain + upsert)
curl -X POST "https://divinevisioninfrabackend.onrender.com/admin/zoho/test-push?key=<TOKEN>"

# Check whether an email exists in Zoho right now (everything syncs to Leads)
curl "https://divinevisioninfrabackend.onrender.com/admin/zoho/lookup?key=<TOKEN>&email=someone@example.com&module=Leads"
```

## Troubleshooting

- **"Invalid Redirect Uri"** — the URI registered in the Zoho API Console doesn't
  exactly match `ZOHO_REDIRECT_URI`. Check for a trailing slash, http vs https, or a typo.
- **`pushed: false` from test-push** — `ZOHO_REFRESH_TOKEN` isn't set yet; re-run step 2.
- **403 `forbidden`** — wrong or missing `key` (must equal `ZOHO_OAUTH_SETUP_TOKEN`).

See also: [`ZOHO_CRM_SETUP.md`](ZOHO_CRM_SETUP.md) for the full production setup.
