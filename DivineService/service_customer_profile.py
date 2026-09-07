import logging
import re
from datetime import datetime, timezone

from Divinepersistence.persistence_customer_profile import persistenceCustomerProfile
from DivineService.loan_utils import normalize_indian_amount
from DivineDTO.models import (
    CustomerAddressDTO,
    CustomerBookingDTO,
    CustomerNextDueDTO,
    CustomerProfileDTO,
    PaymentScheduleRowDTO,
)


class serviceCustomerProfile:
    """Assembles the ``GET /customer/profile`` payload.

    Architecture / rules honoured here:

    * Pure OOP - every method is an instance method that works through ``self``;
      there are no static or class methods and no free functions.
    * The layering is API -> service -> persistence; this class never touches a
      DB session directly, it only calls :class:`persistenceCustomerProfile`.
    * Defensive by construction - each section of the response is built inside
      its own ``try/except`` so one malformed source record can never abort the
      whole response. Only two conditions propagate: a non-customer caller
      (``PermissionError`` -> 403) and a genuinely missing customer
      (``LookupError`` -> 404). Everything else is caught.
    """

    _DATE_INPUT_FORMATS = (
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%Y/%m/%d",
        "%d.%m.%Y",
        "%d %b %Y",
        "%d %B %Y",
    )

    # Candidate keys inside the (client-supplied, schema-free) booking-application
    # form_data blob. Both snake_case and camelCase spellings are accepted.
    _PROJECT_ID_KEYS = ("project_id", "projectId")
    _PROJECT_NAME_KEYS = (
        "project_name", "projectName", "project",
        "township", "townshipName", "township_name",
    )
    _TOWNSHIP_LABEL_KEYS = ("township_label", "townshipLabel")
    _UNIT_NUMBER_KEYS = (
        "unit_number", "unitNumber", "unit_no", "unitNo",
        "plot_number", "plotNumber", "plot_no", "plotNo",
    )
    _PLOT_AREA_KEYS = (
        "plot_area_sq_yd", "plotAreaSqYd", "plot_area", "plotArea",
        "area_sq_yd", "areaSqYd", "area_sqyd", "areaSqyd",
    )
    _UNIT_TYPE_KEYS = ("unit_type", "unitType")
    _BOOKING_DATE_KEYS = ("booking_date", "bookingDate", "date")
    _TOTAL_CONSIDERATION_KEYS = (
        "total_consideration", "totalConsideration",
        "total_price", "totalPrice", "consideration",
        "sale_value", "saleValue", "total_amount", "totalAmount",
    )
    _AMOUNT_RECEIVED_KEYS = (
        "amount_received", "amountReceived",
        "received_amount", "receivedAmount",
        "paid_amount", "paidAmount",
    )
    _PAYMENT_SCHEDULE_KEYS = (
        "payment_schedule", "paymentSchedule",
        "payment_plan", "paymentPlan",
        "installments", "schedule",
    )

    _GENDER_MAP = {
        "m": "male", "male": "male",
        "f": "female", "female": "female",
        "t": "transgender", "transgender": "transgender",
        "o": "other", "other": "other",
    }

    def __init__(self, persistence: persistenceCustomerProfile = None, milestone_service=None):
        self._persistence = persistence or persistenceCustomerProfile()
        self._logger = logging.getLogger(__name__)
        self._milestone_service = milestone_service  # lazily built in _milestones()

    def _milestones(self):
        if self._milestone_service is None:
            try:
                from DivineService.service_milestones import serviceMilestones
                self._milestone_service = serviceMilestones(profile_persistence=self._persistence)
            except Exception as exc:  # pragma: no cover - defensive
                self._logger.warning("profile_milestone_service_init_failed error=%s", exc)
                self._milestone_service = None
        return self._milestone_service

    # -- public API ------------------------------------------------------

    def get_profile(self, customer_id: str, role: str) -> CustomerProfileDTO:
        try:
            if role != "customer":
                raise PermissionError("customer_only")
            if not customer_id:
                raise LookupError("profile_not_found")

            customer = self._persistence.get_customer(customer_id)
            if customer is None:
                raise LookupError("profile_not_found")

            identity = self._safe_identity(customer_id)

            resolved_id = self._text(getattr(customer, "id", None)) or str(customer_id)
            dto = CustomerProfileDTO(customer_id=resolved_id)
            self._apply_names(dto, customer, identity)
            self._apply_contact(dto, customer)
            self._apply_identity_fields(dto, identity)
            self._apply_address(dto, identity)
            dto.booking = self._build_booking(customer_id)
            return dto
        except (PermissionError, LookupError):
            raise
        except Exception:
            self._logger.error(
                "profile_assembly_failed customer_id=%s", customer_id, exc_info=True
            )
            raise

    # -- section builders ---------------------------------------------------

    def _safe_identity(self, customer_id):
        try:
            data = self._persistence.get_identity_extract(customer_id)
            return data if isinstance(data, dict) else {}
        except Exception:
            self._logger.warning(
                "profile_identity_section_failed customer_id=%s",
                customer_id,
                exc_info=True,
            )
            return {}

    def _apply_names(self, dto, customer, identity):
        try:
            first = self._text(getattr(customer, "first_name", None))
            last = self._text(getattr(customer, "last_name", None))
            identity_full = self._text(identity.get("name")) if isinstance(identity, dict) else None

            if not first and not last and identity_full:
                parts = identity_full.split()
                first = parts[0] if parts else None
                last = " ".join(parts[1:]) or None

            dto.first_name = first
            dto.last_name = last
            dto.full_name = " ".join(p for p in (first, last) if p) or identity_full
        except Exception:
            self._logger.warning("profile_names_section_failed", exc_info=True)

    def _apply_contact(self, dto, customer):
        try:
            dto.email = self._text(getattr(customer, "email", None))
            dto.phone = self._text(getattr(customer, "phone", None))
        except Exception:
            self._logger.warning("profile_contact_section_failed", exc_info=True)

    def _apply_identity_fields(self, dto, identity):
        try:
            if not isinstance(identity, dict):
                return
            dto.gender = self._normalise_gender(self._text(identity.get("gender")))
            dob_raw = self._text(identity.get("dob")) or self._text(identity.get("date_of_birth"))
            iso_dob, age = self._parse_dob(dob_raw)
            dto.date_of_birth = iso_dob
            dto.age = age
        except Exception:
            self._logger.warning("profile_identity_fields_section_failed", exc_info=True)

    def _apply_address(self, dto, identity):
        try:
            if not isinstance(identity, dict):
                return
            care_of = self._text(identity.get("careof")) or self._text(identity.get("care_of"))
            house = self._text(identity.get("house"))
            street = self._text(identity.get("street"))
            landmark = self._text(identity.get("landmark"))
            location = self._text(identity.get("location"))
            vtc = self._text(identity.get("vtc"))
            subdistrict = self._text(identity.get("subdistrict"))
            district = self._text(identity.get("district"))
            state = self._text(identity.get("state"))
            pincode = self._text(identity.get("pincode")) or self._text(identity.get("pc"))

            line1 = ", ".join(p for p in (care_of, house, street) if p) or None
            line2 = ", ".join(dict.fromkeys(p for p in (landmark, location) if p)) or None
            city = vtc or subdistrict or district

            if not any((line1, line2, city, state, pincode)):
                return

            dto.address = CustomerAddressDTO(
                line1=line1, line2=line2, city=city, state=state, pincode=pincode
            )
            dto.address_text = self._compose_address_text(line1, line2, city, state, pincode)
        except Exception:
            self._logger.warning("profile_address_section_failed", exc_info=True)

    def _build_booking(self, customer_id):
        booking = CustomerBookingDTO(has_booking=False)

        try:
            record = self._persistence.get_booking_application(customer_id)
        except Exception:
            self._logger.warning(
                "profile_booking_fetch_failed customer_id=%s", customer_id, exc_info=True
            )
            record = None

        if not record:
            total = self._resolve_amount_received(customer_id, None)
            if total:
                booking.amount_received = total
            return booking

        form = record.get("form_data") if isinstance(record, dict) else None
        form = form if isinstance(form, dict) else {}

        try:
            booking.has_booking = True
            booking.project_id = (
                self._text(record.get("project_id"))
                or self._first_present(form, self._PROJECT_ID_KEYS)
            )
            booking.project_name = self._first_present(form, self._PROJECT_NAME_KEYS)
            booking.township_label = self._first_present(form, self._TOWNSHIP_LABEL_KEYS)
            booking.unit_number = self._first_present(form, self._UNIT_NUMBER_KEYS)
            booking.plot_area_sq_yd = self._first_present(form, self._PLOT_AREA_KEYS)
            booking.unit_type = self._first_present(form, self._UNIT_TYPE_KEYS)
            booking.booking_date = self._resolve_booking_date(form, record.get("created_date"))
            booking.total_consideration = self._to_int_rupees(
                self._first_present(form, self._TOTAL_CONSIDERATION_KEYS)
            )
            amount_received = self._resolve_amount_received(customer_id, booking.project_id)
            form_amount = self._to_int_rupees(
                self._first_present(form, self._AMOUNT_RECEIVED_KEYS)
            )
            booking.amount_received = amount_received or form_amount
            booking.payment_schedule = self._build_payment_schedule(form)
            if not booking.payment_schedule and booking.total_consideration:
                # Booking stored before schedules were derived server-side - compute
                # it now so the demand / allotment letters still have amounts.
                booking.payment_schedule = self._derive_payment_schedule(
                    booking.total_consideration, booking.amount_received, booking.booking_date
                )
        except Exception:
            self._logger.warning(
                "profile_booking_build_failed customer_id=%s", customer_id, exc_info=True
            )

        # Milestone-backed enrichment: per-row status / pay_enabled_from / paid_on,
        # a next_due summary, and amount_received re-derived from paid milestones.
        # Falls back silently to the form-data schedule built above on any failure.
        self._apply_milestone_schedule(booking, customer_id)
        return booking

    def _apply_milestone_schedule(self, booking, customer_id):
        try:
            svc = self._milestones()
            if svc is None:
                return
            enriched = svc.enriched_schedule(customer_id)
            rows = (enriched or {}).get("rows") or []
            if rows:
                booking.payment_schedule = [
                    PaymentScheduleRowDTO(
                        id=self._text(r.get("id")),
                        label=self._text(r.get("label")),
                        percent=self._to_float(r.get("percent")),
                        due_days=self._to_int(r.get("due_days")),
                        due_date=self._text(r.get("due_date")),
                        amount=self._to_int_rupees(r.get("amount")),
                        status=self._text(r.get("status")),
                        pay_enabled_from=self._text(r.get("pay_enabled_from")),
                        paid_on=self._text(r.get("paid_on")),
                        paid_payment_id=self._text(r.get("paid_payment_id")),
                    )
                    for r in rows
                ]
            nd = (enriched or {}).get("next_due")
            if nd:
                booking.next_due = CustomerNextDueDTO(
                    milestone_id=self._text(nd.get("milestone_id")),
                    label=self._text(nd.get("label")),
                    amount=self._to_int_rupees(nd.get("amount")),
                    due_date=self._text(nd.get("due_date")),
                    days_until_due=self._to_int(nd.get("days_until_due")),
                    status=self._text(nd.get("status")),
                    last_reminder_kind=self._text(nd.get("last_reminder_kind")),
                    last_reminder_at=self._text(nd.get("last_reminder_at")),
                )
            paid_total = svc.amount_received_rupees(customer_id)
            if paid_total is not None:
                booking.amount_received = paid_total
        except Exception:
            self._logger.warning(
                "profile_milestone_schedule_failed customer_id=%s", customer_id, exc_info=True
            )

    def _resolve_amount_received(self, customer_id, project_id):
        """Paid total (whole rupees) attributable to this booking.

        A customer with at most one booking project has no ambiguity, so the
        whole paid balance is theirs. Once a customer has more than one booking
        project, only the payments linked - through the booking-application
        document - to *this* project are counted, so one unit's figure never
        absorbs another's.
        """
        try:
            if project_id:
                try:
                    if self._persistence.count_booking_projects(customer_id) > 1:
                        return self._persistence.get_amount_received_for_project(
                            customer_id, project_id
                        ) or 0
                except Exception:
                    self._logger.warning(
                        "profile_amount_scope_check_failed customer_id=%s",
                        customer_id,
                        exc_info=True,
                    )
            return self._persistence.get_amount_received(customer_id) or 0
        except Exception:
            self._logger.warning(
                "profile_amount_received_failed customer_id=%s", customer_id, exc_info=True
            )
            return 0

    def _build_payment_schedule(self, form):
        try:
            rows_raw = None
            for key in self._PAYMENT_SCHEDULE_KEYS:
                candidate = form.get(key) if isinstance(form, dict) else None
                if isinstance(candidate, list) and candidate:
                    rows_raw = candidate
                    break
            if not rows_raw:
                return None

            rows = []
            for item in rows_raw:
                if not isinstance(item, dict):
                    continue
                rows.append(
                    PaymentScheduleRowDTO(
                        label=self._text(self._pick(item, "label", "name")),
                        percent=self._to_float(self._pick(item, "percent", "percentage")),
                        due_days=self._to_int(self._pick(item, "due_days", "dueDays")),
                        due_date=self._text(self._pick(item, "due_date", "dueDate")),
                        amount=self._to_int_rupees(item.get("amount")),
                        status=self._text(item.get("status")),
                    )
                )
            return rows or None
        except Exception:
            self._logger.warning("profile_payment_schedule_section_failed", exc_info=True)
            return None

    def _derive_payment_schedule(self, total, received, booking_date):
        try:
            from DivineService.service_payment_schedule import build_payment_schedule
            plan = build_payment_schedule(total, received or 0, booking_date)
            rows = [
                PaymentScheduleRowDTO(
                    label=r.get("label"), percent=r.get("percent"), due_days=r.get("due_days"),
                    due_date=r.get("due_date"), amount=r.get("amount"), status=r.get("status"),
                )
                for r in (plan.get("rows") or [])
            ]
            return rows or None
        except Exception:
            self._logger.warning("profile_payment_schedule_derive_failed", exc_info=True)
            return None

    # -- small value helpers ---------------------------------------------

    def _text(self, value):
        try:
            if value is None:
                return None
            text_value = str(value).strip()
            return text_value or None
        except Exception:
            return None

    def _first_present(self, data, keys):
        if not isinstance(data, dict):
            return None
        for key in keys:
            if key in data:
                value = self._text(data.get(key))
                if value:
                    return value
        return None

    def _pick(self, data, *keys):
        """Value of the first key that is actually present.

        Unlike ``a or b``, a legitimate ``0`` / ``0.0`` (e.g. ``due_days: 0`` for
        an on-booking milestone, or ``percent: 0``) is returned rather than
        skipped as falsy.
        """
        if not isinstance(data, dict):
            return None
        for key in keys:
            if key in data:
                return data.get(key)
        return None

    def _normalise_gender(self, gender):
        if not gender:
            return None
        return self._GENDER_MAP.get(gender.strip().lower(), gender.strip())

    def _today(self):
        return datetime.now(timezone.utc).date()

    def _age_from_date(self, born):
        try:
            today = self._today()
            years = today.year - born.year - (
                (today.month, today.day) < (born.month, born.day)
            )
            return max(0, years)
        except Exception:
            return None

    def _parse_dob(self, dob_raw):
        """Return ``(iso_date_or_None, age_or_None)`` for a free-text DOB."""
        if not dob_raw:
            return None, None
        try:
            if dob_raw.isdigit() and len(dob_raw) == 4:
                return None, max(0, self._today().year - int(dob_raw))
        except Exception:
            pass
        for fmt in self._DATE_INPUT_FORMATS:
            try:
                parsed = datetime.strptime(dob_raw, fmt).date()
            except (ValueError, TypeError):
                continue
            try:
                return parsed.isoformat(), self._age_from_date(parsed)
            except Exception:
                return None, None
        return None, None

    def _resolve_booking_date(self, form, created_date):
        raw = self._first_present(form, self._BOOKING_DATE_KEYS)
        if raw:
            iso, _ = self._parse_dob(raw)
            return iso or raw
        return self._date_only(created_date)

    def _date_only(self, value):
        """Reduce a date / datetime / ISO-ish timestamp string to ``YYYY-MM-DD``.

        The booking document's ``created_date`` comes back as a ``datetime`` on
        Postgres but as a string like ``'2025-03-04 00:00:00+00:00'`` on SQLite;
        this keeps ``booking_date`` date-only in both cases, consistent with the
        other date fields.
        """
        try:
            if value is None:
                return None
            date_attr = getattr(value, "date", None)
            if callable(date_attr):
                return date_attr().isoformat()
            text_value = self._text(value)
            if not text_value:
                return None
            head = re.split(r"[ T]", text_value, maxsplit=1)[0]
            try:
                datetime.strptime(head, "%Y-%m-%d")
                return head
            except ValueError:
                return text_value
        except Exception:
            return None

    def _to_float(self, value):
        """Parse a number or Indian-money expression into a float, or ``None``.

        Delegates to :func:`DivineService.loan_utils.normalize_indian_amount` so
        the ``₹`` / comma / ``lakh`` / ``crore`` / ``k`` handling lives in one
        place instead of being re-implemented here.
        """
        try:
            if value is None or isinstance(value, bool):
                return None
            return normalize_indian_amount(value)
        except Exception:
            return None

    def _to_int(self, value):
        result = self._to_float(value)
        return int(result) if result is not None else None

    def _to_int_rupees(self, value):
        result = self._to_float(value)
        return int(round(result)) if result is not None else None

    def _compose_address_text(self, line1, line2, city, state, pincode):
        try:
            head = [p for p in (line1, line2) if p]
            tail = ", ".join(p for p in (city, state) if p)
            if pincode:
                tail = (tail + " " + pincode).strip() if tail else pincode
            segments = head + ([tail] if tail else [])
            return ", ".join(segments) or None
        except Exception:
            return None
