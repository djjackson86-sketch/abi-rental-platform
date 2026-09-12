"""User account and module-access helpers for Sano Trailers.

Two account kinds exist:
- Main profile: role == 'owner' (seeded from ADMIN_EMAIL). Has every module and
  is the only account that can manage users/access from Settings.
- Additional accounts: role == 'staff'. They share ONE global permission set
  stored in company_settings.staff_permissions_json and are capped at
  ADDITIONAL_USER_LIMIT. Their dashboard is always the reduced "for the day"
  view and the Orders page never shows the top totals bar.

Module keys are coarse top-level areas. Order-scoped document operations stay
usable for accounts with the orders module even though the standalone Documents
screen is separately gated.
"""

import json
import re
import uuid

from flask import has_request_context, session
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
    ("admin.calendar", "calendar"),
    ("admin.online_store", "online_store"),
    ("admin.app_store", "app_store"),
    ("admin.reports", "reports"),
    ("admin.reports_orders_csv", "reports"),
    ("admin.scan_barcode", "scan_barcode"),
    ("customers.", "customers"),
    ("inventory.", "inventory"),
    ("branches.", "branches"),
    ("payments.", "payments"),
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


def user_can_module(session, module):
    """True when the current session may use module (main always can)."""
    if not session.get("user_id"):
        return False
    if session.get("user_role") == "owner":
        return True
    return module in (session.get("staff_modules") or [])


def is_main_session(session):
    return session.get("user_role") == "owner"


def list_users():
    """All accounts with the main profile first, then additional accounts.

    Email is intentionally not selected: accounts sign in by name and the email
    column is no longer surfaced anywhere in the UI.
    """
    db = get_db()
    rows = db.execute(
        """SELECT u.id, u.name, u.initials, u.role, u.branch_id, u.can_view_all_branches, u.active, u.created_at,
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


def create_additional_user(name, password, branch_id=None):
    """Create an additional (staff) account. Returns (user_id, error).

    Accounts are identified by name only — no email is collected from the user.
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
    branch_id, can_view_all = _clean_branch_access(branch_id)
    cur = db.execute(
        "INSERT INTO users (email, password_hash, name, initials, role, branch_id, can_view_all_branches, active, created_at) VALUES (?, ?, ?, ?, 'staff', ?, ?, 1, ?)",
        (_placeholder_email(name), generate_password_hash(password), name, initials, branch_id, can_view_all, now()),
    )
    db.commit()
    return cur.lastrowid, None


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


def update_user_branch(user_id, branch_id):
    """Assign an additional account to one branch, or all branches when blank."""
    db = get_db()
    row = db.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or row["role"] == "owner":
        return False
    branch_id, can_view_all = _clean_branch_access(branch_id)
    db.execute(
        "UPDATE users SET branch_id = ?, can_view_all_branches = ? WHERE id = ?",
        (branch_id, can_view_all, user_id),
    )
    db.commit()
    return True


def current_session_user_id():
    """Return logged-in user id in request contexts, or None for public/background work."""
    if not has_request_context():
        return None
    try:
        return int(session.get("user_id") or 0) or None
    except (TypeError, ValueError):
        return None


def session_branch_scope():
    """Return branch_id for branch-limited staff, otherwise None (all branches)."""
    if not has_request_context():
        return None
    if session.get("user_role") == "owner" or session.get("can_view_all_branches"):
        return None
    try:
        return int(session.get("branch_id") or 0) or None
    except (TypeError, ValueError):
        return None


def resolve_branch_filter(requested=""):
    """Resolve a branch-aware screen's ``?branch=`` filter.

    Returns ``(selected, branch_id, label, branches, scope)``. Branch-limited
    staff are already pinned to their branch by the session, so their filter is
    always empty: a crafted ``?branch=`` must never widen what they see. For an
    all-branch viewer an unknown id is ignored rather than trusted.

    Shared by /calendar, /reports and /orders — reuse it rather than re-deriving
    the logic, so the scoping rule cannot drift between screens.
    """
    from app.services.branches import branch_options

    scope = session_branch_scope()
    branches = branch_options()
    if scope:
        branches = [branch for branch in branches if branch["id"] == scope]
        selected = ""
    else:
        allowed = {str(branch["id"]) for branch in branches}
        requested = (requested or "").strip()
        selected = requested if requested in allowed else ""
    branch_id = int(selected) if selected else None
    label = next((branch["name"] for branch in branches if str(branch["id"]) == selected), "")
    return selected, branch_id, label, branches, scope


def order_branch_clause(alias="o", branch_id=None):
    """SQL restriction for an orders query (collection or return branch).

    Branch-limited staff are pinned to their own branch by the session. A caller
    may also pass an explicit ``branch_id`` for a UI branch filter; the session
    scope always wins, so a filter can only ever narrow a view, never widen it.
    """
    scope = session_branch_scope()
    target = scope or branch_id
    if not target:
        return "", []
    prefix = f"{alias}." if alias else ""
    return f" AND ({prefix}collect_branch_id = ? OR {prefix}return_branch_id = ?)", [target, target]


def product_branch_clause(alias="p", include_unassigned=True, branch_id=None):
    """SQL restriction for a products query.

    Unassigned stock only rides along on the staff branch-scope view: choosing a
    specific branch in a filter means that branch's stock, not "that branch plus
    anything not allocated yet".
    """
    scope = session_branch_scope()
    target = scope or branch_id
    if not target:
        return "", []
    prefix = f"{alias}." if alias else ""
    if include_unassigned and not branch_id:
        return f" AND ({prefix}branch_id = ? OR {prefix}branch_id IS NULL)", [target]
    return f" AND {prefix}branch_id = ?", [target]


def user_can_access_order(order):
    branch_id = session_branch_scope()
    if not branch_id:
        return True
    if not order:
        return False
    return order["collect_branch_id"] == branch_id or order["return_branch_id"] == branch_id
