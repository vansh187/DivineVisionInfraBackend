import json
from sqlalchemy import Column, String, DateTime, Date, Integer, Numeric, JSON, Boolean, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from datetime import datetime, timezone
from .persistence_db import Base, SessionLocal, engine, RowWrapper, load_queries


class PaymentModel(Base):
    __tablename__ = "divine_payments"
    id = Column(String(36), primary_key=True)
    owner_id = Column(String(6), nullable=False, index=True)
    owner_role = Column(String(10), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String(3), nullable=False, default="INR")
    status = Column(String(20), nullable=False, default="created")  # created | paid | failed
    # "razorpay" (default, backward compatible with rows created before this column
    # existed) or "cash". A cash entry has no real gateway order, so razorpay_order_id is
    # nullable and left NULL for those rows - not a synthetic placeholder string, which
    # would make every future reader of this column have to know a lexical convention
    # ("starts with cash_") instead of just checking for NULL.
    method = Column(String(20), nullable=False, default="razorpay")
    # "plot_booking" binds this payment to inventory_id so the unit is flipped to
    # 'booked' when the payment settles; "other" (default, backward compatible with
    # rows created before this column existed) touches no inventory.
    purpose = Column(String(20), nullable=False, default="other", server_default="other")
    inventory_id = Column(String(36), index=True)
    # Set for purpose='installment': which payment_schedule milestone this pays.
    installment_no = Column(Integer)
    due_date = Column(Date)
    # nullable in the model so create_all()'s sqlite schema (used only by the test
    # suite) tolerates an INSERT that omits it. Production keeps NOT NULL DEFAULT
    # false via db_init.sql / the migration - flag_manual_review always sets it.
    needs_manual_review = Column(Boolean, default=False)
    manual_review_reason = Column(String(60))
    # razorpay_* columns are kept for every payment made before the Zoho Payments
    # cutover - never written to for a new payment again, but still read for
    # historical rows (receipts, admin refund/revenue screens). zoho_* are the
    # live gateway columns for every new payment (method='zoho').
    razorpay_order_id = Column(String(64), nullable=True, index=True)
    razorpay_payment_id = Column(String(64))
    razorpay_signature = Column(String(255))
    zoho_payments_session_id = Column(String(64), nullable=True, index=True)
    zoho_payment_id = Column(String(64))
    zoho_signature = Column(String(255))
    # Customer-entered bank reference number for an "rtgs_neft" payment (collected on
    # the payment-method entry screen) - there's no gateway transaction id for those,
    # this is the equivalent of zoho_payment_id for that method.
    utr_number = Column(String(50))
    # Refund bookkeeping - see DivineService/service_payment.py::initiate_refund.
    # 'none' | 'pending' | 'processing' | 'completed' | 'failed'. 'pending'/'processing'
    # cover both an in-flight Zoho refund and a cash/rtgs_neft/legacy-razorpay refund
    # the business still owes manually; 'completed' is set by the admin once a manual
    # refund is actually paid out (Zoho refunds are auto-completed by the gateway).
    refund_status = Column(String(20), nullable=False, default="none", server_default="none")
    refund_amount = Column(Numeric(12, 2))
    razorpay_refund_id = Column(String(64))
    zoho_refund_id = Column(String(64))
    refund_initiated_date = Column(DateTime)
    refund_completed_date = Column(DateTime)
    refund_note = Column(Text)
    notes = Column(JSON().with_variant(JSONB, "postgresql"))
    created_date = Column(DateTime)
    last_updated_date = Column(DateTime)


