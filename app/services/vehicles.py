"""Vehicles recorded against a client — programme phase 2 (feature A / A2).

The NaTIS licence disc scanner (``app/services/vehicle_disk.py``) parses a disc into
fields; this module is where those fields become a real record, one per client.

House rules that this module is responsible for:

* **One owner per registration.** ``vehicles.registration`` holds the NUMBER PLATE
  (decision D3: ``NB72XMGP``), and a non-blank registration belongs to exactly one
  customer. A second customer claiming it gets a ``ValueError`` — never a silent
  second row and never an automatic transfer (A3 offers the transfer explicitly).
  The column also carries a partial unique index as a database-level backstop.
* **No figure is ever invented.** A mass the disc did not carry is stored as NULL,
  never 0, and a nonsense value typed by hand is refused rather than coerced
  (master plan D3). Towing capacity does not exist anywhere in this model — Don
  removed it (D3b).
* **No orphans.** A vehicle cannot outlive its client: the FK is declared
  ``ON DELETE CASCADE`` *and* ``delete_customer`` clears the rows explicitly, because
  the Turso connection does not guarantee ``PRAGMA foreign_keys=ON``.
"""

import json
import re
import sqlite3

from app.db import get_db, now, scan_write_transaction
from app.services.access import current_session_user_id

SOURCE_MANUAL = "manual"
SOURCE_SCAN = "scan"
SOURCE_IMPORT = "import"
VALID_SOURCES = (SOURCE_MANUAL, SOURCE_SCAN, SOURCE_IMPORT)

#: A client's **main vehicle** — the one their invoices print — is mirrored into the client's
#: ``custom_fields_json`` under these canonical keys. Scanning/linking a vehicle can make it the
#: main one; the customer form edits the same fields. The set is deliberately the eight fields staff
#: capture on the scan screen (registration, disc licence number, NaTIS registration number, VIN,
#: engine number, make, disk expiry and colour) — colour joined the original seven on request,
#: because the licence disc decodes it and staff need to see and correct it instead of leaving it to
#: a hand-typed legacy field — while the rest of a decoded disk (model, category, registering
#: authority, control number) stays on the vehicle row only. The mapping is declared once, here, and
#: the label/order metadata in ``app.services.customers`` follows it (a test pins them).
MAIN_VEHICLE_FIELD_MAP = {
    "registration": "vehicle_reg_no",
    "licence_number": "vehicle_licence_number",
    "registration_number": "vehicle_registration_number",
    "vin": "vehicle_vin",
    "engine_number": "vehicle_engine_number",
    "make": "vehicle_make",
    "licence_disk_expiry": "vehicle_licence_disk_expiry",
    "colour": "vehicle_color",
}
#: The custom-field keys that carry the main vehicle (the eight above). A **customer form** posts all
#: of them and is therefore authoritative over the vehicle row; a partial post (public portal, order
#: card's cached snapshot) carries only some, so it must never push stale values onto the main row.
MAIN_VEHICLE_CUSTOM_KEYS = tuple(MAIN_VEHICLE_FIELD_MAP.values())
#: The internal pointer to the chosen main vehicle, kept inside ``custom_fields_json``. It is never
#: rendered (``customers.HIDDEN_CUSTOM_FIELD_KEYS`` hides it) — it only ties the mirror to the row.
MAIN_VEHICLE_ID_KEY = "main_vehicle_id"

#: Every column a form/scan may set, in the order the vehicle form shows them.
TEXT_FIELDS = (
    "registration",
    "make",
    "model",
    "year",
    "vin",
    "engine_number",
    "colour",
    "licence_number",
    "registration_number",
    "control_number",
    "registering_authority",
    "vehicle_type",
    "licence_disk_expiry",
    "raw_scan_text",
)
MASS_FIELDS = ("tare_kg", "gvm_kg")

#: A recorded vehicle's **own** approval state (ABI-341953212) — deliberately separate from the
#: client's verification (``customers.client_verified``) and their rental block
#: (``customers.is_blocked``). The scan screen's "Approve & Save Vehicle" records ``approved`` and
#: its "Disapprove Vehicle" records ``unapproved``; either way the vehicle stays on the client's
#: record (nothing is ever deleted), so the vehicle's state can never be confused with — or drift
#: into — the client's approval. Every non-scan writer (manual entry, import, edit) leaves the
#: column at its additive default of ``approved``, so nothing else changes behaviour.
STATUS_APPROVED = "approved"
STATUS_UNAPPROVED = "unapproved"
VEHICLE_STATUSES = (STATUS_APPROVED, STATUS_UNAPPROVED)


def _field(form, key):
    """Tolerant form read: a dict, a MultiDict or a sqlite Row all work."""
    try:
        return form.get(key)
    except AttributeError:
        return None


def _row_field(row, key, default=None):
    """Read a column from a sqlite ``Row``, a dict or a plain mapping, tolerating absence."""
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def vehicle_status(row):
    """One vehicle's stored approval state: ``approved`` (the default) or ``unapproved``."""
    value = _text(_row_field(row, "status", ""))
    return value if value in VEHICLE_STATUSES else STATUS_APPROVED


