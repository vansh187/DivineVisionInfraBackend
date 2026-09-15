import logging
import random
from datetime import datetime, timezone

from Divinepersistence import persistenceAdmin

logger = logging.getLogger(__name__)


class serviceHelpSupport:
    """Backs the admin panel's Help & Support form (POST /admin/support-tickets).
    A ticket has no database record of its own - the ticket number exists only
    to give the support team and the reporting admin something to reference in
    conversation/email, so submitting one is exactly one random ticket-number
    generation plus one email send. Every public method raises only
    ValueError (bad input, mapped to a 4xx by the API layer) or RuntimeError
    (an internal failure, including an undelivered notification, mapped to a
    5xx) - never lets an unexpected exception escape as anything else."""

    def __init__(self, admin_persistence: persistenceAdmin = None, email_service=None):
        self._admin_persistence = admin_persistence or persistenceAdmin()
        self._email_service_override = email_service

    def _email(self):
        """Lazily built so constructing this service never requires Resend to
        be configured - only actually submitting a ticket does."""
        if self._email_service_override is not None:
            return self._email_service_override
        try:
            from DivineService.service_email import serviceEmail
            self._email_service_override = serviceEmail()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("help_support.email_service_init_failed error=%s", e)
            self._email_service_override = None
        return self._email_service_override

    def _generate_ticket_number(self) -> str:
        # 6 random digits, zero-padded - a human-readable reference number for
        # conversation/email, not a database key, so no uniqueness contract.
        return f"TTK-{random.randint(0, 999999):06d}"

    def _raiser_details(self, admin_id: str):
        """Best-effort lookup of the reporting admin's name/email for the
        notification - a lookup failure must never block the ticket itself
        from going out, so any error here is swallowed."""
        try:
            clean_admin_id = (admin_id or "").strip()
            if not clean_admin_id:
                return None, None
            admin = self._admin_persistence.get_by_id(clean_admin_id)
            if not admin:
                return None, None
            return getattr(admin, "full_name", None), getattr(admin, "email", None)
        except Exception:
            logger.warning("help_support.raiser_lookup_failed admin_id=%s", admin_id, exc_info=True)
            return None, None

    def submit_ticket(self, subject: str, description: str, admin_id: str = None) -> dict:
        """POST /admin/support-tickets. Raises ValueError('empty_subject') /
        ValueError('empty_description') for blank input - the request DTO
        already rejects this at the HTTP layer, but a service used directly
        (a test, a future caller) must not depend on that - and
        RuntimeError('ticket_email_failed') when the notification could not
        be delivered, since an undelivered ticket is a silent failure of the
        entire feature, not a best-effort side effect."""
        try:
            clean_subject = (subject or "").strip()
            clean_description = (description or "").strip()
            if not clean_subject:
                raise ValueError("empty_subject")
            if not clean_description:
                raise ValueError("empty_description")

            ticket_number = self._generate_ticket_number()
            raiser_name, raiser_email = self._raiser_details(admin_id)
            submitted_date = datetime.now(timezone.utc)

            email_service = self._email()
            sent = False
            if email_service and getattr(email_service, "enabled", False):
                sent = bool(email_service.send_ticket_notification(
                    ticket_number=ticket_number, subject=clean_subject, description=clean_description,
                    raised_by_name=raiser_name, raised_by_email=raiser_email,
                ))
            else:
                logger.warning("help_support.email_unconfigured ticket_number=%s", ticket_number)

            if not sent:
                raise RuntimeError("ticket_email_failed")

            return {
                "ticket_number": ticket_number,
                "subject": clean_subject,
                "description": clean_description,
                "raised_by": raiser_name,
                "submitted_date": submitted_date,
                "email_sent": sent,
            }
        except ValueError:
            raise
        except RuntimeError:
            raise
        except Exception:
            logger.exception("help_support.submit_ticket_failed")
            raise RuntimeError("submit_ticket_failed")
