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
        # payment before this feature existed went through Razorpay, so that's correct.
        method=getattr(record, "method", "razorpay"),
        verified=record.status == "paid" if verified is None else verified,
        razorpay_order_id=record.razorpay_order_id,
        razorpay_payment_id=record.razorpay_payment_id,
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
        record, key_id = _payment_service.create_order(
            dto.amount, owner_id=current_user["sub"], owner_role=current_user["role"],
            purpose=dto.purpose, inventory_id=dto.inventory_id,
            installment_no=dto.installment_no, due_date=dto.due_date,
        )
        return PaymentOrderOutDTO(
            payment_id=record.id,
            razorpay_order_id=record.razorpay_order_id,
            razorpay_key_id=key_id,
            amount=float(record.amount),
            amount_paise=int(round(float(record.amount) * 100)),
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
        if str(e).startswith("payment_order_failed:BadRequestError"):
            raise HTTPException(status_code=400, detail=str(e))
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="internal_error")


@router.post("/verify", response_model=PaymentOutDTO)
def verify_payment(dto: PaymentVerifyRequestDTO, current_user: dict = Depends(get_current_user)):
    try:
        record, verified = _payment_service.verify_payment(
            dto.razorpay_order_id, dto.razorpay_payment_id, dto.razorpay_signature,
            owner_id=current_user["sub"],
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
async def razorpay_webhook(request: Request):
    # No auth dependency - Razorpay calls this server-to-server with no user JWT.
    # Authenticity comes entirely from the X-Razorpay-Signature HMAC, verified inside
    # handle_webhook against RAZORPAY_WEBHOOK_SECRET. async def (unlike the KYC/photo
    # routes) is fine here: the only work is reading the body and a fast local HMAC
    # check, not CPU-heavy or blocking enough to need threadpool offload.
    try:
        raw_body = await request.body()
        signature = request.headers.get("x-razorpay-signature", "")
        result = _payment_service.handle_webhook(raw_body, signature)
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