def is_vehicle_unapproved(row):
    return vehicle_status(row) == STATUS_UNAPPROVED


def set_vehicle_status(vehicle_id, status):
    """Store one vehicle's own approval state. ``False`` when the vehicle does not exist.

    Only ever called by the scan screen's Approve/Disapprove actions; the client row is not
    touched, so the vehicle's state and the client's approval stay independent.
    """
    if status not in VEHICLE_STATUSES:
        raise ValueError("Vehicle status must be approved or unapproved")
    try:
        vehicle_id = int(vehicle_id)
    except (TypeError, ValueError):
        return False
    db = get_db()
    cur = db.execute(
        "UPDATE vehicles SET status = ?, updated_at = ? WHERE id = ?",
        (status, now(), vehicle_id),
    )
    db.commit()
    return cur.rowcount > 0


def _text(value):
    return str(value).strip() if value is not None else ""


def normalise_registration(value):
    """The stored shape of a number plate: upper case, inner whitespace collapsed.

    Storing one shape is what makes the unique index a real constraint: ``nb 72 xmgp``
    and ``NB72XMGP`` are the same vehicle to a traffic officer, so they must be the
    same row here.
    """
    return " ".join(_text(value).upper().split())


def registration_key(value):
    """A comparison key for a plate: upper case, all whitespace removed.

    Used wherever two plates have to be proved the same without depending on how they
    were typed (``NB 72 XMGP`` == ``NB72XMGP`` == ``nb72xmgp``).
    """
    return re.sub(r"\s+", "", _text(value).upper())


def _mass(value, label):
    """REAL for a typed mass, or None when it was left blank.

    Blank means "not on the disc", which is a real and common answer (the modern NaTIS
    payload carries no masses at all), so it must stay NULL and must never become 0 —
    a 0 kg tare would read as a recorded figure. Anything that is not a number, or is
    negative, is refused instead of guessed at.
    """
    raw = _text(value)
    if not raw:
        return None
    cleaned = raw.replace(" ", "").replace(",", "")
    try:
        number = float(cleaned)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number in kilograms") from exc
    if number < 0:
        raise ValueError(f"{label} cannot be negative")
    return number


_DATE_SHAPES = (
    re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"),
    re.compile(r"^(\d{4})/(\d{2})/(\d{2})$"),
    re.compile(r"^(\d{2})[-/](\d{2})[-/](\d{4})$"),
)


def normalise_expiry(value):
    """A licence-disc expiry as ISO ``YYYY-MM-DD``, or ``''`` when not recorded.

    Accepts the shapes staff and the parser actually produce (ISO, the app's
    ``dd-mm-yyyy`` display shape and ``dd/mm/yyyy``) and refuses anything else, so a
    date never lands in the column half-typed.
    """
    raw = _text(value)
    if not raw:
        return ""
    for index, pattern in enumerate(_DATE_SHAPES):
        match = pattern.match(raw)
        if not match:
            continue
        if index < 2:
            year, month, day = match.group(1), match.group(2), match.group(3)
        else:
            day, month, year = match.group(1), match.group(2), match.group(3)
        if not (1 <= int(month) <= 12 and 1 <= int(day) <= 31):
            break
        return f"{year}-{month}-{day}"
    raise ValueError("Licence disk expiry must be a date (for example 2027-03-31)")


