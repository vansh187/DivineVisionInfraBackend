import logging
from Divinepersistence import persistenceBooking, persistenceInventory, persistenceCustomer
from DivineService.service_payment import servicePayment
from DivineService.service_document import serviceDocument

logger = logging.getLogger(__name__)

# The KYC document checklist for a booking review - Aadhaar (front/back), PAN,
# applicant photo, and a cancelled cheque (bank-account proof, for any future
# refund) - same document_type values the customer already uploads during the
# normal booking-application flow (DivineService/service_document.py). No new
# "address proof"/separate "passport photo" types - per the client, this
# screen reviews exactly these.
KYC_DOCUMENT_TYPES = ("aadhaar_front", "aadhaar_back", "pan_card", "applicant_photo", "cancelled_cheque")
_DOCUMENT_LABELS = {
    "aadhaar_front": "Aadhaar Card - Front",
    "aadhaar_back": "Aadhaar Card - Back",
    "pan_card": "PAN Card",
    "applicant_photo": "Applicant Photo",
    "cancelled_cheque": "Cancelled Cheque",
}

_ACTIVE_STATUSES = ("pending_kyc_review",)


class serviceBookingKyc:
    def __init__(self, persistence: persistenceBooking = None, inventory_persistence: persistenceInventory = None,
                 payment_service: servicePayment = None, document_service: serviceDocument = None,
                 customer_persistence: persistenceCustomer = None, email_service=None):
        self._persistence = persistence or persistenceBooking()
        self._inventory_persistence = inventory_persistence or persistenceInventory()
        self._payment_service = payment_service or servicePayment()
        self._document_service = document_service or serviceDocument()
        self._customer_persistence = customer_persistence or persistenceCustomer()
        self._email_service_override = email_service

    def _email(self):
        """Lazily built, same reasoning as serviceVisit/servicePayment's lazy
        sub-service getters: a plain queue/detail read never needs to import or
        construct the email client."""
        if self._email_service_override is not None:
            return self._email_service_override
        try:
            from DivineService.service_email import serviceEmail
            self._email_service_override = serviceEmail()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("booking_kyc.email_service_init_failed error=%s", e)
            self._email_service_override = None
        return self._email_service_override

    def _format_customer_name(self, customer) -> str:
        if not customer:
            return None
        first = (getattr(customer, "first_name", None) or "").strip()
        last = (getattr(customer, "last_name", None) or "").strip()
        full = f"{first} {last}".strip()
        return full or None

    def _customer_name(self, customer_id: str) -> str:
        try:
            customer = self._customer_persistence.get_by_id(customer_id)
            return self._format_customer_name(customer)
        except Exception:
            return None

    def _customer_names(self, rows) -> dict:
        ids = list(dict.fromkeys(
            getattr(r, "customer_id", None)
            for r in (rows or [])
            if getattr(r, "customer_id", None)
        ))
        if not ids:
            return {}
        try:
            customers = self._customer_persistence.get_by_ids(ids)
            return {
                getattr(c, "id", None): self._format_customer_name(c)
                for c in customers
                if getattr(c, "id", None)
            }
        except Exception:
            logger.warning("booking_kyc.customer_batch_lookup_failed", exc_info=True)
            return {}

    def _as_queue_item(self, row, customer_names: dict = None) -> dict:
        customer_names = customer_names or {}
        return {
            "id": row.id,
            "customer_id": row.customer_id,
            "customer_name": customer_names.get(row.customer_id),
            "project_name": row.project_name,
            "unit_number": row.unit_number,
            "amount": float(row.amount) if row.amount is not None else None,
            "status": row.status,
            "kyc_status": row.kyc_status,
            "version": row.version,
            "created_at": row.created_date,
            "last_activity_at": row.last_updated_date,
        }

    def list_queue(self, search: str = None, status: str = None, kyc_status: str = None,
                    page: int = 1, page_size: int = 20) -> dict:
        try:
            search = (search or "").strip() or None
            offset = (page - 1) * page_size
            rows = self._persistence.list_queue(
                search=search, status=status, kyc_status=kyc_status, limit=page_size, offset=offset,
            )
            if rows:
                total_items = int(rows[0].total_count)
            else:
                total_items = self._persistence.count_queue(search=search, status=status, kyc_status=kyc_status)
            customer_names = self._customer_names(rows)
            total_pages = (total_items + page_size - 1) // page_size if page_size else 0
            return {
                "items": [self._as_queue_item(r, customer_names) for r in rows],
                "pagination": {
                    "page": page, "page_size": page_size,
                    "total_items": total_items, "total_pages": total_pages,
                },
            }
        except Exception:
            logger.exception("booking_kyc.list_queue_failed")
            raise RuntimeError("list_queue_failed")

    def list_mine(self, customer_id: str) -> list:
        """GET /bookings/mine - every booking (any status) belonging to the
        signed-in customer. Never raises for a customer with no bookings - []
        is a normal answer, not an error."""
        try:
            rows = self._persistence.list_by_customer(customer_id)
            return [
                {
                    "id": r.id,
                    "project_name": r.project_name,
                    "unit_number": r.unit_number,
                    "amount": float(r.amount) if r.amount is not None else None,
                    "status": r.status,
                    "kyc_status": r.kyc_status,
                    "admin_note": r.admin_note,
                    "can_download_receipt": r.status == "booked",
                    "created_at": r.created_date,
                    "last_activity_at": r.last_updated_date,
                }
                for r in rows
            ]
        except Exception:
            logger.exception("booking_kyc.list_mine_failed customer_id=%s", customer_id)
            raise RuntimeError("list_mine_failed")

    def get_receipt(self, booking_id: str, requester_id: str):
        """(pdf_bytes, filename) for a booking's payment receipt - gated on the
        booking itself being 'booked' (KYC approved), not just the payment being
        'paid': the client's requirement is that the download/generate-PDF button
        only lights up once KYC review has actually passed, even though the
        payment settled earlier. Raises ValueError('not_found') /
        PermissionError('forbidden') / ValueError('kyc_not_approved')."""
        booking = self._persistence.get_by_id(booking_id)
        if not booking:
            raise ValueError("not_found")
        if booking.customer_id != requester_id:
            raise PermissionError("forbidden")
        if booking.status != "booked":
            raise ValueError("kyc_not_approved")
        return self._payment_service.get_receipt(booking.payment_id, requester_id=requester_id)

    def _document_checklist(self, customer_id: str) -> list:
        checklist = []
        for doc_type in KYC_DOCUMENT_TYPES:
            try:
                doc, signed_url, expires_in = self._document_service.admin_get_latest(doc_type, customer_id)
            except Exception as e:  # pragma: no cover - admin_get_latest already guards, defensive here too
                logger.warning("booking_kyc.document_lookup_failed document_type=%s customer_id=%s error=%s",
                               doc_type, customer_id, e)
                doc, signed_url, expires_in = None, None, None
            checklist.append({
                "document_type": doc_type,
                "label": _DOCUMENT_LABELS.get(doc_type, doc_type),
                "uploaded": doc is not None,
                "preview_url": signed_url,
                "preview_url_expires_in": expires_in,
                "uploaded_at": getattr(doc, "created_date", None) if doc else None,
            })
        return checklist

    def get_detail(self, booking_id: str) -> dict:
        try:
            booking = self._persistence.get_by_id(booking_id)
            if not booking:
                raise ValueError("not_found")

            payment = None
            try:
                payment = self._payment_service.get(booking.payment_id, requester_id=booking.customer_id)
            except Exception:
                payment = None

            decisions = self._persistence.list_decisions(booking_id)

            customer = None
            try:
                customer = self._customer_persistence.get_by_id(booking.customer_id)
            except Exception:
                customer = None

            return {
                "id": booking.id,
                "status": booking.status,
                "kyc_status": booking.kyc_status,
                "version": booking.version,
                "admin_note": booking.admin_note,
                "customer_id": booking.customer_id,
                "customer_name": self._customer_name(booking.customer_id),
                "customer_email": getattr(customer, "email", None) if customer else None,
                "customer_phone": getattr(customer, "phone", None) if customer else None,
                "project_name": booking.project_name,
                "unit_number": booking.unit_number,
                "amount": float(booking.amount) if booking.amount is not None else None,
                "payment_id": booking.payment_id,
                "payment_method": getattr(payment, "method", None) if payment else None,
                "payment_status": getattr(payment, "status", None) if payment else None,
                "razorpay_payment_id": getattr(payment, "razorpay_payment_id", None) if payment else None,
                "utr_number": getattr(payment, "utr_number", None) if payment else None,
                "documents": self._document_checklist(booking.customer_id),
                "decision_history": [
                    {"actor": d.actor, "action": d.action, "note": d.note, "created_at": d.created_date}
                    for d in decisions
                ],
                "created_at": booking.created_date,
                "last_activity_at": booking.last_updated_date,
            }
        except ValueError:
            raise
        except Exception:
            logger.exception("booking_kyc.get_detail_failed booking_id=%s", booking_id)
            raise RuntimeError("get_detail_failed")

    def _require_reviewable(self, booking):
        """Only a booking still awaiting review can be Approved/Rejected/Cancelled -
        a decision already made is final (no re-deciding an already-booked or
        already-rejected/cancelled booking through this path)."""
        if (getattr(booking, "status", None) or "") not in _ACTIVE_STATUSES:
            raise ValueError("booking_not_reviewable")

    def _require_current_version(self, booking, expected_version: int):
        """Checked BEFORE any inventory mutation - not just as update_decision's
        own DB-level guard afterward. A wrong/stale expected_version (a client
        bug, a stale cached page, or a genuine version_conflict) must never
        proceed to confirm/release the plot first and then try to "roll back" -
        that rollback can only restore 'available', never the exact
        'pending_kyc_review' hold the booking record still claims, permanently
        stranding it (confirm/release's own guard then refuses to match a plot
        that's no longer 'pending_kyc_review', so even a later CORRECT retry
        fails with inventory_confirm_failed). Checking first makes that
        situation unreachable via this path - see test_booking_kyc_api.py's
        test_approve_happy_path_... regression coverage."""
        if (getattr(booking, "version", None) or 0) != expected_version:
            raise ValueError("version_conflict")

    def approve(self, booking_id: str, admin_id: str, note: str, expected_version: int) -> dict:
        """KYC verified -> plot booked. Raises ValueError('not_found') / 409-mapped
        ValueError('version_conflict') / ValueError('booking_not_reviewable') /
        ValueError('inventory_confirm_failed'). Never leaves the booking row and
        the inventory unit in disagreeing states - if the inventory confirm fails,
        the booking decision is NOT applied either."""
        try:
            booking = self._persistence.get_by_id(booking_id)
            if not booking:
                raise ValueError("not_found")
            self._require_reviewable(booking)
            self._require_current_version(booking, expected_version)

            unit = self._inventory_persistence.confirm_booking_after_kyc(
                id=booking.inventory_id, payment_id=booking.payment_id,
            )
            if unit is None:
                raise ValueError("inventory_confirm_failed")

            clean_note = (note or "").strip() or None
            updated = self._persistence.update_decision(
                id=booking_id, expected_version=expected_version,
                status="booked", kyc_status="verified", admin_note=clean_note,
            )
            if not updated:
                # Booking-row update lost the optimistic-lock race after the
                # inventory was already confirmed - undo that confirm rather than
                # leave a booked unit with no matching decision record. The unit
                # is now 'booked' (not 'pending_kyc_review' anymore), so this is
                # unbook_unit - the same repair-path release confirm_booking_after_kyc's
                # own UPDATE would need if it had to reverse itself - not
                # release_from_kyc_review, whose guard only matches the
                # pre-confirm state and would silently no-op here.
                released = self._inventory_persistence.unbook_unit(id=booking.inventory_id)
                if released is None:
                    logger.warning(
                        "booking_kyc.approve_rollback_unbook_failed booking_id=%s inventory_id=%s "
                        "- unit may be stuck 'booked' with no matching decision; needs manual review",
                        booking_id, booking.inventory_id,
                    )
                raise ValueError("version_conflict")

            self._persistence.add_decision(booking_id, actor=admin_id, action="approved", note=clean_note)
            self._payment_service.notify_booking_confirmed(booking.payment_id, booking=updated)
            self._notify_decision(booking, updated, decision="approved", admin_note=clean_note)
            return self.get_detail(booking_id)
        except ValueError:
            raise
        except Exception:
            logger.exception("booking_kyc.approve_failed booking_id=%s", booking_id)
            raise RuntimeError("approve_failed")

    def _reject_or_cancel(self, booking_id: str, admin_id: str, note: str, expected_version: int,
                          target_status: str, action_label: str) -> dict:
        try:
            booking = self._persistence.get_by_id(booking_id)
            if not booking:
                raise ValueError("not_found")
            self._require_reviewable(booking)
            self._require_current_version(booking, expected_version)

            released = self._inventory_persistence.release_from_kyc_review(
                id=booking.inventory_id, payment_id=booking.payment_id,
            )
            if released is None:
                # Not necessarily a bug: the version check on update_decision below
                # is what actually prevents a double-decision from being recorded,
                # so a concurrent double-reject racing here just means the second
                # caller's release is a harmless no-op (the unit's already been
                # released once) - but it's also the signature of a genuine stuck
                # unit (already released some other way), so it's worth a log
                # either way for ops to notice if it keeps happening.
                logger.info(
                    "booking_kyc.release_from_kyc_review_no_op booking_id=%s inventory_id=%s action=%s",
                    booking_id, booking.inventory_id, action_label,
                )

            clean_note = (note or "").strip() or None
            updated = self._persistence.update_decision(
                id=booking_id, expected_version=expected_version,
                status=target_status, kyc_status="rejected", admin_note=clean_note,
            )
            if not updated:
                raise ValueError("version_conflict")

            self._persistence.add_decision(booking_id, actor=admin_id, action=action_label, note=clean_note)

            try:
                self._payment_service.initiate_refund(booking.payment_id, reason=clean_note)
            except Exception as e:
                # The booking decision and plot release already succeeded (both are
                # the customer-facing/business-critical parts) - a refund-kickoff
                # failure here is logged for manual follow-up, never rolled back
                # into a 500 that would make the admin think the reject didn't work.
                logger.warning("booking_kyc.refund_initiate_failed booking_id=%s payment_id=%s error=%s",
                               booking_id, booking.payment_id, e)

            self._notify_decision(booking, updated, decision="rejected", admin_note=clean_note)
            return self.get_detail(booking_id)
        except ValueError:
            raise
        except Exception:
            logger.exception("booking_kyc.%s_failed booking_id=%s", action_label, booking_id)
            raise RuntimeError(f"{action_label}_failed")

    def reject(self, booking_id: str, admin_id: str, note: str, expected_version: int) -> dict:
        """KYC rejected -> plot released, refund initiated. Raises the same
        ValueError set as approve(), plus never applies without a released plot."""
        return self._reject_or_cancel(booking_id, admin_id, note, expected_version,
                                      target_status="rejected", action_label="rejected")

    def cancel(self, booking_id: str, admin_id: str, note: str, expected_version: int) -> dict:
        """Admin cancels the booking outright (e.g. at the customer's own request) -
        same release-plot + refund-initiate effect as reject(), recorded as a
        distinct action/status so the two are distinguishable in the timeline."""
        return self._reject_or_cancel(booking_id, admin_id, note, expected_version,
                                      target_status="cancelled", action_label="cancelled")

    def _notify_decision(self, booking, updated, decision: str, admin_note: str) -> None:
        """Best-effort KYC-decision email. Never raises - see serviceEmail's own
        'designed to never raise' contract; this is one more defensive layer on
        top of that for the customer/project lookups this method itself does."""
        try:
            customer = self._customer_persistence.get_by_id(booking.customer_id)
            email = getattr(customer, "email", None) if customer else None
            if not email:
                return
            refund_instructions = None
            if decision == "rejected":
                payment = None
                try:
                    payment = self._payment_service.get(booking.payment_id, requester_id=booking.customer_id)
                except Exception:
                    payment = None
                method = (getattr(payment, "method", None) or "razorpay").lower()
                if method == "razorpay":
                    refund_instructions = "Your payment is being refunded to your original payment method."
                elif method == "cash":
                    refund_instructions = "Please collect your cash refund from our office within 5-7 business days."
                else:
                    refund_instructions = ("A manual bank transfer (NEFT/RTGS) refund will be initiated by our "
                                           "team within 5-7 business days.")
            from DivineService.service_email import dispatch_kyc_decision_email
            dispatch_kyc_decision_email(
                self._email(), email,
                decision=decision, first_name=getattr(customer, "first_name", None),
                project_name=booking.project_name, unit_number=booking.unit_number,
                booking_id=booking.id, admin_note=admin_note,
                refund_instructions=refund_instructions, amount=booking.amount,
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("booking_kyc.decision_email_failed booking_id=%s error=%s", booking.id, e)
