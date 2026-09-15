import base64
import logging
import os
import threading
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_RESEND_ENDPOINT = "https://api.resend.com/emails"
_REQUEST_TIMEOUT_SECONDS = 10

# Sender / reply-to are configured in .env:
#   DIVINE_RESEND_EMAIL - the verified "from" address the recipient sees
#                         (e.g. noreply@divinevisioninfra.com)
#   RESEND_EMAIL         - a monitored inbox replies should land in
#                         (e.g. marketing1@divinevisioninfra.com)
_DEFAULT_FROM_NAME = "Divine Vision Infra"

# One login destination for both audiences; the button label differs by audience.
_DEFAULT_CUSTOMER_LOGIN_URL = "https://www.divinevisioninfra.com"
_DEFAULT_PARTNER_LOGIN_URL = "https://www.divinevisioninfra.com"

# Optional brand logo embedded inline (as a CID attachment). Falls back to a
# typographic wordmark when the file is absent.
_DEFAULT_LOGO_PATH = str(Path(__file__).resolve().parent / "assets" / "divine_logo.png")
_LOGO_CID = "divinelogo"

CUSTOMER = "customer"
CHANNEL_PARTNER = "channel_partner"


class serviceEmail:
    """Best-effort transactional email via Resend. Modelled on serviceZoho: every
    public method is called *after* the caller's real work has already succeeded
    and is designed to NEVER raise - a Resend outage, missing API key, or bad
    payload must not fail, meaningfully slow, or roll back the caller's request.
    Methods return a bool (sent or not) and dispatch on a daemon thread so signup
    latency is unaffected."""

    def __init__(self, api_key: str = None, from_email: str = None, logo_path: str = None):
        self._api_key = api_key or os.getenv("RESEND_API_KEY")
        if self._api_key:  # strip stray quotes that can survive .env parsing
            self._api_key = self._api_key.strip().strip('"').strip("'")
        self._from_email = (from_email or os.getenv("DIVINE_RESEND_EMAIL") or "").strip()
        self._customer_login_url = (os.getenv("DIVINE_CUSTOMER_LOGIN_URL") or _DEFAULT_CUSTOMER_LOGIN_URL).strip()
        self._partner_login_url = (os.getenv("DIVINE_PARTNER_LOGIN_URL") or _DEFAULT_PARTNER_LOGIN_URL).strip()
        self._logo_b64 = _load_logo_b64(logo_path or os.getenv("DIVINE_LOGO_PATH") or _DEFAULT_LOGO_PATH)
        # True only when a send could actually succeed. Callers check this before
        # dispatching so an unconfigured environment never spawns no-op threads.
        self.enabled = bool(self._api_key and self._from_email)
        if not self.enabled:
            missing = "RESEND_API_KEY" if not self._api_key else "DIVINE_RESEND_EMAIL"
            logger.warning("resend_disabled: %s not set - welcome emails will be skipped", missing)

    # ------------------------------------------------------------------ public

    def send_welcome_async(self, user_type: str, email: str, first_name: str = None, username: str = None) -> None:
        self._run_async(self.send_welcome, user_type, email, first_name=first_name, username=username)

    # Thin sync helpers kept for tests and one-off manual sends.
    def send_customer_welcome(self, email: str, first_name: str = None, username: str = None) -> bool:
        return self.send_welcome(CUSTOMER, email, first_name=first_name, username=username)

    def send_broker_welcome(self, email: str, first_name: str = None, username: str = None) -> bool:
        return self.send_welcome(CHANNEL_PARTNER, email, first_name=first_name, username=username)

    def send_welcome(self, user_type: str, email: str, first_name: str = None, username: str = None) -> bool:
        try:
            variant = CUSTOMER if user_type == CUSTOMER else CHANNEL_PARTNER
            to = (email or "").strip()
            if not to or "@" not in to:
                logger.info("welcome_email_skipped: no valid email user_type=%s", variant)
                return False
            display = (first_name or "").strip() or (username or "").strip() or (
                "Partner" if variant == CHANNEL_PARTNER else "there"
            )
            login_url = self._partner_login_url if variant == CHANNEL_PARTNER else self._customer_login_url
            copy = _COPY[variant]
            subject = copy["subject"]
            html = _welcome_html(variant, display, login_url, has_logo=bool(self._logo_b64))
            text = _welcome_text(variant, display, login_url)
            return self._send(to=to, subject=subject, html=html, text=text)
        except Exception as e:  # never let a welcome email break signup
            logger.warning("welcome_email_failed email=%s user_type=%s error=%s", email, user_type, e)
            return False

    def send_booking_confirmation_async(self, email: str, first_name: str = None, project_name: str = None,
                                        unit_number: str = None, amount=None, currency: str = "INR") -> None:
        self._run_async(
            self.send_booking_confirmation, email, first_name=first_name, project_name=project_name,
            unit_number=unit_number, amount=amount, currency=currency,
        )

    def send_booking_confirmation(self, email: str, first_name: str = None, project_name: str = None,
                                  unit_number: str = None, amount=None, currency: str = "INR") -> bool:
        """Congratulations email sent once a plot booking is backed by a completed
        payment. Called after the booking is already persisted - never raises."""
        try:
            to = (email or "").strip()
            if not to or "@" not in to:
                logger.info("booking_confirmation_skipped: no valid email")
                return False
            display = (first_name or "").strip() or "there"
            details = _booking_detail_rows(project_name, unit_number, amount, currency)
            html = _booking_confirmation_html(display, details, self._customer_login_url, has_logo=bool(self._logo_b64))
            text = _booking_confirmation_text(display, details, self._customer_login_url)
            return self._send(to=to, subject=_BOOKING_SUBJECT, html=html, text=text)
        except Exception as e:  # never let a confirmation email break a booking
            logger.warning("booking_confirmation_failed email=%s error=%s", email, e)
            return False

    # ---- password reset OTP ---------------------------------------------
    def send_otp_email(self, email: str, otp: str, first_name: str = None, expires_minutes: int = 10) -> bool:
        """Synchronous (not fire-and-forget, unlike the other sends here): the
        /forgot-password endpoint's own success/failure response depends on whether
        this actually went out, so the caller needs the real result instead of a
        thread it can't observe. Still never raises - a malformed template or
        unexpected error is treated the same as a send failure."""
        try:
            to = (email or "").strip()
            if not to or "@" not in to:
                logger.info("otp_email_skipped: no valid email")
                return False
            name = (first_name or "").strip() or "there"
            safe_otp = (otp or "").strip()
            html = _otp_email_html(name, safe_otp, int(expires_minutes), has_logo=bool(self._logo_b64))
            text = _otp_email_text(name, safe_otp, int(expires_minutes))
            return self._send(to=to, subject="Your Password Reset Code — Divine Vision Infra", html=html, text=text)
        except Exception as e:
            logger.warning("otp_email_failed email=%s error=%s", email, e)
            return False

    # ---- instalment reminders + receipts --------------------------------
    def send_payment_reminder_async(self, email: str, **kwargs) -> None:
        self._run_async(self.send_payment_reminder, email, **kwargs)

    def send_payment_reminder(self, email: str, first_name: str = None, project_name: str = None,
                              unit_number: str = None, milestone_label: str = None, amount=None,
                              due_date: str = None, days_remaining=None, outstanding=None,
                              kind: str = "T_MINUS_20", currency: str = "INR") -> bool:
        """Premium 'payment due' email with a Pay Now button. Never raises."""
        try:
            to = (email or "").strip()
            if not to or "@" not in to:
                return False
            name = (first_name or "").strip() or "there"
            money = _format_amount(amount, currency) or ""
            overdue = isinstance(days_remaining, (int, float)) and days_remaining < 0
            when = _format_reminder_date(due_date)
            if overdue:
                headline, timing = "Payment Overdue", f"overdue by {abs(int(days_remaining))} day(s)"
            elif isinstance(days_remaining, (int, float)) and days_remaining == 0:
                headline, timing = "Payment Due Today", "due today"
            else:
                left = int(days_remaining) if isinstance(days_remaining, (int, float)) else None
                headline = "Payment Due Soon"
                timing = f"due in {left} day(s)" if left is not None else "due shortly"
            rows = []
            if project_name:
                rows.append(("Project", str(project_name).strip()))
            if unit_number:
                rows.append(("Plot", str(unit_number).strip()))
            if milestone_label:
                rows.append(("Instalment", str(milestone_label).strip()))
            if when:
                rows.append(("Due Date", when))
            if money:
                rows.append(("Amount Due", money))
            out_money = _format_amount(outstanding, currency)
            if out_money:
                rows.append(("Outstanding After This", out_money))
            subject = _reminder_subject(money, project_name, unit_number, when)
            body = [
                f"This is a reminder that your next instalment for your Divine Vision Infra "
                f"plot is <strong>{_escape(timing)}</strong>.",
                "You can pay securely from your account in a few taps using the button below.",
            ]
            html = _luxury_email_html(
                headline=headline, preheader=f"{money} {timing}".strip(), greeting_name=name,
                body_paragraphs=body, detail_rows=rows, cta_label="Pay Now",
                cta_url=_payments_deeplink(self._customer_login_url),
                footer_note="Need help? Reply is not monitored - contact "
                            f"{_SUPPORT_LINE}.",
                has_logo=bool(self._logo_b64), accent="#c0392b" if overdue else "#e67e22",
            )
            text = _luxury_email_text(headline, name, body, rows, "Pay Now",
                                      _payments_deeplink(self._customer_login_url))
            return self._send(to=to, subject=subject, html=html, text=text)
        except Exception as e:
            logger.warning("payment_reminder_failed email=%s error=%s", email, e)
            return False

    def send_installment_receipt_async(self, email: str, **kwargs) -> None:
        self._run_async(self.send_installment_receipt, email, **kwargs)

    def send_installment_receipt(self, email: str, first_name: str = None, project_name: str = None,
                                 milestone_label: str = None, amount=None, receipt_pdf: bytes = None,
                                 receipt_filename: str = "payment-receipt.pdf",
                                 currency: str = "INR") -> bool:
        """Premium 'payment received' email with the receipt PDF attached. Never raises."""
        try:
            to = (email or "").strip()
            if not to or "@" not in to:
                return False
            name = (first_name or "").strip() or "there"
            money = _format_amount(amount, currency) or ""
            rows = []
            if project_name:
                rows.append(("Project", str(project_name).strip()))
            if milestone_label:
                rows.append(("Instalment", str(milestone_label).strip()))
            if money:
                rows.append(("Amount Received", money))
            body = [
                f"We have received your payment of <strong>{_escape(money)}</strong>. Thank you.",
                "Your official payment receipt is attached to this email, and is also available "
                "any time from your account.",
            ]
            html = _luxury_email_html(
                headline="Payment Received", preheader=f"Receipt for {money}".strip(),
                greeting_name=name, body_paragraphs=body, detail_rows=rows,
                cta_label="View My Payments", cta_url=_payments_deeplink(self._customer_login_url),
                footer_note=f"For assistance, contact {_SUPPORT_LINE}.",
                has_logo=bool(self._logo_b64), accent="#1e8449",
            )
            text = _luxury_email_text("Payment Received", name, body, rows,
                                      "View My Payments", _payments_deeplink(self._customer_login_url))
            attachments = None
            if receipt_pdf:
                try:
                    attachments = [{
                        "filename": receipt_filename or "payment-receipt.pdf",
                        "content": base64.b64encode(receipt_pdf).decode("ascii"),
                        "content_type": "application/pdf",
                    }]
                except Exception:
                    attachments = None
            return self._send(to=to, subject="Payment Received — Divine Vision Infra",
                              html=html, text=text, attachments=attachments)
        except Exception as e:
            logger.warning("installment_receipt_failed email=%s error=%s", email, e)
            return False

    def send_kyc_decision_async(self, email: str, **kwargs) -> None:
        self._run_async(self.send_kyc_decision, email, **kwargs)

    def send_kyc_decision(self, email: str, decision: str, first_name: str = None, project_name: str = None,
                          unit_number: str = None, booking_id: str = None, admin_note: str = None,
                          refund_instructions: str = None, amount=None, currency: str = "INR") -> bool:
        """KYC review outcome email (post-payment document review) - 'approved' or
        'rejected'. Carries the admin's own typed note so the customer knows exactly
        why, and (on rejection) how their refund will reach them. Never raises."""
        try:
            to = (email or "").strip()
            if not to or "@" not in to:
                return False
            name = (first_name or "").strip() or "there"
            approved = (decision or "").strip().lower() == "approved"
            rows = _booking_decision_detail_rows(booking_id, project_name, unit_number, amount, currency)

            if approved:
                headline = "KYC Verified — Booking Confirmed"
                body = [
                    "Your KYC documents have been verified and your booking is now confirmed.",
                    "You can now download your official booking receipt from your account.",
                ]
                cta_label = "Download Receipt"
                accent = "#1e8449"
            else:
                headline = "KYC Review — Action Needed"
                body = [
                    "After reviewing your submitted documents, we're unable to confirm this "
                    "booking at this time.",
                ]
                if refund_instructions:
                    body.append(refund_instructions)
                cta_label = "Contact Support"
                accent = "#c0392b"

            note = (admin_note or "").strip()
            if note:
                body.append(f"Note from our team: <em>{_escape(note)}</em>")

            html = _luxury_email_html(
                headline=headline, preheader=headline, greeting_name=name,
                body_paragraphs=body, detail_rows=rows, cta_label=cta_label,
                cta_url=_payments_deeplink(self._customer_login_url),
                footer_note=f"For assistance, contact {_SUPPORT_LINE}.",
                has_logo=bool(self._logo_b64), accent=accent,
            )
            text = _luxury_email_text(headline, name, body, rows, cta_label,
                                      _payments_deeplink(self._customer_login_url))
            subject = f"{headline} — Divine Vision Infra"
            return self._send(to=to, subject=subject, html=html, text=text)
        except Exception as e:
            logger.warning("kyc_decision_email_failed email=%s error=%s", email, e)
            return False

    def send_booking_cancellation_async(self, email: str, **kwargs) -> None:
        self._run_async(self.send_booking_cancellation, email, **kwargs)

    def send_booking_cancellation(self, email: str, first_name: str = None, project_name: str = None,
                                  unit_number: str = None, booking_id: str = None, admin_note: str = None,
                                  refund_instructions: str = None, amount=None, currency: str = "INR") -> bool:
        """Admin-initiated booking-cancellation email (e.g. the customer asked to
        cancel, at either the KYC-review or already-booked stage). Always carries
        refund_instructions - the client's requirement that a refund notice goes
        out no matter how the customer paid. Never raises."""
        try:
            to = (email or "").strip()
            if not to or "@" not in to:
                return False
            name = (first_name or "").strip() or "there"
            rows = _booking_decision_detail_rows(booking_id, project_name, unit_number, amount, currency,
                                                 amount_label="Amount Paid")

            body = ["Your booking with Divine Vision Infra has been cancelled as requested."]
            if refund_instructions:
                body.append(refund_instructions)
            note = (admin_note or "").strip()
            if note:
                body.append(f"Note from our team: <em>{_escape(note)}</em>")

            html = _luxury_email_html(
                headline="Booking Cancelled", preheader="Your booking has been cancelled",
                greeting_name=name, body_paragraphs=body, detail_rows=rows,
                cta_label="Contact Support", cta_url=_payments_deeplink(self._customer_login_url),
                footer_note=f"For assistance, contact {_SUPPORT_LINE}.",
                has_logo=bool(self._logo_b64), accent="#c0392b",
            )
            text = _luxury_email_text("Booking Cancelled", name, body, rows, "Contact Support",
                                      _payments_deeplink(self._customer_login_url))
            return self._send(to=to, subject="Booking Cancelled — Divine Vision Infra", html=html, text=text)
        except Exception as e:
            logger.warning("booking_cancellation_email_failed email=%s error=%s", email, e)
            return False

    # ----------------------------------------------------------------- private

    def _send(self, to: str, subject: str, html: str, text: str = None, attachments: list = None) -> bool:
        if not self.enabled:
            return False
        # No reply_to: this is a no-reply message. Replies bounce by design.
        payload = {
            "from": f"{_DEFAULT_FROM_NAME} <{self._from_email}>",
            "to": [to],
            "subject": subject,
            "html": html,
        }
        if text:
            payload["text"] = text
        files = []
        if self._logo_b64:
            files.append({
                "filename": "divine-vision-infra.png",
                "content": self._logo_b64,
                "content_id": _LOGO_CID,
            })
        for extra in (attachments or []):
            if isinstance(extra, dict) and extra.get("content"):
                files.append(extra)
        if files:
            payload["attachments"] = files
        try:
            resp = requests.post(
                _RESEND_ENDPOINT,
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as e:
            logger.warning("resend_request_failed to=%s error=%s", to, e)
            return False
        if resp.status_code >= 300:
            logger.warning("resend_rejected to=%s status=%s body=%s", to, resp.status_code, resp.text[:500])
            return False
        logger.info("resend_sent to=%s subject=%s", to, subject)
        return True

    def _run_async(self, target, *args, **kwargs) -> None:
        try:
            threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True).start()
        except Exception as e:
            logger.warning("resend_async_dispatch_failed target=%s error=%s", getattr(target, "__name__", target), e)