class persistencePayment:
    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory
        queries = load_queries("payment_queries.yaml")
        queries.setdefault("create_payment", (
            'INSERT INTO divine_payments(id, owner_id, owner_role, amount, currency, status, method, purpose, inventory_id, installment_no, due_date, zoho_payments_session_id, utr_number, notes, created_date, last_updated_date) '
            'VALUES (:id, :owner_id, :owner_role, :amount, :currency, :status, :method, :purpose, :inventory_id, :installment_no, :due_date, :zoho_payments_session_id, :utr_number, :notes, :created_date, :last_updated_date) RETURNING *;'
        ))
        queries.setdefault("get_by_id", 'SELECT * FROM divine_payments WHERE id = :id LIMIT 1;')
        queries.setdefault("get_by_zoho_session_id", 'SELECT * FROM divine_payments WHERE zoho_payments_session_id = :zoho_payments_session_id LIMIT 1;')
        queries.setdefault("update_payment_status", (
            'UPDATE divine_payments SET status = :status, zoho_payment_id = :zoho_payment_id, '
            'zoho_signature = :zoho_signature, last_updated_date = :last_updated_date '
            'WHERE id = :id RETURNING *;'
        ))
        queries.setdefault("flag_manual_review", (
            'UPDATE divine_payments SET needs_manual_review = true, manual_review_reason = :reason, '
            'last_updated_date = :last_updated_date WHERE id = :id RETURNING *;'
        ))
        queries.setdefault("update_refund_status", (
            'UPDATE divine_payments SET refund_status = :refund_status, refund_amount = :refund_amount, '
            'zoho_refund_id = :zoho_refund_id, refund_initiated_date = :refund_initiated_date, '
            'refund_completed_date = :refund_completed_date, refund_note = :refund_note, '
            'last_updated_date = :last_updated_date '
            'WHERE id = :id RETURNING *;'
        ))
        self._queries = queries
        self._engine = engine

    def create_payment(self, id: str, owner_id: str, owner_role: str, amount, currency: str, status: str, zoho_payments_session_id: str = None, method: str = "zoho", notes: dict = None, purpose: str = "other", inventory_id: str = None, installment_no: int = None, due_date=None, utr_number: str = None) -> PaymentModel:
        with self._session_factory() as db:
            try:
                now = datetime.now(timezone.utc)
                query = self._queries.get("create_payment")
                params = {
                    "id": id,
                    "owner_id": owner_id,
                    "owner_role": owner_role,
                    "amount": amount,
                    "currency": currency,
                    "status": status,
                    "method": method,
                    "purpose": purpose or "other",
                    "inventory_id": inventory_id,
                    "installment_no": installment_no,
                    "due_date": due_date,
                    "zoho_payments_session_id": zoho_payments_session_id,
                    "utr_number": utr_number,
                    "notes": json.dumps(notes or {}),
                    "created_date": now,
                    "last_updated_date": now,
                }
                result = db.execute(text(query), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row)
            except IntegrityError:
                db.rollback()
                raise
            except Exception:
                db.rollback()
                raise

    def flag_manual_review(self, id: str, reason: str) -> PaymentModel:
        with self._session_factory() as db:
            try:
                query = self._queries.get("flag_manual_review")
                result = db.execute(text(query), {
                    "id": id, "reason": (reason or "")[:60],
                    "last_updated_date": datetime.now(timezone.utc),
                })
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row) if row else None
            except Exception:
                db.rollback()
                raise

    def update_refund_status(self, id: str, refund_status: str, refund_amount=None,
                             zoho_refund_id: str = None, refund_initiated_date=None,
                             refund_completed_date=None, refund_note: str = None) -> PaymentModel:
        with self._session_factory() as db:
            try:
                query = self._queries.get("update_refund_status")
                params = {
                    "id": id,
                    "refund_status": refund_status,
                    "refund_amount": refund_amount,
                    "zoho_refund_id": zoho_refund_id,
                    "refund_initiated_date": refund_initiated_date,
                    "refund_completed_date": refund_completed_date,
                    "refund_note": refund_note,
                    "last_updated_date": datetime.now(timezone.utc),
                }
                result = db.execute(text(query), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row) if row else None
            except Exception:
                db.rollback()
                raise

    def claim_refund_retry(self, id: str) -> PaymentModel:
        """Atomic compare-and-swap: 'pending' -> 'processing', only for a zoho
        refund with no zoho_refund_id yet. Returns the row on success, None if
        the claim lost (already claimed by a concurrent retry, already resolved,
        or not eligible) - see claim_refund_retry in payment_queries.yaml."""
        with self._session_factory() as db:
            try:
                query = self._queries.get("claim_refund_retry")
                result = db.execute(text(query), {"id": id, "now": datetime.now(timezone.utc)})
                row = result.mappings().first()
                if not row:
                    db.rollback()
                    return None
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    def mark_manual_refund_collected(self, id: str, note: str = None) -> PaymentModel:
        """Atomic compare-and-swap: 'pending'/'processing' -> 'completed', only for
        a cash/rtgs_neft/legacy-razorpay refund (razorpay's own gateway was retired
        at cutover, so any of its refunds are now settled manually, same as
        cash/rtgs_neft) - see mark_refund_collected in payment_queries.yaml. Returns
        None if not eligible (a live 'zoho' refund, already completed, or no refund
        in flight)."""
        with self._session_factory() as db:
            try:
                query = self._queries.get("mark_refund_collected")
                result = db.execute(text(query), {
                    "id": id, "note": note, "now": datetime.now(timezone.utc),
                })
                row = result.mappings().first()
                if not row:
                    db.rollback()
                    return None
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    def seed_legacy_razorpay_fields(self, id: str, razorpay_order_id: str = None,
                                    razorpay_payment_id: str = None, razorpay_signature: str = None,
                                    razorpay_refund_id: str = None) -> PaymentModel:
        """NOT used by any live application code path - the Razorpay gateway was
        retired at the Zoho Payments cutover, so nothing here writes a new
        razorpay_* value ever again. This exists only for tests that need to
        seed a realistic pre-cutover 'razorpay' row (to exercise the legacy-data
        display/refund paths still in service_refund.py/service_revenue.py/etc.)
        and for a one-off manual data-backfill script, should one ever be
        needed. A plain UPDATE, not a query-file entry - deliberately kept out
        of the normal create/update methods above so a reviewer scanning them
        for "what can the app write" never has to mentally exclude this."""
        with self._session_factory() as db:
            try:
                result = db.execute(text(
                    "UPDATE divine_payments SET razorpay_order_id = :razorpay_order_id, "
                    "razorpay_payment_id = :razorpay_payment_id, razorpay_signature = :razorpay_signature, "
                    "razorpay_refund_id = :razorpay_refund_id, last_updated_date = :last_updated_date "
                    "WHERE id = :id RETURNING *;"
                ), {
                    "id": id, "razorpay_order_id": razorpay_order_id,
                    "razorpay_payment_id": razorpay_payment_id, "razorpay_signature": razorpay_signature,
                    "razorpay_refund_id": razorpay_refund_id,
                    "last_updated_date": datetime.now(timezone.utc),
                })
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row) if row else None
            except Exception:
                db.rollback()
                raise

    def get_by_id(self, id: str):
        with self._session_factory() as db:
            query = self._queries.get("get_by_id")
            result = db.execute(text(query), {"id": id})
            row = result.mappings().first()
            if not row:
                return None
            return RowWrapper(row)

    def get_by_zoho_session_id(self, zoho_payments_session_id: str):
        with self._session_factory() as db:
            query = self._queries.get("get_by_zoho_session_id")
            result = db.execute(text(query), {"zoho_payments_session_id": zoho_payments_session_id})
            row = result.mappings().first()
            if not row:
                return None
            return RowWrapper(row)

    def update_payment_status(self, id: str, status: str, zoho_payment_id: str, zoho_signature: str) -> PaymentModel:
        with self._session_factory() as db:
            try:
                query = self._queries.get("update_payment_status")
                params = {
                    "id": id,
                    "status": status,
                    "zoho_payment_id": zoho_payment_id,
                    "zoho_signature": zoho_signature,
                    "last_updated_date": datetime.now(timezone.utc),
                }
                result = db.execute(text(query), params)
                row = result.mappings().first()
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise