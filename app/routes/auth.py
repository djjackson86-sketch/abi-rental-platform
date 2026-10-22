from functools import wraps
from urllib.parse import urlsplit

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash
from app.db import get_db
from app.services.access import (
    login_user_options,
    session_branch_choice_options,
    user_branch_ids,
    user_module_keys_from_row,
    staff_modules_from_settings,
)
from app.services.recovery import (
    RECOVERY_WINDOW_MINUTES,
    locked_out,
    main_profile,
    record_event,
    recovery_configured,
    recovery_password_matches,
    reset_main_profile_password,
)
from app.services.settings import get_company_settings

bp = Blueprint("auth", __name__)


def _safe_next(value):
    """The local path to continue to after signing in, or ``None``.

    Only a single-slash local path is ever accepted: ``//host`` and ``/\\host`` are
    protocol-relative (browsers read the backslash as a slash), so both would leave
    the site, and a full URL is refused outright. Anything unsafe or absent is
    ``None``, which keeps the original landing behaviour untouched — a crafted link
    can never turn the sign-in form into an open redirect.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > 2048:
        return None
    # A backslash is read as a slash by browsers, so a path containing one is
    # refused rather than normalised.
    if chr(92) in candidate:
        return None
    # Control characters (a literal CR, LF, TAB or NUL) have no place in a path.
    if any(ord(char) < 32 for char in candidate):
        return None
    if not candidate.startswith("/") or candidate.startswith("//"):
        return None
    parts = urlsplit(candidate)
    if parts.scheme or parts.netloc:
        return None
    if not parts.path.startswith("/"):
        return None
    # Signing in again is not somewhere to "continue" to.
    for blocked in ("/login", "/logout"):
        if parts.path == blocked or parts.path.startswith(blocked + "/"):
            return None
    return candidate


def _requested_target():
    """The local path this request was trying to reach, for continuation after sign-in.

    Only a GET is remembered: a POST body cannot be replayed from a redirect, and
    replaying a write silently would be worse than sending the person to the
    dashboard.
    """
    if request.method != "GET":
        return None
    query = request.query_string.decode("utf-8", "replace")
    return request.path + (("?" + query) if query else "")


def _forget_push_endpoint(endpoint):
    """Stop notifying *this browser* when its session ends.

    The logout form carries the endpoint in a hidden field that push.js fills in.
    Removal is best effort: the push service owns its table, and if it exposes no
    removal helper (or nothing was passed) logout proceeds exactly as before. Only
    the endpoint that posted is removed, so the same account signed in on another
    phone keeps its notifications.
    """
    endpoint = (endpoint or "").strip()
    user_id = session.get("user_id")
    if not endpoint or not user_id:
        return
    try:
        from app.services import push_notifications as push_service
    except Exception:
        return
    remove = getattr(push_service, "unsubscribe", None)
    if not callable(remove):
        return
    try:
        remove(user_id, endpoint)
    except Exception:
        return


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("auth.login", next=_requested_target()))
        return view(*args, **kwargs)
    return wrapped


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        # Where the person was heading before the sign-in wall, if it is local.
        next_target = _safe_next(request.values.get("next"))
        # Accounts sign in by selecting their name; the session is still keyed to
        # the account id, so the form posts the selected user id, not the label.
        try:
            user_id = int(request.form.get("user_id") or 0)
        except (TypeError, ValueError):
            user_id = 0
        password = request.form.get("password", "")
        user = None
        if user_id:
            user = get_db().execute(
                "SELECT * FROM users WHERE id = ? AND active = 1", (user_id,)
            ).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            session["initials"] = user["initials"]
            session["branch_id"] = user["branch_id"]
            session["can_view_all_branches"] = bool(user["can_view_all_branches"])
            session["user_role"] = user["role"]
            if user["role"] == "owner":
                session["staff_modules"] = []
                session["branch_ids"] = []
            else:
                # An account's own module set wins; a NULL column keeps the shared
                # default, so every existing account behaves exactly as before.
                own_modules = user_module_keys_from_row(user)
                if own_modules is None:
                    session["staff_modules"] = staff_modules_from_settings(get_company_settings())
                else:
                    session["staff_modules"] = own_modules
                # The extra depots this account may see (empty = the historic
                # single branch, or every branch when they are unrestricted).
                session["branch_ids"] = user_branch_ids(user["id"])
            if session_branch_choice_options():
                # An account that manages several depots says which one it is
                # managing for this sign-in before it lands on a screen. The
                # choice can only narrow what it may already reach, and skipping
                # it simply leaves the account with all of its own depots.
                return redirect(url_for("auth.select_branch", next=next_target))
            if next_target:
                return redirect(next_target)
            return redirect(url_for("admin.dashboard"))
        flash("Invalid name or password", "error")
    return render_template(
        "login.html",
        users=login_user_options(),
        next=_safe_next(request.args.get("next")) or "",
    )


@bp.route("/select-branch", methods=["GET", "POST"])
def select_branch():
    """Pick the depot this sign-in is managing.

    Only offered to a multi-depot account and only ever lists (and accepts) the
    depots that account may already reach, so this can narrow a session but
    never widen one. Skipping it is allowed: the account then sees all of its
    own depots, exactly as before this screen existed.
    """
    if not session.get("user_id"):
        return redirect(url_for("auth.login", next=_requested_target()))
    branches = session_branch_choice_options()
    if not branches:
        return redirect(url_for("admin.dashboard"))
    allowed = [branch["id"] for branch in branches]
    # The sign-in step hands the continuation target on, so choosing a depot does
    # not lose the page the person actually asked for.
    next_target = _safe_next(request.values.get("next"))
    if request.method == "POST":
        try:
            branch_id = int(request.form.get("branch_id") or 0)
        except (TypeError, ValueError):
            branch_id = 0
        if branch_id not in allowed:
            flash("Choose one of the branches you manage", "error")
        else:
            session["active_branch_id"] = branch_id
            if next_target:
                return redirect(next_target)
            return redirect(url_for("admin.dashboard"))
    return render_template(
        "select_branch.html",
        branches=branches,
        current=session.get("active_branch_id"),
        next=next_target or "",
    )


@bp.route("/recovery", methods=["GET", "POST"])
def recovery():
    """Reset the main profile with the operator's master password.

    Deliberately outside the normal sign-in: it exists so the operator cannot be
    locked out of a client's system. It can only set a password - it is not a
    login and exposes no data.
    """
    configured = recovery_configured()
    message = ""
    category = "error"
    if request.method == "POST" and configured:
        if locked_out():
            record_event("locked", note="too many failed attempts")
            message = (f"Too many failed attempts. Try again in {RECOVERY_WINDOW_MINUTES} minutes.")
        elif not recovery_password_matches(request.form.get("master_password", "")):
            record_event("failed", note="master password rejected")
            message = "That master password is not correct."
        elif request.form.get("new_password", "") != request.form.get("confirm_password", ""):
            message = "The two new passwords do not match."
        else:
            owner = main_profile()
            ok, detail = reset_main_profile_password(request.form.get("new_password", ""))
            record_event("success" if ok else "failed",
                         user_id=owner["id"] if owner else None, note=detail)
            message = detail
            category = "success" if ok else "error"
    return render_template("recovery.html", configured=configured, message=message, category=category)


@bp.route("/logout", methods=["POST"])
def logout():
    # The hidden push_endpoint field (filled in by push.js) stops notifications for
    # this browser only - other devices stay subscribed.
    _forget_push_endpoint(request.form.get("push_endpoint", ""))
    session.clear()
    return redirect(url_for("auth.login"))
