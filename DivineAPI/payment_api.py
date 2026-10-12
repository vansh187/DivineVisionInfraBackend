import io

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import IntegrityError

from DivineDTO.models import (
    PaymentOrderRequestDTO,
    PaymentOrderOutDTO,
    PaymentVerifyRequestDTO,
    PaymentCashRequestDTO,
    PaymentOutDTO,
)
from DivineService import servicePayment
from DivineService.auth import get_current_user

router = APIRouter(prefix="/payments", tags=["payments"])
_payment_service = servicePayment()


def _to_payment_out(record, verified: bool = None) -> PaymentOutDTO:
    return PaymentOutDTO(
        id=record.id,
        owner_id=record.owner_id,
        owner_role=record.owner_role,
        amount=float(record.amount),
        currency=record.currency,
        status=record.status,
        # Defensive default: if this row (or the whole table, in a not-yet-migrated
        # environment) predates the method column, don't 500 on a plain read - every
        # payment before the Zoho Payments cutover went through Razorpay, but any new
        # payment always has a method set explicitly, so 'zoho' is the correct default.
        method=getattr(record, "method", "zoho"),
        verified=record.status == "paid" if verified is None else verified,
        zoho_payments_session_id=getattr(record, "zoho_payments_session_id", None),
        zoho_payment_id=getattr(record, "zoho_payment_id", None),
        created_date=record.created_date,
        # Booking linkage. inventory_id is a stored column; inventory_status /
        # inventory_conflict_reason are transient, set by the service only on the
        # call that actually settled a plot_booking payment (None on a plain read).
        inventory_id=getattr(record, "inventory_id", None),
        inventory_status=getattr(record, "inventory_status", None),
        inventory_conflict_reason=getattr(record, "inventory_conflict_reason", None),
        booking_id=getattr(record, "booking_id", None),
        # Instalment linkage. purpose / installment_no are stored columns;
        # installment_status is transient (set only on the settling call).
        purpose=getattr(record, "purpose", None),
        installment_no=getattr(record, "installment_no", None),
        installment_status=getattr(record, "installment_status", None),
    )


@router.post("/create-order", response_model=PaymentOrderOutDTO)
def create_order(dto: PaymentOrderRequestDTO, current_user: dict = Depends(get_current_user)):
    try:
        record, session = _payment_service.create_order(
            dto.amount, owner_id=current_user["sub"], owner_role=current_user["role"],
            purpose=dto.purpose, inventory_id=dto.inventory_id,
            installment_no=dto.installment_no, due_date=dto.due_date,
        )
        return PaymentOrderOutDTO(
            payment_id=record.id,
            zoho_payments_session_id=record.zoho_payments_session_id,
            checkout_url=session["checkout_url"],
            access_key=session["access_key"],
            amount=float(record.amount),
            currency=record.currency,
            status=record.status,
        )
    except ValueError as e:
        if str(e) == "unit_not_available":
            raise HTTPException(status_code=409, detail="unit_not_available")
        raise HTTPException(status_code=400, detail=str(e))
    except IntegrityError:
        raise HTTPException(status_code=409, detail="conflict")
    except RuntimeError as e:
        # A 4xx from Zoho (bad request - e.g. a malformed amount/currency) is our
        # own bug, not a gateway outage; anything else (auth failure, 5xx, network)
        # is a genuine upstream problem, surfaced as 502.
        detail = str(e)
        if "status_400" in detail or "status_422" in detail:
            raise HTTPException(status_code=400, detail=detail)
        raise HTTPException(status_code=502, detail=detail)
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.post("/verify", response_model=PaymentOutDTO)
def verify_payment(dto: PaymentVerifyRequestDTO, current_user: dict = Depends(get_current_user)):
    try:
        record, verified = _payment_service.verify_payment(
            dto.payments_session_id, dto.payment_id, dto.payment_status, dto.amount, dto.signature,
            owner_id=current_user["sub"],
            udf1=dto.udf1, udf2=dto.udf2, udf3=dto.udf3, udf4=dto.udf4, udf5=dto.udf5,
        )
        return _to_payment_out(record, verified)
    except ValueError:
        raise HTTPException(status_code=404, detail="not_found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="forbidden")
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.post("/cash", response_model=PaymentOutDTO)
def record_cash_payment(dto: PaymentCashRequestDTO, current_user: dict = Depends(get_current_user)):
    try:
        record = _payment_service.record_cash_payment(
            dto.amount, owner_id=current_user["sub"], owner_role=current_user["role"], note=dto.note,
            purpose=dto.purpose, inventory_id=dto.inventory_id,
            installment_no=dto.installment_no, due_date=dto.due_date,
            method=dto.method, utr_number=dto.utr_number,
        )
        return _to_payment_out(record)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError:
        raise HTTPException(status_code=403, detail="cash_payments_broker_only")
    except IntegrityError:
        raise HTTPException(status_code=409, detail="conflict")
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.post("/webhook")
async def zoho_payments_webhook(request: Request):
    # No auth dependency - Zoho calls this server-to-server with no user JWT.
    # Authenticity comes entirely from the X-Zoho-Webhook-Signature HMAC, verified
    # inside handle_webhook against ZOHO_PAYMENTS_WEBHOOK_SECRET. async def (unlike
    # the KYC/photo routes) is fine here: the only work is reading the body and a
    # fast local HMAC check, not CPU-heavy or blocking enough to need threadpool
    # offload.
    try:
        raw_body = await request.body()
        signature_header = request.headers.get("x-zoho-webhook-signature", "")
        result = _payment_service.handle_webhook(raw_body, signature_header)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")
    return {"status": result}


@router.get("/{payment_id}/receipt")
def get_payment_receipt(payment_id: str, current_user: dict = Depends(get_current_user)):
    """Server-rendered payment receipt / slip PDF for a settled payment. Owner only."""
    try:
        pdf_bytes, filename = _payment_service.get_receipt(payment_id, requester_id=current_user["sub"])
        return StreamingResponse(
            io.BytesIO(pdf_bytes), media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="forbidden")
    except ValueError as e:
        if str(e) == "payment_not_paid":
            raise HTTPException(status_code=400, detail="payment_not_paid")
        raise HTTPException(status_code=404, detail="not_found")
    except Exception:
        raise HTTPException(status_code=500, detail="receipt_failed")


@router.get("/{payment_id}", response_model=PaymentOutDTO)
def get_payment(payment_id: str, current_user: dict = Depends(get_current_user)):
    try:
        record = _payment_service.get(payment_id, requester_id=current_user["sub"])
        return _to_payment_out(record)
    except ValueError:
        raise HTTPException(status_code=404, detail="not_found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="forbidden")
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")
