"""Prod data migration helpers: load the client's inventory sheet and their
offline buyers into the app, and invite those buyers to log in.

Three steps, each a thin CLI in scripts/ on top of this module:

  A. InventorySheetSync     scripts/sync_inventory_from_sheet.py
     Plot statuses / dimensions from the client inventory sheet.
     Sold -> 'sold' (sale_source 'offline'), Unsold -> 'available',
     Hold -> 'held'. Never touches a plot with app activity
     (booked / pending_kyc_review / reserved). Project plots missing from the
     sheet -> 'held'. Every change is recorded in
     divine_inventory_status_history under one batch id (rollback).

  B. OfflineBuyerImport     scripts/import_offline_buyers.py
     One customer account per email (shared email = same owner), and one
     'imported' booking-application record per plot carrying applicant /
     co-applicants / addresses. No payments, bookings or milestones (price
     data comes later), so no demand letter and no reminder emails yet.

  C. OfflineBuyerInvites    scripts/send_offline_buyer_invites.py
     Emails each new account a temporary password (must be changed on first
     login); buyers who already had an account get a "plot added" email.

Every step is a dry run unless apply=True. Pure parsing helpers are
module-level so tests can exercise them without a database.
"""
import csv
import json
import logging
import re
import secrets
import string
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import text

logger = logging.getLogger(__name__)

PROTECTED_STATUSES = ("booked", "pending_kyc_review", "reserved")
SHEET_STATUS_MAP = {"sold": "sold", "unsold": "available", "available": "available",
                    "hold": "held", "held": "held", "on hold": "held"}
IMPORTED_DOC_STATUS = "imported"
IMPORT_SOURCE = "offline_import"
TEMP_PASSWORD_DAYS = 7

# --------------------------------------------------------------------------
# Sheet reading (stdlib only: openpyxl isn't a dependency)
# --------------------------------------------------------------------------
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def _col_index(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_sheet_rows(path: str) -> list:
    """First worksheet of an .xlsx (or a .csv) as a list of lists of strings,
    including the header row. Empty rows are dropped."""
    if path.lower().endswith(".csv"):
        with open(path, encoding="utf-8-sig", newline="") as fh:
            return [[(c or "").strip() for c in row] for row in csv.reader(fh) if any((c or "").strip() for c in row)]
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iter("{%s}t" % _NS["m"])))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
        first = wb.find("m:sheets", _NS)[0]
        target = rels[first.get("{%s}id" % _NS["r"])].lstrip("/")
        sheet_path = target if target.startswith("xl/") else "xl/" + target
        out = []
        for row in ET.fromstring(z.read(sheet_path)).findall(".//m:sheetData/m:row", _NS):
            cells = {}
            for c in row.findall("m:c", _NS):
                v = c.find("m:v", _NS)
                val = v.text if v is not None else None
                if c.get("t") == "s" and val is not None:
                    val = shared[int(val)]
                elif c.get("t") == "inlineStr":
                    val = "".join(t.text or "" for t in c.iter("{%s}t" % _NS["m"]))
                if val is not None and str(val).strip():
                    cells[_col_index(c.get("r"))] = str(val).strip()
            if cells:
                out.append([cells.get(i, "") for i in range(max(cells) + 1)])
        return out


