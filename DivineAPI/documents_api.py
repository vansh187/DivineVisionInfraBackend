from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
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


@router.get("/{document_id}", response_model=DocumentOutDTO)
def get_document(document_id: str, current_user: dict = Depends(get_current_user)):
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
        raise HTTPException(status_code=404, detail="not_found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="forbidden")
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")
