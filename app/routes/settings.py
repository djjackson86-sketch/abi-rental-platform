from functools import wraps
from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from app.routes.auth import login_required
from app.services.access import (
    ADDITIONAL_USER_LIMIT,
    MODULES,
    additional_user_count,
    create_additional_user,
    delete_additional_user,
    list_users,
    reset_user_password,
    save_staff_modules,
    update_user_branch,
    set_user_active,
    staff_modules_from_settings,
)
from app.services.settings import (
    get_company_settings, update_company_settings, list_tax_profiles, create_tax_profile,
    list_operating_hours, global_vat_rate, update_vat_settings,
)
from app.services.branches import branch_options

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
    return render_template(
        "admin/settings/users.html",
        settings=settings,
        users=list_users(),
        additional_count=additional_user_count(),
        additional_limit=ADDITIONAL_USER_LIMIT,
        modules=MODULES,
        active_staff_modules=staff_modules_from_settings(settings),
        branches=branch_options(),
    )


@bp.post("/users/add")
@login_required
@main_required
def users_add():
    user_id, error = create_additional_user(
        request.form.get("name"),
        request.form.get("password"),
        request.form.get("branch_id"),
    )
    if error:
        flash(error, "error")
    else:
        flash("Additional account created", "success")
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
    if not update_user_branch(user_id, request.form.get("branch_id")):
        flash("Main profile always has access to all branches", "error")
    else:
        flash("Account branch access updated. It applies on the next sign-in.", "success")
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