def _header_key(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def sheet_records(rows: list, aliases: dict) -> list:
    """Header row -> dicts keyed by our field names. ``aliases`` maps a field
    name to the accepted normalised header spellings. Each record also gets
    ``_row`` (the 1-based sheet row number)."""
    if not rows:
        return []
    header = [_header_key(h) for h in rows[0]]
    index = {}
    for name, spellings in aliases.items():
        for i, h in enumerate(header):
            if h in spellings:
                index[name] = i
                break
    out = []
    for n, row in enumerate(rows[1:], start=2):
        rec = {name: (row[i].strip() if i < len(row) else "") for name, i in index.items()}
        rec["_row"] = n
        out.append(rec)
    return out


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

def plot_key(value) -> str:
    """Matching key for plot numbers: 'B1-B', 'b1b', 'B 1-B' -> 'B1B'."""
    return re.sub(r"[\s\-_/.]", "", str(value or "")).upper()


def clean_plot_number(value) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def block_from_plot(value) -> str:
    m = re.match(r"^([A-Za-z]+)", str(value or "").strip())
    return m.group(1).upper() if m else None


def to_decimal(value):
    text_value = str(value or "").replace(",", "").strip()
    if not text_value:
        return None
    try:
        d = Decimal(text_value)
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def clean_phone(value) -> str:
    """'9.996031373E9' / 9996031373.0 / '+91 99960 31373' -> '9996031373'.
    Returns '' when nothing usable."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if re.fullmatch(r"[\d.]+[eE][+-]?\d+", raw) or re.fullmatch(r"\d+\.0+", raw):
        try:
            raw = str(int(Decimal(raw)))
        except InvalidOperation:
            pass
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits


_SALUTATION_RE = re.compile(r"^\s*(mrs|mr|ms|miss|smt|shri|sh|dr)\b\.?\s*", re.IGNORECASE)
_MALE = {"mr", "shri", "sh"}
_FEMALE = {"mrs", "ms", "miss", "smt"}


def split_salutation(name):
    """'Mrs. Aparna Yadav' -> ('Female', 'Aparna Yadav'); 'Mrs.Poonam' ->
    ('Female', 'Poonam'); 'Deepak' -> (None, 'Deepak'). Dr. keeps no gender."""
    raw = re.sub(r"\s+", " ", str(name or "")).strip()
    m = _SALUTATION_RE.match(raw)
    if not m:
        return None, raw
    word = m.group(1).lower()
    gender = "Male" if word in _MALE else "Female" if word in _FEMALE else None
    return gender, raw[m.end():].strip()


def strip_residence_prefix(address) -> str:
    """'R/o-H.No. 5, Ganaur' / 'R/o. Ahulana' / 'R/o Sonipat' -> address without
    the prefix (the demand letter adds 'R/o-' itself)."""
    text_value = re.sub(r"\s+", " ", str(address or "")).strip()
    return re.sub(r"^r\s*/\s*o\s*[-.:,]?\s*", "", text_value, flags=re.IGNORECASE).strip()


def clean_email(value) -> str:
    e = str(value or "").strip().lower()
    return e if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e) else ""


_KNOWN_DOMAINS = ("gmail.com", "yahoo.com", "yahoo.co.in", "yahoo.in", "hotmail.com", "outlook.com",
                  "rediffmail.com", "icloud.com", "live.in", "live.com")


def suspicious_email_domain(email: str):
    """The well-known domain an email's domain is probably a typo of
    ('gamil.com' -> 'gmail.com'), else None. Never auto-corrects - the email
    is the login, so a wrong guess is worse than asking the client."""
    domain = (email or "").rsplit("@", 1)[-1].lower()
    if not domain or domain in _KNOWN_DOMAINS:
        return None

    def distance(a, b):
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            cur = [i]
            for j, cb in enumerate(b, 1):
                cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
            prev = cur
        return prev[-1]

    best = min(_KNOWN_DOMAINS, key=lambda d: distance(domain, d))
    return best if distance(domain, best) <= 2 else None


def correct_gmail_typo(email: str):
    """(email, note): a domain that is a near-miss of gmail.com ('gamil.com',
    'gmaiul.com') is corrected to gmail.com - confirmed by the client for the
    offline-buyer sheet. Other domains are left alone (only flagged)."""
    if email and suspicious_email_domain(email) == "gmail.com":
        local, _, domain = email.rpartition("@")
        fixed = f"{local}@gmail.com"
        return fixed, f"'{email}' corrected to '{fixed}'"
    return email, None


def project_slug(project_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (project_name or "").lower()).strip("-")


def generate_temp_password(length: int = 10) -> str:
    """Readable temporary password: upper + lower + digits, no look-alikes
    (0/O, 1/l/I). Always contains at least one of each class."""
    upper = "".join(c for c in string.ascii_uppercase if c not in "OI")
    lower = "".join(c for c in string.ascii_lowercase if c not in "lo")
    digits = "23456789"
    pool = upper + lower + digits
    while True:
        pw = "".join(secrets.choice(pool) for _ in range(length))
        if any(c in upper for c in pw) and any(c in lower for c in pw) and any(c in digits for c in pw):
            return pw


def new_batch_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _now():
    return datetime.now(timezone.utc)


def _json_default(v):
    if isinstance(v, (datetime,)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    return str(v)


def _dec_equal(a, b, places: int) -> bool:
    if a is None or b is None:
        return a is None and b is None
    q = Decimal(1).scaleb(-places)
    return Decimal(str(a)).quantize(q) == Decimal(str(b)).quantize(q)


@dataclass
class Report:
    batch_id: str
    actions: list = field(default_factory=list)    # (kind, plot, detail)
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    counts_before: dict = field(default_factory=dict)
    counts_after: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    def add(self, kind, plot, detail=""):
        self.actions.append((kind, plot, detail))

    @property
    def change_count(self) -> int:
        return sum(1 for k, _, _ in self.actions if k not in ("CONFLICT", "KEEP", "SKIP", "REUSE"))

    def render(self) -> str:
        lines = [f"batch: {self.batch_id}"]
        if self.counts_before:
            lines.append(f"status counts before: {dict(sorted(self.counts_before.items()))}")
        if self.counts_after:
            lines.append(f"status counts after:  {dict(sorted(self.counts_after.items()))}")
        for k, v in self.extra.items():
            lines.append(f"{k}: {v}")
        kinds = {}
        for k, _, _ in self.actions:
            kinds[k] = kinds.get(k, 0) + 1
        lines.append(f"actions: {dict(sorted(kinds.items()))}  (changes: {self.change_count})")
        for k, plot, detail in self.actions:
            lines.append(f"  {k:<10} {plot:<8} {detail}")
        for w in self.warnings:
            lines.append(f"  WARN       {w}")
        for e in self.errors:
            lines.append(f"  ERROR      {e}")
        return "\n".join(lines)


def _history(conn, batch_id, source, inventory_id, project_name, unit_number, old_status, new_status,
             old_unit_number=None, new_unit_number=None, old_values=None, note=None):
    conn.execute(text(
        "INSERT INTO divine_inventory_status_history(id, inventory_id, project_name, unit_number, "
        "old_status, new_status, old_unit_number, new_unit_number, old_values, source, batch_id, note, created_date) "
        "VALUES (:id, :inventory_id, :project_name, :unit_number, :old_status, :new_status, :old_unit_number, "
        ":new_unit_number, :old_values, :source, :batch_id, :note, :created_date)"
    ), {
        "id": str(uuid.uuid4()), "inventory_id": inventory_id, "project_name": project_name,
        "unit_number": unit_number, "old_status": old_status, "new_status": new_status,
        "old_unit_number": old_unit_number, "new_unit_number": new_unit_number,
        "old_values": json.dumps(old_values, default=_json_default) if old_values is not None else None,
        "source": (source or "")[:255], "batch_id": batch_id, "note": (note or "")[:255] or None,
        "created_date": _now(),
    })


_RESTORABLE = ("status", "unit_number", "width_mtr", "length_mtr", "area_sqmt", "area_sqyd",
               "sale_source", "status_note", "status_updated_at", "booked_at", "booked_payment_id",
               "booked_by", "reserved_by_broker_id", "reserved_at", "reserved_until")


def _snapshot(row) -> dict:
    return {k: row.get(k) for k in _RESTORABLE}


def _bindable(v):
    return float(v) if isinstance(v, Decimal) else v


# ==========================================================================
# A. Inventory sheet sync
# ==========================================================================
INVENTORY_ALIASES = {
    "plot": {"plotno", "plotnumber", "plot", "unitno", "unitnumber"},
    "width": {"widthmtr", "width"},
    "length": {"lengthmtr", "length"},
    "area_sqmt": {"areainsqmtrs", "areainsqmtr", "areasqmt", "areainsqm"},
    "area_sqyd": {"areainsqyard", "areainsqyd", "areasqyd"},
    "status": {"status"},
    "project": {"project", "projectname"},
}


class InventorySheetSync:
    def __init__(self, engine=None):
        if engine is None:
            from Divinepersistence.persistence_db import engine as default_engine
            engine = default_engine
        self._engine = engine

    def _load_db(self, conn, project_name):
        rows = conn.execute(text(
            "SELECT * FROM divine_project_inventory WHERE project_name = :p"
        ), {"p": project_name}).mappings().all()
        return [dict(r) for r in rows]

    def run(self, sheet_rows: list, project_name: str, city: str, locality: str = None,
            overrides: dict = None, apply: bool = False, source: str = "client_sheet",
            batch_id: str = None) -> Report:
        """Plan (and with apply=True, perform in one transaction) the sync."""
        report = Report(batch_id=batch_id or new_batch_id("inv"))
        overrides = {plot_key(k): v for k, v in (overrides or {}).items()}
        records = sheet_records(sheet_rows, INVENTORY_ALIASES)
        records = [r for r in records if r.get("plot")]
        if not records:
            report.errors.append("no plot rows found in the sheet (header 'Plot No.' missing?)")
            return report

        seen = {}
        for r in records:
            k = plot_key(r["plot"])
            if k in seen:
                report.errors.append(f"sheet lists plot {r['plot']} twice (rows {seen[k]} and {r['_row']})")
            seen[k] = r["_row"]
            sheet_project = (r.get("project") or "").strip()
            if sheet_project and sheet_project.lower() != project_name.lower():
                report.errors.append(f"row {r['_row']}: project '{sheet_project}' is not '{project_name}'")
        for k, v in overrides.items():
            if v not in ("sold", "available", "held"):
                report.errors.append(f"override {k}={v}: status must be sold/available/held")
            if k not in seen:
                report.errors.append(f"override {k}: plot not in the sheet")

        with self._engine.begin() as conn:
            db_rows = self._load_db(conn, project_name)
            by_key = {}
            for row in db_rows:
                k = plot_key(row["unit_number"])
                if k in by_key:
                    report.errors.append(
                        f"DB has two rows for plot {row['unit_number']} / {by_key[k]['unit_number']}")
                by_key[k] = row
            report.counts_before = self._counts(db_rows)
            if report.errors:
                return report

            booking_refs = self._booking_refs(conn, [r["id"] for r in db_rows])
            planned = []   # callables run only when applying
            matched = set()
            for r in records:
                k = plot_key(r["plot"])
                sheet_plot = clean_plot_number(r["plot"])
                raw_status = (r.get("status") or "").strip()
                desired = SHEET_STATUS_MAP.get(raw_status.lower())
                note = f"Client sheet: {raw_status or '(blank)'}"
                if k in overrides:
                    desired = overrides[k]
                    note = f"Override {desired} (sheet said {raw_status or 'blank'})"
                    report.warnings.append(f"{sheet_plot}: sheet status '{raw_status}' overridden to '{desired}'")
                if desired is None:
                    report.errors.append(f"row {r['_row']} plot {sheet_plot}: unknown status '{raw_status}'")
                    continue
                if raw_status and raw_status != raw_status.capitalize() and k not in overrides:
                    report.warnings.append(f"{sheet_plot}: status written as '{raw_status}'")
                dims = {
                    "width_mtr": to_decimal(r.get("width")), "length_mtr": to_decimal(r.get("length")),
                    "area_sqmt": to_decimal(r.get("area_sqmt")), "area_sqyd": to_decimal(r.get("area_sqyd")),
                }
                missing = [n for n in ("width_mtr", "length_mtr") if dims[n] is None]
                if missing:
                    report.warnings.append(f"{sheet_plot}: sheet has no {', '.join(missing)} (existing value kept)")
                self._check_dimensions(report, sheet_plot, dims)
                row = by_key.get(k)
                if row is None:
                    if dims["area_sqmt"] is None or dims["area_sqyd"] is None:
                        report.errors.append(f"{sheet_plot}: new plot without area - cannot insert")
                        continue
                    report.add("INSERT", sheet_plot, f"status={desired}")
                    planned.append(("insert", sheet_plot, desired, note, dims))
                    continue
                matched.add(row["id"])
                planned.extend(self._plan_existing(report, row, sheet_plot, desired, note, dims, booking_refs))

            for row in db_rows:
                if row["id"] in matched:
                    continue
                plot = row["unit_number"]
                if row["status"] in PROTECTED_STATUSES:
                    report.add("CONFLICT", plot, f"not in sheet but '{row['status']}' in app - kept")
                    continue
                if row["status"] == "held" and (row.get("status_note") or "").startswith("Not in client sheet"):
                    continue
                report.add("EXTRA", plot, f"{row['status']} -> held (not in client sheet)")
                planned.append(("status", row, "held", "Not in client sheet", None, False))

            if apply and not report.errors:
                for step in planned:
                    self._apply_step(conn, report, step, project_name, city, locality, source)
                report.counts_after = self._counts(self._load_db(conn, project_name))
            elif not apply:
                report.counts_after = {}
        return report

    def _plan_existing(self, report, row, sheet_plot, desired, note, dims, booking_refs):
        steps = []
        plot = row["unit_number"]
        if plot != sheet_plot:
            report.add("RENAME", plot, f"-> {sheet_plot}")
            steps.append(("rename", row, sheet_plot))
        current = row["status"]
        if current in PROTECTED_STATUSES:
            if desired == "sold":
                if current in ("booked", "pending_kyc_review") and row.get("sale_source") != "app":
                    report.add("SOURCE", sheet_plot, f"{current} in app -> sale_source=app")
                    steps.append(("source", row, "app"))
                else:
                    report.add("KEEP", sheet_plot, f"{current} in app, sheet says Sold")
            else:
                report.add("CONFLICT", sheet_plot,
                           f"sheet says {desired} but plot is '{current}' in the app - kept, resolve with client")
        else:
            want_source = "offline" if desired == "sold" else None
            if current != desired or (row.get("sale_source") or None) != want_source:
                clear = desired == "available" and row["id"] not in booking_refs
                report.add("STATUS", sheet_plot, f"{current} -> {desired}")
                steps.append(("status", row, desired, note, want_source, clear))
        changed = {}
        for name, places in (("width_mtr", 3), ("length_mtr", 3), ("area_sqmt", 3), ("area_sqyd", 2)):
            new = dims.get(name)
            if new is not None and not _dec_equal(row.get(name), new, places):
                changed[name] = new
        if changed:
            report.add("DIMENSIONS", sheet_plot, ", ".join(f"{k}: {row.get(k)} -> {v}" for k, v in changed.items()))
            steps.append(("dims", row, changed))
        derived = block_from_plot(sheet_plot)
        if row.get("block") and derived and row["block"].upper() != derived:
            report.warnings.append(f"{sheet_plot}: DB block '{row['block']}' differs from prefix '{derived}' (not changed)")
        return steps

    @staticmethod
    def _check_dimensions(report, plot, dims):
        w, length, sqm, sqyd = (dims[k] for k in ("width_mtr", "length_mtr", "area_sqmt", "area_sqyd"))
        if w and length and sqm and abs(w * length - sqm) > Decimal("0.5"):
            report.warnings.append(f"{plot}: width x length = {(w * length):.3f} but area is {sqm} sq m")
        if sqm and sqyd and abs(sqm * Decimal("1.19599") - sqyd) > Decimal("0.05"):
            report.warnings.append(f"{plot}: {sqm} sq m is {(sqm * Decimal('1.19599')):.2f} sq yd, sheet says {sqyd}")

    @staticmethod
    def _counts(rows):
        counts = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        return counts

    @staticmethod
    def _booking_refs(conn, inventory_ids):
        """Inventory ids an active divine_bookings row points at (their booked_*
        columns must not be cleared)."""
        if not inventory_ids:
            return set()
        try:
            rows = conn.execute(text(
                "SELECT inventory_id FROM divine_bookings WHERE status IN ('pending_kyc_review', 'booked')"
            )).all()
            return {r[0] for r in rows}
        except Exception:
            return set(inventory_ids)   # can't tell -> never clear

    def _apply_step(self, conn, report, step, project_name, city, locality, source):
        kind = step[0]
        now = _now()
        if kind == "insert":
            _, plot, status, note, dims = step
            new_id = str(uuid.uuid4())
            conn.execute(text(
                "INSERT INTO divine_project_inventory(id, project_name, city, locality, block, unit_number, unit_type, "
                "width_mtr, length_mtr, area_sqmt, area_sqyd, status, sale_source, status_note, status_updated_at, "
                "created_date, last_updated_date) VALUES (:id, :project_name, :city, :locality, :block, :unit_number, "
                "'plot', :w, :l, :sqm, :sqyd, :status, :sale_source, :note, :now, :now, :now)"
            ), {"id": new_id, "project_name": project_name, "city": city, "locality": locality,
                "block": block_from_plot(plot), "unit_number": plot,
                "w": _bindable(dims["width_mtr"]), "l": _bindable(dims["length_mtr"]),
                "sqm": _bindable(dims["area_sqmt"]), "sqyd": _bindable(dims["area_sqyd"]),
                "status": status, "sale_source": "offline" if status == "sold" else None,
                "note": note, "now": now})
            _history(conn, report.batch_id, source, new_id, project_name, plot, None, status,
                     new_unit_number=plot, note="insert")
            return
        row = step[1]
        snap = _snapshot(row)
        if kind == "rename":
            new_plot = step[2]
            conn.execute(text("UPDATE divine_project_inventory SET unit_number = :u, last_updated_date = :now "
                              "WHERE id = :id"), {"u": new_plot, "now": now, "id": row["id"]})
            conn.execute(text("UPDATE divine_bookings SET unit_number = :u WHERE inventory_id = :id"),
                         {"u": new_plot, "id": row["id"]})
            _history(conn, report.batch_id, source, row["id"], project_name, new_plot, row["status"], row["status"],
                     old_unit_number=row["unit_number"], new_unit_number=new_plot, old_values=snap, note="rename")
            row["unit_number"] = new_plot
        elif kind == "source":
            conn.execute(text("UPDATE divine_project_inventory SET sale_source = :s, last_updated_date = :now "
                              "WHERE id = :id"), {"s": step[2], "now": now, "id": row["id"]})
            _history(conn, report.batch_id, source, row["id"], project_name, row["unit_number"], row["status"],
                     row["status"], old_values=snap, note="sale_source")
        elif kind == "status":
            _, _, status, note, sale_source, clear = step
            sql = ("UPDATE divine_project_inventory SET status = :status, sale_source = :sale_source, "
                   "status_note = :note, status_updated_at = :now, last_updated_date = :now, "
                   "reserved_by_broker_id = NULL, reserved_at = NULL, reserved_until = NULL")
            if clear:
                sql += ", booked_at = NULL, booked_payment_id = NULL, booked_by = NULL"
            # Guard: never overwrite a plot that turned app-active since planning.
            sql += " WHERE id = :id AND status NOT IN ('booked', 'pending_kyc_review', 'reserved')"
            res = conn.execute(text(sql), {"status": status, "sale_source": sale_source, "note": note,
                                           "now": now, "id": row["id"]})
            if res.rowcount:
                _history(conn, report.batch_id, source, row["id"], project_name, row["unit_number"],
                         row["status"], status, old_values=snap, note=note)
            else:
                report.warnings.append(f"{row['unit_number']}: became app-active during the run - left unchanged")
        elif kind == "dims":
            changed = step[2]
            sets = ", ".join(f"{k} = :{k}" for k in changed)
            params = {k: _bindable(v) for k, v in changed.items()}
            params.update({"id": row["id"], "now": now})
            conn.execute(text(f"UPDATE divine_project_inventory SET {sets}, last_updated_date = :now WHERE id = :id"),
                         params)
            _history(conn, report.batch_id, source, row["id"], project_name, row["unit_number"], row["status"],
                     row["status"], old_values=snap, note="dimensions")

    def rollback(self, batch_id: str) -> Report:
        """Undo one sync batch: newest change first, restoring each row's
        snapshot; plots the batch inserted are deleted (unless app-active)."""
        report = Report(batch_id=batch_id)
        with self._engine.begin() as conn:
            hist = conn.execute(text(
                "SELECT * FROM divine_inventory_status_history WHERE batch_id = :b ORDER BY created_date DESC, id DESC"
            ), {"b": batch_id}).mappings().all()
            if not hist:
                report.errors.append("no history rows for this batch")
                return report
            for h in hist:
                if h["note"] == "insert":
                    res = conn.execute(text(
                        "DELETE FROM divine_project_inventory WHERE id = :id "
                        "AND status NOT IN ('booked', 'pending_kyc_review', 'reserved')"), {"id": h["inventory_id"]})
                    report.add("DELETE" if res.rowcount else "KEPT", h["unit_number"] or "",
                               "" if res.rowcount else "app-active since import")
                    continue
                old = json.loads(h["old_values"] or "{}")
                if not old:
                    continue
                cols = [k for k in _RESTORABLE if k in old]
                sets = ", ".join(f"{k} = :{k}" for k in cols)
                # Same guard as the forward step: a plot booked / held for KYC /
                # reserved through the app after this batch is never rolled back
                # (that would erase a real booking).
                res = conn.execute(text(
                    f"UPDATE divine_project_inventory SET {sets} WHERE id = :id "
                    "AND (status NOT IN ('booked', 'pending_kyc_review', 'reserved') OR status = :old_status)"),
                    {**{k: old[k] for k in cols}, "id": h["inventory_id"], "old_status": old.get("status")})
                if not res.rowcount:
                    report.add("KEPT", h["unit_number"] or "", "app-active since the sync - not rolled back")
                    report.warnings.append(f"{h['unit_number']}: booked/reserved in the app after this batch - left as is")
                    continue
                if h["note"] == "rename":
                    conn.execute(text("UPDATE divine_bookings SET unit_number = :u WHERE inventory_id = :id"),
                                 {"u": old.get("unit_number"), "id": h["inventory_id"]})
                report.add("RESTORE", h["unit_number"] or "", h["note"] or "")
            conn.execute(text("DELETE FROM divine_inventory_status_history WHERE batch_id = :b"), {"b": batch_id})
        return report


# ==========================================================================
# B. Offline buyers -> accounts + plot records
# ==========================================================================
BUYER_ALIASES = {
    "plot": {"plotno", "plotnumber", "plot", "unitno"},
    "applicant": {"applicant", "applicantname", "name"},
    "co1": {"1coapplicant", "coapplicant", "coapplicant1", "1stcoapplicant"},
    "co2": {"2coapplicant", "coapplicant2", "2ndcoapplicant"},
    "phone": {"contactno", "contactnumber", "mobile", "phone", "mobileno"},
    "alt_phone": {"alternativenumber", "alternatenumber", "alternatephone", "altnumber"},
    "email": {"emailid", "email", "emailaddress"},
    "co_email": {"coapplicantemail", "coapplicantemailid"},
    "address": {"adders", "address", "applicantaddress", "residentialaddress"},
    "co_address": {"address1", "coapplicantaddress"},
    "plot_address": {"plotaddress"},
}


@dataclass
class Buyer:
    row: int
    plot_raw: str
    applicant: str
    gender: str
    co1: str
    co1_gender: str
    co2: str
    phone: str
    alt_phone: str
    email: str
    co_email: str
    address: str
    co_address: str
    plot_address: str


def parse_buyers(sheet_rows: list):
    """(buyers, warnings). Pure - no DB."""
    warnings = []
    buyers = []
    for r in sheet_records(sheet_rows, BUYER_ALIASES):
        if not r.get("plot"):
            if any(v for k, v in r.items() if k != "_row"):
                warnings.append(f"row {r['_row']}: no plot number - skipped")
            continue
        gender, applicant = split_salutation(r.get("applicant"))
        co1_gender, co1 = split_salutation(r.get("co1"))
        _, co2 = split_salutation(r.get("co2"))
        email_raw = (r.get("email") or "").strip()
        email = clean_email(email_raw)
        if email_raw and not email:
            warnings.append(f"row {r['_row']} plot {r['plot']}: invalid email '{email_raw}' ignored")
        email, fix_note = correct_gmail_typo(email)
        if fix_note:
            warnings.append(f"row {r['_row']} plot {r['plot']}: email {fix_note}")
        co_email, co_fix_note = correct_gmail_typo(clean_email(r.get("co_email")))
        if co_fix_note:
            warnings.append(f"row {r['_row']} plot {r['plot']}: co-applicant email {co_fix_note}")
        for label, value in (("email", email), ("co-applicant email", co_email)):
            guess = suspicious_email_domain(value)
            if guess:
                warnings.append(f"row {r['_row']} plot {r['plot']}: {label} '{value}' looks like a typo of "
                                f"@{guess} - the invite will bounce; confirm with the client and fix the sheet")
        phone = clean_phone(r.get("phone"))
        if phone and len(phone) != 10:
            warnings.append(f"row {r['_row']} plot {r['plot']}: phone '{r.get('phone')}' is not 10 digits")
        buyers.append(Buyer(
            row=r["_row"], plot_raw=r["plot"], applicant=applicant, gender=gender,
            co1=co1, co1_gender=co1_gender, co2=co2, phone=phone,
            alt_phone=clean_phone(r.get("alt_phone")), email=email,
            co_email=co_email,
            address=strip_residence_prefix(r.get("address")),
            co_address=strip_residence_prefix(r.get("co_address")),
            plot_address=re.sub(r"\s+", " ", r.get("plot_address") or "").strip(),
        ))
    return buyers, warnings


def group_buyers(buyers: list):
    """One account per email; a buyer without email gets an account keyed by
    phone. Rows missing name/phone inherit them from their group.
    Returns (groups: {key: [Buyer]}, warnings)."""
    warnings = []
    groups = {}
    for b in buyers:
        key = f"email:{b.email}" if b.email else (f"phone:{b.phone}" if b.phone else f"row:{b.row}")
        groups.setdefault(key, []).append(b)
    for key, members in groups.items():
        name = next((m.applicant for m in members if m.applicant), "")
        gender = next((m.gender for m in members if m.applicant and m.gender), None)
        phone = next((m.phone for m in members if m.phone), "")
        address = next((m.address for m in members if m.address), "")
        for m in members:
            if not m.applicant and name:
                warnings.append(f"plot {m.plot_raw}: no applicant name - using '{name}' from the same email")
                m.applicant, m.gender = name, m.gender or gender
            if not m.phone and phone:
                m.phone = phone
            if not m.address and address:
                m.address = address
        names = sorted({m.applicant.lower() for m in members if m.applicant})
        if key.startswith("email:") and len(names) > 1:
            warnings.append(
                f"{key[6:]} is shared by different names {[m.applicant for m in members]} on plots "
                f"{[m.plot_raw for m in members]} - ONE login; each plot keeps its own applicant on the letters")
        if key.startswith("phone:"):
            warnings.append(f"plots {[m.plot_raw for m in members]}: no email - account can't log in until an "
                            f"email is added (username = mobile {key[6:]})")
        if key.startswith("row:"):
            warnings.append(f"plot {members[0].plot_raw}: no email and no phone - cannot create an account")
    return groups, warnings


class OfflineBuyerImport:
    def __init__(self, engine=None, password_hasher=None):
        if engine is None:
            from Divinepersistence.persistence_db import engine as default_engine
            engine = default_engine
        self._engine = engine
        if password_hasher is None:
            from passlib.context import CryptContext
            password_hasher = CryptContext(schemes=["bcrypt"], deprecated="auto").hash
        self._hash = password_hasher

    def run(self, sheet_rows: list, project_name: str, apply: bool = False, batch_id: str = None) -> Report:
        report = Report(batch_id=batch_id or new_batch_id("buyers"))
        buyers, warnings = parse_buyers(sheet_rows)
        report.warnings.extend(warnings)
        groups, warnings = group_buyers(buyers)
        report.warnings.extend(warnings)
        source = f"import:{report.batch_id}"
        slug = project_slug(project_name)
        postgres = self._engine.dialect.name == "postgresql"

        with self._engine.begin() as conn:
            inventory = {plot_key(r["unit_number"]): dict(r) for r in conn.execute(text(
                "SELECT * FROM divine_project_inventory WHERE project_name = :p"), {"p": project_name}).mappings()}
            seen_plots = {}
            for b in buyers:
                k = plot_key(b.plot_raw)
                if k in seen_plots:
                    report.errors.append(f"plot {b.plot_raw} appears twice (rows {seen_plots[k]} and {b.row})")
                seen_plots[k] = b.row
                unit = inventory.get(k)
                if unit is None:
                    report.errors.append(f"row {b.row}: plot {b.plot_raw} is not in the {project_name} inventory")
                elif unit["status"] not in ("sold", "booked", "pending_kyc_review"):
                    report.errors.append(
                        f"row {b.row}: plot {unit['unit_number']} is '{unit['status']}' - it must be sold before "
                        f"a buyer is attached (sync the inventory sheet / use --override first)")
            unassigned = sorted(u["unit_number"] for k, u in inventory.items()
                                if u["status"] == "sold" and k not in seen_plots)
            report.extra["sold plots with no buyer row"] = f"{len(unassigned)}: {', '.join(unassigned)}"

            accounts = []
            for key, members in groups.items():
                if key.startswith("row:"):
                    continue
                kind, ident = key.split(":", 1)
                if kind == "email":
                    existing = conn.execute(text(
                        "SELECT * FROM divine_customer_users WHERE lower(email) = :e OR lower(username) = :e"
                    ), {"e": ident}).mappings().all()
                else:
                    existing = conn.execute(text(
                        "SELECT * FROM divine_customer_users WHERE username = :u"), {"u": ident}).mappings().all()
                if len(existing) > 1:
                    report.errors.append(f"{ident}: {len(existing)} existing customer accounts - resolve manually")
                    continue
                accounts.append((key, kind, ident, members, dict(existing[0]) if existing else None))

            new_count = sum(1 for a in accounts if a[4] is None)
            report.extra["accounts"] = (f"{len(accounts)} ({new_count} new, {len(accounts) - new_count} existing "
                                        f"website accounts reused)")
            if report.errors:
                return report

            for key, kind, ident, members, existing in accounts:
                first = members[0]
                if existing:
                    customer_id = existing["id"]
                    report.add("REUSE", customer_id, f"{ident} plots {[m.plot_raw for m in members]}")
                else:
                    customer_id = None
                    report.add("ACCOUNT", "(new)", f"{ident} '{first.applicant}' plots {[m.plot_raw for m in members]}")
                if apply and existing is None:
                    customer_id = self._create_customer(conn, kind, ident, first, members, source)
                added = 0
                for m in members:
                    unit = inventory[plot_key(m.plot_raw)]
                    if customer_id and self._has_plot_doc(conn, customer_id, unit["unit_number"]):
                        report.add("SKIP", unit["unit_number"], f"{customer_id} already has a record for this plot")
                        continue
                    if unit.get("booked_by") and customer_id != unit["booked_by"]:
                        report.add("CONFLICT", unit["unit_number"],
                                   f"already linked to customer {unit['booked_by']} - not changed")
                        continue
                    if apply:
                        self._insert_plot_record(conn, report.batch_id, customer_id, project_name, slug, unit, m,
                                                 postgres)
                        self._link_inventory(conn, report.batch_id, source, project_name, unit, customer_id,
                                             reused=existing is not None)
                    added += 1
                    report.add("PLOT", unit["unit_number"],
                               f"{m.applicant or '-'}" + (f" + {m.co1}" if m.co1 else "") + (f" + {m.co2}" if m.co2 else ""))
                if apply and existing is not None and added:
                    # A website account the buyer already had: tell them the plot
                    # is there (Loader C); their password is left alone.
                    conn.execute(text(
                        "UPDATE divine_customer_users SET invite_status = 'plot_added_pending' WHERE id = :id "
                        "AND (invite_status IS NULL OR invite_status NOT IN ('pending', 'failed', 'no_email'))"
                    ), {"id": customer_id})
        return report

    def _create_customer(self, conn, kind, ident, first, members, source):
        from Divinepersistence.persistence_db import new_user_ids
        candidate = new_user_ids(conn, "C")[0]
        parts = (first.applicant or "").split(" ", 1)
        now = _now()
        # Unusable until the invite step sets a real temporary password.
        unusable = self._hash(secrets.token_urlsafe(32))
        conn.execute(text(
            "INSERT INTO divine_customer_users(id, username, first_name, last_name, email, phone, password_hash, "
            "created_by, created_date, last_updated_by, last_updated_date, source, must_change_password, "
            "invite_status) VALUES (:id, :username, :first, :last, :email, :phone, :pw, :by, :now, :by, :now, "
            ":source, :must, :invite)"
        ), {"id": candidate, "username": ident[:255], "first": (parts[0] or "")[:150] or None,
            "last": parts[1][:150] if len(parts) > 1 else None,
            "email": ident if kind == "email" else None,
            "phone": next((m.phone for m in members if m.phone), None),
            "pw": unusable, "by": source, "now": now, "source": IMPORT_SOURCE, "must": True,
            "invite": "pending" if kind == "email" else "no_email"})
        return candidate

    @staticmethod
    def _has_plot_doc(conn, customer_id, unit_number) -> bool:
        rows = conn.execute(text(
            "SELECT form_data FROM divine_documents WHERE owner_id = :o AND owner_role = 'customer' "
            "AND document_type IN ('booking_application', 'project_booking_application')"
        ), {"o": customer_id}).all()
        target = plot_key(unit_number)
        for (fd,) in rows:
            fd = json.loads(fd) if isinstance(fd, str) else (fd or {})
            for k in ("unit_number", "unitNumber", "unit_no", "unitNo", "plot_number", "plotNumber", "plot_no", "plotNo"):
                if fd.get(k) and plot_key(fd[k]) == target:
                    return True
        return False

    @staticmethod
    def _insert_plot_record(conn, batch_id, customer_id, project_name, slug, unit, b: Buyer, postgres: bool):
        form = {
            "projectId": slug, "project_name": project_name,
            "unitNo": unit["unit_number"],
            "plotAreaSqYd": str(unit["area_sqyd"]) if unit.get("area_sqyd") is not None else "",
            "plotAreaSqMtr": str(unit["area_sqmt"]) if unit.get("area_sqmt") is not None else "",
            "plot_address": b.plot_address,
            "applicantName": b.applicant, "gender": b.gender or "",
            "phone": b.phone, "mobile": b.phone, "alternate_phone": b.alt_phone,
            "email": b.email, "permanentAddress": b.address,
            "coApplicantName": b.co1, "coApplicantGender": b.co1_gender or "",
            "coApplicantEmail": b.co_email, "coApplicantPermanentAddress": b.co_address,
            "coApplicant2Name": b.co2,
            "source": IMPORT_SOURCE, "finance_status": "pending", "import_batch": batch_id,
            "import_sheet_row": b.row,
        }
        form = {k: v for k, v in form.items() if v not in (None, "")}
        now = _now()
        form_sql = "CAST(:form AS jsonb)" if postgres else ":form"
        conn.execute(text(
            "INSERT INTO divine_documents(id, owner_id, owner_role, document_type, form_data, storage_path, status, "
            f"project_id, created_date, last_updated_date) VALUES (:id, :owner, 'customer', "
            f"'project_booking_application', {form_sql}, :path, :status, :project_id, :now, :now)"
        ), {"id": str(uuid.uuid4()), "owner": customer_id, "form": json.dumps(form),
            "path": f"imported://{batch_id}/{slug}/{unit['unit_number']}", "status": IMPORTED_DOC_STATUS,
            "project_id": slug, "now": now})

    @staticmethod
    def _link_inventory(conn, batch_id, source, project_name, unit, customer_id, reused: bool):
        snap = _snapshot(unit)
        sale_source = unit.get("sale_source") or ("offline" if unit["status"] == "sold" else "app")
        conn.execute(text(
            "UPDATE divine_project_inventory SET booked_by = :c, sale_source = :s, last_updated_date = :now "
            "WHERE id = :id AND (booked_by IS NULL OR booked_by = :c)"
        ), {"c": customer_id, "s": sale_source, "now": _now(), "id": unit["id"]})
        _history(conn, batch_id, source, unit["id"], project_name, unit["unit_number"], unit["status"],
                 unit["status"], old_values=snap,
                 note=f"buyer:{customer_id}" + (" reused" if reused else ""))

    def rollback(self, batch_id: str) -> Report:
        """Remove what one import batch created: its plot records, the accounts
        it created (only if they hold nothing else), inventory buyer links, and
        the 'plot added' flag on reused accounts."""
        report = Report(batch_id=batch_id)
        source = f"import:{batch_id}"
        with self._engine.begin() as conn:
            docs = conn.execute(text("DELETE FROM divine_documents WHERE storage_path LIKE :p"),
                                {"p": f"imported://{batch_id}/%"})
            report.add("DOCS", "", f"{docs.rowcount} plot records deleted")
            hist = conn.execute(text(
                "SELECT * FROM divine_inventory_status_history WHERE batch_id = :b"), {"b": batch_id}).mappings().all()
            for h in hist:
                old = json.loads(h["old_values"] or "{}")
                conn.execute(text("UPDATE divine_project_inventory SET booked_by = :bb, sale_source = :ss WHERE id = :id"),
                             {"bb": old.get("booked_by"), "ss": old.get("sale_source"), "id": h["inventory_id"]})
                note = h["note"] or ""
                if note.endswith(" reused"):
                    conn.execute(text(
                        "UPDATE divine_customer_users SET invite_status = NULL WHERE id = :id "
                        "AND invite_status = 'plot_added_pending'"), {"id": note.split(":", 1)[1].split()[0]})
            created = conn.execute(text("SELECT id FROM divine_customer_users WHERE created_by = :s"),
                                   {"s": source}).all()
            removed = 0
            for (cid,) in created:
                busy = conn.execute(text("SELECT 1 FROM divine_documents WHERE owner_id = :c LIMIT 1"), {"c": cid}).first() \
                    or conn.execute(text("SELECT 1 FROM divine_payments WHERE owner_id = :c LIMIT 1"), {"c": cid}).first()
                if busy:
                    report.add("KEPT", cid, "account has other records")
                    continue
                conn.execute(text("DELETE FROM divine_customer_users WHERE id = :c"), {"c": cid})
                removed += 1
            report.add("ACCOUNTS", "", f"{removed} accounts deleted")
            conn.execute(text("DELETE FROM divine_inventory_status_history WHERE batch_id = :b"), {"b": batch_id})
        return report


# ==========================================================================
# C. Invites
# ==========================================================================
class OfflineBuyerInvites:
    def __init__(self, engine=None, email_service=None, password_hasher=None, sleep=time.sleep):
        if engine is None:
            from Divinepersistence.persistence_db import engine as default_engine
            engine = default_engine
        self._engine = engine
        if email_service is None:
            from DivineService.service_email import serviceEmail
            email_service = serviceEmail()
        self._email = email_service
        if password_hasher is None:
            from passlib.context import CryptContext
            password_hasher = CryptContext(schemes=["bcrypt"], deprecated="auto").hash
        self._hash = password_hasher
        self._sleep = sleep

    def _plots(self, conn, customer_id):
        rows = conn.execute(text(
            "SELECT form_data FROM divine_documents WHERE owner_id = :o AND status = :s ORDER BY created_date"
        ), {"o": customer_id, "s": IMPORTED_DOC_STATUS}).all()
        plots, project = [], None
        for (fd,) in rows:
            fd = json.loads(fd) if isinstance(fd, str) else (fd or {})
            if fd.get("unitNo"):
                plots.append(fd["unitNo"])
            project = project or fd.get("project_name")
        return plots, project

    def run(self, apply: bool = False, customer_ids=None, batch_id: str = None,
            delay_seconds: float = 2.0) -> Report:
        report = Report(batch_id=batch_id or "invites")
        with self._engine.connect() as conn:
            sql = ("SELECT * FROM divine_customer_users WHERE source = :src "
                   "AND invite_status IN ('pending', 'failed', 'no_email')")
            params = {"src": IMPORT_SOURCE}
            if batch_id:
                sql += " AND created_by = :by"
                params["by"] = f"import:{batch_id}"
            new_accounts = [dict(r) for r in conn.execute(text(sql), params).mappings()]
            reused = [dict(r) for r in conn.execute(text(
                "SELECT * FROM divine_customer_users WHERE invite_status IN ('plot_added_pending', 'plot_added_failed')"
            )).mappings()]
        if customer_ids:
            wanted = set(customer_ids)
            new_accounts = [a for a in new_accounts if a["id"] in wanted]
            reused = [a for a in reused if a["id"] in wanted]
            # An already-invited account is only re-sent after the caller resets
            # its invite_status to 'pending' (scripts/...invites.py --resend).

        sent = failed = 0
        csv_rows = []
        for acct in new_accounts:
            with self._engine.connect() as conn:
                plots, project = self._plots(conn, acct["id"])
            email = (acct.get("email") or "").strip()
            name = " ".join(p for p in (acct.get("first_name"), acct.get("last_name")) if p)
            if not email:
                report.add("NO_EMAIL", acct["id"], f"{name} plots {plots} - collect an email, then rerun")
                csv_rows.append((acct["id"], name, "", ";".join(plots), "no_email"))
                continue
            guess = suspicious_email_domain(email)
            if guess:
                report.warnings.append(f"{acct['id']}: '{email}' looks like a typo of @{guess} - fix it before "
                                       f"sending (the invite would bounce)")
            if not apply:
                report.add("INVITE", acct["id"], f"{email} plots {plots}")
                csv_rows.append((acct["id"], name, email, ";".join(plots), "would_send"))
                continue
            status = self._send_invite(acct, email, name, plots, project)
            sent += status == "sent"
            failed += status == "failed"
            report.add("SENT" if status == "sent" else "FAILED", acct["id"], f"{email} plots {plots}")
            csv_rows.append((acct["id"], name, email, ";".join(plots), status))
            self._sleep(delay_seconds)

        for acct in reused:
            with self._engine.connect() as conn:
                plots, project = self._plots(conn, acct["id"])
            email = (acct.get("email") or "").strip()
            name = " ".join(p for p in (acct.get("first_name"), acct.get("last_name")) if p)
            if not apply:
                report.add("PLOT_MAIL", acct["id"], f"{email} plots {plots} (existing account, password unchanged)")
                csv_rows.append((acct["id"], name, email, ";".join(plots), "would_send_plot_added"))
                continue
            ok = self._email.send_plot_added_to_account(email, first_name=acct.get("first_name"), plots=plots,
                                                        project_name=project)
            with self._engine.begin() as conn:
                conn.execute(text("UPDATE divine_customer_users SET invite_status = :s, invite_sent_at = :now "
                                  "WHERE id = :id"),
                             {"s": "plot_added_sent" if ok else "plot_added_failed", "now": _now(), "id": acct["id"]})
            sent += bool(ok)
            failed += not ok
            report.add("SENT" if ok else "FAILED", acct["id"], f"{email} plot-added {plots}")
            csv_rows.append((acct["id"], name, email, ";".join(plots), "plot_added_sent" if ok else "plot_added_failed"))
            self._sleep(delay_seconds)

        report.extra["sent"] = sent
        report.extra["failed"] = failed
        with self._engine.connect() as conn:
            stuck = [r[0] for r in conn.execute(text(
                "SELECT id FROM divine_customer_users WHERE invite_status = 'sending'")).all()]
        if stuck:
            report.warnings.append(
                f"{len(stuck)} account(s) stuck in 'sending' (email may have gone out, status not recorded): "
                f"{', '.join(stuck)} - check the mail log before resending with --resend")
        report.extra["csv_rows"] = csv_rows
        return report

    def _send_invite(self, acct, email, name, plots, project) -> str:
        temp_password = generate_temp_password()
        expires = _now() + timedelta(days=TEMP_PASSWORD_DAYS)
        # 1. Store the hash first ('sending'): whatever the email does next, the
        #    password a buyer may receive is already the one that works. If this
        #    fails, nothing is sent.
        try:
            with self._engine.begin() as conn:
                conn.execute(text(
                    "UPDATE divine_customer_users SET password_hash = :pw, must_change_password = :must, "
                    "temp_password_expires_at = :exp, invite_status = 'sending', last_updated_date = :now "
                    "WHERE id = :id"
                ), {"pw": self._hash(temp_password), "must": True, "exp": expires, "now": _now(), "id": acct["id"]})
        except Exception as e:
            logger.warning("offline_invite_store_failed customer_id=%s error=%s", acct["id"], type(e).__name__)
            return "failed"
        # 2. Send.
        ok = False
        try:
            ok = self._email.send_offline_buyer_invite(
                email, first_name=acct.get("first_name"), temp_password=temp_password, plots=plots,
                project_name=project, expires_on=expires)
        except Exception as e:  # pragma: no cover - send_* never raises
            logger.warning("offline_invite_send_error customer_id=%s error=%s", acct["id"], type(e).__name__)
        del temp_password
        # 3. Record the outcome. A failed send left a hash nobody knows (harmless:
        #    the retry issues a new one). If only this status write fails the
        #    account stays 'sending' - reported, never auto-resent, so a delivered
        #    password is not silently replaced.
        try:
            with self._engine.begin() as conn:
                conn.execute(text(
                    "UPDATE divine_customer_users SET invite_status = :s, invite_sent_at = :sent WHERE id = :id"
                ), {"s": "sent" if ok else "failed", "sent": _now() if ok else None, "id": acct["id"]})
        except Exception as e:
            logger.warning("offline_invite_status_write_failed customer_id=%s sent=%s error=%s",
                           acct["id"], ok, type(e).__name__)
            return "sending"
        return "sent" if ok else "failed"


# ==========================================================================
# 0. Clear website booking data (before loading the real prod data)
# ==========================================================================
CLEANUP_CONFIRM = "DELETE-WEBSITE-BOOKINGS"


class WebsiteBookingCleanup:
    """Removes plot-booking data created through the website/app before go-live
    (test bookings): divine_bookings + their decisions and admin document
    uploads, plot_booking / installment payments, milestones, reminders,
    offline instalment submissions and booking-application documents; plots
    held/booked by them go back to 'available' (the client sheet sync then sets
    the real status). Customer accounts, identity/KYC uploads and
    offline-import records are NOT touched. Uploaded files in Supabase storage
    are not deleted (their DB rows are). Dry run unless apply=True; apply
    returns a JSON-able backup of every deleted/changed row."""

    def __init__(self, engine=None):
        if engine is None:
            from Divinepersistence.persistence_db import engine as default_engine
            engine = default_engine
        self._engine = engine

    @staticmethod
    def _select(conn, sql, params=None):
        return [dict(r) for r in conn.execute(text(sql), params or {}).mappings().all()]

    def run(self, apply: bool = False) -> Report:
        from sqlalchemy import inspect as sa_inspect
        report = Report(batch_id=new_batch_id("cleanup"))
        backup = {}
        with self._engine.begin() as conn:
            tables = set(sa_inspect(conn).get_table_names())

            def rows(table, where="1=1", params=None):
                if table not in tables:
                    return []
                return self._select(conn, f"SELECT * FROM {table} WHERE {where}", params)

            backup["divine_bookings"] = rows("divine_bookings")
            booking_ids = [b["id"] for b in backup["divine_bookings"]]
            backup["divine_booking_decisions"] = rows("divine_booking_decisions")
            backup["divine_booking_document_uploads"] = rows("divine_booking_document_uploads")
            backup["divine_payments"] = rows("divine_payments", "purpose IN ('plot_booking', 'installment')")
            backup["divine_payment_milestones"] = rows("divine_payment_milestones")
            backup["divine_payment_reminders"] = rows("divine_payment_reminders")
            backup["divine_offline_payment_submissions"] = rows("divine_offline_payment_submissions")
            upload_doc_ids = {u.get("document_id") for u in backup["divine_booking_document_uploads"]} | \
                             {u.get("previous_document_id") for u in backup["divine_booking_document_uploads"]}
            upload_doc_ids.discard(None)
            docs = rows("divine_documents",
                        "document_type IN ('booking_application', 'project_booking_application') "
                        "AND (status IS NULL OR status <> :imported)", {"imported": IMPORTED_DOC_STATUS})
            if upload_doc_ids:
                doc_ids = {d["id"] for d in docs}
                docs += [d for d in rows("divine_documents") if d["id"] in upload_doc_ids and d["id"] not in doc_ids]
            backup["divine_documents"] = docs
            inv_cols = {c["name"] for c in sa_inspect(conn).get_columns("divine_project_inventory")}                 if "divine_project_inventory" in tables else set()
            offline_guard = " AND (sale_source IS NULL OR sale_source <> 'offline')" if "sale_source" in inv_cols else ""
            backup["divine_project_inventory"] = rows(
                "divine_project_inventory",
                "(status IN ('booked', 'pending_kyc_review') OR booked_payment_id IS NOT NULL)" + offline_guard)

            for table, items in backup.items():
                report.extra[table] = len(items)
            for b in backup["divine_bookings"]:
                report.add("BOOKING", b.get("unit_number") or "-",
                           f"{b['id']} customer {b.get('customer_id')} status {b.get('status')}")
            for u in backup["divine_project_inventory"]:
                report.add("RESET", u["unit_number"], f"{u['project_name']}: {u['status']} -> available")

            if not apply:
                return report

            def delete(table, where="1=1", params=None):
                if table in tables:
                    conn.execute(text(f"DELETE FROM {table} WHERE {where}"), params or {})

            delete("divine_payment_reminders")
            delete("divine_offline_payment_submissions")
            delete("divine_payment_milestones")
            delete("divine_booking_document_uploads")
            delete("divine_booking_decisions")
            delete("divine_bookings")
            delete("divine_payments", "purpose IN ('plot_booking', 'installment')")
            for d in backup["divine_documents"]:
                delete("divine_documents", "id = :id", {"id": d["id"]})
            for u in backup["divine_project_inventory"]:
                extra = (", sale_source = NULL, status_note = 'Website test booking cleared', "
                         "status_updated_at = :now") if "sale_source" in inv_cols else ""
                conn.execute(text(
                    "UPDATE divine_project_inventory SET status = 'available', booked_at = NULL, "
                    f"booked_payment_id = NULL, booked_by = NULL{extra}, last_updated_date = :now "
                    "WHERE id = :id"), {"id": u["id"], "now": _now()})
            report.extra["backup"] = backup
        return report
