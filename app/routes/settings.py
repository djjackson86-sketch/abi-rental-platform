from functools import wraps
from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from app.routes.auth import login_required
from app.services.access import (
    ADDITIONAL_USER_LIMIT,
    MODULES,
    additional_user_count,
    change_own_password,
    clear_user_modules,
    create_additional_user,
    delete_additional_user,
    list_users,
    reset_user_password,
    save_staff_modules,
    save_user_modules,
    set_user_active,
    staff_modules_from_settings,
    update_user_branches,
    user_branch_map,
    user_module_assignments,
)
from app.services.settings import (
    get_company_settings, update_company_settings, list_tax_profiles, create_tax_profile,
    list_operating_hours, global_vat_rate, update_vat_settings,
)
from app.services.branches import branch_options
from app.services.branches import get_branch
from app.services import popia_pack, portal, portal_intake

bp = Blueprint("settings", __name__, url_prefix="/settings")


def main_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("user_role") != "owner":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@bp.route("/general", methods=["GET", "POST"])
@login_required
def general():
    if request.method == "POST":
        update_company_settings(request.form)
        flash("Settings saved", "success")
        return redirect(url_for("settings.general"))
    return render_template("admin/settings/general.html", settings=get_company_settings())


@bp.route("/users", methods=["GET"])
@login_required
@main_required
def users():
    settings = get_company_settings()
    accounts = list_users()
    shared_modules = staff_modules_from_settings(settings)
    return render_template(
        "admin/settings/users.html",
        settings=settings,
        users=accounts,
        additional_count=additional_user_count(),
        additional_limit=ADDITIONAL_USER_LIMIT,
        modules=MODULES,
        active_staff_modules=shared_modules,
        # Per-account ticks: {'own': bool, 'keys': [...]} keyed by account id.
        user_modules=user_module_assignments(accounts, shared_modules),
        branch_map=user_branch_map(accounts),
        branches=branch_options(),
    )


@bp.post("/users/add")
@login_required
@main_required
def users_add():
    branch_ids = request.form.getlist("branch_ids")
    user_id, error = create_additional_user(
        request.form.get("name"),
        request.form.get("password"),
        request.form.get("branch_id"),
        branch_ids=branch_ids,
    )
    if error:
        flash(error, "error")
    else:
        flash("Additional account created", "success")
    return redirect(url_for("settings.users"))


@bp.post("/users/me/password")
@login_required
@main_required
def users_own_password():
    """The main profile changing its own password - no other helper touches role 'owner'."""
    error = change_own_password(
        session.get("user_id"),
        request.form.get("current_password"),
        request.form.get("password"),
    )
    if error:
        flash(error, "error")
    else:
        flash("Your password has been updated", "success")
    return redirect(url_for("settings.users"))


@bp.post("/users/<int:user_id>/password")
@login_required
@main_required
def users_password(user_id):
    error = reset_user_password(user_id, request.form.get("password"))
    if error:
        flash(error, "error")
    else:
        flash("Password updated", "success")
    return redirect(url_for("settings.users"))


@bp.post("/users/<int:user_id>/branch")
@login_required
@main_required
def users_branch(user_id):
    """Set an account's branch access: one depot, several depots, or all.

    Ticking nothing means all branches. ``branches_edited`` marks the multi-tick
    form, so a stale single ``branch_id`` field cannot resurrect a branch the
    admin just unticked.
    """
    submitted = request.form.getlist("branch_ids")
    if not submitted and request.form.get("branches_edited") != "1":
        single = (request.form.get("branch_id") or "").strip()
        submitted = [single] if single else []
    if not update_user_branches(user_id, submitted):
        flash("Main profile always has access to all branches", "error")
    else:
        flash("Account branch access updated. It applies on the next sign-in.", "success")
    return redirect(url_for("settings.users"))


@bp.post("/users/<int:user_id>/modules")
@login_required
@main_required
def users_modules(user_id):
    """Give one additional account its own module set (editable after it exists)."""
    ok, detail = save_user_modules(user_id, request.form.getlist("module"))
    if not ok:
        flash(str(detail), "error")
    else:
        flash("Account modules saved. They apply on the next sign-in.", "success")
    return redirect(url_for("settings.users"))


@bp.post("/users/<int:user_id>/modules/reset")
@login_required
@main_required
def users_modules_reset(user_id):
    """Return an account to the shared default module set."""
    if not clear_user_modules(user_id):
        flash("The main profile always has every function", "error")
    else:
        flash("Account now uses the shared default modules", "success")
    return redirect(url_for("settings.users"))


@bp.post("/users/<int:user_id>/active")
@login_required
@main_required
def users_active(user_id):
    if not set_user_active(user_id, request.form.get("active") == "1"):
        flash("Main profile cannot be changed", "error")
    else:
        flash("Account updated", "success")
    return redirect(url_for("settings.users"))


@bp.post("/users/<int:user_id>/delete")
@login_required
@main_required
def users_delete(user_id):
    if not delete_additional_user(user_id):
        flash("Main profile cannot be deleted", "error")
    else:
        flash("Account deleted", "success")
    return redirect(url_for("settings.users"))


@bp.post("/users/permissions")
@login_required
@main_required
def users_permissions():
    save_staff_modules(request.form.getlist("module"))
    flash("Additional account permissions saved. They apply on the next sign-in.", "success")
    return redirect(url_for("settings.users"))


