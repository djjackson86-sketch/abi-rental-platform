from functools import wraps
from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash
from app.db import get_db
from app.services.access import login_user_options, staff_modules_from_settings
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


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)
    return wrapped


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
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
            else:
                session["staff_modules"] = staff_modules_from_settings(get_company_settings())
            return redirect(url_for("admin.dashboard"))
        flash("Invalid name or password", "error")
    return render_template("login.html", users=login_user_options())


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
    session.clear()
    return redirect(url_for("auth.login"))