def dispatch_welcome_email(email_service, user_type: str, email: str,
                           first_name: str = None, username: str = None) -> None:
    """Fire-and-forget welcome email for a signup path. Skips silently when email
    is unconfigured or the user gave no address, and can never raise into the
    caller. Shared by broker and customer signup so the guard lives in one place."""
    try:
        if email_service and getattr(email_service, "enabled", False) and email:
            email_service.send_welcome_async(user_type, email, first_name=first_name, username=username)
    except Exception as e:
        logger.warning("welcome_email_dispatch_failed user_type=%s error=%s", user_type, e)


def dispatch_booking_confirmation_email(email_service, email: str, first_name: str = None,
                                        project_name: str = None, unit_number: str = None,
                                        amount=None, currency: str = "INR") -> None:
    """Fire-and-forget booking-confirmation email. Skips silently when email is
    unconfigured or no address is given, and can never raise into the caller."""
    try:
        if email_service and getattr(email_service, "enabled", False) and email:
            email_service.send_booking_confirmation_async(
                email, first_name=first_name, project_name=project_name,
                unit_number=unit_number, amount=amount, currency=currency,
            )
    except Exception as e:
        logger.warning("booking_confirmation_dispatch_failed error=%s", e)


def dispatch_kyc_decision_email(email_service, email: str, **kwargs) -> None:
    """Fire-and-forget KYC approve/reject notification. Never raises."""
    try:
        if email_service and getattr(email_service, "enabled", False) and email:
            email_service.send_kyc_decision_async(email, **kwargs)
    except Exception as e:
        logger.warning("kyc_decision_dispatch_failed error=%s", e)