@bp.route("/taxes", methods=["GET", "POST"])
@login_required
def taxes():
    if request.method == "POST":
        update_vat_settings(request.form)
        flash("VAT settings saved", "success")
        return redirect(url_for("settings.taxes"))
    return render_template("admin/settings/taxes.html", settings=get_company_settings(),
                           vat_rate=global_vat_rate())


@bp.route("/pricing", methods=["GET", "POST"])
@login_required
def pricing():
    if request.method == "POST":
        update_company_settings(request.form)
        flash("Pricing settings saved", "success")
        return redirect(url_for("settings.pricing"))
    return render_template("admin/settings/pricing.html", settings=get_company_settings())


@bp.route("/rental-period", methods=["GET", "POST"])
@login_required
def rental_period():
    if request.method == "POST":
        update_company_settings(request.form)
        flash("Rental period settings saved", "success")
        return redirect(url_for("settings.rental_period"))
    return render_template("admin/settings/rental_period.html", settings=get_company_settings(), hours=list_operating_hours())


# --- Customer portal: the per-branch link, the QR sheet and the on/off switch (feature B §B3) ---
#
# §B1 added `public_slug` / `portal_enabled` / `portal_intro` to `branches` and §B2 built the form
# those settings point at, but neither shipped a screen for them — a slug could only be changed
# with a database client. These three routes are that screen: the list (one card per branch), the
# printable A4 sheet, and the save. Gated on the `settings` module (see `access.py`) so the main
# profile ticks it per account like every other settings area.


def _portal_page_context():
    """Everything the portal pages need, so the three routes cannot disagree about the state.

    The POPIA half is read from the *document* (decision D11), not from a second copy of the truth:
    while the reviewed notice still carries Sano's open facts the public form refuses every
    submission, and the branch staff who hand out a QR must be able to see that here.

    The page shows **how many** facts are still open, not which ones: a token quoted onto a page is
    the very thing the D11 gate exists to stop, and the first draft of this tick proved the point —
    stripping the brackets off still printed ``our branches at ____ are covered by CCTV`` and
    ``TO CONFIRM — the public web address`` on the counter sheet. The list itself lives in the
    privacy-notice review, which is the main profile's to work through, not a branch's.
    """
    registration_open = portal_intake.registration_is_open()
    return {
        "links": portal.all_portal_links(request.url_root),
        "registration_open": registration_open,
        "outstanding_count": (
            0
            if registration_open
            else len(popia_pack.outstanding_fields(popia_pack.PRIVACY_NOTICE_KEY))
        ),
        "intro_limit": portal.MAX_PORTAL_INTRO_CHARS,
    }


@bp.route("/portal")
@login_required
def portal_index():
    """Every branch's customer link, QR preview and portal switch on one page."""
    return render_template("admin/portal_index.html", **_portal_page_context())


def _render_portal_index(status):
    """Re-render the list after a refused save, with that HTTP status rather than a redirect.

    Returning 409/400 keeps the refusal honest (the client skipped a redirect *and* a 200) while
    the flash carries the reason and the form keeps what was typed, which is what a person behind
    the counter needs.
    """
    return render_template("admin/portal_index.html", **_portal_page_context()), status


@bp.post("/portal/<int:branch_id>")
@login_required
def portal_save(branch_id):
    """Save one branch's portal settings: the slug, the on/off switch and the welcome line."""
    if get_branch(branch_id) is None:
        abort(404)
    try:
        result = portal.update_portal_settings(branch_id, request.form)
    except portal.DuplicateSlugError as exc:
        flash(str(exc), "error")
        return _render_portal_index(409)
    except ValueError as exc:
        flash(str(exc), "error")
        return _render_portal_index(400)

    name = result["name"]
    messages = []
    if result["slug_changed"]:
        suffix = " (saved in lower case with dashes)" if result["slug_normalised"] else ""
        messages.append(f"Link updated to /portal/{result['slug']}{suffix}.")
    if result["enabled_changed"]:
        if result["enabled"]:
            messages.append(f"{name}'s portal is live again — its link and QR work as printed.")
        else:
            messages.append(
                f"{name}'s portal is switched off, so its link and QR now return \"page not found\"."
            )
    if result["intro_changed"]:
        messages.append("Welcome line updated.")
    if not messages:
        messages.append(f"{name}'s portal settings are unchanged.")
    flash(" ".join(messages), "success" if result["enabled"] else "error")
    return redirect(url_for("settings.portal_index"))


@bp.route("/portal/<int:branch_id>/print")
@login_required
def portal_print(branch_id):
    """The A4 sheet a branch prints and puts at the counter.

    A branch whose portal is switched off is refused rather than served: its QR returns 404 to the
    customer (decision D7's shape — one switch decides both), so printing it would put a dead code
    in someone's hand. The refusal is a flash plus the list page, so staff see the switch that
    needs flipping instead of a bare error.
    """
    branch = get_branch(branch_id)
    if branch is None:
        abort(404)
    if not (branch["portal_enabled"] and branch["active"]):
        flash(
            f"{branch['name']}'s portal is switched off, so its QR does not work yet — "
            "turn it on before printing a sheet.",
            "error",
        )
        return redirect(url_for("settings.portal_index"))
    context = _portal_page_context()
    return render_template(
        "admin/portal_print.html",
        branch=branch,
        address=portal.branch_address(branch),
        portal_url=portal.portal_url(branch, request.url_root),
        qr_box=portal.QR_PRINT_BOX_SIZE_PX,
        registration_open=context["registration_open"],
        outstanding_count=context["outstanding_count"],
    )
