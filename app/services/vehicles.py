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

import re

from app.db import get_db, now
from app.services.access import current_session_user_id

SOURCE_MANUAL = "manual"
SOURCE_SCAN = "scan"
SOURCE_IMPORT = "import"
VALID_SOURCES = (SOURCE_MANUAL, SOURCE_SCAN, SOURCE_IMPORT)

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


def _field(form, key):
    """Tolerant form read: a dict, a MultiDict or a sqlite Row all work."""
    try:
        return form.get(key)
    except AttributeError:
        return None


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
    db = get_db()
    db.execute(
        "UPDATE vehicles SET customer_id = ?, updated_at = ? WHERE id = ?",
        (target, now(), int(existing["id"])),
    )
    db.commit()
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
    return True


def delete_vehicle(vehicle_id):
    try:
        vehicle_id = int(vehicle_id)
    except (TypeError, ValueError):
        return False
    db = get_db()
    cur = db.execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))
    db.commit()
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