def dispatch_booking_cancellation_email(email_service, email: str, **kwargs) -> None:
    """Fire-and-forget booking-cancellation notification. Never raises."""
    try:
        if email_service and getattr(email_service, "enabled", False) and email:
            email_service.send_booking_cancellation_async(email, **kwargs)
    except Exception as e:
        logger.warning("booking_cancellation_dispatch_failed error=%s", e)


def dispatch_installment_receipt_email(email_service, email: str, **kwargs) -> None:
    """Fire-and-forget 'payment received' email + receipt PDF. Never raises."""
    try:
        if email_service and getattr(email_service, "enabled", False) and email:
            email_service.send_installment_receipt_async(email, **kwargs)
    except Exception as e:
        logger.warning("installment_receipt_dispatch_failed error=%s", e)


def dispatch_payment_reminder_email(email_service, email: str, **kwargs) -> bool:
    """Synchronous send used by the daily reminder job (it needs the sent/failed
    result to write the dedupe row). Never raises - returns False on any problem."""
    try:
        if not (email_service and getattr(email_service, "enabled", False) and email):
            return False
        return bool(email_service.send_payment_reminder(email, **kwargs))
    except Exception as e:
        logger.warning("payment_reminder_dispatch_failed error=%s", e)
        return False


def _load_logo_b64(path: str):
    try:
        p = Path(path)
        if not p.is_file():
            logger.info("brand_logo_not_found path=%s - using text wordmark", path)
            return None
        return base64.b64encode(p.read_bytes()).decode("ascii")
    except Exception as e:
        logger.warning("brand_logo_load_failed path=%s error=%s", path, e)
        return None


