"""User account and module-access helpers for Sano Trailers.

Two account kinds exist:
- Main profile: role == 'owner' (seeded from ADMIN_EMAIL). Has every module and
  is the only account that can manage users/access from Settings.
- Additional accounts: role == 'staff'. They are capped at ADDITIONAL_USER_LIMIT.
  Their dashboard is always the reduced "for the day" view and the Orders page
  never shows the top totals bar.

Modules: company_settings.staff_permissions_json holds the **shared default** set
that every additional account inherits. An account may instead carry its own set
in users.modules_json (NULL = inherit the shared default), which is what makes a
per-account permission edit possible after the account was created.

Branch access: users.branch_id + users.can_view_all_branches still describe the
*primary* branch and the "every branch" flag. user_branch_access holds the extra
branches an account may also see, so an account can manage two of three depots
without seeing all of them. No rows + can_view_all_branches + a primary branch is
exactly the old single-branch behaviour.

Module keys are coarse top-level areas. Order-scoped document operations stay
usable for accounts with the orders module even though the standalone Documents
screen is separately gated.
"""

import json
import re
import uuid
from functools import wraps

from flask import abort, has_request_context, session
from werkzeug.security import check_password_hash, generate_password_hash

from app.db import get_db, now

ADDITIONAL_USER_LIMIT = 10

DEFAULT_STAFF_MODULES = [
    "new_order",
    "dashboard",
    "calendar",
    "orders",
    "customers",
]

MODULES = [
    ("new_order", "New order"),
    ("dashboard", "Dashboard"),
    ("calendar", "Calendar"),
    ("orders", "Orders"),
    ("customers", "Customers"),
    ("inventory", "Inventory"),
    ("branches", "Branches"),
    ("documents", "Documents"),
    ("payments", "Payments"),
    ("online_store", "Online store"),
    ("app_store", "App store"),
    ("reports", "Reports"),
    ("scan_barcode", "Scan a barcode"),
    ("scan_vehicle", "Scan a vehicle licence disk"),
    ("scan_return", "Scan to return a trailer"),
    ("settings", "Settings"),
]

MODULE_KEYS = [key for key, _label in MODULES]

# Endpoints that are always allowed without module gating.
_OPEN_ENDPOINT_PREFIXES = (
    "auth.",
    "public.",
    "static",
    "internal_telegram.",
)

# endpoint prefix -> module key. Order matters; first match wins.
_ENDPOINT_MODULE_RULES = [
    ("orders.new", "new_order"),
    ("orders.", "orders"),
    ("documents.index", "documents"),
    ("documents.export_csv", "documents"),
    # Everything else under documents is reached from an order detail page and
    # stays usable for accounts that have the orders module (invoices, quotes,
    # finalize, download, send email).
    ("documents.", "orders"),
    ("admin.dashboard", "dashboard"),
    # The dashboard's spare wheel panel (ABI-341953033) lives on the dashboard.
    # It is already covered by the prefix above; listed here so the gate is
    # explicit rather than implied.
    ("admin.dashboard_spare_wheels", "dashboard"),
    ("admin.calendar", "calendar"),
    ("admin.online_store", "online_store"),
    ("admin.app_store", "app_store"),
    ("admin.reports", "reports"),
    ("admin.reports_orders_csv", "reports"),
    ("admin.scan_barcode", "scan_barcode"),
    # Programme phase 3 (feature A / A3): the licence-disc scan screen and the vehicle actions it
    # owns. The client page's read-only vehicles feed is the exception — it shows what is already
    # on the client's record, so the accounts that may open the client page may read it.
    ("vehicles.customer_vehicles", "customers"),
    ("vehicles.", "scan_vehicle"),
    # Programme phase 6 (feature D / D2): scanning a disk to bring a rental back.
    # Like scan_vehicle it stays out of the shared staff default set, so the main
    # profile ticks it per account; the routes 403 without it.
    ("returns.", "scan_return"),
    # The dashboard's cash-up / end of day panel. Same module as the screen it
    # lives on, so an account that may see the dashboard may cash its drawer up.
    ("cash.", "dashboard"),
    ("customers.", "customers"),
    ("inventory.", "inventory"),
    ("branches.", "branches"),
    ("payments.", "payments"),
    # Programme phase 10 (feature B / §B3): the customer-portal admin pages — the per-branch link
    # list, the QR sheet and the slug/on-off save. They live under /settings/..., so the
    # ("settings.", "settings") prefix below would already gate them; listed here so the gate is
    # explicit rather than implied (same reason the dashboard's spare-wheel panel is listed).
    ("settings.portal_index", "settings"),
    ("settings.portal_save", "settings"),
    ("settings.portal_print", "settings"),
    ("settings.", "settings"),
]


