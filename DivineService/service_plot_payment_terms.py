"""Payment plans of customers' plots, for the admin Customers section.

A website booking gets its plan, prices and booking date from the booking
payment. An offline buyer's plot (the 'imported' booking application record
the buyer import created) gets them here, from an admin or from the client's
sheet: the payment plan and booking date first, the prices (Basic Sale Price,
PLC, IFMS, CAM) and the amount received to date when known.

The terms are stored on the plot record in the same form_data keys a website
booking uses (payment_plan, payment_plan_code, booking_date,
total_consideration, amount_received, payment_schedule), and once priced its
instalments are written to divine_payment_milestones (booking_id = the record's
id), marked paid oldest-first for the amount received. So the existing demand
letter, customer profile and instalment code read them unchanged. A received
amount that stops partway through an instalment is kept as
received_unallocated and credited by the demand letter.

Payment-due reminder emails stay off for these plots: the record keeps
form_data.source = 'offline_import', which the reminder job skips.
"""
import logging
import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from Divinepersistence.persistence_plot_terms import persistencePlotTerms
from DivineService.inventory_export import _Form, project_key
from DivineService.service_demand_letter import (
    _NAME_KEYS, _PLOT_KEYS, _first, _s, finance_pending,
)
from DivineService.service_milestones import compute_status
from DivineService.service_offline_import import (
    Report, clean_phone, new_batch_id, plot_key, sheet_records, split_salutation,
)
from DivineService.service_payment_plans import compute_schedule, ist_today

logger = logging.getLogger(__name__)

IMPORTED_STATUS = "imported"
OFFLINE_SOURCE = "offline_import"
# A received amount within a rupee of an instalment counts as covering it.
_ROUNDING = Decimal("1")
# A plot's co-applicants (first, second), with the keys the buyer import and
# the customer profile use.
_CO_APPLICANT_KEYS = (("coApplicantName", "co_applicant_name"), ("coApplicant2Name", "co_applicant_2_name"))
# Website bookings that still hold their plot (divine_bookings.status).
_ACTIVE_BOOKING_STATUSES = ("pending_kyc_review", "booked")
# Form keys that only make sense with prices; cleared when a plot loses them.
_PRICE_KEYS = ("total_consideration", "amount_received", "received_unallocated", "total_outstanding",
               "total_outstanding_words", "payment_schedule")

# terms_status of a plot, for the admin panel.
PLAN_NEEDED = "plan_needed"
PRICE_NEEDED = "price_needed"
COMPLETE = "complete"


def _money(value, code: str):
    """None for blank; otherwise a non-negative Decimal, or ValueError(code)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(code)
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        raise ValueError(code)
    if not amount.is_finite() or amount < 0:
        raise ValueError(code)
    return amount


def parse_booking_date(value) -> date:
    """YYYY-MM-DD, DD-MM-YYYY, DD/MM/YYYY, DD.MM.YYYY, or an Excel date serial
    (a date cell read raw from an .xlsx). ValueError('invalid_booking_date')."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if re.fullmatch(r"\d{5}(\.0+)?", text):            # Excel serial: days since 1899-12-30
        return date(1899, 12, 30) + timedelta(days=int(float(text)))
    for parse in (lambda t: date.fromisoformat(t[:10]),
                  lambda t: datetime.strptime(t[:10], "%d-%m-%Y").date(),
                  lambda t: datetime.strptime(t[:10], "%d/%m/%Y").date(),
                  lambda t: datetime.strptime(t[:10], "%d.%m.%Y").date()):
        try:
            return parse(text)
        except ValueError:
            continue
    raise ValueError("invalid_booking_date")


def _percents(text) -> list:
    return [Decimal(n) for n in re.findall(r"\d+(?:\.\d+)?", str(text or ""))]


def _natural(value) -> tuple:
    """Sort key for plot numbers: 'C16' < 'C100' < 'D7' (letters, then numbers by value)."""
    return tuple((0, int(part)) if part.isdigit() else (1, part.upper())
                 for part in re.split(r"(\d+)", str(value or "")) if part)


def _num(value):
    if value is None:
        return None
    number = float(value)
    return int(number) if number.is_integer() else number