# --------------------------------------------------------------------- copy

# Brand palette - Trust Navy #2C3E50, Safety Orange #E67E22, Concrete Grey #95A5A6.
# Header type evokes Oswald (condensed, wide-tracked, uppercase); body evokes Lato.
# Email clients won't load webfonts reliably, so every stack falls back to solid
# system fonts.
_FONT_HEAD = "'Oswald','Arial Narrow',Arial,Helvetica,sans-serif"
_FONT_BODY = "'Lato','Helvetica Neue',Helvetica,Arial,sans-serif"

# A short, enhanced statement of what Divine Vision Infra is - shared by both emails.
_VISION_LINE = (
    "Divine Vision Infra was founded on a single conviction &mdash; that land is not "
    "merely an asset, but the foundation on which families build their future. We "
    "develop RERA-approved plotted communities and residential addresses where every "
    "boundary is surveyed, every title is clear, and every commitment is put in "
    "writing, so that what you hold today stands unquestioned for generations."
)

_COPY = {
    CHANNEL_PARTNER: {
        "subject": "Welcome to the Divine Vision Infra Partner Network",
        "heading": "Welcome to the Network",
        "greeting_word": "Dear",
        "intro": (
            "It is our privilege to welcome you as a <strong>channel partner</strong> of "
            "Divine Vision Infra. Your account is now active, and you now represent "
            "developments built on verified titles, transparent pricing and enduring value."
        ),
        "features": [
            ("Partner Dashboard", "Manage your profile, your clients and your performance in one place."),
            ("Live Inventory", "See real-time availability, layouts and pricing across every project."),
            ("Visits &amp; Commissions", "Register client site visits and track every commission you earn."),
        ],
        "cta_label": "Log in as a Channel Partner",
        "signoff": "The Divine Vision Infra Partnerships Team",
        "footer_note": (
            "This is an automated message sent to you because a channel partner account "
            "was created with this address."
        ),
    },
    CUSTOMER: {
        "subject": "Welcome to Divine Vision Infra",
        "heading": "Welcome Home",
        "greeting_word": "Dear",
        "intro": (
            "It is our privilege to welcome you to <strong>Divine Vision Infra</strong>. "
            "Your account is now active, and you have joined a community of homeowners and "
            "investors who chose certainty over compromise."
        ),
        "features": [
            ("Your Dashboard", "Track your profile, your documents and your KYC status in one secure place."),
            ("Live Inventory", "Browse real-time plot and unit availability, layouts and pricing."),
            ("Site Visits &amp; Bookings", "Schedule a site visit and follow your reservation and payments."),
        ],
        "cta_label": "Log in as a Customer",
        "signoff": "The Divine Vision Infra Team",
        "footer_note": (
            "This is an automated message sent to you because an account was created with "
            "this address."
        ),
    },
}


def _welcome_text(variant: str, name: str, login_url: str) -> str:
    c = _COPY[variant]
    feats = "\n".join(f"  - {t}: {d}" for t, d in _strip_tags_pairs(c["features"]))
    return (
        f"{c['greeting_word']} {name},\n\n"
        f"{_strip_tags(c['intro'])}\n\n"
        f"{_strip_tags(_VISION_LINE)}\n\n"
        "What is ready for you:\n"
        f"{feats}\n\n"
        f"{c['cta_label']}: {login_url}\n\n"
        "This mailbox is not monitored. For assistance, visit www.divinevisioninfra.com.\n\n"
        f"Warm regards,\n{c['signoff']}\n"
        "Divine Vision Infra"
    )