def module_for_endpoint(endpoint):
    """Return the module key that gates this endpoint, or None if ungated."""
    if not endpoint:
        return None
    if endpoint.startswith(_OPEN_ENDPOINT_PREFIXES):
        return None
    if endpoint == "admin.index" or endpoint == "admin.health":
        return None
    for prefix, module in _ENDPOINT_MODULE_RULES:
        if endpoint == prefix.rstrip(".") or endpoint.startswith(prefix):
            return module
    return None


def parse_staff_modules(value):
    """Parse persisted staff module JSON safely for sqlite/libsql rows."""
    if value is None:
        return list(DEFAULT_STAFF_MODULES)
    if isinstance(value, (list, tuple)):
        return [key for key in value if key in MODULE_KEYS]
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "[]")
        except (TypeError, ValueError):
            return list(DEFAULT_STAFF_MODULES)
        return [key for key in parsed if key in MODULE_KEYS]
    return list(DEFAULT_STAFF_MODULES)


def staff_modules_from_settings(settings_row):
    """Extract staff module list from a company_settings row."""
    if settings_row is None:
        return list(DEFAULT_STAFF_MODULES)
    try:
        value = settings_row["staff_permissions_json"]
    except (KeyError, TypeError):
        try:
            value = settings_row.asdict().get("staff_permissions_json")
        except (AttributeError, TypeError):
            return list(DEFAULT_STAFF_MODULES)
    return parse_staff_modules(value)


def user_module_keys_from_row(row):
    """An account's OWN module list from a users row, or None when it inherits.

    Unlike ``parse_staff_modules`` this distinguishes "never set" (NULL column,
    inherit the shared default) from "saved as empty" (the account may open no
    module at all) — the difference matters once an admin can edit one account's
    modules after it was created.
    """
    if row is None:
        return None
    try:
        value = row["modules_json"]
    except (KeyError, IndexError, TypeError):
        try:
            value = row.asdict().get("modules_json")
        except (AttributeError, TypeError):
            return None
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return parse_staff_modules(value)


def user_module_keys(user_id):
    """An account's own module list, or None when it uses the shared default."""
    row = get_db().execute("SELECT modules_json FROM users WHERE id = ?", (user_id,)).fetchone()
    return user_module_keys_from_row(row)


def user_module_assignments(users, default_keys):
    """{user_id: {'own': bool, 'keys': [...]}} for the per-account module ticks.

    ``own`` is False for an account still inheriting the shared default, so the
    page can show its ticks without pretending they are a saved per-user choice.
    """
    assignments = {}
    for user in users or []:
        try:
            user_id = int(user["id"])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        own = user_module_keys_from_row(user)
        assignments[user_id] = {
            "own": own is not None,
            "keys": own if own is not None else list(default_keys or []),
        }
    return assignments


def save_user_modules(user_id, module_keys):
    """Give one additional account its own module set. Returns (ok, keys|error).

    Unknown keys are dropped. The main profile is deliberately refused: it always
    has every module and cannot be restricted.
    """
    db = get_db()
    row = db.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or row["role"] == "owner":
        return False, "The main profile always has every function"
    valid = []
    for key in (module_keys or []):
        if key in MODULE_KEYS and key not in valid:
            valid.append(key)
    db.execute("UPDATE users SET modules_json = ? WHERE id = ?", (json.dumps(valid), user_id))
    db.commit()
    return True, valid


