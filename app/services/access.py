"""User account and module-access helpers for ABI Rental.

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

from flask import has_request_context, session
from werkzeug.security import generate_password_hash

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
    """All accounts with the main profile first, then additional accounts."""
    db = get_db()
    rows = db.execute(
        """SELECT u.id, u.email, u.name, u.initials, u.role, u.branch_id, u.can_view_all_branches, u.active, u.created_at,
               b.name AS branch_name
        FROM users u
        LEFT JOIN branches b ON b.id = u.branch_id
        ORDER BY CASE WHEN u.role = 'owner' THEN 0 ELSE 1 END, u.id"""
    ).fetchall()
    return rows


def additional_user_count():
    row = get_db().execute("SELECT COUNT(*) AS c FROM users WHERE role <> 'owner'").fetchone()
    return int(row["c"] if row is not None else 0)


def _normalise_email(email):
    return (email or "").strip().lower()


def _clean_branch_access(branch_id):
    try:
        branch_id = int(branch_id or 0)
    except (TypeError, ValueError):
        branch_id = 0
    return (branch_id or None, 0 if branch_id else 1)


def create_additional_user(name, email, password, branch_id=None):
    """Create an additional (staff) account. Returns (user_id, error)."""
    db = get_db()
    name = (name or "").strip()
    email = _normalise_email(email)
    if not name:
        return None, "Name is required"
    if not email or "@" not in email:
        return None, "A valid email is required"
    if not password or len(password) < 6:
        return None, "Password must be at least 6 characters"
    if additional_user_count() >= ADDITIONAL_USER_LIMIT:
        return None, f"Limit reached: only {ADDITIONAL_USER_LIMIT} additional accounts are allowed"
    existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        return None, "A user with that email already exists"
    initials = "".join(part[0] for part in name.split() if part)[:2].upper() or "US"
    branch_id, can_view_all = _clean_branch_access(branch_id)
    cur = db.execute(
        "INSERT INTO users (email, password_hash, name, initials, role, branch_id, can_view_all_branches, active, created_at) VALUES (?, ?, ?, ?, 'staff', ?, ?, 1, ?)",
        (email, generate_password_hash(password), name, initials, branch_id, can_view_all, now()),
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


def order_branch_clause(alias="o"):
    branch_id = session_branch_scope()
    if not branch_id:
        return "", []
    prefix = f"{alias}." if alias else ""
    return f" AND ({prefix}collect_branch_id = ? OR {prefix}return_branch_id = ?)", [branch_id, branch_id]


def product_branch_clause(alias="p", include_unassigned=True):
    branch_id = session_branch_scope()
    if not branch_id:
        return "", []
    prefix = f"{alias}." if alias else ""
    if include_unassigned:
        return f" AND ({prefix}branch_id = ? OR {prefix}branch_id IS NULL)", [branch_id]
    return f" AND {prefix}branch_id = ?", [branch_id]


def user_can_access_order(order):
    branch_id = session_branch_scope()
    if not branch_id:
        return True
    if not order:
        return False
    return order["collect_branch_id"] == branch_id or order["return_branch_id"] == branch_id