def _welcome_html(variant: str, name: str, login_url: str, has_logo: bool) -> str:
    c = _COPY[variant]
    safe_name = _escape(name)
    safe_url = _escape(login_url)

    if has_logo:
        brand_mark = (
            f'<img src="cid:{_LOGO_CID}" width="180" alt="Divine Vision Infra" '
            f'style="display:block;border:0;outline:none;width:180px;max-width:60%;height:auto;margin:0 auto;">'
        )
    else:
        brand_mark = (
            f'<div style="font-family:{_FONT_HEAD};font-size:22px;letter-spacing:6px;'
            f'text-transform:uppercase;color:#ffffff;font-weight:700;">Divine Vision Infra</div>'
        )

    feature_rows = ""
    last = len(c["features"]) - 1
    for i, (title, desc) in enumerate(c["features"]):
        bottom = "border-bottom:1px solid #e5e8ea;" if i == last else ""
        feature_rows += f"""
                <tr>
                  <td style="padding:14px 0;border-top:1px solid #e5e8ea;{bottom}font-family:{_FONT_BODY};">
                    <span style="font-family:{_FONT_HEAD};font-size:15px;letter-spacing:2px;text-transform:uppercase;color:#2c3e50;font-weight:700;">{title}</span><br>
                    <span style="font-size:15px;line-height:1.6;color:#7b8a99;">{desc}</span>
                  </td>
                </tr>"""

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="x-apple-disable-message-reformatting">
<title>{c['subject']}</title>
</head>
<body style="margin:0;padding:0;background-color:#1c2833;">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;">
    Your Divine Vision Infra account is now active.
  </div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#1c2833;">
    <tr>
      <td align="center" style="padding:40px 16px;">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;background-color:#ffffff;border-radius:2px;overflow:hidden;border:1px solid #2c3e50;">

          <!-- Top rule -->
          <tr><td style="height:6px;background-color:#e67e22;font-size:0;line-height:0;">&nbsp;</td></tr>

          <!-- Header -->
          <tr>
            <td align="center" style="background-color:#2c3e50;padding:40px 40px 36px 40px;">
              {brand_mark}
              <div style="height:20px;line-height:20px;font-size:0;">&nbsp;</div>
              <div style="font-family:{_FONT_HEAD};font-size:32px;letter-spacing:3px;text-transform:uppercase;color:#ffffff;font-weight:700;line-height:1.2;">
                {c['heading']}
              </div>
              <div style="height:14px;line-height:14px;font-size:0;">&nbsp;</div>
              <div style="width:64px;height:3px;background-color:#e67e22;margin:0 auto;font-size:0;line-height:0;">&nbsp;</div>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:44px 48px 12px 48px;font-family:{_FONT_BODY};color:#2c3e50;">
              <p style="margin:0 0 20px 0;font-size:18px;line-height:1.6;color:#2c3e50;">
                {c['greeting_word']} {safe_name},
              </p>
              <p style="margin:0 0 20px 0;font-size:16px;line-height:1.7;color:#4a5b6b;">
                {c['intro']}
              </p>
              <p style="margin:0 0 22px 0;font-size:16px;line-height:1.7;color:#4a5b6b;">
                {_VISION_LINE}
              </p>
              <p style="margin:0 0 8px 0;font-size:16px;line-height:1.7;color:#4a5b6b;">
                Everything you need is ready for you:
              </p>
            </td>
          </tr>

          <!-- Feature blocks -->
          <tr>
            <td style="padding:0 48px 8px 48px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{feature_rows}
              </table>
            </td>
          </tr>

          <!-- CTA -->
          <tr>
            <td align="center" style="padding:34px 48px 14px 48px;">
              <table role="presentation" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td align="center" bgcolor="#e67e22" style="border-radius:2px;">
                    <a href="{safe_url}" target="_blank"
                       style="display:inline-block;padding:16px 42px;font-family:{_FONT_HEAD};font-size:15px;letter-spacing:3px;text-transform:uppercase;color:#ffffff;text-decoration:none;font-weight:700;">
                      {c['cta_label']}
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <tr>
            <td style="padding:20px 48px 44px 48px;font-family:{_FONT_BODY};">
              <p style="margin:0;font-size:15px;line-height:1.7;color:#7b8a99;">
                This mailbox is not monitored. For assistance, visit
                <a href="{safe_url}" target="_blank" style="color:#e67e22;text-decoration:none;">www.divinevisioninfra.com</a>.
              </p>
              <p style="margin:22px 0 0 0;font-size:16px;line-height:1.6;color:#2c3e50;">
                Warm regards,<br>
                <strong style="color:#2c3e50;">{c['signoff']}</strong>
              </p>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td align="center" style="background-color:#2c3e50;padding:26px 40px;font-family:{_FONT_BODY};">
              <div style="font-family:{_FONT_HEAD};font-size:12px;letter-spacing:5px;text-transform:uppercase;color:#ffffff;font-weight:700;">
                Divine Vision Infra
              </div>
              <div style="height:8px;line-height:8px;font-size:0;">&nbsp;</div>
              <div style="font-size:12px;line-height:1.6;color:#95a5a6;">
                {c['footer_note']}
              </div>
            </td>
          </tr>
          <tr><td style="height:6px;background-color:#e67e22;font-size:0;line-height:0;">&nbsp;</td></tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


_BOOKING_SUBJECT = "Your Booking Is Confirmed — Divine Vision Infra"
_BOOKING_INTRO = (
    "Congratulations. Your booking with <strong>Divine Vision Infra</strong> is "
    "confirmed, your payment has been received, and your plot is now reserved in "
    "your name. This is the beginning of something built to stand for generations."
)
_BOOKING_CLOSING = (
    "Our team will be in touch shortly with your documentation and the next steps. "
    "You can view your booking at any time from your dashboard."
)


def _format_amount(amount, currency: str = "INR") -> str:
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    symbol = "₹" if (currency or "INR").upper() == "INR" else f"{currency} "
    return f"{symbol}{value:,.0f}"


def _booking_decision_detail_rows(booking_id, project_name, unit_number, amount, currency: str = "INR",
                                  amount_label: str = "Amount"):
    """[(label, value)] shared by the KYC-decision and booking-cancellation
    emails - Booking ID, Project, Plot, Amount, only the rows that actually have
    a value. Kept as one helper so a future change to this shape (e.g. adding a
    Payment Method row) only needs to happen once."""
    rows = []
    if booking_id and str(booking_id).strip():
        rows.append(("Booking ID", str(booking_id).strip()))
    if project_name and str(project_name).strip():
        rows.append(("Project", str(project_name).strip()))
    if unit_number and str(unit_number).strip():
        rows.append(("Plot", str(unit_number).strip()))
    money = _format_amount(amount, currency)
    if money:
        rows.append((amount_label, money))
    return rows


def _booking_detail_rows(project_name, unit_number, amount, currency: str = "INR"):
    """[(label, value)] for the details block - only rows that actually have a value."""
    rows = []
    if project_name and str(project_name).strip():
        rows.append(("Project", str(project_name).strip()))
    if unit_number and str(unit_number).strip():
        rows.append(("Unit / Plot", str(unit_number).strip()))
    money = _format_amount(amount, currency)
    if money:
        rows.append(("Amount Received", money))
    return rows


def _booking_confirmation_text(name: str, details, login_url: str) -> str:
    lines = "\n".join(f"  - {label}: {value}" for label, value in details)
    block = f"Your booking\n{lines}\n\n" if lines else ""
    return (
        f"Dear {name},\n\n"
        f"{_strip_tags(_BOOKING_INTRO)}\n\n"
        f"{block}"
        f"{_strip_tags(_VISION_LINE)}\n\n"
        f"{_strip_tags(_BOOKING_CLOSING)}\n\n"
        f"View my booking: {login_url}\n\n"
        "This mailbox is not monitored. For assistance, visit www.divinevisioninfra.com.\n\n"
        "Warm regards,\nThe Divine Vision Infra Team\n"
        "Divine Vision Infra"
    )