def clear_user_modules(user_id):
    """Return an account to the shared default module set (the inverse of a save)."""
    db = get_db()
    row = db.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or row["role"] == "owner":
        return False
    db.execute("UPDATE users SET modules_json = NULL WHERE id = ?", (user_id,))
    db.commit()
    return True


def user_can_module(session, module):
    """True when the current session may use module (main always can)."""
    if not session.get("user_id"):
        return False
    if session.get("user_role") == "owner":
        return True
    return module in (session.get("staff_modules") or [])


def is_main_session(session):
    return session.get("user_role") == "owner"


def main_required(view):
    """Restrict a view to the main profile (role 'owner').

    Permanent deletions are reserved for the main profile, so the check has to
    live on the endpoint — hiding the button in a template is not a permission
    boundary. Returns 403 (not a redirect) so a crafted POST is visibly refused.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("user_role") != "owner":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def list_users():
    """All accounts with the main profile first, then additional accounts.

    Email is intentionally not selected: accounts sign in by name and the email
    column is no longer surfaced anywhere in the UI. ``modules_json`` rides along
    so the Users page can render each account's own module ticks.
    """
    db = get_db()
    rows = db.execute(
        """SELECT u.id, u.name, u.initials, u.role, u.branch_id, u.can_view_all_branches, u.active, u.created_at,
               u.modules_json,
               b.name AS branch_name
        FROM users u
        LEFT JOIN branches b ON b.id = u.branch_id
        ORDER BY CASE WHEN u.role = 'owner' THEN 0 ELSE 1 END, u.id"""
    ).fetchall()
    return rows


def login_user_options():
    """Active accounts for the sign-in name dropdown (main profile first)."""
    rows = get_db().execute(
        """SELECT id, name, role FROM users
        WHERE active = 1
        ORDER BY CASE WHEN role = 'owner' THEN 0 ELSE 1 END, LOWER(name), id"""
    ).fetchall()
    return rows


def additional_user_count():
    row = get_db().execute("SELECT COUNT(*) AS c FROM users WHERE role <> 'owner'").fetchone()
    return int(row["c"] if row is not None else 0)


def _normalise_name(name):
    return " ".join((name or "").split())


def _placeholder_email(name):
    """Hidden internal value for the legacy NOT NULL UNIQUE users.email column.

    Accounts are created and signed in by name only, and email is never surfaced
    in the UI. The column is kept (not dropped) so the owner seed path and any
    historic rows stay intact.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", _normalise_name(name).lower()).strip("-") or "user"
    return f"{slug}-{uuid.uuid4().hex[:8]}@abi.local"


def name_taken(name, exclude_user_id=None):
    """True when another account already uses this name (case-insensitive)."""
    name = _normalise_name(name)
    if not name:
        return False
    sql = "SELECT id FROM users WHERE LOWER(name) = ?"
    params = [name.lower()]
    if exclude_user_id:
        sql += " AND id <> ?"
        params.append(exclude_user_id)
    return get_db().execute(sql, params).fetchone() is not None


def _clean_branch_access(branch_id):
    try:
        branch_id = int(branch_id or 0)
    except (TypeError, ValueError):
        branch_id = 0
    return (branch_id or None, 0 if branch_id else 1)


def create_additional_user(name, password, branch_id=None, branch_ids=None):
    """Create an additional (staff) account. Returns (user_id, error).

    Accounts are identified by name only — no email is collected from the user.
    ``branch_ids`` (optional) is the multi-branch selection; when it is given the
    first entry is also the account's primary branch.
    """
    db = get_db()
    name = _normalise_name(name)
    if not name:
        return None, "Name is required"
    if not password or len(password) < 6:
        return None, "Password must be at least 6 characters"
    if additional_user_count() >= ADDITIONAL_USER_LIMIT:
        return None, f"Limit reached: only {ADDITIONAL_USER_LIMIT} additional accounts are allowed"
    if name_taken(name):
        return None, "An account with that name already exists"
    initials = "".join(part[0] for part in name.split() if part)[:2].upper() or "US"
    selected = []
    for value in (branch_ids or []):
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value and value not in selected:
            selected.append(value)
    if selected:
        branch_id = selected[0]
    branch_id, can_view_all = _clean_branch_access(branch_id)
    cur = db.execute(
        "INSERT INTO users (email, password_hash, name, initials, role, branch_id, can_view_all_branches, active, created_at) VALUES (?, ?, ?, ?, 'staff', ?, ?, 1, ?)",
        (_placeholder_email(name), generate_password_hash(password), name, initials, branch_id, can_view_all, now()),
    )
    user_id = cur.lastrowid
    # Keep the multi-branch rows in step with the primary branch so a freshly
    # created single-branch account looks the same as one saved on the Users page.
    if selected:
        for selected_id in selected:
            db.execute(
                "INSERT INTO user_branch_access (user_id, branch_id) VALUES (?, ?)",
                (user_id, selected_id),
            )
    db.commit()
    return user_id, None


