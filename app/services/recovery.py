"""Emergency access to the main profile.

The recovery password is a MASTER credential held by the operator, not by the
client:

- it is stored only as a hash in the RECOVERY_PASSWORD_HASH environment
  variable, so it never lives in the repository or the database and a database
  dump does not expose it;
- it cannot be read or changed from inside the app, so a client cannot quietly
  replace it and lock the operator out;
- it is NOT a login and grants no data access on its own. All it can do is set
  a new password on the main profile, after which normal sign-in applies;
- every attempt, successful or not, is written to recovery_events with the
  caller's address and time, so emergency access is always attributable.

Together those make the standing credential auditable rather than invisible,
which is what an operator access clause needs in order to be defensible under
POPIA.
"""
from datetime import timedelta

from flask import current_app, request
from werkzeug.security import check_password_hash, generate_password_hash

from app.db import get_db, now
from app.services.timezone import local_now

RECOVERY_WINDOW_MINUTES = 15
RECOVERY_MAX_FAILURES = 5


def recovery_configured():
    """True when a master password hash has been supplied by the environment."""
    return bool((current_app.config.get("RECOVERY_PASSWORD_HASH") or "").strip())


def _client_ip():
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return forwarded or request.remote_addr or ""


def record_event(outcome, user_id=None, note=""):
    db = get_db()
    db.execute(
        "INSERT INTO recovery_events (at, ip, outcome, user_id, note) VALUES (?, ?, ?, ?, ?)",
        (now(), _client_ip(), outcome, user_id, note),
    )
    db.commit()


def _failure_cutoff():
    cutoff = local_now() - timedelta(minutes=RECOVERY_WINDOW_MINUTES)
    return cutoff.isoformat(timespec="seconds")


def recent_failures():
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) AS n FROM recovery_events WHERE ip = ? AND outcome = 'failed' AND at >= ?",
        (_client_ip(), _failure_cutoff()),
    ).fetchone()
    return int((row["n"] if row is not None else 0) or 0)


def locked_out():
    """Throttle brute force: too many failed attempts from this address."""
    return recent_failures() >= RECOVERY_MAX_FAILURES


def recovery_password_matches(password):
    stored = (current_app.config.get("RECOVERY_PASSWORD_HASH") or "").strip()
    if not stored or not password:
        return False
    try:
        return check_password_hash(stored, password)
    except (ValueError, TypeError):
        return False


def main_profile():
    return get_db().execute(
        "SELECT id, name FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
    ).fetchone()


def reset_main_profile_password(new_password):
    """Set a new password on the main profile. Returns (ok, message)."""
    if not new_password or len(new_password) < 8:
        return False, "New password must be at least 8 characters"
    owner = main_profile()
    if owner is None:
        return False, "No main profile exists on this system"
    db = get_db()
    db.execute("UPDATE users SET password_hash = ?, active = 1 WHERE id = ?",
               (generate_password_hash(new_password), owner["id"]))
    db.commit()
    return True, f"Password reset for {owner['name']}. You can now sign in."
