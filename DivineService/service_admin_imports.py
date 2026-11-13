"""Admin CSV import: POST /admin/imports/{entity} for customers, brokers, visits
and payment-plans.

The admin panel parses the CSV and sends ``{"rows": [{"row": 2, ...}, ...]}``.
Everything is checked again here:

1. Every row is validated (and the whole file checked against the database:
   unknown ids, duplicate emails, existing plan codes, ...). Every problem
   found is reported, each with the spreadsheet row and the field.
2. Only if no row has a problem are all rows written, in ONE transaction,
   together with the Idempotency-Key record. Any failure writes nothing.

"Blank means unchanged": a field missing from a row (or sent as null / "")
was blank in the file. On update it is left alone; on create it gets the
same default the single-record create uses.
"""
import hashlib
import json
import logging
import re
import secrets
from datetime import date

from passlib.context import CryptContext
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy.exc import IntegrityError

from Divinepersistence.persistence_admin_imports import persistenceAdminImports
from DivineService.service_admin_customers import _split_full_name
from DivineService.service_payment_plans import normalize_definition

logger = logging.getLogger(__name__)

MAX_ROWS = 5000
ENTITIES = ("customers", "brokers", "visits", "payment-plans")
PROJECTS = ("suraksha-enclave", "ops-divine-greens")
VISIT_STATUSES = ("requested", "scheduled", "confirmed", "completed", "follow_up", "no_show",
                  "converted", "cancelled")
# divine_site_visits has a CHECK constraint on this column in production.
PREFERRED_WINDOWS = ("today", "tomorrow", "weekend")
ORIGIN_TYPES = ("CUSTOMER", "CHANNEL_PARTNER")
# first_name / last_name are varchar(150) on both account tables.
NAME_PART_MAX = 150
# A concurrent signup can take one of the new account ids between choosing it
# and commit; the whole file is retried with fresh ids this many times.
WRITE_ATTEMPTS = 3

# Imported accounts get a random 256-bit password nobody ever sees (the user
# sets their own through forgot-password). bcrypt's work factor protects
# guessable human passwords; it adds nothing against a 256-bit random secret,
# and at the default cost 5,000 rows would take minutes. Any bcrypt cost
# verifies normally, and a password reset stores a default-cost hash.
_import_pwd_context = CryptContext(schemes=["bcrypt"], bcrypt__rounds=4)

_email_adapter = TypeAdapter(EmailStr)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class AdminImportError(Exception):
    """A whole-request outcome other than success: carries the HTTP status and
    the ``detail`` to send."""

    def __init__(self, status_code: int, detail):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def _random_password_hash() -> str:
    return _import_pwd_context.hash(secrets.token_urlsafe(32))