def licence_disk_expiry_state(value, today=None):
    """A small status dict for the licence-disk-expiry warning, keyed off the stored/typed value.

    ``expired`` is ``True``/``False`` only when there is a real date to judge (the value normalises
    to ISO); anything blank or unparseable gives ``expired: None`` so the UI shows no verdict rather
    than guessing. ``warning`` is the ready-made sentence for an expired disk, or ``None``.
    """
    from datetime import date, datetime

    raw = _text(value)
    if not raw:
        return {"expiry": "", "expired": None, "days_left": None, "warning": None}
    try:
        iso = normalise_expiry(raw)
        expiry = datetime.strptime(iso, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return {"expiry": raw, "expired": None, "days_left": None, "warning": None}
    if today is None:
        current = date.today()
    elif isinstance(today, str):
        try:
            current = datetime.strptime(today[:10], "%Y-%m-%d").date()
        except ValueError:
            current = date.today()
    else:
        current = today
    days_left = (expiry - current).days
    if days_left < 0:
        return {
            "expiry": iso,
            "expired": True,
            "days_left": days_left,
            "warning": f"Licence disk expired on {iso}",
        }
    return {"expiry": iso, "expired": False, "days_left": days_left, "warning": None}


def _clean_source(value):
    source = _text(value) or SOURCE_MANUAL
    if source not in VALID_SOURCES:
        raise ValueError("Vehicle source must be manual, scan or import")
    return source


def _customer_exists(customer_id):
    try:
        customer_id = int(customer_id)
    except (TypeError, ValueError):
        return None
    row = get_db().execute("SELECT id FROM customers WHERE id = ?", (customer_id,)).fetchone()
    return int(row["id"]) if row else None


def _registration_owner(registration, exclude_vehicle_id=None):
    """The customer row that already owns ``registration``, if any.

    Matched on :func:`registration_key` so spacing and case cannot sneak a second
    owner in, and excludes the row being edited so re-saving a vehicle is never
    treated as a clash with itself.
    """
    key = registration_key(registration)
    if not key:
        return None
    sql = """SELECT c.*, v.id AS vehicle_id FROM vehicles v
        JOIN customers c ON c.id = v.customer_id
        WHERE REPLACE(UPPER(v.registration), ' ', '') = ?"""
    params = [key]
    if exclude_vehicle_id is not None:
        sql += " AND v.id <> ?"
        params.append(exclude_vehicle_id)
    return get_db().execute(sql + " ORDER BY v.id LIMIT 1", params).fetchone()


def list_vehicles(customer_id):
    """Every vehicle recorded for one client, newest first."""
    try:
        customer_id = int(customer_id)
    except (TypeError, ValueError):
        return []
    return get_db().execute(
        "SELECT * FROM vehicles WHERE customer_id = ? ORDER BY created_at DESC, id DESC",
        (customer_id,),
    ).fetchall()


def get_vehicle(vehicle_id):
    try:
        vehicle_id = int(vehicle_id)
    except (TypeError, ValueError):
        return None
    return get_db().execute("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,)).fetchone()


def get_vehicle_by_registration(registration):
    """The vehicle row carrying this plate, if one is recorded (feature D matching)."""
    key = registration_key(registration)
    if not key:
        return None
    return get_db().execute(
        "SELECT * FROM vehicles WHERE REPLACE(UPPER(registration), ' ', '') = ? ORDER BY id LIMIT 1",
        (key,),
    ).fetchone()


def customer_for_vehicle_registration(registration):
    """The client a plate is already allocated to, or None when it is free."""
    return _registration_owner(registration)


def get_customer_vehicle_by_registration(customer_id, registration):
    """The vehicle row this *client* already holds for a plate, if any.

    A staff member re-scanning a disc usually has a correction, not a second vehicle, so the save
    route looks here first: when the plate is already this client's, the record is updated in place
    rather than refused as a duplicate (``create_vehicle`` keeps its own duplicate guard).
    """
    key = registration_key(registration)
    if not key:
        return None
    try:
        customer_id = int(customer_id)
    except (TypeError, ValueError):
        return None
    return get_db().execute(
        "SELECT * FROM vehicles WHERE customer_id = ? AND REPLACE(UPPER(registration), ' ', '') = ? "
        "ORDER BY id LIMIT 1",
        (customer_id, key),
    ).fetchone()


# ── the main vehicle mirror (the client's "Custom customer details") ─────────


def _form_has(form, key):
    """True when the submitted form actually carries this key (a MultiDict or a dict)."""
    try:
        return key in form
    except TypeError:
        return False


def _raw_custom_fields(customer_id):
    """The client's ``custom_fields_json`` as a dict, or ``None`` when there is no such client."""
    try:
        customer_id = int(customer_id)
    except (TypeError, ValueError):
        return None
    row = get_db().execute("SELECT custom_fields_json FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row["custom_fields_json"] or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_custom_fields(customer_id, custom_fields):
    get_db().execute(
        "UPDATE customers SET custom_fields_json = ? WHERE id = ?",
        (json.dumps(custom_fields, ensure_ascii=False), int(customer_id)),
    )
    get_db().commit()


def _sync_customer_main(customer_id, vehicle_row):
    """Write a vehicle row (or its absence) into the client's mirrored ``vehicle_*`` fields.

    Only the mirrored keys and the internal pointer are touched — every other custom field the
    client carries (VAT, company reg, alternative contacts, anything imported) is preserved exactly
    as it was. Replacing the main vehicle therefore clears the mirrored fields the new vehicle does
    not have, so nothing of the previous main vehicle survives (no stale detail, no orphan key).
    """
    fields = _raw_custom_fields(customer_id)
    if fields is None:
        return False
    if vehicle_row is None:
        fields.pop(MAIN_VEHICLE_ID_KEY, None)
        for key in MAIN_VEHICLE_CUSTOM_KEYS:
            fields.pop(key, None)
    else:
        from app.services.reports import row_dict
        row = row_dict(vehicle_row)
        fields[MAIN_VEHICLE_ID_KEY] = int(row["id"])
        for column, key in MAIN_VEHICLE_FIELD_MAP.items():
            value = _text(row.get(column))
            if value:
                fields[key] = value
            else:
                fields.pop(key, None)
    _write_custom_fields(customer_id, fields)
    return True


def get_main_vehicle_id(customer_id):
    """The id of the client's chosen main vehicle, or ``None``.

    A pointer left behind by a vehicle that has since been removed is dropped here (together with
    the stale mirrored fields), so no page can show a main vehicle that no longer exists.
    """
    fields = _raw_custom_fields(customer_id)
    if not fields:
        return None
    raw = fields.get(MAIN_VEHICLE_ID_KEY)
    if raw is None or raw == "":
        return None
    try:
        main_id = int(raw)
    except (TypeError, ValueError):
        return None
    row = get_db().execute(
        "SELECT id FROM vehicles WHERE id = ? AND customer_id = ?", (main_id, int(customer_id))
    ).fetchone()
    if row is None:
        _sync_customer_main(int(customer_id), None)
        return None
    return main_id


def get_main_vehicle(customer_id):
    """The client's main vehicle row, or ``None`` when none is chosen."""
    main_id = get_main_vehicle_id(customer_id)
    if main_id is None:
        return None
    return get_db().execute(
        "SELECT * FROM vehicles WHERE id = ? AND customer_id = ?", (main_id, int(customer_id))
    ).fetchone()


def customer_custom_vehicle_plate(customer_id):
    """The plate typed straight into the client's Custom vehicle fields (no vehicle row at all)."""
    fields = _raw_custom_fields(customer_id) or {}
    return _text(fields.get("vehicle_reg_no"))


def promote_main_vehicle(customer_id, vehicle_id):
    """Make one recorded vehicle the client's main (invoiced) vehicle.

    The vehicle must already belong to the client. The mirrored ``vehicle_*`` fields become this
    vehicle's own values and any field it does not carry is cleared, so the pointer and the mirror
    can never point at different vehicles.
    """
    row = get_vehicle(vehicle_id)
    if row is None:
        raise ValueError("That vehicle is no longer on file")
    try:
        customer = int(customer_id)
    except (TypeError, ValueError):
        raise ValueError("Choose the client this vehicle belongs to")
    if int(row["customer_id"]) != customer:
        raise ValueError("That vehicle is not recorded for this client")
    return _sync_customer_main(customer, row)


def clear_main_vehicle(customer_id):
    """Forget the client's main vehicle (used when it is deleted, or transferred away)."""
    return _sync_customer_main(customer_id, None)


def sync_main_vehicle_to_customer(customer_id, vehicle_id=None):
    """Re-mirror the client's main vehicle after it changed.

    Called after a vehicle edit so the invoiced details keep up. When ``vehicle_id`` is given the
    mirror is only touched if that vehicle really is the client's main — editing an *additional*
    vehicle never rewrites the main one.
    """
    main_id = get_main_vehicle_id(customer_id)
    if main_id is None:
        return False
    if vehicle_id is not None and int(vehicle_id) != main_id:
        return False
    row = get_db().execute(
        "SELECT * FROM vehicles WHERE id = ? AND customer_id = ?", (main_id, int(customer_id))
    ).fetchone()
    return _sync_customer_main(int(customer_id), row)


def parse_main_choice(value):
    """'yes'/'no' (or 1/0, on/off) as ``True``/``False``; anything else — including absent — None."""
    text = _text(value).lower()
    if text in ("yes", "1", "true", "on", "y"):
        return True
    if text in ("no", "0", "false", "off", "n"):
        return False
    return None


def _registration_conflict_error(customer_id, registration, vehicle_id):
    """Raise the right 'that plate is taken' error for a mirrored plate change, or return None."""
    if not registration_key(registration):
        return None
    owner = _registration_owner(registration, exclude_vehicle_id=vehicle_id)
    if owner is None:
        return None
    if int(owner["id"]) == int(customer_id):
        raise ValueError(
            f"Registration {registration} is already recorded on another vehicle for this client — "
            "open that vehicle and make it the main one instead of changing the plate here"
        )
    raise ValueError(
        f"Registration {registration} is already recorded for {owner['name']} — transfer it "
        "explicitly if it is now this client's vehicle"
    )


def apply_main_vehicle_form_sync(customer_id, form, custom_fields_json):
    """Reconcile a customer save with the client's linked main vehicle.

    When the client has a main vehicle, the mirrored ``vehicle_*`` fields are always re-written from
    that vehicle row, so the on-screen card, the customer record and the invoice can never drift —
    and a partial post from the public portal or the order screen's cached snapshot can neither
    blank those fields nor push a stale value into the row.

    Only the **customer form** (which carries all eight mirrored fields) is authoritative over the
    vehicle row: an edit there saves to the row, with a plate another client owns refused rather
    than swapped in. Any other caller's vehicle fields are ignored (the row wins).

    ``custom_fields_json`` is the value ``_clean`` produced; the (possibly re-mirrored) JSON string
    is returned for the caller to save. With no linked main vehicle, nothing is touched and the
    client's plain custom fields (the legacy hand-typed vehicle fields included) are left as-is.
    """
    main_id = get_main_vehicle_id(customer_id)
    if main_id is None:
        return custom_fields_json
    vehicle = get_vehicle(main_id)
    if vehicle is None:
        return custom_fields_json

    if all(_form_has(form, key) for key in MAIN_VEHICLE_CUSTOM_KEYS):
        updates = {}
        for column, key in MAIN_VEHICLE_FIELD_MAP.items():
            value = _text(_field(form, key))
            if column == "registration":
                value = normalise_registration(value)
            elif column == "licence_disk_expiry":
                value = normalise_expiry(value)
            if value != _text(vehicle[column]):
                updates[column] = value
        if updates:
            _registration_conflict_error(customer_id, updates.get("registration"), main_id)
            assignments = ", ".join(f"{column} = :{column}" for column in updates)
            get_db().execute(
                f"UPDATE vehicles SET {assignments}, updated_at = :updated_at WHERE id = :id",
                {**updates, "id": int(main_id), "updated_at": now()},
            )
            get_db().commit()
            refreshed = get_vehicle(main_id)
            if refreshed is not None:
                vehicle = refreshed

    try:
        fields = json.loads(custom_fields_json or "{}")
    except Exception:
        fields = {}
    if not isinstance(fields, dict):
        fields = {}
    fields[MAIN_VEHICLE_ID_KEY] = int(main_id)
    for column, key in MAIN_VEHICLE_FIELD_MAP.items():
        value = _text(vehicle[column])
        if value:
            fields[key] = value
        else:
            fields.pop(key, None)
    return json.dumps(fields, ensure_ascii=False)


def fields_from_disc(parsed):
    """Map a ``parse_disc_text()`` result onto this module's form field names.

    The mapping is explicit because the parser's key names and the column names disagree:
    ``parse_disc_text()`` returns the **number plate** under ``licence_number`` (it mirrors
    ``saDiscParser.ts``'s ``licenceNumber``), while ``vehicles.licence_number`` is the disc's own
    licence number (``5120367QP4HD``). Copying the parser dict straight into a vehicle row would
    put the plate in the licence-number column — the trap tick 2 flagged for A3.

    Masses come back as ``float | None`` from the parser and are handed to the form as text, where
    blank means NULL: a disc that carries no tare/GVM (every modern NaTIS payload) must never
    become a recorded 0 kg.
    """
    def text(key):
        return str(parsed.get(key) or "").strip()

    fields = {
        "registration": text("licence_number"),
        "registration_number": text("registration_number"),
        "licence_number": text("disc_licence_number"),
        "make": text("make"),
        "model": text("model"),
        "colour": text("colour"),
        "vin": text("vin"),
        "engine_number": text("engine_number"),
        "control_number": text("control_number"),
        "registering_authority": text("registering_authority"),
        "vehicle_type": text("vehicle_type"),
        "licence_disk_expiry": text("expiry_date"),
        "raw_scan_text": str(parsed.get("raw_text") or ""),
        "source": SOURCE_SCAN,
    }
    for field in MASS_FIELDS:
        value = parsed.get(field)
        fields[field] = "" if value is None else str(value)
    return fields


def transfer_vehicle(vehicle_id, customer_id):
    """Move one recorded vehicle onto another client. Explicit action only.

    A3's review form offers this when a scanned plate is already recorded for somebody else, with
    the owner named in the warning. Nothing calls it automatically — a scan can never silently
    re-own a vehicle — and the row is *moved*, not copied, so the plate keeps exactly one owner.
    """
    existing = get_vehicle(vehicle_id)
    if existing is None:
        return False
    target = _customer_exists(customer_id)
    if target is None:
        raise ValueError("Choose the client this vehicle belongs to")
    if int(existing["customer_id"]) == target:
        return True
    former = int(existing["customer_id"])
    was_main = get_main_vehicle_id(former) == int(existing["id"])
    db = get_db()
    db.execute(
        "UPDATE vehicles SET customer_id = ?, updated_at = ? WHERE id = ?",
        (target, now(), int(existing["id"])),
    )
    db.commit()
    # A transferred vehicle is no longer the former owner's, so if it was their main vehicle the
    # mirror is cleared — their invoice can never keep printing a vehicle they have given up.
    if was_main:
        _sync_customer_main(former, None)
    return True


def vehicle_counts():
    """Small on-file summary for the dashboard/ledger (no customer data)."""
    row = get_db().execute(
        """SELECT COUNT(*) AS total,
            SUM(source = 'scan') AS scan,
            SUM(source = 'manual') AS manual,
            SUM(source = 'import') AS import,
            SUM(registration = '') AS blank_registration
        FROM vehicles"""
    ).fetchone()
    return {
        "total": row["total"] or 0,
        "scan": row["scan"] or 0,
        "manual": row["manual"] or 0,
        "import": row["import"] or 0,
        "blank_registration": row["blank_registration"] or 0,
    }


def _values_from_form(form, *, existing=None):
    """The column values a form supplies, validated. Absent keys keep their old value."""
    existing = existing if existing is not None else {}
    values = {}
    for field in TEXT_FIELDS:
        value = _field(form, field)
        if value is None:
            if field in existing:
                values[field] = existing[field]
            continue
        if field == "registration":
            values[field] = normalise_registration(value)
        elif field == "licence_disk_expiry":
            values[field] = normalise_expiry(value)
        else:
            values[field] = _text(value)
    for field in MASS_FIELDS:
        value = _field(form, field)
        if value is None:
            if field in existing:
                values[field] = existing[field]
            continue
        label = "Tare mass" if field == "tare_kg" else "GVM"
        values[field] = _mass(value, label)
    source = _field(form, "source")
    if source is not None:
        values["source"] = _clean_source(source)
    else:
        # An edit that does not post the source keeps the stored one (a scanned disc
        # must not silently downgrade to "manual" just because the form omits it).
        values["source"] = _field(existing, "source") or SOURCE_MANUAL
    return values


def _assert_registration_is_free(registration, customer_id, exclude_vehicle_id=None):
    if not registration_key(registration):
        return
    owner = _registration_owner(registration, exclude_vehicle_id=exclude_vehicle_id)
    if owner is None:
        return
    if int(owner["id"]) != int(customer_id):
        raise ValueError(
            f"Registration {registration} is already recorded for {owner['name']} — "
            "transfer it explicitly if it is now this client's vehicle"
        )
    # The same client scanning the same plate twice is not a transfer, it is a double entry: the
    # partial unique index would refuse the row anyway (A3 measured that as an IntegrityError
    # bubbling out of the save route), so it is refused here with something a staff member can act
    # on instead of a 500.
    raise ValueError(
        f"Registration {registration} is already recorded for {owner['name']} — "
        "open that vehicle and edit it instead of adding it a second time"
    )


def _create_values(form, customer_id):
    values = _values_from_form(form)
    values.setdefault("source", SOURCE_MANUAL)
    for field in TEXT_FIELDS:
        values.setdefault(field, "")
    for field in MASS_FIELDS:
        values.setdefault(field, None)
    resolved_customer = _customer_exists(_field(form, "customer_id") if customer_id is None else customer_id)
    if resolved_customer is None:
        raise ValueError("Choose the client this vehicle belongs to")
    return values, resolved_customer


def create_vehicle(form, customer_id=None):
    """Record a vehicle against a client. Returns the new row id."""
    values, resolved_customer = _create_values(form, customer_id)
    _assert_registration_is_free(values["registration"], resolved_customer)
    timestamp = now()
    db = get_db()
    cur = db.execute(
        f"""INSERT INTO vehicles ({", ".join(TEXT_FIELDS)}, {", ".join(MASS_FIELDS)}, source,
            customer_id, created_by_user_id, created_at, updated_at)
        VALUES ({", ".join(":" + field for field in TEXT_FIELDS)}, {", ".join(":" + field for field in MASS_FIELDS)},
            :source, :customer_id, :created_by_user_id, :created_at, :updated_at)""",
        {
            **values,
            "customer_id": resolved_customer,
            "created_by_user_id": current_session_user_id(),
            "created_at": timestamp,
            "updated_at": timestamp,
        },
    )
    db.commit()
    return cur.lastrowid


def update_vehicle(vehicle_id, form):
    """Apply the supplied fields to a vehicle. False when the vehicle does not exist.

    Only the keys present in ``form`` are written, so an edit screen that posts part of
    the record cannot blank the rest of it (and an empty mass field clears a mass to
    NULL deliberately).
    """
    existing = get_vehicle(vehicle_id)
    if existing is None:
        return False
    values = _values_from_form(form, existing=dict(existing))
    _assert_registration_is_free(values["registration"], existing["customer_id"], exclude_vehicle_id=existing["id"])
    assignments = ", ".join(f"{field} = :{field}" for field in (*TEXT_FIELDS, *MASS_FIELDS, "source"))
    params = {**values, "id": int(existing["id"]), "updated_at": now()}
    db = get_db()
    db.execute(f"UPDATE vehicles SET {assignments}, updated_at = :updated_at WHERE id = :id", params)
    db.commit()
    # If this is the client's main vehicle, the mirrored invoice fields follow the edit. An edit to
    # an additional vehicle leaves the main one (and the mirror) untouched.
    sync_main_vehicle_to_customer(int(existing["customer_id"]), int(existing["id"]))
    return True


def delete_vehicle(vehicle_id):
    try:
        vehicle_id = int(vehicle_id)
    except (TypeError, ValueError):
        return False
    existing = get_vehicle(vehicle_id)
    if existing is None:
        return False
    customer_id = int(existing["customer_id"])
    was_main = get_main_vehicle_id(customer_id) == vehicle_id
    db = get_db()
    cur = db.execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))
    db.commit()
    # Removing the main vehicle clears its mirrored fields, so the client's invoice can never print
    # a vehicle that is no longer recorded (no stale detail left behind).
    if was_main:
        _sync_customer_main(customer_id, None)
    return cur.rowcount > 0


def delete_vehicles_for_customer(customer_id):
    """Remove every vehicle of a client — called by ``customers.delete_customer``.

    Explicit on purpose: the FK says ``ON DELETE CASCADE``, but a Turso connection does
    not guarantee ``PRAGMA foreign_keys=ON``, so the children are cleared here as well
    and a client can never be deleted leaving orphan vehicle rows behind.
    """
    try:
        customer_id = int(customer_id)
    except (TypeError, ValueError):
        return 0
    db = get_db()
    cur = db.execute("DELETE FROM vehicles WHERE customer_id = ?", (customer_id,))
    db.commit()
    return cur.rowcount


# ── scanned vehicles waiting for a client (ABI-341953230) ────────────────────────────────
#
# ``vehicles.customer_id`` is NOT NULL, so a licence disc scanned before the client exists (or
# before staff have finished verifying a pending portal sign-up) cannot be recorded against
# anybody yet. The counter therefore parks up to THREE validated scans in this separate queue and
# allocates one to a client later. A waiting row carries **no customer**: it is never a
# half-allocated vehicle, and nothing here touches the ``vehicles`` table until allocation.
#
# The cap is a property of the table, not of the caller: ``slot`` is NOT NULL, CHECK 1..3 and
# UNIQUE, and the insert is conditional on that slot being free, so two simultaneous saves can
# never write a fourth row. Duplicate protection compares plates on :func:`registration_key`
# (case and spacing ignored), against both this queue and the recorded vehicles — matching how
# ``vehicles`` itself behaves.

WAITING_LIST_SLOTS = (1, 2, 3)
WAITING_LIST_FULL_MESSAGE = (
    "The scanned-vehicle waiting list is full (3 vehicles). Allocate or remove one before "
    "scanning another."
)
WAITING_LIST_NO_REGISTRATION_MESSAGE = (
    "A registration number is required before a scanned vehicle can wait for a client."
)
WAITING_LIST_STALE_MESSAGE = (
    "That scanned vehicle is no longer waiting — it may already have been allocated or removed."
)


def _waiting_columns():
    return (
        "slot",
        *TEXT_FIELDS,
        *MASS_FIELDS,
        "source",
        "status",
        "created_by_user_id",
        "created_at",
        "updated_at",
    )


def list_waiting_vehicles():
    """Every scanned vehicle waiting for a client, in counter order (slot 1 first)."""
    return get_db().execute(
        "SELECT * FROM vehicle_waiting_list ORDER BY slot, id"
    ).fetchall()


def get_waiting_vehicle(waiting_id):
    """One waiting row, or ``None`` when it has been allocated or removed."""
    try:
        waiting_id = int(waiting_id)
    except (TypeError, ValueError):
        return None
    return get_db().execute(
        "SELECT * FROM vehicle_waiting_list WHERE id = ?", (waiting_id,)
    ).fetchone()


def _waiting_for_registration(registration):
    """The waiting row already holding this plate (spacing/case ignored), if any."""
    key = registration_key(registration)
    if not key:
        return None
    return get_db().execute(
        "SELECT * FROM vehicle_waiting_list "
        "WHERE REPLACE(UPPER(registration), ' ', '') = ? ORDER BY slot, id LIMIT 1",
        (key,),
    ).fetchone()


def _waiting_duplicate_error(registration, queued):
    """The one 'that plate is already waiting' refusal, naming the slot it sits in."""
    return ValueError(
        f"Registration {registration} is already waiting on the list "
        f"(slot {_row_field(queued, 'slot', '?')}) — allocate or remove that scanned "
        "vehicle instead of adding it again."
    )


def _is_constraint_error(exc):
    """True when either adapter refused a UNIQUE/CHECK constraint (not a generic failure)."""
    if isinstance(exc, sqlite3.IntegrityError):
        return True
    return type(exc).__name__ == "LibsqlError" and "constraint" in str(exc).lower()


def _insert_waiting(values, timestamp):
    """Write one waiting row into the first free slot, or refuse when all three are taken.

    The INSERT is conditional on the slot being free and the slot is UNIQUE, so the check and
    the write are one statement — two requests cannot both claim slot 2, and no fourth row can
    ever exist (the CHECK makes slots 1..3 the only possibilities). A constraint refusal means a
    concurrent save won the race: a duplicate plate is refused with the normal clear message,
    any other slot conflict simply tries the next slot.
    """
    columns = _waiting_columns()
    sql = (
        f"INSERT INTO vehicle_waiting_list ({', '.join(columns)}) "
        f"SELECT {', '.join(':' + column for column in columns)} "
        "WHERE NOT EXISTS (SELECT 1 FROM vehicle_waiting_list WHERE slot = :slot)"
    )
    params = {
        **{field: values.get(field, "") for field in TEXT_FIELDS},
        **{field: values.get(field) for field in MASS_FIELDS},
        "source": values.get("source") or SOURCE_SCAN,
        "status": STATUS_APPROVED,
        "created_by_user_id": current_session_user_id(),
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    db = get_db()
    for slot in WAITING_LIST_SLOTS:
        params["slot"] = slot
        try:
            cur = db.execute(sql, params)
        except Exception as exc:  # noqa: BLE001 - narrowed immediately below
            if not _is_constraint_error(exc):
                raise
            queued = _waiting_for_registration(values["registration"])
            if queued is not None:
                raise _waiting_duplicate_error(values["registration"], queued) from None
            continue
        if cur.rowcount == 1:
            return int(cur.lastrowid)
    raise ValueError(WAITING_LIST_FULL_MESSAGE)


def save_waiting_vehicle(form):
    """Queue one validated scan until a client can be allocated. Returns the waiting row id.

    The scan screen's field values are validated by the same helpers a real vehicle save uses
    (:func:`_values_from_form`: registration normalised, expiry ISO, masses numeric-or-NULL), so a
    row that waits cannot hold anything a vehicle row would refuse. The row is recorded
    ``approved`` and carries **no customer**. A plate already waiting, or already recorded on a
    vehicle (spacing/case ignored), is refused with a message naming the slot or the owner —
    a replay never silently updates or duplicates a scan.
    """
    values = _values_from_form(form)
    if not _form_has(form, "source"):
        values["source"] = SOURCE_SCAN
    for field in TEXT_FIELDS:
        values.setdefault(field, "")
    for field in MASS_FIELDS:
        values.setdefault(field, None)
    values["registration"] = normalise_registration(values.get("registration"))
    if not registration_key(values["registration"]):
        raise ValueError(WAITING_LIST_NO_REGISTRATION_MESSAGE)

    timestamp = now()
    with scan_write_transaction():
        queued = _waiting_for_registration(values["registration"])
        if queued is not None:
            raise _waiting_duplicate_error(values["registration"], queued)
        owner = _registration_owner(values["registration"])
        if owner is not None:
            raise ValueError(
                f"Registration {values['registration']} is already recorded for {owner['name']} — "
                "open that client's vehicle instead of adding it to the waiting list."
            )
        waiting_id = _insert_waiting(values, timestamp)
    return waiting_id


def remove_waiting_vehicle(waiting_id):
    """Take one waiting scan off the list (no vehicle, no client). ``False`` when absent."""
    try:
        waiting_id = int(waiting_id)
    except (TypeError, ValueError):
        return False
    with scan_write_transaction():
        cur = get_db().execute(
            "DELETE FROM vehicle_waiting_list WHERE id = ?", (waiting_id,)
        )
        removed = cur.rowcount > 0
    return removed


def allocate_waiting_vehicle(waiting_id, customer_id):
    """Allocate one waiting scan to a pending portal client. Returns the new vehicle id.

    Only a client whose portal registration is **still pending** and inside the session's branch
    scope may receive a waiting vehicle, and only when they have no main vehicle yet — neither a
    ``main_vehicle_id`` pointer nor any hand-typed legacy ``vehicle_*`` field. The new vehicle is
    written exactly as a scan save would write it (``approved``) and becomes the client's main
    vehicle, so the mirror and the row agree.

    The waiting row is removed **in the same transaction** as the vehicle insert and the mirror
    write: the removal is the claim, so two concurrent allocations of the same scan cannot both
    succeed — the loser finds nothing to claim and is refused. Any failure after the claim rolls
    the whole thing back, leaving the scan waiting.
    """
    from app.services import portal_registrations

    with scan_write_transaction():
        # Acquire SQLite's write lock before reading either the queue or the main vehicle.
        # Two different waiting scans allocated to the same client must serialize too.
        get_db().execute("UPDATE vehicle_waiting_list SET slot = slot WHERE id = ?", (waiting_id,))
        waiting = get_waiting_vehicle(waiting_id)
        if waiting is None:
            raise ValueError(WAITING_LIST_STALE_MESSAGE)
        target = _customer_exists(customer_id)
        if target is None:
            raise ValueError("Choose the client this scanned vehicle belongs to.")

        pending = portal_registrations.pending_registration_for_customer(target)
        if pending is None:
            raise ValueError(
                "A waiting vehicle may only be allocated to a client whose portal registration "
                "is still pending approval."
            )
        if not portal_registrations.branch_in_session_scope(pending):
            raise ValueError("That portal registration is outside the branches you can access.")

        if get_main_vehicle_id(target) is not None:
            raise ValueError(
                "That client already has a main vehicle on file — open the client and edit it "
                "instead of allocating another."
            )
        mirror = _raw_custom_fields(target) or {}
        if any(_text(mirror.get(key)) for key in MAIN_VEHICLE_CUSTOM_KEYS):
            raise ValueError(
                "That client already has vehicle details captured on their record — clear or "
                "edit those before allocating a scanned vehicle."
            )

        # Claim the row first. It is the only writer that can remove this id, so a concurrent
        # allocation that read the same row is refused at the next line instead of writing a
        # second vehicle.
        claim = get_db().execute(
            "DELETE FROM vehicle_waiting_list WHERE id = ? AND slot = ?",
            (int(waiting["id"]), int(waiting["slot"])),
        )
        if claim.rowcount != 1:
            raise ValueError(WAITING_LIST_STALE_MESSAGE)

        # A plate recorded for somebody between scanning and allocation is refused by
        # create_vehicle's own duplicate guard; the rollback puts the scan back on the list.
        from app.services.reports import row_dict
        vehicle_id = create_vehicle(row_dict(waiting), customer_id=target)
        set_vehicle_status(vehicle_id, STATUS_APPROVED)
        promote_main_vehicle(target, vehicle_id)
    return int(vehicle_id)
