import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
import razorpay
from razorpay.errors import SignatureVerificationError
from Divinepersistence import persistencePayment, persistenceInventory, persistenceBooking

logger = logging.getLogger(__name__)

DEFAULT_CURRENCY = "INR"
MAX_AMOUNT_INR = 10_000_000  # 1 crore - a sanity ceiling, not a real business limit

ALLOWED_PURPOSES = ("plot_booking", "installment", "other")
BOOKING_PURPOSE = "plot_booking"
INSTALLMENT_PURPOSE = "installment"

# Only these events flip a payment's status - everything else (refund/dispute events,
# order.paid, etc.) is out of scope for this feature and acknowledged without action.
_WEBHOOK_EVENT_STATUS = {"payment.captured": "paid", "payment.failed": "failed"}


class servicePayment:
    def __init__(self, persistence: persistencePayment = None,
                 inventory_persistence: persistenceInventory = None,
                 booking_persistence: persistenceBooking = None,
                 milestone_service=None):
        self._persistence = persistence or persistencePayment()
        self._inventory_persistence = inventory_persistence or persistenceInventory()
        self._booking_persistence = booking_persistence or persistenceBooking()
        self._milestone_service_override = milestone_service
        self._key_id = os.getenv("RAZORPAY_KEY_ID")
        self._key_secret = os.getenv("RAZORPAY_KEY_SECRET")

    def _milestones(self):
        """Lazily built so a plain payment flow never imports the milestone stack,
        and tests can inject a fake via the constructor."""
        if self._milestone_service_override is not None:
            return self._milestone_service_override
        try:
            from DivineService.service_milestones import serviceMilestones
            self._milestone_service_override = serviceMilestones()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("payment.milestone_service_init_failed error=%s", e)
            self._milestone_service_override = None
        return self._milestone_service_override

    def _clean_purpose(self, purpose: str) -> str:
        purpose = (purpose or "other").strip().lower()
        if purpose not in ALLOWED_PURPOSES:
            raise ValueError("invalid_purpose")
        return purpose

    def _booking_flip_trusted(self, record) -> bool:
        """Whether a plot-booking payment may put a unit on HOLD for admin KYC
        review (see _apply_booking_to_inventory - this no longer books outright,
        so the bar for "trusted enough" is lower than it used to be: the worst
        case for an untrustworthy flip is a temporary hold an admin later
        releases on reject, not a permanently lost unit):

          * a Razorpay payment - the caller only reaches _apply_booking_to_inventory
            after verify_payment / the webhook has confirmed the signature, so a
            'paid' razorpay row is real; or
          * a cash payment RECORDED BY A BROKER - staff logging cash they physically
            collected, same trust level as the receipt book; or
          * an rtgs_neft payment - self-reported by the customer with a UTR
            reference from their own bank transfer, but only ever a HOLD pending
            admin review (who confirms the actual bank credit before Approve), not
            a final booking - see the KYC-review gate this money now sits behind.

        A customer's own self-reported CASH entry is still NOT trusted (cash
        leaves no verifiable trail before it's physically handed over) - it
        settles 'paid' straight from typed input, so honouring it here would let
        anyone lock arbitrary plots for a rupee. Those still create a payment
        row; a broker confirms the plot via POST /inventory/{id}/book."""
        method = (getattr(record, "method", None) or "razorpay").lower()
        role = (getattr(record, "owner_role", None) or "").lower()
        if method == "razorpay":
            return True
        if method == "cash" and role == "broker":
            return True
        if method == "rtgs_neft":
            return True
        return False

    def _apply_booking_to_inventory(self, record):
        """Called right after a plot-booking payment settles. HOLDS the linked
        inventory unit as 'pending_kyc_review' in its own guarded UPDATE - it no
        longer books the unit outright; an admin must Approve the customer's KYC
        documents first (see DivineService/service_booking_kyc.py). Also creates
        the Booking record (divine_bookings) that tracks that review, with its
        first decision-history entry. Attaches the outcome to `record` as
        transient attributes the API echoes back:

            record.inventory_status          -> "pending_kyc_review" | "conflict" | None
            record.inventory_conflict_reason -> str | None
            record.booking_id                -> str | None

        Wrapped end to end - the money is already real, so ANY failure here becomes
        a flag-for-review at worst, never a failed payment or a raised exception."""
        try:
            record.inventory_status = None
            record.inventory_conflict_reason = None
            record.booking_id = None
        except Exception:  # pragma: no cover - record is always a mutable RowWrapper
            return record

        try:
            purpose = (getattr(record, "purpose", None) or "other")
            inventory_id = getattr(record, "inventory_id", None)
            payment_id = getattr(record, "id", None)
            owner_id = getattr(record, "owner_id", None)
            if purpose != BOOKING_PURPOSE or not inventory_id or not payment_id:
                return record
            if not self._booking_flip_trusted(record):
                logger.info("payment.booking.flip_skipped_untrusted payment_id=%s method=%s role=%s",
                            payment_id, getattr(record, "method", None), getattr(record, "owner_role", None))
                return record

            try:
                unit = self._inventory_persistence.hold_for_kyc_review(
                    id=inventory_id, payment_id=payment_id, customer_id=owner_id,
                )
            except Exception as e:
                logger.warning("payment.booking.inventory_flip_failed payment_id=%s inventory_id=%s error=%s",
                               payment_id, inventory_id, e)
                record.inventory_status = "conflict"
                record.inventory_conflict_reason = "inventory_update_failed"
                self._flag_review(payment_id, "inventory_update_failed")
                return record

            if unit is None:
                logger.info("payment.booking.unit_not_available payment_id=%s inventory_id=%s",
                            payment_id, inventory_id)
                record.inventory_status = "conflict"
                record.inventory_conflict_reason = "unit_not_available"
                self._flag_review(payment_id, "inventory_unavailable")
                return record

            record.inventory_status = "pending_kyc_review"
            self._create_booking_record(record, unit)
            self._push_booking_contact_to_zoho(record)
            return record
        except Exception as e:  # pragma: no cover - defensive catch-all
            logger.warning("payment.booking.apply_failed error=%s", e)
            return record

    def _create_booking_record(self, record, unit) -> None:
        """Creates the divine_bookings row for a newly-held unit. Idempotent for a
        re-run on the same payment_id (create_booking returns the existing row
        instead of erroring - see persistenceBooking.create_booking). Never
        raises - a failure here is logged and flagged for manual review, but must
        not undo the inventory hold that already succeeded (the money is real)."""
        try:
            booking = self._booking_persistence.create_booking(
                payment_id=getattr(record, "id", None),
                inventory_id=getattr(record, "inventory_id", None),
                customer_id=getattr(record, "owner_id", None),
                project_name=getattr(unit, "project_name", None),
                unit_number=getattr(unit, "unit_number", None),
                amount=getattr(record, "amount", None),
            )
            record.booking_id = getattr(booking, "id", None) if booking else None
        except Exception as e:
            logger.warning("payment.booking.record_create_failed payment_id=%s error=%s",
                           getattr(record, "id", None), e)
            self._flag_review(getattr(record, "id", None), "booking_record_create_failed")

    def _push_booking_contact_to_zoho(self, record) -> None:
        """Fire-and-forget: only on the FIRST successful plot booking (the unit
        actually flips to 'booked', on any payment method - Razorpay or manually
        recorded cash/RTGS/cheque) mirror the customer into Zoho CRM Contacts. Later
        instalments on that same plot do NOT push again - per the client, only the
        booking itself should land in Contacts. Never raises and never blocks the
        payment - a Zoho outage must not affect the real payment that already
        happened."""
        try:
            from DivineService.service_zoho import serviceZoho
            owner_id = getattr(record, "owner_id", None)
            customer = self._load_customer(owner_id)
            if not customer:
                return
            serviceZoho().push_booking_contact_async(
                customer_id=owner_id,
                first_name=getattr(customer, "first_name", None),
                last_name=getattr(customer, "last_name", None),
                email=getattr(customer, "email", None),
                phone=getattr(customer, "phone", None),
                inventory_id=getattr(record, "inventory_id", None),
                payment_id=getattr(record, "id", None),
                purpose=getattr(record, "purpose", None),
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("payment.zoho_contact_push_failed error=%s", e)

    def _flag_review(self, payment_id: str, reason: str):
        try:
            self._persistence.flag_manual_review(payment_id, reason)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("payment.booking.flag_review_failed payment_id=%s error=%s", payment_id, e)

    def _guard_unit_bookable(self, inventory_id: str) -> None:
        """Best-effort pre-check at order time: reject starting a booking payment for
        a unit that is already booked / sold / reserved, so the customer isn't
        charged for a plot they can't get. This is NOT a hold - two orders for the
        same still-available unit both pass here and the loser is caught later by
        the guarded flip (inventory_status='conflict'); a time-boxed hold is the
        deferred Phase-2 fix for that residual race. A lookup failure never blocks
        the order."""
        try:
            unit = self._inventory_persistence.get_by_id(inventory_id)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("payment.booking.prelookup_failed inventory_id=%s error=%s", inventory_id, e)
            return
        if unit is not None and (getattr(unit, "status", None) or "") not in ("available", "held"):
            raise ValueError("unit_not_available")

    _INSTALLMENT_GUARD_CODES = (
        "no_booking", "installment_not_found", "installment_already_paid",
        "installment_out_of_order", "installment_not_payable", "installment_amount_mismatch",
    )

    def _guard_installment(self, owner_id: str, installment_no, amount, due_date,
                           inventory_id: str = None) -> None:
        """Pre-check at order time. Raises ValueError(<guard code>) so the API returns
        400 {"detail": "<code>"}. A milestone service failure surfaces as 'no_booking'
        rather than a 500. ``inventory_id`` (optional) says which plot's instalment
        #N is meant when the customer holds more than one booking."""
        svc = self._milestones()
        if svc is None:
            raise ValueError("no_booking")
        try:
            _, code = svc.validate_installment(
                owner_id, installment_no, amount, due_date, inventory_id=inventory_id,
            )
        except Exception as e:  # pragma: no cover - validate_installment already guards
            logger.warning("payment.installment.guard_failed owner_id=%s error=%s", owner_id, e)
            raise ValueError("no_booking")
        if code:
            raise ValueError(code)

    def _apply_installment_settlement(self, record):
        """Called after an instalment payment settles. Re-runs the guard rails (order
        and settle can be minutes apart) and marks the milestone paid. The money is
        already real, so a guard failure here keeps the payment, flags it for review
        and returns installment_status='rejected' - it never raises or 4xx."""
        try:
            record.installment_status = None
        except Exception:  # pragma: no cover
            return record
        try:
            if (getattr(record, "purpose", None) or "other") != INSTALLMENT_PURPOSE:
                return record
            owner_id = getattr(record, "owner_id", None)
            installment_no = getattr(record, "installment_no", None)
            amount = getattr(record, "amount", None)
            due_date = getattr(record, "due_date", None)
            inventory_id = getattr(record, "inventory_id", None)
            svc = self._milestones()
            if svc is None:
                record.installment_status = "rejected"
                self._flag_review(getattr(record, "id", None), "no_booking")
                return record

            milestone, code = svc.validate_installment(
                owner_id, installment_no, amount, due_date, inventory_id=inventory_id,
            )
            if code and code != "installment_already_paid":
                record.installment_status = "rejected"
                self._flag_review(getattr(record, "id", None), code)
                return record

            milestone_id = getattr(milestone, "id", None) if milestone else None
            if not milestone_id:
                record.installment_status = "rejected"
                self._flag_review(getattr(record, "id", None), "installment_not_found")
                return record

            if code == "installment_already_paid":
                # A duplicate settle (webhook after verify) - not an error.
                record.installment_status = "paid"
                return record

            updated_milestone = svc.mark_paid(milestone_id, getattr(record, "id", None))
            record.installment_status = "paid"
            self._send_installment_receipt(record, updated_milestone or milestone)
            return record
        except Exception as e:  # pragma: no cover - defensive catch-all
            logger.warning("payment.installment.settle_failed error=%s", e)
            try:
                record.installment_status = "rejected"
                self._flag_review(getattr(record, "id", None), "settle_error")
            except Exception:
                pass
            return record

    def _client(self) -> razorpay.Client:
        if not self._key_id or not self._key_secret:
            raise RuntimeError("payment_not_configured")
        return razorpay.Client(auth=(self._key_id, self._key_secret))

    def _validate_amount(self, amount: float) -> None:
        if amount <= 0:
            raise ValueError("invalid_amount")
        if amount > MAX_AMOUNT_INR:
            raise ValueError("amount_too_large")

    def _persist_new_payment(self, owner_id: str, owner_role: str, amount: float, status: str,
                              razorpay_order_id: str = None, method: str = "razorpay", notes: dict = None,
                              purpose: str = "other", inventory_id: str = None,
                              installment_no: int = None, due_date=None, utr_number: str = None):
        payment_id = str(uuid.uuid4())
        return self._persistence.create_payment(
            id=payment_id,
            owner_id=owner_id,
            owner_role=owner_role,
            amount=amount,
            currency=DEFAULT_CURRENCY,
            status=status,
            razorpay_order_id=razorpay_order_id,
            method=method,
            notes=notes or {},
            purpose=purpose,
            inventory_id=inventory_id,
            installment_no=installment_no,
            due_date=due_date,
            utr_number=utr_number,
        )

    def create_order(self, amount: float, owner_id: str, owner_role: str,
                     purpose: str = "other", inventory_id: str = None,
                     installment_no: int = None, due_date=None):
        """Creates a Razorpay Order and a matching local record. The order is created with
        payment NOT yet captured - amount only becomes "paid" once verify_payment() confirms
        a valid signature from Razorpay, never from the client's own say-so."""
        self._validate_amount(amount)
        purpose = self._clean_purpose(purpose)
        inventory_id = (inventory_id or "").strip() or None
        installment_no = self._clean_installment_no(installment_no)
        due_date = self._clean_due_date(due_date)
        if purpose == BOOKING_PURPOSE and inventory_id:
            self._guard_unit_bookable(inventory_id)
        if purpose == INSTALLMENT_PURPOSE:
            self._guard_installment(owner_id, installment_no, amount, due_date, inventory_id)

        client = self._client()
        amount_paise = int(round(amount * 100))
        try:
            order = client.order.create({
                "amount": amount_paise,
                "currency": DEFAULT_CURRENCY,
                "payment_capture": 1,
            })
        except Exception as e:
            detail = self._gateway_error_detail(e)
            logger.warning(
                "payment.order.create_failed owner_id=%s purpose=%s amount=%s amount_paise=%s "
                "inventory_id=%s gateway_error=%s",
                owner_id, purpose, amount, amount_paise, inventory_id, e,
                exc_info=True,
            )
            raise RuntimeError(f"payment_order_failed:{detail}")

        record = self._persist_new_payment(
            owner_id=owner_id, owner_role=owner_role, amount=amount,
            status="created", razorpay_order_id=order["id"],
            purpose=purpose, inventory_id=inventory_id,
            installment_no=installment_no, due_date=due_date,
        )
        return record, self._key_id

    def _clean_installment_no(self, value):
        if value in (None, ""):
            return None
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise ValueError("installment_not_found")
        return n if n > 0 else None

    def _clean_due_date(self, value):
        text = str(value or "").strip()
        return text[:10] or None

    def _gateway_error_detail(self, exc: Exception) -> str:
        """Short, client-safe gateway failure detail for troubleshooting."""
        kind = type(exc).__name__
        raw = str(exc or "").strip()
        if not raw:
            return kind
        safe = re.sub(r"[^a-zA-Z0-9_.:-]+", "_", raw).strip("_").lower()
        if not safe:
            return kind
        return f"{kind}:{safe[:120]}"

    _MANUAL_METHODS = ("cash", "rtgs_neft")

    def record_cash_payment(self, amount: float, owner_id: str, owner_role: str, note: str = None,
                            purpose: str = "other", inventory_id: str = None,
                            installment_no: int = None, due_date=None, method: str = "cash",
                            utr_number: str = None):
        """Records money already collected/transferred outside the gateway - cash
        handed over in person, or an RTGS/NEFT bank transfer identified by its UTR
        reference - there's no gateway transaction to create or verify (unlike
        create_order/verify_payment), so this settles the record as "paid"
        immediately, straight from what was typed in. Available to both customers
        (self-reporting money they've sent/handed over) and brokers (logging cash
        collected on a visit) - it's an unverified, self-reported record either
        way, same trust model as someone writing it in a physical receipt book,
        not a cryptographically confirmed transaction like the Razorpay flow.
        Raises ValueError("invalid_method") / ValueError("utr_number_required")."""
        self._validate_amount(amount)
        purpose = self._clean_purpose(purpose)
        inventory_id = (inventory_id or "").strip() or None
        installment_no = self._clean_installment_no(installment_no)
        due_date = self._clean_due_date(due_date)
        clean_method = (method or "cash").strip().lower()
        if clean_method not in self._MANUAL_METHODS:
            raise ValueError("invalid_method")
        clean_utr = (utr_number or "").strip() or None
        if clean_method == "rtgs_neft" and not clean_utr:
            raise ValueError("utr_number_required")
        if purpose == INSTALLMENT_PURPOSE:
            self._guard_installment(owner_id, installment_no, amount, due_date, inventory_id)

        note = (note or "").strip()
        record = self._persist_new_payment(
            owner_id=owner_id, owner_role=owner_role, amount=amount, status="paid",
            razorpay_order_id=None, method=clean_method, notes={"note": note} if note else {},
            purpose=purpose, inventory_id=inventory_id,
            installment_no=installment_no, due_date=due_date, utr_number=clean_utr,
        )
        # Cash / RTGS-NEFT - any manually-recorded method - settles immediately ->
        # lock the plot / mark the milestone now (a first booking also mirrors the
        # customer into Zoho Contacts; instalments don't - see
        # _push_booking_contact_to_zoho).
        return self._settle_by_purpose(record)

    def verify_payment(self, razorpay_order_id: str, razorpay_payment_id: str, razorpay_signature: str, owner_id: str):
        """Verifies the payment signature Razorpay's checkout hands back to the client -
        the ONLY trustworthy confirmation that a payment actually succeeded. Never trust a
        client claiming success without this check; the signature is an HMAC over
        order_id|payment_id keyed with our account's key_secret, which only Razorpay and we
        know, so a forged "success" can't produce a valid signature."""
        record = self._persistence.get_by_razorpay_order_id(razorpay_order_id)
        if not record:
            raise ValueError("not_found")
        if record.owner_id != owner_id:
            raise PermissionError("forbidden")

        client = self._client()
        try:
            client.utility.verify_payment_signature({
                "razorpay_order_id": razorpay_order_id,
                "razorpay_payment_id": razorpay_payment_id,
                "razorpay_signature": razorpay_signature,
            })
            verified = True
        except SignatureVerificationError:
            verified = False

        status = "paid" if verified else "failed"
        updated = self._persistence.update_payment_status(
            id=record.id,
            status=status,
            razorpay_payment_id=razorpay_payment_id,
            razorpay_signature=razorpay_signature,
        )
        # update_payment_status doesn't carry purpose/inventory_id/installment fields
        # forward on every backend - copy them from the record we already loaded so
        # the settle step below always has them.
        if getattr(updated, "purpose", None) in (None, "other"):
            updated.purpose = getattr(record, "purpose", "other")
        if not getattr(updated, "inventory_id", None):
            updated.inventory_id = getattr(record, "inventory_id", None)
        if getattr(updated, "installment_no", None) in (None, ""):
            updated.installment_no = getattr(record, "installment_no", None)
        if getattr(updated, "due_date", None) in (None, ""):
            updated.due_date = getattr(record, "due_date", None)

        if verified:
            self._settle_by_purpose(updated)
        return updated, verified

    def handle_webhook(self, raw_body: bytes, signature: str) -> str:
        """Verifies and processes a Razorpay webhook delivery - the durable,
        server-to-server confirmation of payment status that doesn't depend on the
        client's browser staying open long enough to call verify_payment(). Razorpay
        retries on any non-2xx response, so "nothing to do here" cases (an event type
        this feature doesn't track, an unrecognized payload shape, an order we have no
        record of, a payment already settled) return a status string instead of raising
        - those are normal deliveries, not errors, and raising would just make Razorpay
        retry a webhook that would never succeed. Only signature verification and
        configuration problems raise, since those genuinely need the caller's attention."""
        secret = os.getenv("RAZORPAY_WEBHOOK_SECRET")
        if not secret:
            raise RuntimeError("payment_webhook_not_configured")

        try:
            body_text = raw_body.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ValueError("invalid_webhook_body") from e

        try:
            razorpay.Utility().verify_webhook_signature(body_text, signature or "", secret)
        except SignatureVerificationError as e:
            logger.info("payment.webhook.response result=invalid_signature")
            raise ValueError("invalid_webhook_signature") from e

        try:
            event = json.loads(body_text)
        except ValueError as e:
            raise ValueError("invalid_webhook_body") from e

        event_type = event.get("event") if isinstance(event, dict) else None
        new_status = _WEBHOOK_EVENT_STATUS.get(event_type)
        if not new_status:
            logger.info("payment.webhook.response event=%s result=ignored_event", event_type)
            return f"ignored_event:{event_type}"

        try:
            payment_entity = event["payload"]["payment"]["entity"]
            order_id = payment_entity["order_id"]
            payment_id = payment_entity["id"]
        except (KeyError, TypeError):
            logger.info("payment.webhook.response event=%s result=ignored_malformed_payload", event_type)
            return "ignored_malformed_payload"

        record = self._persistence.get_by_razorpay_order_id(order_id)
        if not record:
            logger.info("payment.webhook.response event=%s result=ignored_unknown_order", event_type)
            return "ignored_unknown_order"
        if record.status == "paid":
            # Already settled - a later "failed" event (out-of-order delivery, or a
            # duplicate retry after we already processed "captured") must not clobber a
            # captured payment back to failed. But a "captured" retry IS the chance to
            # finish a plot lock that a transient error dropped during /payments/verify
            # (book_unit is idempotent for the same payment id, so a re-run on an
            # already-booked unit is a harmless no-op).
            if new_status == "paid":
                self._settle_by_purpose(record)
            logger.info(
                "payment.webhook.response event=%s payment_id=%s result=ignored_already_settled",
                event_type, record.id,
            )
            return "ignored_already_settled"

        self._persistence.update_payment_status(
            id=record.id, status=new_status, razorpay_payment_id=payment_id, razorpay_signature=None,
        )
        if new_status == "paid":
            # Durable fallback: if the browser closed before /payments/verify ran, the
            # webhook is what locks the plot / marks the milestone. Both flips are
            # idempotent for the same payment id, so a later /verify is a harmless no-op.
            self._settle_by_purpose(record)
        logger.info(
            "payment.webhook.response event=%s payment_id=%s new_status=%s result=processed",
            event_type, record.id, new_status,
        )
        return f"processed:{new_status}"

    def _settle_by_purpose(self, record):
        if (getattr(record, "purpose", None) or "other") == INSTALLMENT_PURPOSE:
            return self._apply_installment_settlement(record)
        return self._apply_booking_to_inventory(record)

    def _load_customer(self, owner_id: str):
        try:
            from Divinepersistence import persistenceCustomer
            return persistenceCustomer().get_by_id(owner_id)
        except Exception:  # pragma: no cover - name is optional on the receipt
            return None

    def _send_installment_receipt(self, record, milestone) -> None:
        """Best-effort 'payment received' email with the receipt PDF attached.
        Never raises - a mail failure must not affect the settled payment."""
        try:
            customer = self._load_customer(getattr(record, "owner_id", None))
            email = getattr(customer, "email", None) if customer else None
            if not email:
                return
            from DivineService.service_payment_receipt import generate_receipt_pdf, receipt_filename
            from DivineService.service_email import serviceEmail, dispatch_installment_receipt_email
            pdf = generate_receipt_pdf(record, milestone=milestone, customer=customer)
            label = None
            if milestone is not None:
                label = getattr(milestone, "label", None) if not isinstance(milestone, dict) else milestone.get("label")
            project = None
            if milestone is not None:
                project = getattr(milestone, "project_id", None) if not isinstance(milestone, dict) else milestone.get("project_id")
            dispatch_installment_receipt_email(
                serviceEmail(), email,
                first_name=getattr(customer, "first_name", None),
                project_name=project, milestone_label=label,
                amount=getattr(record, "amount", None),
                receipt_pdf=pdf, receipt_filename=receipt_filename(getattr(record, "id", "")),
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("payment.installment.receipt_email_failed error=%s", e)

    def get_receipt(self, payment_id: str, requester_id: str):
        """(pdf_bytes, filename) for a settled payment the caller owns. Raises
        ValueError('not_found') / PermissionError('forbidden') /
        ValueError('payment_not_paid'). The PDF render itself never raises."""
        record = self._persistence.get_by_id(payment_id)
        if not record:
            raise ValueError("not_found")
        if record.owner_id != requester_id:
            raise PermissionError("forbidden")
        if (getattr(record, "status", None) or "") != "paid":
            raise ValueError("payment_not_paid")

        milestone = None
        if (getattr(record, "purpose", None) or "other") == INSTALLMENT_PURPOSE:
            svc = self._milestones()
            if svc is not None:
                milestone = svc.milestone_by_payment(record.owner_id, record.id)

        from DivineService.service_payment_receipt import generate_receipt_pdf, receipt_filename
        pdf = generate_receipt_pdf(record, milestone=milestone, customer=self._load_customer(record.owner_id))
        return pdf, receipt_filename(record.id)

    def get(self, payment_id: str, requester_id: str):
        record = self._persistence.get_by_id(payment_id)
        if not record:
            raise ValueError("not_found")
        if record.owner_id != requester_id:
            raise PermissionError("forbidden")
        return record

    def initiate_refund(self, payment_id: str, reason: str = None):
        """Starts a refund for a KYC-rejected/cancelled booking payment (called by
        serviceBookingKyc.reject/cancel, never directly from a router). Branches
        by HOW the money arrived:

          * razorpay - calls Razorpay's real refund API. A gateway failure here
            still leaves the payment refund_status='pending' (not 'failed') so an
            admin can see it needs a retry, rather than silently losing track of
            an owed refund; only a successful gateway call marks 'completed'
            (Razorpay refunds settle same-day for online payments, so there's no
            separate async "processing" webhook this codebase listens for yet).
          * cash / rtgs_neft - no gateway to call. Settles refund_status='pending'
            with a human-readable instruction note (collect from office / manual
            NEFT-RTGS by the business, both within 5-7 days) for the admin/business
            team to action and later mark 'completed' themselves.

        Raises ValueError('not_found') / ValueError('payment_not_paid') /
        ValueError('refund_already_initiated'). Never leaves the payment in an
        ambiguous state - every path ends in a persisted refund_status."""
        record = self._persistence.get_by_id(payment_id)
        if not record:
            raise ValueError("not_found")
        if (getattr(record, "status", None) or "") != "paid":
            raise ValueError("payment_not_paid")
        existing_refund_status = (getattr(record, "refund_status", None) or "none")
        if existing_refund_status not in ("none", "failed"):
            raise ValueError("refund_already_initiated")

        method = (getattr(record, "method", None) or "razorpay").lower()
        amount = getattr(record, "amount", None)
        now = datetime.now(timezone.utc)

        if method == "razorpay":
            razorpay_payment_id = getattr(record, "razorpay_payment_id", None)
            if not razorpay_payment_id:
                # Never actually charged (e.g. a booking cancelled before the
                # gateway payment settled) - nothing to refund at the gateway.
                return self._persistence.update_refund_status(
                    id=payment_id, refund_status="completed", refund_amount=0,
                    refund_initiated_date=now, refund_completed_date=now,
                    refund_note="No gateway charge was captured - nothing to refund.",
                )
            try:
                client = self._client()
                amount_paise = int(round(float(amount) * 100)) if amount is not None else None
                refund_kwargs = {"amount": amount_paise} if amount_paise else {}
                refund = client.payment.refund(razorpay_payment_id, refund_kwargs)
                return self._persistence.update_refund_status(
                    id=payment_id, refund_status="completed", refund_amount=amount,
                    razorpay_refund_id=refund.get("id") if isinstance(refund, dict) else None,
                    refund_initiated_date=now, refund_completed_date=now,
                    refund_note=(reason or "").strip() or None,
                )
            except Exception as e:
                detail = self._gateway_error_detail(e)
                logger.warning("payment.refund.razorpay_failed payment_id=%s gateway_error=%s",
                               payment_id, e, exc_info=True)
                return self._persistence.update_refund_status(
                    id=payment_id, refund_status="pending", refund_amount=amount,
                    refund_initiated_date=now,
                    refund_note=f"Automatic refund failed ({detail}) - needs manual retry.",
                )

        # cash / rtgs_neft - no gateway involved, purely a bookkeeping/notification entry.
        if method == "cash":
            note = "Refund approved - please collect the cash refund from our office within 5-7 business days."
        else:
            note = ("Refund approved - a manual bank transfer (NEFT/RTGS) will be initiated by our "
                    "team within 5-7 business days.")
        if reason:
            note = f"{note} Reason: {reason.strip()}"
        return self._persistence.update_refund_status(
            id=payment_id, refund_status="pending", refund_amount=amount,
            refund_initiated_date=now, refund_note=note,
        )