def request_hash(entity: str, rows: list) -> str:
    canonical = json.dumps({"entity": entity, "rows": rows}, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _Rows:
    """Collects problems for one file and reads typed values out of row dicts."""

    def __init__(self):
        self.errors = []

    def error(self, row_no: int, field, message: str):
        err = {"row": row_no, "message": message}
        if field:
            err["field"] = field
        self.errors.append(err)

    @staticmethod
    def blank(row: dict, field: str) -> bool:
        value = row.get(field)
        return value is None or (isinstance(value, str) and not value.strip())

    def text(self, row: dict, row_no: int, field: str, label: str, min_len: int = 1, max_len: int = 200):
        """Stripped text, or None when blank. Records an error and returns None
        when it isn't text or its length is out of range."""
        if self.blank(row, field):
            return None
        value = row[field]
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            self.error(row_no, field, f"{label} must be text.")
            return None
        value = str(value).strip()
        if len(value) < min_len or len(value) > max_len:
            if min_len > 1:
                self.error(row_no, field, f"{label} must be {min_len} to {max_len} characters.")
            else:
                self.error(row_no, field, f"{label} can be at most {max_len} characters.")
            return None
        return value

    def email(self, row: dict, row_no: int, field: str, label: str = "Email"):
        value = self.text(row, row_no, field, label, max_len=255)
        if value is None:
            return None
        try:
            _email_adapter.validate_python(value)
        except ValidationError:
            self.error(row_no, field, f"{label} is not a valid email address.")
            return None
        return value.lower()

    def choice(self, row: dict, row_no: int, field: str, label: str, options, lower: bool = False):
        value = self.text(row, row_no, field, label, max_len=100)
        if value is None:
            return None
        normalised = value.lower() if lower else value
        if normalised not in options:
            self.error(row_no, field, f"{label} must be one of: {', '.join(options)}.")
            return None
        return normalised

    def required(self, row_no: int, field: str, label: str):
        self.error(row_no, field, f"{label} is required for a new record.")


def _row_numbers(rows: list, problems: _Rows) -> list:
    """Each row's spreadsheet number: the ``row`` it was sent with, else its
    position (header is row 1). A row that isn't an object is reported."""
    numbers = []
    for index, row in enumerate(rows):
        fallback = index + 2
        if not isinstance(row, dict):
            problems.error(fallback, None, "This row could not be read.")
            numbers.append(fallback)
            continue
        number = row.get("row")
        numbers.append(number if isinstance(number, int) and not isinstance(number, bool) and number > 0
                       else fallback)
    return numbers


def _unknown_fields(row: dict, row_no: int, allowed: set, problems: _Rows) -> None:
    for key in row:
        if key != "row" and key not in allowed:
            problems.error(row_no, key, "This column can't be imported.")


def _repeated_ids(parsed: list, problems: _Rows, label: str) -> None:
    seen = {}
    for item in parsed:
        rid = item.get("id")
        if not rid:
            continue
        if rid in seen:
            problems.error(item["row"], "id", f"This {label} is already updated by row {seen[rid]}.")
        else:
            seen[rid] = item["row"]


def _without_blanks(values: dict) -> dict:
    """Drops null / empty-text cells, so a blank cell gets the single-record
    create's default (sort_order 0, no charge, ...) instead of failing schema
    validation."""
    return {k: v for k, v in values.items()
            if not (v is None or (isinstance(v, str) and not v.strip()))}


class serviceAdminImports:
    def __init__(self, persistence: persistenceAdminImports = None, plan_persistence=None,
                 plan_service=None):
        self._persistence = persistence or persistenceAdminImports()
        if plan_persistence is None:
            from Divinepersistence import persistencePaymentPlan
            plan_persistence = persistencePaymentPlan()
        self._plan_persistence = plan_persistence
        # The plan service whose catalogue cache must be cleared after new
        # plans land (the one behind the public /payment-plans endpoints).
        self._plan_service = plan_service

    # ---- entry point --------------------------------------------------------
    def run(self, entity: str, rows, admin_id: str, idempotency_key: str) -> tuple:
        """Returns (response_dict, replayed). Raises AdminImportError for 409 /
        413 / 422 outcomes."""
        if entity not in ENTITIES:
            raise AdminImportError(404, "not_found")
        if not isinstance(rows, list):
            raise AdminImportError(422, {"code": "import_invalid", "message": "The file has no rows.",
                                         "errors": []})
        if len(rows) > MAX_ROWS:
            raise AdminImportError(413, {"code": "import_too_large",
                                         "message": f"A file can have at most {MAX_ROWS:,} rows."})
        if not rows:
            raise AdminImportError(422, {"code": "import_invalid", "message": "The file has no rows.",
                                         "errors": []})

        digest = request_hash(entity, rows)
        replay = self._replay(admin_id, idempotency_key, digest)
        if replay is not None:
            return replay, True

        prepare = {
            "customers": self._prepare_customers,
            "brokers": self._prepare_brokers,
            "visits": self._prepare_visits,
            "payment-plans": self._prepare_plans,
        }[entity]
        problems = _Rows()
        write, counts = prepare(rows, problems, admin_id)
        if problems.errors:
            errors = sorted(problems.errors, key=lambda e: e["row"])
            bad_rows = len({e["row"] for e in errors})
            noun = "row has" if bad_rows == 1 else "rows have"
            raise AdminImportError(422, {
                "code": "import_invalid",
                "message": f"{bad_rows} {noun} problems. Nothing was imported.",
                "errors": errors,
            })

        response = {"created": counts["created"], "updated": counts["updated"]}
        for attempt in range(1, WRITE_ATTEMPTS + 1):
            try:
                with self._persistence.transaction() as db:
                    write(db)
                    self._persistence.save_request(db, admin_id, idempotency_key, entity, digest,
                                                   json.dumps(response))
                break
            except IntegrityError:
                # Nothing from this attempt was written. Either a concurrent
                # request with the same key committed first (replay it), a
                # signup took one of the new account ids (the next attempt
                # picks fresh ones), or a record changed since validation,
                # e.g. someone else took one of these emails.
                replay = self._replay(admin_id, idempotency_key, digest)
                if replay is not None:
                    return replay, True
                logger.warning("admin_import_write_conflict entity=%s admin_id=%s attempt=%s",
                               entity, admin_id, attempt)
        else:
            raise AdminImportError(409, "import_conflict")
        self._persistence.purge_expired()
        if entity == "payment-plans" and self._plan_service is not None:
            self._plan_service.clear_cache()
        logger.info("admin_import_done entity=%s admin_id=%s created=%s updated=%s",
                    entity, admin_id, response["created"], response["updated"])
        return response, False

    def _replay(self, admin_id: str, idempotency_key: str, digest: str):
        stored = self._persistence.get_request(admin_id, idempotency_key)
        if stored is None:
            return None
        if stored["request_hash"] != digest:
            raise AdminImportError(409, "idempotency_conflict")
        return json.loads(stored["response_json"])

    # ---- customers ------------------------------------------------------------
    def _parse_people(self, rows: list, problems: _Rows, extra_fields: set) -> list:
        allowed = {"id", "full_name", "email", "phone", *extra_fields}
        parsed = []
        for row, row_no in zip(rows, _row_numbers(rows, problems)):
            if not isinstance(row, dict):
                continue
            _unknown_fields(row, row_no, allowed, problems)
            item = {"row": row_no, "id": problems.text(row, row_no, "id", "ID", max_len=36)}
            values = {
                "full_name": problems.text(row, row_no, "full_name", "Full name", min_len=2, max_len=200),
                "email": problems.email(row, row_no, "email"),
                "phone": problems.text(row, row_no, "phone", "Phone", max_len=20),
            }
            if values["full_name"] and any(len(part or "") > NAME_PART_MAX
                                           for part in _split_full_name(values["full_name"])):
                problems.error(row_no, "full_name", "The first name and the surname can each be at most "
                                                    f"{NAME_PART_MAX} characters.")
                values["full_name"] = None
            if "project" in extra_fields:
                values["project"] = problems.choice(row, row_no, "project", "Project", PROJECTS)
            item["sent"] = {k for k in values if not problems.blank(row, k)}
            item.update(values)
            parsed.append(item)
        return parsed

    def _check_people(self, parsed: list, problems: _Rows, existing: dict, using_emails: list,
                      label: str, required: tuple) -> None:
        """Unknown ids, missing create fields, and email uniqueness against the
        database and within the file."""
        for item in parsed:
            if item["id"] and item["id"] not in existing:
                problems.error(item["row"], "id", f"No {label} with this ID.")
            if not item["id"]:
                for field, field_label in required:
                    if field not in item["sent"]:
                        problems.required(item["row"], field, field_label)
        _repeated_ids(parsed, problems, label)

        owners = {}
        for r in using_emails:
            for value in (r.get("email"), r.get("username")):
                if value:
                    owners.setdefault(value.strip().lower(), set()).add(r["id"])
        first_row_for = {}
        for item in parsed:
            email = item["email"]
            if not email:
                continue
            if email in first_row_for:
                problems.error(item["row"], "email", f"This email is also used in row {first_row_for[email]}.")
                continue
            first_row_for[email] = item["row"]
            if owners.get(email, set()) - {item["id"]}:
                problems.error(item["row"], "email", f"A {label} with this email already exists.")

    def _prepare_customers(self, rows: list, problems: _Rows, admin_id: str):
        parsed = self._parse_people(rows, problems, set())
        existing = self._persistence.customers_by_id([p["id"] for p in parsed if p["id"]])
        using = self._persistence.customers_using_emails([p["email"] for p in parsed if p["email"]])
        self._check_people(parsed, problems, existing, using, "customer",
                           (("full_name", "Full name"), ("email", "Email"), ("phone", "Phone")))
        creates, updates = self._people_writes(parsed, existing, problems)
        by = f"admin:{admin_id}"

        def write(db):
            self._persistence.insert_customers(db, creates, created_by=by)
            for row_id, changes in updates:
                self._persistence.update_customer(db, row_id, changes, updated_by=by)

        return write, {"created": len(creates), "updated": len(updates)}

    # ---- brokers --------------------------------------------------------------
    def _prepare_brokers(self, rows: list, problems: _Rows, admin_id: str):
        parsed = self._parse_people(rows, problems, {"project"})
        existing = self._persistence.brokers_by_id([p["id"] for p in parsed if p["id"]])
        using = self._persistence.brokers_using_emails([p["email"] for p in parsed if p["email"]])
        self._check_people(parsed, problems, existing, using, "channel partner",
                           (("full_name", "Full name"), ("email", "Email"), ("phone", "Phone"),
                            ("project", "Project")))
        creates, updates = self._people_writes(parsed, existing, problems)
        by = f"admin:{admin_id}"

        def write(db):
            self._persistence.insert_brokers(db, creates, created_by=by)
            for row_id, changes in updates:
                self._persistence.update_broker(db, row_id, changes, updated_by=by)

        return write, {"created": len(creates), "updated": len(updates)}

    def _people_writes(self, parsed: list, existing: dict, problems: _Rows):
        """Column values for each create / update. Skipped once any problem is
        recorded, since nothing will be written."""
        if problems.errors:
            return [], []
        creates, updates = [], []
        for item in parsed:
            changes = {}
            if item["full_name"]:
                changes["first_name"], changes["last_name"] = _split_full_name(item["full_name"])
            if item["phone"]:
                changes["phone"] = item["phone"]
            if item.get("project"):
                changes["project"] = item["project"]
            if item["email"]:
                changes["email"] = item["email"]
            if not item["id"]:
                # Same shape as the single-record create: username = email,
                # random password set later through forgot-password.
                changes["username"] = item["email"]
                changes["password_hash"] = _random_password_hash()
                creates.append(changes)
                continue
            current = existing[item["id"]]
            if item["email"]:
                old_email = (current.get("email") or "").strip().lower()
                # Accounts created with username = email keep logging in with
                # their (new) email; a separately chosen username is kept.
                if (current.get("username") or "").strip().lower() == old_email:
                    changes["username"] = item["email"]
            updates.append((item["id"], changes))
        return creates, updates

    # ---- visits ---------------------------------------------------------------
    _VISIT_TEXT = (
        ("customer_name", "Customer name", 2, 200),
        ("customer_contact", "Customer contact", 1, 20),
        ("plot_number", "Plot number", 1, 50),
        ("source", "Source", 1, 100),
        ("assigned_to", "Assigned to", 1, 100),
        ("notes", "Notes", 1, 2000),
    )

    def _prepare_visits(self, rows: list, problems: _Rows, admin_id: str):
        allowed = {"id", "origin_type", "broker_id", "customer_email", "project_name", "preferred_window",
                   "visit_date", "visit_time", "status", *(f for f, *_ in self._VISIT_TEXT)}
        parsed = []
        for row, row_no in zip(rows, _row_numbers(rows, problems)):
            if not isinstance(row, dict):
                continue
            _unknown_fields(row, row_no, allowed, problems)
            item = {"row": row_no, "id": problems.text(row, row_no, "id", "ID", max_len=36), "values": {}}
            values = item["values"]
            for field, label, lo, hi in self._VISIT_TEXT:
                values[field] = problems.text(row, row_no, field, label, min_len=lo, max_len=hi)
            values["customer_email"] = problems.email(row, row_no, "customer_email", "Customer email")
            values["project_name"] = problems.choice(row, row_no, "project_name", "Project", PROJECTS)
            values["preferred_window"] = problems.choice(row, row_no, "preferred_window", "Preferred window",
                                                         PREFERRED_WINDOWS, lower=True)
            values["status"] = problems.choice(row, row_no, "status", "Status", VISIT_STATUSES, lower=True)
            item["origin_type"] = problems.choice(row, row_no, "origin_type", "Origin type", ORIGIN_TYPES)
            values["visit_date"] = self._visit_date(row, row_no, problems)
            values["visit_time"] = self._visit_time(row, row_no, problems)
            # The channel partner who owns the visit: it then shows in their
            # visit lists and they can act on it, as with one they booked.
            values["broker_id"] = problems.text(row, row_no, "broker_id", "Channel partner ID", max_len=6)
            item["sent"] = {k for k in values if not problems.blank(row, k)}
            item["origin_sent"] = not problems.blank(row, "origin_type")
            parsed.append(item)

        existing = self._persistence.visits_by_id([p["id"] for p in parsed if p["id"]])
        brokers = self._persistence.brokers_by_id([p["values"]["broker_id"] for p in parsed
                                                   if p["values"]["broker_id"]])
        for item in parsed:
            if item["values"]["broker_id"] and item["values"]["broker_id"] not in brokers:
                problems.error(item["row"], "broker_id", "No channel partner with this ID.")
            if item["id"]:
                stored = existing.get(item["id"])
                if stored is None:
                    problems.error(item["row"], "id", "No site visit with this ID.")
                elif item["origin_type"] and item["origin_type"] != stored["origin_type"]:
                    problems.error(item["row"], "origin_type", "Origin type can't be changed on an existing visit.")
                continue
            if not item["origin_sent"]:
                problems.required(item["row"], "origin_type", "Origin type")
            if "customer_name" not in item["sent"]:
                problems.required(item["row"], "customer_name", "Customer name")
            if "visit_date" not in item["sent"]:
                # divine_site_visits.visit_date is NOT NULL.
                problems.required(item["row"], "visit_date", "Visit date")
        _repeated_ids(parsed, problems, "site visit")

        creates, updates = [], []
        if not problems.errors:
            # Link a visit to the website account with its email, as a signed-in
            # visit request does (customer_id is what GET /visits/mine reads).
            accounts = self._persistence.customer_ids_by_email(
                [p["values"]["customer_email"] for p in parsed if p["values"]["customer_email"]])
            for item in parsed:
                values = {k: v for k, v in item["values"].items() if v is not None}
                account = accounts.get(values.get("customer_email"))
                if item["id"]:
                    # Never re-points a visit already linked to an account.
                    stored = existing[item["id"]]
                    if account and stored["origin_type"] == "CUSTOMER" and not stored.get("customer_id"):
                        values["customer_id"] = account
                    updates.append((item["id"], values))
                    continue
                if account and item["origin_type"] == "CUSTOMER":
                    values["customer_id"] = account
                values["origin_type"] = item["origin_type"]
                values.setdefault("status", "scheduled")
                if item["origin_type"] == "CUSTOMER":
                    # Matches the website request flow's default source.
                    values.setdefault("source", "Website")
                creates.append(values)

        def write(db):
            self._persistence.insert_visits(db, creates)
            for row_id, changes in updates:
                self._persistence.update_visit(db, row_id, changes)

        return write, {"created": len(creates), "updated": len(updates)}

    @staticmethod
    def _visit_date(row: dict, row_no: int, problems: _Rows):
        if problems.blank(row, "visit_date"):
            return None
        value = row["visit_date"]
        try:
            if not isinstance(value, str) or not _DATE_RE.match(value.strip()):
                raise ValueError
            return date.fromisoformat(value.strip())
        except ValueError:
            problems.error(row_no, "visit_date", "Visit date must be a real date in YYYY-MM-DD form.")
            return None

    @staticmethod
    def _visit_time(row: dict, row_no: int, problems: _Rows):
        if problems.blank(row, "visit_time"):
            return None
        value = row["visit_time"]
        if not isinstance(value, str) or not _TIME_RE.match(value.strip()):
            problems.error(row_no, "visit_time", "Visit time must be HH:MM in 24-hour form, e.g. 14:30.")
            return None
        return value.strip()

    # ---- payment plans --------------------------------------------------------
    _PLAN_ERRORS = {
        "invalid_plan_code": ("code", "Plan code must be 2 to 80 characters: letters, digits and / _ . - "
                                      "only, starting with a letter or digit."),
        "invalid_short_code": ("short_code", "Short code must be 1 to 20 letters, digits or hyphens."),
        "invalid_plan_name": ("name", "Plan name must be 2 to 150 characters."),
        "invalid_description": ("description", "Description can be at most 1000 characters."),
        "invalid_project_id": ("project_id", "Project must be a project id such as suraksha-enclave."),
        "invalid_sort_order": ("sort_order", "Sort order must be a whole number from 0 to 1000."),
        "invalid_instalments": ("instalments", "One or more instalment lines are invalid. Check the label, "
                                               "percent, due trigger, days and charge."),
        "invalid_payment_plan": ("instalments", "Instalment percents must add up to 100, and each charge "
                                                "(PLC, IFMS, CAM) can sit on only one line."),
        "sale_price_too_small": ("instalments", "An instalment works out to zero on the example price. "
                                                "Check the percents."),
        "total_amount_too_large": ("instalments", "The instalments don't work out on the example price. "
                                                  "Check the percents."),
    }

    def _prepare_plans(self, rows: list, problems: _Rows, admin_id: str):
        # Lazy: the single-plan endpoint's own request model, so the import
        # accepts exactly what POST /admin/payment-plans accepts.
        from DivineAPI.payment_plan_api import PlanCreateDTO, PlanInstalmentInDTO

        plan_fields = set(PlanCreateDTO.model_fields)
        instalment_fields = set(PlanInstalmentInDTO.model_fields)
        known = self._plan_persistence.list_plan_codes()
        existing_codes = {p["code"] for p in known}
        active_short = {str(p.get("short_code") or "").lower() for p in known if p.get("is_active")}

        plans = []
        seen_codes, seen_short = {}, {}
        for row, row_no in zip(rows, _row_numbers(rows, problems)):
            if not isinstance(row, dict):
                continue
            errors_before = len(problems.errors)
            _unknown_fields(row, row_no, plan_fields, problems)
            body = _without_blanks({k: v for k, v in row.items() if k != "row"})
            if isinstance(body.get("instalments"), list):
                lines = []
                for n, line in enumerate(body["instalments"], start=1):
                    if isinstance(line, dict):
                        for key in line:
                            if key not in instalment_fields:
                                problems.error(row_no, "instalments",
                                               f"Instalment {n}: the column '{key}' can't be imported.")
                        line = _without_blanks(line)
                    lines.append(line)
                body["instalments"] = lines
            try:
                dto = PlanCreateDTO.model_validate(body)
            except ValidationError as e:
                for err in e.errors():
                    loc = err.get("loc") or ()
                    field = str(loc[0]) if loc else None
                    message = ("This field is required." if err.get("type") == "missing"
                               else err.get("msg", "Invalid value."))
                    if field == "instalments" and len(loc) >= 3:
                        message = f"Instalment {int(loc[1]) + 1}, {loc[2]}: {message}"
                    elif field:
                        message = f"{field}: {message}"
                    problems.error(row_no, field, message)
                continue
            if len(problems.errors) > errors_before:
                continue
            try:
                plan = normalize_definition(dto.model_dump())
            except ValueError as e:
                field, message = self._PLAN_ERRORS.get(str(e), (None, "This payment plan is invalid."))
                problems.error(row_no, field, message)
                continue

            if plan["code"] in existing_codes:
                problems.error(row_no, "code", "A payment plan with this code already exists.")
            elif plan["code"] in seen_codes:
                problems.error(row_no, "code", f"This plan code is also used in row {seen_codes[plan['code']]}.")
            else:
                seen_codes[plan["code"]] = row_no
            short = plan["short_code"].lower()
            if short in active_short:
                problems.error(row_no, "short_code", "Another active plan already uses this short code.")
            elif short in seen_short:
                problems.error(row_no, "short_code", f"This short code is also used in row {seen_short[short]}.")
            else:
                seen_short[short] = row_no
            plan.update({"version": 1, "is_active": True})
            plans.append(plan)

        def write(db):
            for plan in plans:
                self._plan_persistence.write_plan(db, plan)

        return write, {"created": len(plans), "updated": 0}
