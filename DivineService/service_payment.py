import json
import logging
import os
import uuid
import razorpay
from razorpay.errors import SignatureVerificationError
from Divinepersistence import persistencePayment, persistenceInventory

logger = logging.getLogger(__name__)

DEFAULT_CURRENCY = "INR"
MAX_AMOUNT_INR = 10_000_000  # 1 crore - a sanity ceiling, not a real business limit

ALLOWED_PURPOSES = ("plot_booking", "other")
BOOKING_PURPOSE = "plot_booking"

# Only these events flip a payment's status - everything else (refund/dispute events,
# order.paid, etc.) is out of scope for this feature and acknowledged without action.
_WEBHOOK_EVENT_STATUS = {"payment.captured": "paid", "payment.failed": "failed"}


class servicePayment:
    def __init__(self, persistence: persistencePayment = None,
                 inventory_persistence: persistenceInventory = None):
        self._persistence = persistence or persistencePayment()
        self._inventory_persistence = inventory_persistence or persistenceInventory()
        self._key_id = os.getenv("RAZORPAY_KEY_ID")
        self._key_secret = os.getenv("RAZORPAY_KEY_SECRET")

    def _clean_purpose(self, purpose: str) -> str:
        purpose = (purpose or "other").strip().lower()
        if purpose not in ALLOWED_PURPOSES:
            raise ValueError("invalid_purpose")
        return purpose

    def _booking_flip_trusted(self, record) -> bool:
        """A plot flip permanently removes a unit from the available pool, so it may
        only ride on money we actually trust:

          * a Razorpay payment - the caller only reaches _apply_booking_to_inventory
            after verify_payment / the webhook has confirmed the signature, so a
            'paid' razorpay row is real; or
          * a cash payment RECORDED BY A BROKER - staff logging cash they physically
            collected, same trust level as the receipt book.

        A customer's own self-reported cash entry is NOT trusted: it settles
        'paid' straight from typed input with no verification, so honouring it here
        would let anyone lock arbitrary plots for a rupee. Those still create a
        payment row; a broker confirms the plot via POST /inventory/{id}/book."""
        method = (getattr(record, "method", None) or "razorpay").lower()
        role = (getattr(record, "owner_role", None) or "").lower()
        if method == "razorpay":
            return True
        if method == "cash" and role == "broker":
            return True
        return False

    def _apply_booking_to_inventory(self, record):
        """Called right after a plot-booking payment settles. Flips the linked
        inventory unit to 'booked' in its own guarded UPDATE. Attaches the outcome
        to `record` as transient attributes the API echoes back:

            record.inventory_status          -> "booked" | "conflict" | None
            record.inventory_conflict_reason -> str | None

        Wrapped end to end - the money is already real, so ANY failure here becomes
        a flag-for-review at worst, never a failed payment or a raised exception."""
        try:
            record.inventory_status = None
            record.inventory_conflict_reason = None
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
                unit = self._inventory_persistence.book_unit(
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

            record.inventory_status = "booked"
            return record
        except Exception as e:  # pragma: no cover - defensive catch-all
            logger.warning("payment.booking.apply_failed error=%s", e)
            return record

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
                              purpose: str = "other", inventory_id: str = None):
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
        )

    def create_order(self, amount: float, owner_id: str, owner_role: str,
                     purpose: str = "other", inventory_id: str = None):
        """Creates a Razorpay Order and a matching local record. The order is created with
        payment NOT yet captured - amount only becomes "paid" once verify_payment() confirms
        a valid signature from Razorpay, never from the client's own say-so."""
        self._validate_amount(amount)
        purpose = self._clean_purpose(purpose)
        inventory_id = (inventory_id or "").strip() or None
        if purpose == BOOKING_PURPOSE and inventory_id:
            self._guard_unit_bookable(inventory_id)

        client = self._client()
        amount_paise = int(round(amount * 100))
        try:
            order = client.order.create({
                "amount": amount_paise,
                "currency": DEFAULT_CURRENCY,
                "payment_capture": 1,
            })
        except Exception as e:
            raise RuntimeError(f"payment_order_failed:{type(e).__name__}")

        record = self._persist_new_payment(
            owner_id=owner_id, owner_role=owner_role, amount=amount,
            status="created", razorpay_order_id=order["id"],
            purpose=purpose, inventory_id=inventory_id,
        )
        return record, self._key_id

    def record_cash_payment(self, amount: float, owner_id: str, owner_role: str, note: str = None,
                            purpose: str = "other", inventory_id: str = None):
        """Records cash already collected in person - there's no gateway transaction to
        create or verify (unlike create_order/verify_payment), so this settles the record
        as "paid" immediately, straight from what was typed in. Available to both customers
        (self-reporting cash they handed over) and brokers (logging cash collected on a
        visit) - it's an unverified, self-reported record either way, same trust model as
        someone writing it in a physical receipt book, not a cryptographically confirmed
        transaction like the Razorpay flow."""
        self._validate_amount(amount)
        purpose = self._clean_purpose(purpose)
        inventory_id = (inventory_id or "").strip() or None

        note = (note or "").strip()
        record = self._persist_new_payment(
            owner_id=owner_id, owner_role=owner_role, amount=amount, status="paid",
            razorpay_order_id=None, method="cash", notes={"note": note} if note else {},
            purpose=purpose, inventory_id=inventory_id,
        )
        # Cash settles immediately -> lock the plot now.
        return self._apply_booking_to_inventory(record)

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
        # update_payment_status doesn't carry purpose/inventory_id forward on every
        # backend - copy them from the record we already loaded so the booking flip
        # below always has them.
        if getattr(updated, "purpose", None) in (None, "other"):
            updated.purpose = getattr(record, "purpose", "other")
        if not getattr(updated, "inventory_id", None):
            updated.inventory_id = getattr(record, "inventory_id", None)

        if verified:
            self._apply_booking_to_inventory(updated)
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
                self._apply_booking_to_inventory(record)
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
            # webhook is what locks the plot. book_unit is idempotent for the same
            # payment id, so a later /verify on the same order is a harmless no-op.
            self._apply_booking_to_inventory(record)
        logger.info(
            "payment.webhook.response event=%s payment_id=%s new_status=%s result=processed",
            event_type, record.id, new_status,
        )
        return f"processed:{new_status}"

    def get(self, payment_id: str, requester_id: str):
        record = self._persistence.get_by_id(payment_id)
        if not record:
            raise ValueError("not_found")
        if record.owner_id != requester_id:
            raise PermissionError("forbidden")
        return record