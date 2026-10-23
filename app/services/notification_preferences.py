"""Per-account notification preferences.

Each staff member decides which kinds of staff alert *they* receive. The preference is
stored per ``users`` row, not per branch or per role: two people at the same depot can
make different choices, and a person's choice follows them to whichever device they use.

Design rules:

* **Absence of a row means the type's default.** Every type today defaults to ON, so the
  staff who already receive alerts keep receiving them the moment this ships — nobody has
  to re-opt-in and no backfill is needed. A switch-off is written as an explicit
  ``enabled = 0`` row, which is what the dispatch filter reads.
* **The type list is the extension point.** ``NOTIFICATION_TYPES`` is the single registry
  of types, labels, help text and defaults; the settings page renders whatever it holds and
  the save only writes known keys. A future alert type is one entry here plus one
  ``recipient_filter`` call at its dispatch point.
* **Rows are read safely on sqlite and libSQL.** ``sqlite3.Row`` raises ``IndexError`` for
  a missing column while the libSQL SDK row raises ``KeyError`` and has no ``keys()``, so
  every value goes through ``_value`` (the same tolerance the rest of the app uses).
* **Writes are self-only.** There is no user id parameter on the save path; the caller
  passes the signed-in account's id.
"""

from app.db import get_db, now

#: Customer portal submissions and online shop enquiries for the signed-in staff
#: member's own branch. Both dispatch paths use this one key, so a person who turns it
#: off hears about neither.
CUSTOMER_NOTIFICATIONS = "customer_notifications"

#: The registry the settings page and the save path both read. ``default`` is what an
#: account with no stored row gets, and it is deliberately ON for every existing type.
NOTIFICATION_TYPES = (
    {
        "key": CUSTOMER_NOTIFICATIONS,
        "label": "Customer notifications",
        "description": (
            "New customer portal submissions and online shop enquiries for your branch."
        ),
        "default": True,
    },
)

TYPE_KEYS = tuple(entry["key"] for entry in NOTIFICATION_TYPES)
DEFAULT_ENABLED = {entry["key"]: bool(entry["default"]) for entry in NOTIFICATION_TYPES}


def row_value(row, key, default=None):
    """Read one column from a sqlite3.Row or a libSQL SDK Row, or return ``default``.

    sqlite3.Row raises ``IndexError``/``TypeError`` for a missing name; the libSQL Row
    raises ``KeyError`` and exposes no ``keys()``. Both are handled, so this module does
    not care which driver served the row.
    """
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        pass
    try:
        return row.asdict().get(key, default)
    except (AttributeError, TypeError):
        return default


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    try:
        return int(value) == 1
    except (TypeError, ValueError):
        return str(value).strip().lower() in {"1", "true", "yes", "on"}


def is_known_type(notification_type):
    return notification_type in DEFAULT_ENABLED


def default_for(notification_type):
    return DEFAULT_ENABLED.get(notification_type, False)


def stored_preferences(user_id):
    """``{type: bool}`` for the rows this account actually has (no defaults filled in)."""
    user_id = _as_int(user_id)
    if user_id <= 0:
        return {}
    rows = get_db().execute(
        "SELECT notification_type, enabled FROM notification_preferences WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    stored = {}
    for row in rows:
        key = str(row_value(row, "notification_type") or "").strip()
        if key in DEFAULT_ENABLED:
            stored[key] = _as_bool(row_value(row, "enabled"))
    return stored


def preferences_for(user_id):
    """Every registered type's effective value for this account, in registry order."""
    stored = stored_preferences(user_id)
    return {key: stored.get(key, DEFAULT_ENABLED[key]) for key in TYPE_KEYS}


def is_enabled(user_id, notification_type=CUSTOMER_NOTIFICATIONS):
    """True when this account should receive ``notification_type``.

    An unknown type is off: dispatch must never invent a category. A known type with no
    stored row uses its registry default (ON today).
    """
    if not is_known_type(notification_type):
        return False
    return stored_preferences(user_id).get(notification_type, DEFAULT_ENABLED[notification_type])


def enabled_user_ids(user_ids, notification_type=CUSTOMER_NOTIFICATIONS):
    """The subset of ``user_ids`` whose preference allows this type.

    One query, whatever the recipient count. Order is preserved and duplicates drop.
    """
    if not is_known_type(notification_type):
        return []
    ordered = []
    for value in user_ids or []:
        user_id = _as_int(value)
        if user_id > 0 and user_id not in ordered:
            ordered.append(user_id)
    if not ordered:
        return []

    marks = ",".join("?" for _ in ordered)
    rows = get_db().execute(
        f"""SELECT user_id, enabled FROM notification_preferences
            WHERE notification_type = ? AND user_id IN ({marks})""",
        [notification_type] + ordered,
    ).fetchall()
    stored = {}
    for row in rows:
        stored[_as_int(row_value(row, "user_id"))] = _as_bool(row_value(row, "enabled"))

    default = DEFAULT_ENABLED[notification_type]
    return [user_id for user_id in ordered if stored.get(user_id, default)]


def _write(user_id, notification_type, enabled):
    """Atomic upsert, supported identically by SQLite and libSQL."""
    get_db().execute(
        """INSERT INTO notification_preferences (user_id, notification_type, enabled, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(user_id, notification_type) DO UPDATE
           SET enabled=excluded.enabled, updated_at=excluded.updated_at""",
        (user_id, notification_type, 1 if enabled else 0, now()),
    )


def save_preferences(user_id, form):
    """Persist the settings form for ``user_id`` and return the effective preferences.

    A checkbox is only in the payload when ticked, so "absent" is a deliberate off. Only
    registry keys are written; an unknown field cannot create a preference.
    """
    user_id = _as_int(user_id)
    if user_id <= 0:
        raise ValueError("A signed-in account is required to save notification settings.")
    values = {}
    for key in TYPE_KEYS:
        raw = form.get(key) if form is not None else None
        values[key] = _as_bool(raw)
        _write(user_id, key, values[key])
    get_db().commit()
    return values
