# Prod data migration: Suraksha Enclave inventory and offline buyers

Status: code ready; **not yet applied to production**.

This loads the client's real data into the app:

- **Inventory sheet:** each plot's status (Sold / Unsold / Hold).
- **Buyer sheet:** creates an account for every offline buyer and attaches their plot(s).

Price and payment data come later (Phase 3). Until then, imported plots show on the buyer's profile, but the allotment and demand letters stay disabled. No payment reminders are sent for them.

## What changed in the app

| Area | Behaviour |
|---|---|
| Inventory | `held` plots can't be booked online. Re-running `ingest_inventory.py` only changes the status of plots that are still `available`; booked, reserved, sold and held plots keep theirs. Cash, RTGS and cheque money already collected for a taken plot is still recorded, and flagged for manual review. |
| Customer login | An account that has an emailed temporary password gets `must_change_password: true` from `POST /customer/login`, plus a restricted token. That token is refused everywhere (403 `password_change_required`) except `POST /customer/change-password` and `GET /customer/profile`. An expired temporary password returns 401 `temp_password_expired`; the buyer then uses Forgot password. |
| `POST /customer/change-password` | Body `{current_password, new_password}` (new password at least 8 characters). Returns a fresh token. Clears the temporary-password flags, and so does Forgot password / reset. |
| Imported plot records | `divine_documents` rows with `status='imported'`. `GET /documents/{id}` returns 404 `no_file_for_imported_record`. While a plot has no total price and no payment schedule (from the form or from live milestones), the demand letter returns 409 `payment_details_pending` and the profile shows `finance_pending`. Both clear automatically once price data is loaded; `form_data.finance_status` is informational only. Each profile booking now includes `finance_pending`, `plot_address`, `applicant_name` and `co_applicants`. |
| Admin customers list | `source` can now be `OFFLINE_IMPORT`. |
| Frontend | After a temporary-password login, a "Set your new password" screen blocks the app until the password is changed. The letter buttons show "Payment details pending" for imported plots. |

## Schema (additive)

`scripts/add_offline_import_support.py` adds the schema below. App startup also adds the columns.

- `divine_customer_users`: adds `source` (default `website`), `must_change_password`, `temp_password_expires_at`, `invite_status`, `invite_sent_at`.
- `divine_project_inventory`: adds `sale_source` (`offline` or `app`), `status_note`, `status_updated_at`.
- New table `divine_inventory_status_history`: an audit trail of every bulk change, grouped by batch id. The rollback options use it.

## Runbook

Every script is a dry run unless you pass `--apply`. Each run writes a report (and, for apply runs, a backup) to `migration_reports/`. That folder is git-ignored because the files contain personal data.

1. **Deploy** the backend and frontend changes.
2. Add the schema:

   ```
   python scripts/add_offline_import_support.py
   ```
3. Clear the website test bookings. This removes bookings, their payments, instalments, reminders, decisions and booking-application records, and frees their plots. Customer accounts and KYC uploads are kept.

   ```
   python scripts/clear_website_bookings.py
   python scripts/clear_website_bookings.py --apply --confirm DELETE-WEBSITE-BOOKINGS
   ```
4. Load the inventory sheet:

   ```
   python scripts/sync_inventory_from_sheet.py "Web Site Data (1).xlsx" --project "Suraksha Enclave" \
       --city Sonipat --locality "Sector-15, Ganaur" --override B1-B=sold --override C14=sold [--apply]
   ```

   - Sold → `sold` (with `sale_source=offline`), Unsold → `available`, Hold → `held`.
   - A plot with app activity is never changed; it is reported as a CONFLICT instead.
   - Plots of this project that are missing from the sheet → `held`.
   - Undo with `--rollback <batch>`.
5. Import the buyers:

   ```
   python scripts/import_offline_buyers.py "WEB Developer  (1).xlsx" --project "Suraksha Enclave" [--apply]
   ```

   - One account per email (login = email + password). An existing website account with that email is reused.
   - One `imported` plot record per plot.
   - No email is sent at this step.
   - Undo with `--rollback <batch>`.
6. Send the invites. Test with one account first:

   ```
   python scripts/send_offline_buyer_invites.py --customer <id> --apply
   python scripts/send_offline_buyer_invites.py                 # dry run
   python scripts/send_offline_buyer_invites.py --apply
   ```

   - **New accounts** get a unique 10-character temporary password, valid for 7 days. The password hash is saved only after the email is accepted. The password itself is never logged or saved anywhere.
   - **Existing accounts** get a "plot added to your account" email instead.
   - **Resending:** use `--customer <id> --resend --apply`. This issues a new password.

## Data decisions (client sheet, 2026-11-06)

- **B1-B and C14** are marked "Unsold" but have buyers, so both are overridden to sold.
  - C14's row has only an email, which is the same as C15's, so C14 belongs to C15's buyer (Priya Chaudhary).
  - B1-B shares an email with B3-A.
- **Shared emails:** a shared email is treated as the same owner (one login). 15 emails are shared by different names. Each plot still keeps its own applicant and co-applicant names on its letters.
- **C54 (Sanjay Dhingra)** has no email. The account is created with the mobile number as its username and is listed for sales to collect an email.
- **Email typos:** `gamil.com` and `gmaiul.com` were corrected to `gmail.com`.
- **Sold plots without a buyer row:** 30 plots stay sold with no account attached.
- **Data-quality warnings** are reported but not changed:
  - C74: area mismatch
  - C27 and B1-A: missing width/length
  - C89 and C90: status written as lowercase "sold"
