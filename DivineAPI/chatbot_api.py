import base64
import binascii
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request

from DivineDTO.models import (
    SessionInitRequestDTO, SessionInitResponseDTO,
    ChatMessageRequestDTO, ChatMessageResponseDTO, CallbackConfirmedDTO,
    CallbackRequestOutDTO,
)
from DivineService import serviceChatbot
from DivineService.auth import get_current_admin_or_broker

router = APIRouter(prefix="/chatbot", tags=["chatbot"])
_chatbot_service = serviceChatbot()


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


@router.post("/session/init", response_model=SessionInitResponseDTO)
def init_session(dto: SessionInitRequestDTO, request: Request):
    try:
        result = _chatbot_service.init_session(
            referrer=dto.referrer, utm_source=dto.utm_source, utm_medium=dto.utm_medium,
            utm_campaign=dto.utm_campaign, device_type=dto.device_type, ip_address=_client_ip(request),
        )
        return SessionInitResponseDTO(**result)
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.post("/message", response_model=ChatMessageResponseDTO)
def send_message(dto: ChatMessageRequestDTO):
    audio_bytes = None
    if dto.audio_b64:
        try:
            audio_bytes = base64.b64decode(dto.audio_b64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=400, detail="invalid_audio_encoding")

    try:
        result = _chatbot_service.handle_message(
            session_id=dto.session_id, text=dto.text, audio_bytes=audio_bytes, intent=dto.intent,
            precise_lat=dto.precise_lat, precise_long=dto.precise_long,
        )

        if result.get("error") == "stt_failed":
            raise HTTPException(status_code=422, detail="stt_failed")

        callback_confirmed = None
        if result.get("callback_confirmed"):
            callback_confirmed = CallbackConfirmedDTO(**result["callback_confirmed"])

        return ChatMessageResponseDTO(
            session_id=result["session_id"], reply=result["reply"], buttons=result.get("buttons"),
            callback_confirmed=callback_confirmed,
            guardrail_passed=result.get("guardrail_passed"), llm_provider=result.get("llm_provider"),
        )
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.get("/callback-requests", response_model=List[CallbackRequestOutDTO])
def list_callback_requests(current_user: dict = Depends(get_current_admin_or_broker)):
    try:
        records = _chatbot_service.list_callback_requests()
        return [CallbackRequestOutDTO(
            id=r.id, lead_id=r.lead_id, visitor_name=r.visitor_name, phone=r.phone,
            preferred_time=r.preferred_time, status=r.status, requested_at=r.requested_at, actioned_at=r.actioned_at,
        ) for r in records]
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")