def set_user_active(user_id, active):
    db = get_db()
    row = db.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or row["role"] == "owner":
        return False
    db.execute("UPDATE users SET active = ? WHERE id = ?", (1 if active else 0, user_id))
    db.commit()
    return True


def change_own_password(user_id, current_password, new_password):
    """Change the signed-in account's own password.

    This is the one password change the main profile is allowed to make for
    itself - every other account helper deliberately refuses to touch role
    'owner', which previously left the main profile with no way to rotate its
    own password. The current password is required so a hijacked session alone
    cannot take the account over.
    """
    db = get_db()
    row = db.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        return "Account not found"
    if not check_password_hash(row["password_hash"], current_password or ""):
        return "Current password is incorrect"
    if not new_password or len(new_password) < 6:
        return "New password must be at least 6 characters"
    if new_password == current_password:
        return "New password must be different from the current one"
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
               (generate_password_hash(new_password), user_id))
    db.commit()
    return None


def reset_user_password(user_id, password):
    if not password or len(password) < 6:
        return "Password must be at least 6 characters"
    db = get_db()
    row = db.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or row["role"] == "owner":
        return "Only additional accounts can have their password reset here"
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(password), user_id))
    db.commit()
    return None


def delete_additional_user(user_id):
    db = get_db()
    row = db.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or row["role"] == "owner":
        return False
    # libsql autocommits, so the branch rows are deleted explicitly rather than
    # relying on the FK cascade.
    db.execute("DELETE FROM user_branch_access WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    return True


def save_staff_modules(module_keys):
    valid = [key for key in (module_keys or []) if key in MODULE_KEYS]
    payload = json.dumps(valid)
    db = get_db()
    db.execute("UPDATE company_settings SET staff_permissions_json = ? WHERE id = 1", (payload,))
    db.commit()
    return valid


def user_branch_ids(user_id):
    """Branch ids an additional account is limited to (empty = every branch).

    Reads the extra-depot rows. An account with no rows keeps the old
    single-branch outcome through ``users.branch_id`` (see
    ``session_branch_scope_ids``).
    """
    rows = get_db().execute(
        "SELECT branch_id FROM user_branch_access WHERE user_id = ? ORDER BY branch_id",
        (user_id,),
    ).fetchall()
    ids = []
    for row in rows:
        try:
            value = int(row["branch_id"])
        except (TypeError, ValueError):
            continue
        if value and value not in ids:
            ids.append(value)
    return ids


def update_user_branches(user_id, branch_ids):
    """Set an additional account's branch access. Returns False for the owner.

    An empty selection means "all branches" (the historic default). With one or
    more branches the account's existing primary branch stays the default for
    orders they create, when it is still among the selected depots; otherwise the
    first selected branch becomes the default. Selection order never leaks into
    the stored primary, so re-saving the same ticks cannot silently move it.
    """
    db = get_db()
    row = db.execute("SELECT role, branch_id FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or row["role"] == "owner":
        return False
    known = set()
    for branch in db.execute("SELECT id FROM branches").fetchall():
        try:
            known.add(int(branch["id"]))
        except (TypeError, ValueError):
            continue
    wanted = []
    for value in (branch_ids or []):
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value in known and value not in wanted:
            wanted.append(value)
    db.execute("DELETE FROM user_branch_access WHERE user_id = ?", (user_id,))
    if not wanted:
        db.execute(
            "UPDATE users SET branch_id = NULL, can_view_all_branches = 1 WHERE id = ?",
            (user_id,),
        )
    else:
        try:
            primary = int(row["branch_id"] or 0)
        except (TypeError, ValueError):
            primary = 0
        if primary not in wanted:
            primary = wanted[0]
        for branch_id in wanted:
            db.execute(
                "INSERT INTO user_branch_access (user_id, branch_id) VALUES (?, ?)",
                (user_id, branch_id),
            )
        db.execute(
            "UPDATE users SET branch_id = ?, can_view_all_branches = 0 WHERE id = ?",
            (primary, user_id),
        )
    db.commit()
    return True


def update_user_branch(user_id, branch_id):
    """Assign an additional account to one branch, or all branches when blank.

    Kept for the single-branch callers; it is now the one-branch case of
    ``update_user_branches`` so both paths write the same rows.
    """
    branch_id, can_view_all = _clean_branch_access(branch_id)
    return update_user_branches(user_id, [] if can_view_all else [branch_id])


def user_branch_map(users):
    """{user_id: {'ids': [...], 'all': bool, 'primary': id}} for the Users page.

    One query for every account. An account with no extra-depot rows but a
    primary branch is reported as that single branch, so its tick is shown
    exactly where the historic single-branch select would have been.
    """
    mapping = {}
    user_ids = []
    for user in users or []:
        try:
            user_id = int(user["id"])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        try:
            primary = int(user["branch_id"] or 0) or None
        except (TypeError, ValueError):
            primary = None
        mapping[user_id] = {
            "ids": [],
            "all": bool(user["can_view_all_branches"]),
            "primary": primary,
        }
        user_ids.append(user_id)
    if user_ids:
        marks = ",".join("?" for _ in user_ids)
        for row in get_db().execute(
            f"SELECT user_id, branch_id FROM user_branch_access WHERE user_id IN ({marks}) ORDER BY branch_id",
            user_ids,
        ).fetchall():
            try:
                owner_id = int(row["user_id"])
                branch_id = int(row["branch_id"])
            except (TypeError, ValueError):
                continue
            info = mapping.get(owner_id)
            if info is not None and branch_id not in info["ids"]:
                info["ids"].append(branch_id)
    for info in mapping.values():
        if not info["ids"] and not info["all"] and info["primary"]:
            info["ids"] = [info["primary"]]
    return mapping


def current_session_user_id():
    """Return logged-in user id in request contexts, or None for public/background work."""
    if not has_request_context():
        return None
    try:
        return int(session.get("user_id") or 0) or None
    except (TypeError, ValueError):
        return None


def session_primary_branch_id():
    """The branch this session acts as — the default for orders they create.

    When a multi-depot account has chosen the branch it is managing for this
    sign-in, that choice is the answer: the account may only see that depot, so
    defaulting new work back to a different depot would hide the record the
    moment it was saved. Otherwise it is the account's own primary branch.
    """
    if not has_request_context():
        return None
    active = session_active_branch_id()
    if active:
        return active
    try:
        return int(session.get("branch_id") or 0) or None
    except (TypeError, ValueError):
        return None


def session_active_branch_id():
    """The depot this sign-in chose to manage, or None when it manages all.

    Only ever written by ``/select-branch`` after validating the id against the
    depots the account may already reach, so it can narrow a session but never
    widen one.
    """
    if not has_request_context():
        return None
    try:
        return int(session.get("active_branch_id") or 0) or None
    except (TypeError, ValueError):
        return None


def session_active_branch():
    """The active depot as ``{'id', 'name'}`` for the chrome, or None.

    Read straight from the database when a choice exists (so a rename shows up
    immediately) and skipped entirely otherwise, which keeps the common case at
    zero extra queries.
    """
    branch_id = session_active_branch_id()
    if not branch_id:
        return None
    row = get_db().execute("SELECT id, name FROM branches WHERE id = ?", (branch_id,)).fetchone()
    if row is None:
        return None
    return {"id": int(row["id"]), "name": row["name"]}


def _session_granted_branch_ids():
    """Every depot this session may reach, or None when it may reach them all.

    Deliberately independent of the active-branch choice: this is the *grant*,
    and it is what both the scope and the chooser validate against.
    """
    if not has_request_context():
        return None
    if session.get("user_role") == "owner" or session.get("can_view_all_branches"):
        return None
    cleaned = []
    raw = session.get("branch_ids")
    if isinstance(raw, (list, tuple)):
        for value in raw:
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if value and value not in cleaned:
                cleaned.append(value)
    if cleaned:
        return cleaned
    # No extra-depot rows: the historic single branch (blank branch = all
    # branches) still applies, so an existing account is unaffected.
    try:
        primary = int(session.get("branch_id") or 0) or None
    except (TypeError, ValueError):
        primary = None
    return [primary] if primary else None


def session_branch_choice_options():
    """Depots a multi-depot account may choose between; [] when there is none.

    The main profile, an all-branch account and a single-depot account have
    nothing to choose, so they are never asked. The list is narrowed to the
    account's own depots, which is also what makes a posted id safe.
    """
    granted = _session_granted_branch_ids()
    if not granted or len(granted) < 2:
        return []
    from app.services.branches import branch_options

    return [branch for branch in branch_options() if branch["id"] in granted]


def session_branch_scope_ids():
    """Branch ids this session may see, or None when it may see every branch.

    None means unrestricted (the main profile, "all branches" accounts and every
    non-request/public context). A list means restricted to exactly those depots;
    an empty list means restricted to nothing. The session value is written at
    sign-in, so a crafted ``?branch=`` or a posted branch id can never widen it.

    A multi-depot account that chose the branch it is managing for this sign-in
    is scoped to that one depot. The choice is validated against the granted set
    here as well, so even a tampered session cannot escape it.
    """
    granted = _session_granted_branch_ids()
    if granted is None:
        return None
    active = session_active_branch_id()
    if active is not None and active in granted:
        return [active]
    return granted


def session_branch_scope():
    """Return branch_id for branch-limited staff, otherwise None (all branches).

    Single-branch callers keep working: for an account limited to several depots
    this returns its primary branch, while the SQL helpers below use the full
    list through ``session_branch_scope_ids()``.
    """
    ids = session_branch_scope_ids()
    if not ids:
        return None
    primary = session_primary_branch_id()
    if primary and primary in ids:
        return primary
    return ids[0]


def _branch_id_list(branch_id=None):
    """Branch ids a query is restricted to, or None when nothing restricts it.

    An empty list is a real restriction (to nothing) and must render as an empty
    result set, never as "no restriction". A requested ``branch_id`` can only ever
    NARROW a session scope (and is ignored when it falls outside it), so a crafted
    ``?branch=`` can never widen what a limited account sees.
    """
    scope = session_branch_scope_ids()
    if scope is None:
        return [branch_id] if branch_id else None
    scope = list(scope)
    if branch_id:
        try:
            requested = int(branch_id)
        except (TypeError, ValueError):
            return scope
        return [requested] if requested in scope else scope
    return scope


def resolve_branch_filter(requested=""):
    """Resolve a branch-aware screen's ``?branch=`` filter.

    Returns ``(selected, branch_id, label, branches, scope)``. A single-depot
    account is pinned to its branch by the session, so its filter is always
    empty and the template renders a *disabled, fixed* label instead of a
    chooser. An account with several depots gets a chooser narrowed to those
    depots — still a filter that can only narrow, never a way to widen. For an
    all-branch viewer an unknown id is ignored rather than trusted.

    Shared by /calendar, /reports and /orders — reuse it rather than re-deriving
    the logic, so the scoping rule cannot drift between screens.
    """
    from app.services.branches import branch_options

    scope_ids = session_branch_scope_ids()
    branches = branch_options()
    if scope_ids is not None:
        branches = [branch for branch in branches if branch["id"] in scope_ids]
        if len(branches) == 1:
            return "", None, "", branches, branches[0]["id"]
        if not branches:
            # Restricted to nothing: no chooser, and the SQL clause returns no rows.
            return "", None, "", [], 1
        allowed = {str(branch["id"]) for branch in branches}
        requested = (requested or "").strip()
        selected = requested if requested in allowed else ""
        branch_id = int(selected) if selected else None
        label = next((branch["name"] for branch in branches if str(branch["id"]) == selected), "")
        return selected, branch_id, label, branches, None
    allowed = {str(branch["id"]) for branch in branches}
    requested = (requested or "").strip()
    selected = requested if requested in allowed else ""
    branch_id = int(selected) if selected else None
    label = next((branch["name"] for branch in branches if str(branch["id"]) == selected), "")
    return selected, branch_id, label, branches, scope_ids


def order_branch_clause(alias="o", branch_id=None):
    """SQL restriction for an orders query (collection or return branch).

    Branch-limited staff are pinned to their depots by the session; an account
    limited to several branches matches any of them. A caller may also pass an
    explicit ``branch_id`` for a UI branch filter; the session scope always wins,
    so a filter can only ever narrow a view, never widen it.

    The single-branch clause is byte-identical to the pre-multi-branch version so
    that path stays provably unchanged.
    """
    targets = _branch_id_list(branch_id)
    if targets is None:
        return "", []
    prefix = f"{alias}." if alias else ""
    if not targets:
        return " AND 0=1", []
    if len(targets) == 1:
        target = targets[0]
        return f" AND ({prefix}collect_branch_id = ? OR {prefix}return_branch_id = ?)", [target, target]
    marks = ",".join("?" for _ in targets)
    return (
        f" AND ({prefix}collect_branch_id IN ({marks}) OR {prefix}return_branch_id IN ({marks}))",
        [*targets, *targets],
    )


def product_branch_clause(alias="p", include_unassigned=True, branch_id=None):
    """SQL restriction for a products query.

    Unassigned stock only rides along on the staff branch-scope view: choosing a
    specific branch in a filter means that branch's stock, not "that branch plus
    anything not allocated yet".

    The single-branch clause is byte-identical to the pre-multi-branch version.
    """
    targets = _branch_id_list(branch_id)
    if targets is None:
        return "", []
    prefix = f"{alias}." if alias else ""
    if not targets:
        return " AND 0=1", []
    if len(targets) == 1:
        target = targets[0]
        if include_unassigned and not branch_id:
            return f" AND ({prefix}branch_id = ? OR {prefix}branch_id IS NULL)", [target]
        return f" AND {prefix}branch_id = ?", [target]
    marks = ",".join("?" for _ in targets)
    if include_unassigned and not branch_id:
        return f" AND ({prefix}branch_id IN ({marks}) OR {prefix}branch_id IS NULL)", list(targets)
    return f" AND {prefix}branch_id IN ({marks})", list(targets)


def customer_branch_clause(alias="c", branch_id=None):
    """SQL restriction for a customers query, by the branch that created them.

    Deliberately NO unassigned ride-along (unlike stock): a depot's "customers
    added by this branch" figure must not be inflated by head-office rows, and
    imported/legacy customers carry no branch, so they belong to nobody's depot
    figure. With no restriction at all (main profile, no filter) nothing is
    appended, so the company-wide count stays exactly what it always was.

    Same non-negotiable rule as the other scopes: the session scope always wins,
    so a requested branch can only ever NARROW the view.
    """
    targets = _branch_id_list(branch_id)
    if targets is None:
        return "", []
    prefix = f"{alias}." if alias else ""
    if not targets:
        return " AND 0=1", []
    if len(targets) == 1:
        return f" AND {prefix}branch_id = ?", [targets[0]]
    marks = ", ".join("?" for _ in targets)
    return f" AND {prefix}branch_id IN ({marks})", list(targets)


def user_can_access_order(order):
    targets = _branch_id_list()
    if targets is None:
        return True
    if not order:
        return False
    if not targets:
        return False
    try:
        collect = order["collect_branch_id"]
        returning = order["return_branch_id"]
    except (KeyError, IndexError, TypeError):
        return False
    return collect in targets or returning in targets