def _booking_confirmation_html(name: str, details, login_url: str, has_logo: bool) -> str:
    safe_name = _escape(name)
    safe_url = _escape(login_url)

    if has_logo:
        brand_mark = (
            f'<img src="cid:{_LOGO_CID}" width="180" alt="Divine Vision Infra" '
            f'style="display:block;border:0;outline:none;width:180px;max-width:60%;height:auto;margin:0 auto;">'
        )
    else:
        brand_mark = (
            f'<div style="font-family:{_FONT_HEAD};font-size:22px;letter-spacing:6px;'
            f'text-transform:uppercase;color:#ffffff;font-weight:700;">Divine Vision Infra</div>'
        )

    detail_block = ""
    if details:
        detail_rows = ""
        last = len(details) - 1
        for i, (label, value) in enumerate(details):
            bottom = "border-bottom:1px solid #e5e8ea;" if i == last else ""
            detail_rows += f"""
                <tr>
                  <td style="padding:14px 0;border-top:1px solid #e5e8ea;{bottom}font-family:{_FONT_BODY};width:42%;">
                    <span style="font-family:{_FONT_HEAD};font-size:13px;letter-spacing:2px;text-transform:uppercase;color:#7b8a99;font-weight:700;">{_escape(label)}</span>
                  </td>
                  <td style="padding:14px 0;border-top:1px solid #e5e8ea;{bottom}font-family:{_FONT_BODY};text-align:right;">
                    <span style="font-size:16px;line-height:1.5;color:#2c3e50;font-weight:700;">{_escape(value)}</span>
                  </td>
                </tr>"""
        detail_block = f"""
          <tr>
            <td style="padding:6px 48px 8px 48px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{detail_rows}
              </table>
            </td>
          </tr>"""

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="x-apple-disable-message-reformatting">
<title>{_BOOKING_SUBJECT}</title>
</head>
<body style="margin:0;padding:0;background-color:#1c2833;">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;">
    Your plot booking and payment have been received.
  </div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#1c2833;">
    <tr>
      <td align="center" style="padding:40px 16px;">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;background-color:#ffffff;border-radius:2px;overflow:hidden;border:1px solid #2c3e50;">

          <tr><td style="height:6px;background-color:#e67e22;font-size:0;line-height:0;">&nbsp;</td></tr>

          <tr>
            <td align="center" style="background-color:#2c3e50;padding:40px 40px 36px 40px;">
              {brand_mark}
              <div style="height:20px;line-height:20px;font-size:0;">&nbsp;</div>
              <div style="font-family:{_FONT_HEAD};font-size:32px;letter-spacing:3px;text-transform:uppercase;color:#ffffff;font-weight:700;line-height:1.2;">
                Booking Confirmed
              </div>
              <div style="height:14px;line-height:14px;font-size:0;">&nbsp;</div>
              <div style="width:64px;height:3px;background-color:#e67e22;margin:0 auto;font-size:0;line-height:0;">&nbsp;</div>
            </td>
          </tr>

          <tr>
            <td style="padding:44px 48px 12px 48px;font-family:{_FONT_BODY};color:#2c3e50;">
              <p style="margin:0 0 20px 0;font-size:18px;line-height:1.6;color:#2c3e50;">
                Dear {safe_name},
              </p>
              <p style="margin:0 0 22px 0;font-size:16px;line-height:1.7;color:#4a5b6b;">
                {_BOOKING_INTRO}
              </p>
            </td>
          </tr>
{detail_block}
          <tr>
            <td style="padding:16px 48px 4px 48px;font-family:{_FONT_BODY};">
              <p style="margin:0 0 22px 0;font-size:16px;line-height:1.7;color:#4a5b6b;">
                {_VISION_LINE}
              </p>
              <p style="margin:0;font-size:16px;line-height:1.7;color:#4a5b6b;">
                {_BOOKING_CLOSING}
              </p>
            </td>
          </tr>

          <tr>
            <td align="center" style="padding:32px 48px 14px 48px;">
              <table role="presentation" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td align="center" bgcolor="#e67e22" style="border-radius:2px;">
                    <a href="{safe_url}" target="_blank"
                       style="display:inline-block;padding:16px 42px;font-family:{_FONT_HEAD};font-size:15px;letter-spacing:3px;text-transform:uppercase;color:#ffffff;text-decoration:none;font-weight:700;">
                      View My Booking
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <tr>
            <td style="padding:20px 48px 44px 48px;font-family:{_FONT_BODY};">
              <p style="margin:0;font-size:15px;line-height:1.7;color:#7b8a99;">
                This mailbox is not monitored. For assistance, visit
                <a href="{safe_url}" target="_blank" style="color:#e67e22;text-decoration:none;">www.divinevisioninfra.com</a>.
              </p>
              <p style="margin:22px 0 0 0;font-size:16px;line-height:1.6;color:#2c3e50;">
                Warm regards,<br>
                <strong style="color:#2c3e50;">The Divine Vision Infra Team</strong>
              </p>
            </td>
          </tr>

          <tr>
            <td align="center" style="background-color:#2c3e50;padding:26px 40px;font-family:{_FONT_BODY};">
              <div style="font-family:{_FONT_HEAD};font-size:12px;letter-spacing:5px;text-transform:uppercase;color:#ffffff;font-weight:700;">
                Divine Vision Infra
              </div>
              <div style="height:8px;line-height:8px;font-size:0;">&nbsp;</div>
              <div style="font-size:12px;line-height:1.6;color:#95a5a6;">
                This is an automated confirmation of a booking made under this account.
              </div>
            </td>
          </tr>
          <tr><td style="height:6px;background-color:#e67e22;font-size:0;line-height:0;">&nbsp;</td></tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _escape(raw: str) -> str:
    return (
        (raw or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _strip_tags(raw: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", raw).replace("&mdash;", "-").replace("&amp;", "&")


def _strip_tags_pairs(pairs):
    return [(_strip_tags(t), _strip_tags(d)) for t, d in pairs]


# --------------------------------------------------------- instalment emails

_SUPPORT_LINE = "crm2@divinevisioninfra.com / +91-92549 72701"


def _payments_deeplink(login_url: str) -> str:
    base = (login_url or _DEFAULT_CUSTOMER_LOGIN_URL).strip().rstrip("/")
    return f"{base}/customer/profile#payments"


def _format_reminder_date(value) -> str:
    from datetime import date as _date, datetime as _dt
    if isinstance(value, (_date, _dt)):
        d = value.date() if isinstance(value, _dt) else value
        return d.strftime("%d %b %Y")
    text = str(value or "").strip()
    for f in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return _dt.strptime(text[:10], f).strftime("%d %b %Y")
        except ValueError:
            continue
    return text[:10]


def _reminder_subject(money: str, project_name, unit_number, when: str) -> str:
    bits = []
    if money:
        bits.append(money)
    where = " ".join(str(x).strip() for x in (project_name, ("Plot " + str(unit_number)) if unit_number else "") if x)
    tail = f" for {where}" if where else ""
    by = f" by {when}" if when else ""
    amount_part = bits[0] if bits else "instalment"
    return f"Payment due - {amount_part}{tail}{by}".strip()


def _luxury_email_html(headline: str, preheader: str, greeting_name: str, body_paragraphs: list,
                       detail_rows: list, cta_label: str, cta_url: str, footer_note: str,
                       has_logo: bool, accent: str = "#e67e22") -> str:
    safe_name = _escape(greeting_name)
    safe_url = _escape(cta_url)
    accent = accent if (accent or "").startswith("#") else "#e67e22"

    if has_logo:
        brand_mark = (
            f'<img src="cid:{_LOGO_CID}" width="180" alt="Divine Vision Infra" '
            f'style="display:block;border:0;outline:none;width:180px;max-width:60%;height:auto;margin:0 auto;">'
        )
    else:
        brand_mark = (
            f'<div style="font-family:{_FONT_HEAD};font-size:22px;letter-spacing:6px;'
            f'text-transform:uppercase;color:#ffffff;font-weight:700;">Divine Vision Infra</div>'
        )

    detail_block = ""
    if detail_rows:
        last = len(detail_rows) - 1
        trs = ""
        for i, (label, value) in enumerate(detail_rows):
            bottom = "border-bottom:1px solid #e5e8ea;" if i == last else ""
            trs += f"""
                <tr>
                  <td style="padding:13px 0;border-top:1px solid #e5e8ea;{bottom}font-family:{_FONT_BODY};width:46%;">
                    <span style="font-family:{_FONT_HEAD};font-size:12px;letter-spacing:2px;text-transform:uppercase;color:#7b8a99;font-weight:700;">{_escape(label)}</span>
                  </td>
                  <td style="padding:13px 0;border-top:1px solid #e5e8ea;{bottom}font-family:{_FONT_BODY};text-align:right;">
                    <span style="font-size:16px;line-height:1.5;color:#2c3e50;font-weight:700;">{_escape(value)}</span>
                  </td>
                </tr>"""
        detail_block = f"""
          <tr><td style="padding:6px 48px 8px 48px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{trs}
            </table>
          </td></tr>"""

    paras = "".join(
        f'<p style="margin:0 0 18px 0;font-size:16px;line-height:1.7;color:#4a5b6b;">{p}</p>'
        for p in body_paragraphs
    )

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="x-apple-disable-message-reformatting">
<title>{_escape(headline)} - Divine Vision Infra</title>
</head>
<body style="margin:0;padding:0;background-color:#1c2833;">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;">{_escape(preheader)}</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#1c2833;">
    <tr><td align="center" style="padding:40px 16px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;background-color:#ffffff;border-radius:2px;overflow:hidden;border:1px solid #2c3e50;">
        <tr><td style="height:6px;background-color:{accent};font-size:0;line-height:0;">&nbsp;</td></tr>
        <tr><td align="center" style="background-color:#2c3e50;padding:40px 40px 34px 40px;">
          {brand_mark}
          <div style="height:18px;line-height:18px;font-size:0;">&nbsp;</div>
          <div style="font-family:{_FONT_HEAD};font-size:30px;letter-spacing:3px;text-transform:uppercase;color:#ffffff;font-weight:700;line-height:1.2;">{_escape(headline)}</div>
          <div style="height:12px;line-height:12px;font-size:0;">&nbsp;</div>
          <div style="width:64px;height:3px;background-color:{accent};margin:0 auto;font-size:0;line-height:0;">&nbsp;</div>
        </td></tr>
        <tr><td style="padding:42px 48px 10px 48px;font-family:{_FONT_BODY};color:#2c3e50;">
          <p style="margin:0 0 20px 0;font-size:18px;line-height:1.6;color:#2c3e50;">Dear {safe_name},</p>
          {paras}
        </td></tr>
{detail_block}
        <tr><td align="center" style="padding:30px 48px 14px 48px;">
          <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
            <td align="center" bgcolor="{accent}" style="border-radius:2px;">
              <a href="{safe_url}" target="_blank" style="display:inline-block;padding:16px 44px;font-family:{_FONT_HEAD};font-size:15px;letter-spacing:3px;text-transform:uppercase;color:#ffffff;text-decoration:none;font-weight:700;">{_escape(cta_label)}</a>
            </td>
          </tr></table>
        </td></tr>
        <tr><td style="padding:18px 48px 42px 48px;font-family:{_FONT_BODY};">
          <p style="margin:0;font-size:14px;line-height:1.7;color:#7b8a99;">{_escape(footer_note)}</p>
          <p style="margin:20px 0 0 0;font-size:16px;line-height:1.6;color:#2c3e50;">Warm regards,<br><strong style="color:#2c3e50;">The Divine Vision Infra Team</strong></p>
        </td></tr>
        <tr><td align="center" style="background-color:#2c3e50;padding:24px 40px;font-family:{_FONT_BODY};">
          <div style="font-family:{_FONT_HEAD};font-size:12px;letter-spacing:5px;text-transform:uppercase;color:#ffffff;font-weight:700;">Divine Vision Infra</div>
          <div style="height:8px;line-height:8px;font-size:0;">&nbsp;</div>
          <div style="font-size:12px;line-height:1.6;color:#95a5a6;">This is an automated message for a booking held under this account.</div>
        </td></tr>
        <tr><td style="height:6px;background-color:{accent};font-size:0;line-height:0;">&nbsp;</td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


_OTP_GOLD = "#c9a648"
_OTP_GOLD_DIM = "#8a712f"
_OTP_INK = "#0e1116"
_OTP_CHARCOAL = "#171b21"
_FONT_DISPLAY = "'Playfair Display','Georgia',serif"


def _otp_email_html(name: str, otp: str, expires_minutes: int, has_logo: bool) -> str:
    """Bespoke, higher-end treatment reserved for the OTP moment specifically -
    black/charcoal ground, a hairline gold frame and wide-tracked serif display type,
    with the code itself as the one deliberate focal point on the page."""
    safe_name = _escape(name)
    spaced_otp = " ".join(list(otp)) if otp else ""

    if has_logo:
        brand_mark = (
            f'<img src="cid:{_LOGO_CID}" width="160" alt="Divine Vision Infra" '
            f'style="display:block;border:0;outline:none;width:160px;max-width:55%;height:auto;margin:0 auto;">'
        )
    else:
        brand_mark = (
            f'<div style="font-family:{_FONT_DISPLAY};font-size:20px;letter-spacing:7px;'
            f'text-transform:uppercase;color:{_OTP_GOLD};font-weight:700;">Divine Vision Infra</div>'
        )

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="x-apple-disable-message-reformatting">
<title>Your Password Reset Code</title>
</head>
<body style="margin:0;padding:0;background-color:{_OTP_INK};">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;">Your one-time code is {_escape(otp)}. Valid for {expires_minutes} minutes.</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:{_OTP_INK};">
    <tr>
      <td align="center" style="padding:48px 16px;">
        <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0"
               style="width:560px;max-width:560px;background-color:{_OTP_CHARCOAL};border:1px solid {_OTP_GOLD_DIM};">

          <tr><td style="height:2px;background:linear-gradient(90deg,{_OTP_INK},{_OTP_GOLD},{_OTP_INK});font-size:0;line-height:0;">&nbsp;</td></tr>

          <!-- Crest / brand -->
          <tr>
            <td align="center" style="padding:48px 40px 28px 40px;">
              {brand_mark}
              <div style="height:22px;line-height:22px;font-size:0;">&nbsp;</div>
              <div style="width:40px;height:1px;background-color:{_OTP_GOLD_DIM};margin:0 auto;font-size:0;line-height:0;">&nbsp;</div>
              <div style="height:22px;line-height:22px;font-size:0;">&nbsp;</div>
              <div style="font-family:{_FONT_DISPLAY};font-size:26px;letter-spacing:1px;color:#f4efe4;font-weight:700;line-height:1.3;">
                Private Access Code
              </div>
            </td>
          </tr>

          <!-- Greeting / copy -->
          <tr>
            <td style="padding:0 48px 8px 48px;font-family:{_FONT_BODY};">
              <p style="margin:0 0 18px 0;font-size:16px;line-height:1.7;color:#c7ccd3;">
                Dear {safe_name},
              </p>
              <p style="margin:0 0 8px 0;font-size:15px;line-height:1.7;color:#8f97a3;">
                A request was made to reset the password on your Divine Vision Infra account.
                Present the code below to complete it.
              </p>
            </td>
          </tr>

          <!-- The code itself -->
          <tr>
            <td align="center" style="padding:20px 40px 8px 40px;">
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border:1px solid {_OTP_GOLD};background-color:{_OTP_INK};">
                <tr>
                  <td style="padding:26px 38px;">
                    <div style="font-family:{_FONT_HEAD};font-size:38px;letter-spacing:14px;color:{_OTP_GOLD};font-weight:700;text-align:center;">
                      {_escape(spaced_otp)}
                    </div>
                  </td>
                </tr>
              </table>
              <div style="height:16px;line-height:16px;font-size:0;">&nbsp;</div>
              <div style="font-family:{_FONT_BODY};font-size:12.5px;letter-spacing:2px;text-transform:uppercase;color:{_OTP_GOLD_DIM};">
                Valid for {expires_minutes} minutes
              </div>
            </td>
          </tr>

          <!-- Security note -->
          <tr>
            <td style="padding:28px 48px 8px 48px;font-family:{_FONT_BODY};">
              <p style="margin:0;font-size:14px;line-height:1.7;color:#6d7480;">
                For your security, this code is single-use and known only to you. Divine Vision Infra
                will never call or write to ask for it. If you did not request this, no action is
                needed - your password remains unchanged.
              </p>
            </td>
          </tr>

          <tr>
            <td style="padding:26px 48px 44px 48px;font-family:{_FONT_BODY};border-top:1px solid #2a303a;margin-top:10px;">
              <p style="margin:22px 0 0 0;font-size:14px;line-height:1.7;color:#5b6270;">
                For assistance, contact {_SUPPORT_LINE}.
              </p>
              <p style="margin:18px 0 0 0;font-size:15px;line-height:1.6;color:#c7ccd3;">
                With distinction,<br>
                <strong style="color:{_OTP_GOLD};">The Divine Vision Infra Team</strong>
              </p>
            </td>
          </tr>

          <tr>
            <td align="center" style="padding:22px 40px;background-color:{_OTP_INK};">
              <div style="font-family:{_FONT_HEAD};font-size:11px;letter-spacing:5px;text-transform:uppercase;color:{_OTP_GOLD_DIM};font-weight:700;">
                Divine Vision Infra
              </div>
            </td>
          </tr>
          <tr><td style="height:2px;background:linear-gradient(90deg,{_OTP_INK},{_OTP_GOLD},{_OTP_INK});font-size:0;line-height:0;">&nbsp;</td></tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _otp_email_text(name: str, otp: str, expires_minutes: int) -> str:
    return (
        f"Dear {name},\n\n"
        "A request was made to reset the password on your Divine Vision Infra account.\n\n"
        f"Your one-time access code:\n\n    {otp}\n\n"
        f"This code is valid for {expires_minutes} minutes and can be used once.\n\n"
        "Divine Vision Infra will never call or write to ask for this code. If you did not "
        "request this, no action is needed - your password remains unchanged.\n\n"
        f"For assistance, contact {_SUPPORT_LINE}.\n\n"
        "With distinction,\nThe Divine Vision Infra Team"
    )


def _luxury_email_text(headline: str, name: str, body_paragraphs: list, detail_rows: list,
                       cta_label: str, cta_url: str) -> str:
    paras = "\n\n".join(_strip_tags(p) for p in body_paragraphs)
    lines = "\n".join(f"  - {label}: {value}" for label, value in (detail_rows or []))
    block = f"\n\n{lines}" if lines else ""
    return (
        f"{headline}\n\n"
        f"Dear {name},\n\n"
        f"{paras}{block}\n\n"
        f"{cta_label}: {cta_url}\n\n"
        f"For assistance, contact {_SUPPORT_LINE}.\n\n"
        "Warm regards,\nThe Divine Vision Infra Team"
    )