class servicePlotPaymentTerms:
    def __init__(self, persistence: persistencePlotTerms = None, plan_service=None, document_service=None):
        self._persistence = persistence or persistencePlotTerms()
        if plan_service is None:
            from DivineService.service_payment_plans import servicePaymentPlans
            plan_service = servicePaymentPlans()
        self._plans = plan_service
        self._documents = document_service

    # ---- plans -------------------------------------------------------------
    def resolve_plan(self, label, project=None) -> dict:
        """The active plan an admin or a sheet names: its code ("PP1/FPP/...")
        or short code ("PP1"), or a label listing its instalment percentages in
        order ("PL-10-15-25-25-15-10" is PP1). Raises payment_plan_not_found,
        payment_plan_ambiguous or payment_plan_not_for_project."""
        label = str(label or "").strip()
        if not label:
            raise ValueError("payment_plan_not_found")
        project_id = project_key(project) if project else None
        try:
            plan = self._plans.get_plan(label)
        except ValueError:
            wanted = _percents(label)
            if not wanted:
                raise
            # Active plans for the project (and plans for any project).
            candidates = [p for p in self._plans.list_plans(project_id)
                          if [Decimal(str(r["percent"])) for r in p["instalments"]] == wanted]
            if not candidates:
                raise ValueError("payment_plan_not_found")
            if len(candidates) > 1:
                raise ValueError("payment_plan_ambiguous")
            plan = self._plans.get_plan(candidates[0]["code"])
        if project_id and plan.get("project_id") and plan["project_id"] != project_id:
            raise ValueError("payment_plan_not_for_project")
        return plan

    # ---- reading -------------------------------------------------------------
    @staticmethod
    def _plan_label(form: dict):
        plan = form.get("payment_plan")
        if isinstance(plan, dict) and (plan.get("short_code") or plan.get("code")):
            return str(plan.get("short_code") or plan.get("code"))
        return _s(form.get("payment_plan_code")) or None

    def plot_plans_for_customers(self, customer_ids) -> dict:
        """customer id -> {"plot_plans": [...], "payment_plans": [...]} for the
        admin customers list.

        plot_plans: one entry per plot the customer holds, with that plot's
        payment plan (short code, or None while none is linked) and applicant,
        in natural plot order (C16, C100, D7). A plot is held through:
          - a website booking that is still active (its divine_bookings row is
            pending KYC review or booked; a record with no booking row counts),
            located by the booking's plot, else by the record's own plot;
          - an offline buyer's record for a plot linked to the customer
            (divine_project_inventory.booked_by), located by the plot id the
            terms stored on it, else by plot number + project (the export's rule);
          - a plot linked to the customer with no record (plan None).
        When a plot has several records, the newest one is its record - the
        same one link_from_sheet writes to.
        payment_plans: the distinct short codes across those plots."""
        docs = self._persistence.booking_docs_for_customers(customer_ids)
        units = self._persistence.units_for_customers(customer_ids)
        bookings = self._persistence.bookings_for_payments([d.get("payment_id") for d in docs])
        units_by_id = {u["id"]: u for u in units}
        units_by_plot = {}
        for unit in units:
            units_by_plot.setdefault((unit["booked_by"], plot_key(unit["unit_number"])), []).append(unit)

        held = {}                                  # owner -> {plot key: entry}
        for unit in units:
            held.setdefault(unit["booked_by"], {})[unit["id"]] = {
                "project_name": unit["project_name"], "unit_number": unit["unit_number"],
                "payment_plan": None, "applicant_name": None}
        for doc in docs:                           # oldest first: a newer record replaces an older one
            owner, record = doc["owner_id"], _Form(doc)
            entry = {"payment_plan": self._plan_label(record.data),
                     "applicant_name": _s(_first(record.data, _NAME_KEYS)) or None}
            if doc.get("status") != IMPORTED_STATUS:
                booking = bookings.get(doc.get("payment_id"))
                if booking and booking.get("status") not in _ACTIVE_BOOKING_STATUSES:
                    continue                       # rejected / cancelled: the plot was released
                unit = units_by_id.get((booking or {}).get("inventory_id"))
                if unit is None:
                    unit = next((u for u in units_by_plot.get((owner, record.plot_key), []) if record.is_for(u)), None)
                if unit is not None:
                    key, place = unit["id"], unit
                else:
                    place = {"project_name": (booking or {}).get("project_name")
                             or _s(record.data.get("project_name")) or doc.get("project_id"),
                             "unit_number": (booking or {}).get("unit_number") or _s(_first(record.data, _PLOT_KEYS))
                             or None}
                    key = ("record", project_key(place["project_name"]), plot_key(place["unit_number"]))
            else:
                stored = units_by_id.get(record.data.get("inventory_id"))
                unit = stored if stored is not None and stored["booked_by"] == owner else next(
                    (u for u in units_by_plot.get((owner, record.plot_key), []) if record.is_for(u)), None)
                if unit is None:
                    continue                       # the plot is no longer linked to this buyer
                key, place = unit["id"], unit
            held.setdefault(owner, {})[key] = {"project_name": place["project_name"],
                                               "unit_number": place["unit_number"], **entry}
        out = {}
        for owner, plots in held.items():
            ordered = sorted(plots.values(), key=lambda p: (project_key(p["project_name"]),
                                                            _natural(p["unit_number"])))
            out[owner] = {"plot_plans": ordered,
                          "payment_plans": list(dict.fromkeys(p["payment_plan"] for p in ordered
                                                              if p["payment_plan"]))}
        return out

    def plot_view(self, doc: dict) -> dict:
        form = doc["form_data"]
        plan = form.get("payment_plan") if isinstance(form.get("payment_plan"), dict) else {}
        plan_code = plan.get("code") or _s(form.get("payment_plan_code")) or None
        milestones = self._persistence.milestones_for(doc["id"])
        today = ist_today()
        schedule = [{
            "milestone_no": m.get("milestone_no"),
            "label": m.get("label"),
            "percent": _num(m.get("percent")),
            "due_date": m["due_date"].isoformat() if hasattr(m.get("due_date"), "isoformat") else m.get("due_date"),
            "amount": _num(m.get("amount")) or 0,
            "status": compute_status(m.get("due_date"), (m.get("status") or "") == "paid", today=today),
        } for m in milestones]
        received = None
        if schedule:
            received = sum(r["amount"] for r in schedule if r["status"] == "paid") \
                + float(form.get("received_unallocated") or 0)
        elif form.get("amount_received") not in (None, ""):
            received = float(form["amount_received"])
        total = form.get("total_consideration")
        total = float(total) if total not in (None, "") else None
        if not plan_code:
            terms_status = PLAN_NEEDED
        elif total is None:
            terms_status = PRICE_NEEDED
        else:
            terms_status = COMPLETE
        overlay = dict(form, payment_schedule=schedule) if schedule else form
        return {
            "document_id": doc["id"],
            "record_type": "offline" if doc.get("status") == IMPORTED_STATUS else "website",
            "project_name": _s(form.get("project_name")) or doc.get("project_id"),
            "unit_number": _s(_first(form, _PLOT_KEYS)) or None,
            # Each plot keeps its own applicant: buyers who share an email share
            # one login account, but not their plots' applicants or letters.
            "applicant_name": _s(_first(form, _NAME_KEYS)) or None,
            "co_applicant_names": [n for n in dict.fromkeys(_s(_first(form, keys)) for keys in _CO_APPLICANT_KEYS)
                                   if n],
            "payment_plan": {
                "code": plan_code, "short_code": plan.get("short_code"), "name": plan.get("name"),
                "version": plan.get("version"),
            } if plan_code else None,
            "booking_date": _s(form.get("booking_date")) or None,
            "sale_price": plan.get("sale_price"), "plc": plan.get("plc"), "ifms": plan.get("ifms"),
            "cam": plan.get("cam"), "total": _num(total),
            "amount_received": _num(received) if received is not None else None,
            "outstanding": _num(max(0.0, total - (received or 0))) if total is not None else None,
            "terms_status": terms_status,
            "terms_editable": doc.get("status") == IMPORTED_STATUS,
            "demand_letter_available": not finance_pending(overlay),
            "schedule": schedule,
        }

    def customer_detail(self, customer_id: str) -> dict:
        customer = self._persistence.get_customer(customer_id)
        if not customer:
            raise ValueError("not_found")
        docs = self._persistence.booking_docs_for_customers([customer_id])
        name = " ".join(f"{customer.get('first_name') or ''} {customer.get('last_name') or ''}".split())
        return {
            "id": customer["id"], "full_name": name, "email": customer.get("email"),
            "phone": customer.get("phone"),
            "source": "OFFLINE_IMPORT" if customer.get("source") == OFFLINE_SOURCE else "WEBSITE",
            "plots": [self.plot_view(d) for d in docs],
        }

    # ---- the write -------------------------------------------------------------
    def _owned_record(self, customer_id: str, document_id: str) -> dict:
        doc = self._persistence.get_booking_doc(document_id)
        if not doc or doc.get("owner_role") != "customer" or doc["owner_id"] != customer_id:
            raise ValueError("plot_not_found")
        return doc

    def prepare_terms(self, customer_id: str, document_id: str, payment_plan, booking_date,
                      sale_price=None, plc=None, ifms=None, cam=None, amount_received=None,
                      set_by: str = None) -> tuple:
        """Validates and builds (record, new form_data, milestone rows) without
        saving. ValueError codes: plot_not_found, terms_from_website_booking,
        payment_plan_not_found, payment_plan_ambiguous,
        payment_plan_not_for_project, invalid_booking_date, booking_date_in_future,
        invalid_sale_price, invalid_plc_amount, invalid_ifms_amount,
        invalid_cam_amount, sale_price_required, amount_received_exceeds_total,
        total_amount_too_large, sale_price_too_small."""
        doc = self._owned_record(customer_id, document_id)
        if doc.get("status") != IMPORTED_STATUS:
            # A website booking's terms are the ones its payment was made against.
            raise ValueError("terms_from_website_booking")
        form = dict(doc["form_data"])
        project = _s(form.get("project_name")) or doc.get("project_id")
        plan = self.resolve_plan(payment_plan, project)
        booked_on = parse_booking_date(booking_date)
        if booked_on > ist_today():
            raise ValueError("booking_date_in_future")
        price = _money(sale_price, "invalid_sale_price")
        charges = {"plc": _money(plc, "invalid_plc_amount"), "ifms": _money(ifms, "invalid_ifms_amount"),
                   "cam": _money(cam, "invalid_cam_amount")}
        received = _money(amount_received, "invalid_amount_received")
        if price is None and (received or any(charges.values())):
            raise ValueError("sale_price_required")

        project_id = plan.get("project_id") or project_key(project) or None
        unit = self._unit_for(customer_id, form, project)
        if unit:
            # The plot this record is for - counted under its plan in the admin
            # payment-plans list, and on its instalments.
            form["inventory_id"] = unit["id"]
        plan_info = {"code": plan["code"], "short_code": plan.get("short_code"), "name": plan.get("name"),
                     "version": int(plan.get("version") or 1)}
        form.update({
            "payment_plan_code": plan["code"], "booking_date": booked_on.isoformat(),
            "payment_terms_set_by": set_by, "payment_terms_set_at": datetime.now(timezone.utc).isoformat(),
            # Reminder emails stay off for these plots (the job skips this source).
            "source": form.get("source") or OFFLINE_SOURCE,
        })
        if price is None:
            for key in _PRICE_KEYS:
                form.pop(key, None)
            form["payment_plan"] = plan_info
            form["finance_status"] = "pending"
            return doc, form, []

        schedule = compute_schedule(plan, price, charges["plc"] or 0, charges["ifms"] or 0, charges["cam"] or 0,
                                    booking_date=booked_on, possession_date=self._plans.possession_date(project_id))
        total = Decimal(str(schedule["total"]))
        received = received or Decimal(0)
        if received > total + _ROUNDING:
            raise ValueError("amount_received_exceeds_total")
        rows, left = [], received
        for row in schedule["rows"]:
            amount = Decimal(str(row["amount"]))
            paid = left + _ROUNDING >= amount and (not rows or rows[-1]["status"] == "paid")
            if paid:
                left -= min(left, amount)
            rows.append({**row, "status": "paid" if paid else "due"})
        unallocated = max(Decimal(0), left)
        outstanding = max(Decimal(0), total - received)
        from DivineService.loan_report_data import amount_in_words_indian
        form.update({
            "payment_plan": {**plan_info, "sale_price": schedule["sale_price"], "plc": schedule["plc"],
                             "ifms": schedule["ifms"], "cam": schedule["cam"], "total": schedule["total"],
                             "booking_amount": schedule["booking_amount"]},
            "total_consideration": schedule["total"],
            "amount_received": _num(received),
            "total_outstanding": _num(outstanding),
            "total_outstanding_words": amount_in_words_indian(float(outstanding)) or None,
            "payment_schedule": rows,
            "finance_status": "complete",
        })
        if unallocated > 0:
            form["received_unallocated"] = _num(unallocated)
        else:
            form.pop("received_unallocated", None)

        milestones = [{
            "id": f"{doc['id']}-m{i}", "booking_id": doc["id"], "customer_id": customer_id,
            "project_id": project_id, "inventory_id": unit.get("id") if unit else None,
            "milestone_no": i, "label": row["label"], "percent": row["percent"],
            "due_days": row.get("due_days"),
            "due_date": date.fromisoformat(row["due_date"]) if row.get("due_date") else None,
            "amount": int(round(float(row["amount"]))),
            "status": "paid" if row["status"] == "paid" else "upcoming",
            "paid_on": None, "paid_payment_id": None,
            "basic_amount": row.get("basic_amount"), "charge_head": row.get("charge_head"),
            "charge_amount": row.get("charge_amount"), "due_trigger": row.get("due_trigger"),
            "due_from": row.get("due_from"),
        } for i, row in enumerate(rows, start=1)]
        return doc, form, milestones

    def set_terms(self, customer_id: str, document_id: str, **terms) -> dict:
        """Links a payment plan, booking date and (optionally) prices and amount
        received to an offline buyer's plot. Returns the plot as plot_view shows
        it. Same ValueError codes as prepare_terms, plus schedule_has_payments."""
        doc, form, milestones = self.prepare_terms(customer_id, document_id, **terms)
        self._persistence.save_terms(doc["id"], form, milestones)
        logger.info("plot_payment_terms_set document_id=%s plan=%s priced=%s by=%s",
                    doc["id"], form.get("payment_plan_code"), bool(milestones), terms.get("set_by"))
        return self.plot_view({**doc, "form_data": form})

    def _unit_for(self, customer_id: str, form: dict, project):
        target = plot_key(_first(form, _PLOT_KEYS))
        for unit in self._persistence.customer_units(customer_id):
            if plot_key(unit["unit_number"]) == target and project_key(unit["project_name"]) == project_key(project):
                return unit
        return None

    # ---- demand letter -------------------------------------------------------
    def demand_letter(self, customer_id: str, document_id: str) -> tuple:
        """(pdf_bytes, filename): the same letter the customer downloads from
        GET /documents/{id}/demand-letter. ValueError plot_not_found /
        payment_details_pending (no prices yet)."""
        self._owned_record(customer_id, document_id)
        if self._documents is None:
            from DivineService.service_document import serviceDocument
            self._documents = serviceDocument()
        try:
            return self._documents.get_demand_letter(document_id, requester_id=customer_id,
                                                     requester_role="customer")
        except PermissionError:
            raise ValueError("plot_not_found")

    # ---- the client's sheet ----------------------------------------------------
    SHEET_ALIASES = {
        "plot": {"plotno", "plotnumber", "plot", "unitno"},
        "plan": {"paymentplan", "plan", "paymentplancode"},
        "name": {"clientname", "customername", "applicant", "applicantname", "name"},
        "phone": {"phone", "contactno", "mobile", "mobileno", "phoneno"},
        "booking_date": {"bookingdate", "dateofbooking", "date"},
        "sale_price": {"saleprice", "basicsaleprice", "bsp"},
        # The plot's total price (Basic Sale Price + any PLC / IFMS / CAM).
        "plot_value": {"plotvalue", "totalvalue", "totalconsideration", "plotprice", "totalprice"},
        "plc": {"plc"},
        "ifms": {"ifms"},
        "cam": {"cam"},
        "amount_received": {"amountreceived", "received", "totalreceived", "receivedtodate"},
    }

    def link_from_sheet(self, sheet_rows: list, project_name: str, apply: bool = False) -> Report:
        """Plan (and with apply=True, save) each sheet row's terms on the buyer's
        plot. A row is matched to its plot by plot number within the project,
        then to the buyer linked to that plot and their record for it; the
        sheet's client name and phone are checked against that record (a
        difference is a warning, never a guess). Every row is validated before
        anything is saved; rows are saved one by one."""
        report = Report(batch_id=new_batch_id("plans"))
        records = [r for r in sheet_records(sheet_rows, self.SHEET_ALIASES) if r.get("plot")]
        if not records:
            report.errors.append("no plot rows found (header 'Plot No' missing?)")
            return report
        units = {plot_key(u["unit_number"]): u for u in self._persistence.project_units(project_name)}
        planned = []
        for rec in records:
            plot = rec["plot"]
            unit = units.get(plot_key(plot))
            if unit is None:
                report.errors.append(f"row {rec['_row']} plot {plot}: not a plot of {project_name}")
                continue
            if not unit.get("booked_by"):
                report.errors.append(f"row {rec['_row']} plot {plot}: no buyer is linked to this plot")
                continue
            # The plot's newest record (the one the customers list reads).
            doc = next((d for d in reversed(self._persistence.booking_docs_for_customers([unit["booked_by"]]))
                        if _Form(d).is_for(unit)), None)
            if doc is None:
                report.errors.append(f"row {rec['_row']} plot {plot}: the buyer has no record for this plot")
                continue
            self._check_identity(report, rec, doc)
            terms = {k: rec.get(k) for k in ("sale_price", "plc", "ifms", "cam", "amount_received")}
            try:
                terms["sale_price"] = self._sale_price_from_plot_value(rec, terms)
            except ValueError as e:
                report.errors.append(f"row {rec['_row']} plot {plot}: {e}")
                continue
            try:
                _, form, milestones = self.prepare_terms(
                    unit["booked_by"], doc["id"], payment_plan=rec.get("plan"),
                    booking_date=rec.get("booking_date"), set_by="sheet", **terms)
            except ValueError as e:
                report.errors.append(f"row {rec['_row']} plot {plot}: {e}")
                continue
            old = doc["form_data"]
            detail = f"plan {form['payment_plan_code']}, booked {form['booking_date']}"
            detail += (f", total {form['total_consideration']}, received {form['amount_received']}"
                       if milestones else ", price pending")
            if old.get("payment_plan_code") or old.get("booking_date"):
                detail += f" (was {old.get('payment_plan_code')} / {old.get('booking_date')})"
            report.add("LINK", unit["unit_number"], detail)
            planned.append((unit["booked_by"], doc["id"], rec, terms))
        if apply and not report.errors:
            for customer_id, document_id, rec, terms in planned:
                try:
                    self.set_terms(customer_id, document_id, payment_plan=rec.get("plan"),
                                   booking_date=rec.get("booking_date"), set_by=f"sheet:{report.batch_id}",
                                   **terms)
                except ValueError as e:
                    # e.g. schedule_has_payments: a payment landed since the dry run.
                    report.errors.append(f"row {rec['_row']} plot {rec['plot']}: not saved ({e})")
        return report

    @staticmethod
    def _sale_price_from_plot_value(rec: dict, terms: dict):
        """A sheet may give the plot's total (Plot Value) instead of its Basic
        Sale Price: BSP = plot value - PLC - IFMS - CAM given in the row. A row
        with both must agree. ValueError invalid_plot_value / plot_value_mismatch."""
        def rupees(value, code):
            # Whole rupees, rounded the way compute_schedule rounds each amount,
            # so the saved total is exactly the (rounded) plot value.
            amount = _money(value, code)
            return None if amount is None else amount.quantize(Decimal(1), rounding=ROUND_HALF_UP)

        value = rupees(rec.get("plot_value"), "invalid_plot_value")
        if value is None:
            return terms.get("sale_price")
        charges = sum((rupees(terms.get(k), f"invalid_{k}_amount") or Decimal(0)) for k in ("plc", "ifms", "cam"))
        derived = value - charges
        if derived <= 0:
            raise ValueError("invalid_plot_value")
        given = rupees(terms.get("sale_price"), "invalid_sale_price")
        if given is not None and abs(given - derived) > _ROUNDING:
            raise ValueError("plot_value_mismatch")
        return str(derived)

    @staticmethod
    def _check_identity(report: Report, rec: dict, doc: dict) -> None:
        form = doc["form_data"]
        sheet_name = split_salutation(rec.get("name"))[1].lower()
        names = {split_salutation(form.get(k))[1].lower() for k in ("applicantName", "coApplicantName",
                                                                    "coApplicant2Name") if form.get(k)}
        if sheet_name and names and sheet_name not in names and not any(
                sheet_name in n or n in sheet_name for n in names):
            report.warnings.append(f"{rec['plot']}: sheet name '{rec.get('name')}' differs from the record "
                                   f"('{form.get('applicantName')}') - check it is the same buyer")
        sheet_phone = clean_phone(rec.get("phone"))
        phones = {clean_phone(form.get(k)) for k in ("phone", "mobile", "alternate_phone")} - {""}
        if sheet_phone and phones and sheet_phone not in phones:
            report.warnings.append(f"{rec['plot']}: sheet phone {sheet_phone} is not on the record "
                                   f"({', '.join(sorted(phones))})")
