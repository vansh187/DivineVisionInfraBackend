import json
from typing import Optional
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.exc import IntegrityError

from DivineDTO.models import KycVerificationOutDTO
from DivineService import serviceKyc
from DivineService.auth import get_current_user

router = APIRouter(prefix="/kyc/aadhaar", tags=["kyc"])
_kyc_service = serviceKyc()

_KYC_FAILURE_MESSAGES = {
    "signature_invalid": "Aadhaar signature verification failed. This document could not be verified as genuine.",
}


def _kyc_message(verified: bool, failure_reason: Optional[str]) -> str:
    if verified:
        return "Aadhaar verification successful."
    if failure_reason in _KYC_FAILURE_MESSAGES:
        return _KYC_FAILURE_MESSAGES[failure_reason]
    if failure_reason and failure_reason.startswith("xml_signature_invalid"):
        return "Aadhaar Offline XML signature verification failed. This document could not be verified as genuine."
    if failure_reason and failure_reason.startswith("signature_verification_error"):
        return "Aadhaar verification could not be completed due to a technical error. Please try again."
    return "Aadhaar verification failed."


def _kyc_result_to_dto(record) -> KycVerificationOutDTO:
    extracted_data = record.extracted_data
    if isinstance(extracted_data, str):
        # Postgres (jsonb) hands back a dict already; SQLite (used in tests) hands back
        # the raw JSON text we stored, since this goes through raw SQL, not the ORM.
        try:
            extracted_data = json.loads(extracted_data)
        except ValueError:
            extracted_data = {}
    if not isinstance(extracted_data, dict):
        extracted_data = {}
    verified = bool(record.verified)
    return KycVerificationOutDTO(
        id=record.id,
        owner_id=record.owner_id,
        owner_role=record.owner_role,
        method=record.method,
        verified=verified,
        status="success" if verified else "error",
        message=_kyc_message(verified, record.failure_reason),
        masked_aadhaar=record.masked_aadhaar,
        extracted_data=extracted_data,
        failure_reason=record.failure_reason,
        created_date=record.created_date,
    )


@router.post("/qr/verify", response_model=KycVerificationOutDTO)
async def verify_aadhaar_qr(file: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    try:
        image_bytes = await file.read()
        record = _kyc_service.verify_qr(
            image_bytes, owner_id=current_user["sub"], owner_role=current_user["role"]
        )
        return _kyc_result_to_dto(record)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except IntegrityError:
        raise HTTPException(status_code=409, detail="conflict")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.post("/xml/verify", response_model=KycVerificationOutDTO)
async def verify_aadhaar_offline_xml(
    file: UploadFile = File(...),
    share_code: str = Form(...),
    current_user: dict = Depends(get_current_user),
):
    try:
        zip_bytes = await file.read()
        record = _kyc_service.verify_offline_xml(
            zip_bytes, share_code, owner_id=current_user["sub"], owner_role=current_user["role"]
        )
        return _kyc_result_to_dto(record)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except IntegrityError:
        raise HTTPException(status_code=409, detail="conflict")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")
