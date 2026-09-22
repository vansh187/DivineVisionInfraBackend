import io

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import IntegrityError

from DivineDTO.models import DocumentGenerateRequestDTO, DocumentOutDTO
from DivineService import serviceDocument
from DivineService.auth import get_current_user

router = APIRouter(prefix="/documents", tags=["documents"])
_doc_service = serviceDocument()


@router.post("/generate", response_model=DocumentOutDTO)
def generate_document(dto: DocumentGenerateRequestDTO, current_user: dict = Depends(get_current_user)):
    try:
        doc, signed_url, expires_in = _doc_service.generate(
            dto, owner_id=current_user["sub"], owner_role=current_user["role"]
        )
        return DocumentOutDTO(
            id=doc.id,
            owner_id=doc.owner_id,
            owner_role=doc.owner_role,
            document_type=doc.document_type,
            status=doc.status,
            created_date=doc.created_date,
            signed_url=signed_url,
            signed_url_expires_in=expires_in,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except IntegrityError:
        raise HTTPException(status_code=409, detail="conflict")
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.post("/aadhaar-photo", response_model=DocumentOutDTO)
def upload_aadhaar_photo(
    file: UploadFile = File(...),
    side: str = Form(...),
    current_user: dict = Depends(get_current_user),
):
    # Plain def, not async def: upload_aadhaar_photo does a blocking network call to
    # Supabase Storage (requests.post, up to 30s). FastAPI threadpools sync routes
    # automatically; async def here would instead block the event loop for that duration.
    try:
        file_bytes = file.file.read()
        doc, signed_url, expires_in = _doc_service.upload_aadhaar_photo(
            file_bytes, file.content_type, side, owner_id=current_user["sub"], owner_role=current_user["role"]
        )
        return DocumentOutDTO(
            id=doc.id,
            owner_id=doc.owner_id,
            owner_role=doc.owner_role,
            document_type=doc.document_type,
            status=doc.status,
            created_date=doc.created_date,
            signed_url=signed_url,
            signed_url_expires_in=expires_in,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except IntegrityError:
        raise HTTPException(status_code=409, detail="conflict")
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.post("/pan-photo", response_model=DocumentOutDTO)
def upload_pan_photo(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    # See upload_aadhaar_photo above: plain def so FastAPI threadpools the blocking
    # Supabase Storage upload instead of running it on the event loop.
    try:
        file_bytes = file.file.read()
        doc, signed_url, expires_in = _doc_service.upload_pan_photo(
            file_bytes, file.content_type, owner_id=current_user["sub"], owner_role=current_user["role"]
        )
        return DocumentOutDTO(
            id=doc.id,
            owner_id=doc.owner_id,
            owner_role=doc.owner_role,
            document_type=doc.document_type,
            status=doc.status,
            created_date=doc.created_date,
            signed_url=signed_url,
            signed_url_expires_in=expires_in,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except IntegrityError:
        raise HTTPException(status_code=409, detail="conflict")
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.post("/applicant-photo", response_model=DocumentOutDTO)
def upload_applicant_photo(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    # See upload_aadhaar_photo above: plain def so FastAPI threadpools the blocking
    # Supabase Storage upload instead of running it on the event loop.
    try:
        file_bytes = file.file.read()
        doc, signed_url, expires_in = _doc_service.upload_applicant_photo(
            file_bytes, file.content_type, owner_id=current_user["sub"], owner_role=current_user["role"]
        )
        return DocumentOutDTO(
            id=doc.id,
            owner_id=doc.owner_id,
            owner_role=doc.owner_role,
            document_type=doc.document_type,
            status=doc.status,
            created_date=doc.created_date,
            signed_url=signed_url,
            signed_url_expires_in=expires_in,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except IntegrityError:
        raise HTTPException(status_code=409, detail="conflict")
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.post("/co-applicant-photo", response_model=DocumentOutDTO)
def upload_co_applicant_photo(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    # See upload_aadhaar_photo above: plain def so FastAPI threadpools the blocking
    # Supabase Storage upload instead of running it on the event loop.
    try:
        file_bytes = file.file.read()
        doc, signed_url, expires_in = _doc_service.upload_co_applicant_photo(
            file_bytes, file.content_type, owner_id=current_user["sub"], owner_role=current_user["role"]
        )
        return DocumentOutDTO(
            id=doc.id,
            owner_id=doc.owner_id,
            owner_role=doc.owner_role,
            document_type=doc.document_type,
            status=doc.status,
            created_date=doc.created_date,
            signed_url=signed_url,
            signed_url_expires_in=expires_in,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except IntegrityError:
        raise HTTPException(status_code=409, detail="conflict")
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.post("/cancelled-cheque", response_model=DocumentOutDTO)
def upload_cancelled_cheque(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    # See upload_aadhaar_photo above: plain def so FastAPI threadpools the blocking
    # Supabase Storage upload instead of running it on the event loop.
    try:
        file_bytes = file.file.read()
        doc, signed_url, expires_in = _doc_service.upload_cancelled_cheque(
            file_bytes, file.content_type, owner_id=current_user["sub"], owner_role=current_user["role"]
        )
        return DocumentOutDTO(
            id=doc.id,
            owner_id=doc.owner_id,
            owner_role=doc.owner_role,
            document_type=doc.document_type,
            status=doc.status,
            created_date=doc.created_date,
            signed_url=signed_url,
            signed_url_expires_in=expires_in,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except IntegrityError:
        raise HTTPException(status_code=409, detail="conflict")
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.post("/project-booking-application", response_model=DocumentOutDTO)
def upload_project_booking_application(
    file: UploadFile = File(...),
    document_type: str = Form(...),
    project_id: str = Form(...),
    payment_id: str = Form(...),
    zoho_payments_session_id: str = Form(None),
    zoho_payment_id: str = Form(None),
    form_data: str = Form(None),
    inventory_id: str = Form(None),
    current_user: dict = Depends(get_current_user),
):
    # Plain def, not async def: this does blocking network calls (Supabase Storage upload +
    # sign, plus a DB round-trip for the payment lookup). See upload_aadhaar_photo above -
    # FastAPI threadpools sync routes automatically, so this doesn't block the event loop.
    try:
        file_bytes = file.file.read()
        doc, signed_url, expires_in, payment_plan = _doc_service.upload_booking_application(
            file_bytes,
            file.content_type,
            document_type,
            project_id,
            payment_id,
            zoho_payments_session_id,
            zoho_payment_id,
            form_data,
            owner_id=current_user["sub"],
            owner_role=current_user["role"],
            inventory_id=inventory_id,
        )
        return DocumentOutDTO(
            id=doc.id,
            owner_id=doc.owner_id,
            owner_role=doc.owner_role,
            document_type=doc.document_type,
            status=doc.status,
            created_date=doc.created_date,
            signed_url=signed_url,
            signed_url_expires_in=expires_in,
            payment_plan=payment_plan,
            inventory_id=getattr(doc, "inventory_id", None),
            inventory_status=getattr(doc, "inventory_status", None),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError:
        raise HTTPException(status_code=403, detail="forbidden")
    except IntegrityError:
        raise HTTPException(status_code=409, detail="conflict")
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.get("/latest/{document_type}", response_model=DocumentOutDTO)
def get_latest_document(document_type: str, current_user: dict = Depends(get_current_user)):
    """The caller's most-recent document of a given type, with a fresh signed_url.
    Works for every type the app uploads - the identity photos (aadhaar_front,
    aadhaar_back, pan_card, applicant_photo, co_applicant_photo) included - so the
    app can fall back to it when it doesn't hold a document id. Owner-scoped:
    404 {"detail": "not_found"} when the caller has no such document."""
    try:
        doc, signed_url, expires_in = _doc_service.get_latest(
            document_type, requester_id=current_user["sub"], requester_role=current_user["role"]
        )
        return DocumentOutDTO(
            id=doc.id,
            owner_id=doc.owner_id,
            owner_role=doc.owner_role,
            document_type=doc.document_type,
            status=doc.status,
            created_date=doc.created_date,
            signed_url=signed_url,
            signed_url_expires_in=expires_in,
        )
    except ValueError:
        raise HTTPException(status_code=404, detail="not_found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="forbidden")
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.get("/{document_id}/demand-letter")
def get_demand_letter(document_id: str, current_user: dict = Depends(get_current_user)):
    """Server-rendered Demand Letter PDF for a booking application, built from the
    stored payment schedule + plot / customer details. Owner (customer) only."""
    try:
        pdf_bytes, filename = _doc_service.get_demand_letter(
            document_id, requester_id=current_user["sub"], requester_role=current_user["role"],
        )
        return StreamingResponse(
            io.BytesIO(pdf_bytes), media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="forbidden")
    except ValueError as e:
        if str(e) == "not_a_booking_application":
            raise HTTPException(status_code=400, detail="not_a_booking_application")
        raise HTTPException(status_code=404, detail="not_found")
    except Exception:
        raise HTTPException(status_code=500, detail="demand_letter_failed")


@router.get("/{document_id}", response_model=DocumentOutDTO)
def get_document(document_id: str, current_user: dict = Depends(get_current_user)):
    """Re-sign any document the caller owns - identity photos (aadhaar_front,
    aadhaar_back, pan_card, applicant_photo, co_applicant_photo), generated PDFs,
    and booking applications alike. Authorisation is by ownership only. Response
    shape is unchanged; the point is a fresh signed_url + signed_url_expires_in on
    every call so an expired URL can be refreshed.

    404 {"detail": "document_not_found"} when the id is unknown;
    403 {"detail": "forbidden"} for another user's document."""
    try:
        doc, signed_url, expires_in = _doc_service.get(
            document_id, requester_id=current_user["sub"], requester_role=current_user["role"]
        )
        return DocumentOutDTO(
            id=doc.id,
            owner_id=doc.owner_id,
            owner_role=doc.owner_role,
            document_type=doc.document_type,
            status=doc.status,
            created_date=doc.created_date,
            signed_url=signed_url,
            signed_url_expires_in=expires_in,
        )
    except ValueError:
        raise HTTPException(status_code=404, detail="document_not_found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="forbidden")
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")
