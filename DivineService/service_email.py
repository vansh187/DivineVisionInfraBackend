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

    # ----------------------------------------------------------------- private

    def _send(self, to: str, subject: str, html: str, text: str = None) -> bool:
        if not self.enabled:
            return False
        # No reply_to: this is a no-reply welcome message. Replies bounce by design.
        payload = {
            "from": f"{_DEFAULT_FROM_NAME} <{self._from_email}>",
            "to": [to],
            "subject": subject,
            "html": html,
        }
        if text:
            payload["text"] = text
        if self._logo_b64:
            payload["attachments"] = [{
                "filename": "divine-vision-infra.png",
                "content": self._logo_b64,
                "content_id": _LOGO_CID,
            }]
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
