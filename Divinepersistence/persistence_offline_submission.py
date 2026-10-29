import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, Date, DateTime, Integer, Numeric, String, Text, bindparam, text

from .persistence_db import Base, SessionLocal, RowWrapper


class OfflinePaymentSubmissionModel(Base):
    __tablename__ = "divine_offline_payment_submissions"
    id = Column(String(36), primary_key=True)
    customer_id = Column(String(6), nullable=False, index=True)
    booking_id = Column(String(36), nullable=False, index=True)
    inventory_id = Column(String(36))
    milestone_id = Column(String(48), nullable=False, index=True)
    installment_no = Column(Integer, nullable=False)
    milestone_label = Column(String(120))
    amount = Column(Numeric(14, 2), nullable=False)
    method = Column(String(20), nullable=False)
    utr_number = Column(String(50))
    cheque_number = Column(String(50))
    due_date = Column(Date)
    status = Column(String(24), nullable=False, default="pending_review", index=True)
    created_date = Column(DateTime(timezone=True), nullable=False)
    reviewer_id = Column(String(20))
    reviewed_at = Column(DateTime(timezone=True))
    decision = Column(String(12))
    reason = Column(Text)
    payment_id = Column(String(36))


class persistenceOfflineSubmission:
    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory

    def create(self, customer_id, milestone, amount, method, utr_number, due_date, cheque_number=None):
        now = datetime.now(timezone.utc)
        record_id = str(uuid.uuid4())
        with self._session_factory() as db:
            try:
                if db.get_bind().dialect.name == "postgresql":
                    db.execute(text("SELECT id FROM divine_payment_milestones WHERE id=:id FOR UPDATE"),
                               {"id": milestone.id}).first()
                exists = db.execute(text(
                    "SELECT id FROM divine_offline_payment_submissions "
                    "WHERE customer_id=:customer_id AND milestone_id=:milestone_id "
                    "AND status='pending_review' LIMIT 1"
                ), {"customer_id": customer_id, "milestone_id": milestone.id}).first()
                if exists:
                    raise ValueError("duplicate_pending_submission")
                db.execute(text(
                    "INSERT INTO divine_offline_payment_submissions "
                    "(id, customer_id, booking_id, inventory_id, milestone_id, installment_no, milestone_label, amount, "
                    "method, utr_number, cheque_number, due_date, status, created_date) VALUES "
                    "(:id,:customer_id,:booking_id,:inventory_id,:milestone_id,:installment_no,:milestone_label,:amount, "
                    ":method,:utr_number,:cheque_number,:due_date,'pending_review',:created_date)"
                ), {"id": record_id, "customer_id": customer_id, "booking_id": milestone.booking_id,
                    "inventory_id": getattr(milestone, "inventory_id", None), "milestone_id": milestone.id,
                    "installment_no": milestone.milestone_no, "milestone_label": getattr(milestone, "label", None),
                    "amount": amount, "method": method, "utr_number": utr_number,
                    "cheque_number": cheque_number, "due_date": due_date, "created_date": now})
                row = db.execute(text("SELECT * FROM divine_offline_payment_submissions WHERE id=:id"),
                                 {"id": record_id}).mappings().first()
                db.commit()
                return RowWrapper(row)
            except Exception:
                db.rollback()
                raise

    def list(self, status="pending_review"):
        with self._session_factory() as db:
            rows = db.execute(text(
                "SELECT s.*, c.first_name, c.last_name, i.project_name, i.unit_number "
                "FROM divine_offline_payment_submissions s "
                "LEFT JOIN divine_customer_users c ON c.id=s.customer_id "
                "LEFT JOIN divine_project_inventory i ON i.id=s.inventory_id "
                "WHERE (:status IS NULL OR s.status=:status) ORDER BY s.created_date ASC"
            ), {"status": status}).mappings().all()
            return [RowWrapper(r) for r in rows]

    def pending_milestones(self, booking_ids):
        if not booking_ids:
            return set()
        with self._session_factory() as db:
            rows = db.execute(text(
                "SELECT milestone_id FROM divine_offline_payment_submissions "
                "WHERE status='pending_review' AND booking_id IN :booking_ids"
            ).bindparams(bindparam("booking_ids", expanding=True)),
                {"booking_ids": list(booking_ids)}).all()
            return {r[0] for r in rows}

    def decide(self, submission_id, reviewer_id, decision, reason=None):
        now = datetime.now(timezone.utc)
        with self._session_factory() as db:
            try:
                row = db.execute(text(
                    "UPDATE divine_offline_payment_submissions SET status=:status, decision=:decision, "
                    "reviewer_id=:reviewer_id, reviewed_at=:now, reason=:reason "
                    "WHERE id=:id AND status='pending_review' RETURNING *"
                ), {"status": "approved" if decision == "approved" else "rejected", "decision": decision,
                    "reviewer_id": reviewer_id, "now": now, "reason": reason, "id": submission_id}).mappings().first()
                if row is None:
                    exists = db.execute(text("SELECT id FROM divine_offline_payment_submissions WHERE id=:id"),
                                        {"id": submission_id}).first()
                    db.rollback()
                    raise ValueError("submission_already_decided" if exists else "not_found")

                payment_id = None
                if decision == "approved":
                    payment_id = str(uuid.uuid4())
                    # recorded_by_admin_id stays NULL: the customer submitted this, an
                    # admin only approved it - so it isn't mistaken for an admin-entered
                    # payment. The approver is kept in notes (and on the submission row).
                    db.execute(text(
                        "INSERT INTO divine_payments (id, owner_id, owner_role, amount, currency, status, method, "
                        "purpose, inventory_id, installment_no, due_date, utr_number, cheque_number, notes, "
                        "created_date, last_updated_date) VALUES (:id,:customer_id,'customer',:amount,'INR','paid', "
                        ":method,'installment',:inventory_id,:installment_no,:due_date,:utr_number,:cheque_number,"
                        ":notes,:now,:now)"
                    ), {"id": payment_id, "customer_id": row["customer_id"], "amount": row["amount"],
                        "method": row["method"], "inventory_id": row["inventory_id"],
                        "installment_no": row["installment_no"], "due_date": row["due_date"],
                        "utr_number": row["utr_number"], "cheque_number": row.get("cheque_number"),
                        "notes": json.dumps({"offline_submission_id": submission_id, "approved_by": reviewer_id}),
                        "now": now})
                    updated = db.execute(text(
                        "UPDATE divine_payment_milestones SET status='paid', paid_on=:now, "
                        "paid_payment_id=:payment_id, last_updated_date=:now "
                        "WHERE id=:milestone_id AND booking_id=:booking_id AND customer_id=:customer_id "
                        "AND status <> 'paid' RETURNING id"
                    ), {"now": now, "payment_id": payment_id, "milestone_id": row["milestone_id"],
                        "booking_id": row["booking_id"], "customer_id": row["customer_id"]}).first()
                    if updated is None:
                        raise ValueError("installment_already_paid")
                    db.execute(text("UPDATE divine_offline_payment_submissions SET payment_id=:payment_id WHERE id=:id"),
                               {"payment_id": payment_id, "id": submission_id})
                result = db.execute(text("SELECT * FROM divine_offline_payment_submissions WHERE id=:id"),
                                    {"id": submission_id}).mappings().first()
                db.commit()
                return RowWrapper(result)
            except Exception:
                db.rollback()
                raise
