import logging
import time
from fastapi import APIRouter, Depends, HTTPException

from DivineDTO.models import SupportTicketRequestDTO, SupportTicketResponseDTO
from DivineService import serviceHelpSupport
from DivineService.auth import get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/support-tickets", tags=["admin", "support"])
_help_support_service = serviceHelpSupport()


@router.post("", response_model=SupportTicketResponseDTO)
def submit_support_ticket(dto: SupportTicketRequestDTO, current_admin: dict = Depends(get_current_admin)):
    """Admin panel's Help & Support form. Generates a ticket number and emails
    it, along with the subject and description, to the Divine Vision Infra
    support inbox. Admin-only. Returns 502 if the notification could not be
    delivered - an undelivered ticket is a failed request, not a best-effort
    side effect the caller should treat as a success."""
    start = time.monotonic()
    try:
        return _help_support_service.submit_ticket(
            subject=dto.subject, description=dto.description, admin_id=current_admin.get("sub"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        code = str(e)
        if code == "ticket_email_failed":
            raise HTTPException(status_code=502, detail="ticket_email_failed")
        logger.error("admin_submit_support_ticket_failed code=%s", code)
        raise HTTPException(status_code=500, detail="internal_error")
    except Exception:
        logger.exception("admin_submit_support_ticket_failed")
        raise HTTPException(status_code=500, detail="internal_error")
    finally:
        logger.debug("admin_submit_support_ticket_latency_ms=%.2f", (time.monotonic() - start) * 1000)
