import json
import logging
import os
import uuid
import razorpay
from razorpay.errors import SignatureVerificationError
from Divinepersistence import persistencePayment

logger = logging.getLogger(__name__)

DEFAULT_CURRENCY = "INR"
MAX_AMOUNT_INR = 10_000_000  # 1 crore - a sanity ceiling, not a real business limit

# Only these events flip a payment's status - everything else (refund/dispute events,
# order.paid, etc.) is out of scope for this feature and acknowledged without action.
_WEBHOOK_EVENT_STATUS = {"payment.captured": "paid", "payment.failed": "failed"}


class servicePayment:
    def __init__(self, persistence: persistencePayment = None):
        self._persistence = persistence or persistencePayment()
        self._key_id = os.getenv("RAZORPAY_KEY_ID")
        self._key_secret = os.getenv("RAZORPAY_KEY_SECRET")

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
                              razorpay_order_id: str = None, method: str = "razorpay", notes: dict = None):
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
        )

    def create_order(self, amount: float, owner_id: str, owner_role: str):
        """Creates a Razorpay Order and a matching local record. The order is created with
        payment NOT yet captured - amount only becomes "paid" once verify_payment() confirms
        a valid signature from Razorpay, never from the client's own say-so."""
        self._validate_amount(amount)

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
        )
        return record, self._key_id

    def record_cash_payment(self, amount: float, owner_id: str, owner_role: str, note: str = None):
        """Records cash already collected in person - there's no gateway transaction to
        create or verify (unlike create_order/verify_payment), so this settles the record
        as "paid" immediately, straight from what was typed in. Available to both customers
        (self-reporting cash they handed over) and brokers (logging cash collected on a
        visit) - it's an unverified, self-reported record either way, same trust model as
        someone writing it in a physical receipt book, not a cryptographically confirmed
        transaction like the Razorpay flow."""
        self._validate_amount(amount)

        note = (note or "").strip()
        record = self._persist_new_payment(
            owner_id=owner_id, owner_role=owner_role, amount=amount, status="paid",
            razorpay_order_id=None, method="cash", notes={"note": note} if note else {},
        )
        return record

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
            # captured payment back to failed.
            logger.info(
                "payment.webhook.response event=%s payment_id=%s result=ignored_already_settled",
                event_type, record.id,
            )
            return "ignored_already_settled"

        self._persistence.update_payment_status(
            id=record.id, status=new_status, razorpay_payment_id=payment_id, razorpay_signature=None,
        )
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