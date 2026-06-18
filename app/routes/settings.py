from flask import Blueprint, flash, redirect, render_template, request, url_for
from app.routes.auth import login_required
from app.services.settings import get_company_settings, update_company_settings, list_tax_profiles, create_tax_profile, list_operating_hours

bp = Blueprint("settings", __name__, url_prefix="/settings")


@bp.route("/general", methods=["GET", "POST"])
@login_required
def general():
    if request.method == "POST":
        update_company_settings(request.form)
        flash("Settings saved", "success")
        return redirect(url_for("settings.general"))
    return render_template("admin/settings/general.html", settings=get_company_settings())


@bp.route("/taxes", methods=["GET", "POST"])
@login_required
def taxes():
    if request.method == "POST":
        create_tax_profile(request.form.get("name", "Tax profile"), request.form.get("rate", 0), bool(request.form.get("is_default")))
        flash("Tax profile added", "success")
        return redirect(url_for("settings.taxes"))
    return render_template("admin/settings/taxes.html", settings=get_company_settings(), tax_profiles=list_tax_profiles())


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
