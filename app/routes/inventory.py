import csv
from io import StringIO

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from werkzeug.datastructures import MultiDict

from app.routes.auth import login_required
from app.services.products import (
    archive_product,
    create_product,
    create_product_group,
    get_product,
    get_product_group,
    group_products_for_display,
    list_product_groups,
    list_products,
    product_counts,
    product_filter_counts,
    tracking_label,
    update_product,
    update_product_group,
)
from app.services.settings import get_company_settings, list_tax_profiles
from app.services.branches import branch_options
from app.services.access import session_branch_scope

bp = Blueprint("inventory", __name__, url_prefix="/inventory")


def _scoped_branch_options():
    branch_id = session_branch_scope()
    branches = branch_options()
    if not branch_id:
        return branches
    return [branch for branch in branches if branch["id"] == branch_id]


def _force_staff_product_branch(form):
    branch_id = session_branch_scope()
    if not branch_id:
        return form
    mutable = MultiDict(form)
    mutable["branch_id"] = str(branch_id)
    return mutable


def _ensure_product_access(product):
    branch_id = session_branch_scope()
    if branch_id and product and product["branch_id"] not in (None, branch_id):
        abort(404)


@bp.route("")
@login_required
def index():
    query = request.args.get("query", "").strip()
    product_type = request.args.get("product_type", "")
    visibility = request.args.get("visibility", "")
    product_group_id = request.args.get("product_group_id", "")
    products = list_products(query=query, product_type=product_type, visibility=visibility, product_group_id=product_group_id)
    return render_template(
        "admin/inventory/index.html",
        settings=get_company_settings(),
        products=products,
        grouped_products=group_products_for_display(products),
        product_groups=list_product_groups(),
        counts=product_counts(),
        filter_counts=product_filter_counts(),
        filters={"query": query, "product_type": product_type, "visibility": visibility, "product_group_id": product_group_id},
        tracking_label=tracking_label,
    )


@bp.route("/groups")
@login_required
def groups():
    return render_template("admin/inventory/group_form.html", settings=get_company_settings(), group=None, groups=list_product_groups())


@bp.route("/groups/new", methods=["GET", "POST"])
@login_required
def new_group():
    if request.method == "POST":
        try:
            group_id = create_product_group(request.form)
            flash("Product group created", "success")
            return redirect(url_for("inventory.edit_group", group_id=group_id))
        except ValueError as exc:
            flash(str(exc), "error")
    return render_template("admin/inventory/group_form.html", settings=get_company_settings(), group=None, groups=list_product_groups())


@bp.route("/groups/<int:group_id>/edit", methods=["GET", "POST"])
@login_required
def edit_group(group_id):
    group = get_product_group(group_id)
    if not group:
        flash("Product group not found", "error")
        return redirect(url_for("inventory.index"))
    if request.method == "POST":
        try:
            update_product_group(group_id, request.form)
            flash("Product group saved", "success")
            return redirect(url_for("inventory.edit_group", group_id=group_id))
        except ValueError as exc:
            flash(str(exc), "error")
    group = get_product_group(group_id)
    return render_template("admin/inventory/group_form.html", settings=get_company_settings(), group=group, groups=list_product_groups())


@bp.route("/export.csv")
@login_required
def export_csv():
    query = request.args.get("query", "").strip()
    product_type = request.args.get("product_type", "")
    visibility = request.args.get("visibility", "")
    product_group_id = request.args.get("product_group_id", "")
    products = list_products(query=query, product_type=product_type, visibility=visibility, product_group_id=product_group_id)
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["group", "name", "sku", "product_type", "branch", "price_amount", "price_unit", "quantity", "security_deposit", "hourly_extra_rate", "active", "public_visible"])
    for product in products:
        writer.writerow([
            product["product_group_name"] or "Ungrouped products",
            product["name"],
            product["sku"],
            product["product_type"],
            product["branch_name"] or "Unassigned",
            product["price_amount"],
            product["price_unit"],
            product["quantity"],
            product["security_deposit"],
            product["hourly_extra_rate"],
            product["active"],
            product["public_visible"],
        ])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=products.csv"})


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        try:
            product_id = create_product(_force_staff_product_branch(request.form))
            flash("Product created", "success")
            return redirect(url_for("inventory.edit", product_id=product_id))
        except ValueError as exc:
            flash(str(exc), "error")
    return render_template(
        "admin/inventory/form.html",
        settings=get_company_settings(),
        product=None,
        tax_profiles=list_tax_profiles(),
        branches=_scoped_branch_options(),
        product_groups=list_product_groups(include_inactive=False),
        tracking_label=tracking_label,
    )


@bp.route("/<int:product_id>/edit", methods=["GET", "POST"])
@login_required
def edit(product_id):
    product = get_product(product_id)
    if not product:
        flash("Product not found", "error")
        return redirect(url_for("inventory.index"))
    _ensure_product_access(product)
    if request.method == "POST":
        try:
            immutable_change_requested = update_product(product_id, _force_staff_product_branch(request.form))
            flash("Product saved", "success")
            if immutable_change_requested:
                flash("Product type and tracking method cannot be changed after saving", "info")
            return redirect(url_for("inventory.edit", product_id=product_id))
        except ValueError as exc:
            flash(str(exc), "error")
    product = get_product(product_id)
    return render_template(
        "admin/inventory/form.html",
        settings=get_company_settings(),
        product=product,
        tax_profiles=list_tax_profiles(),
        branches=_scoped_branch_options(),
        product_groups=list_product_groups(include_inactive=False),
        tracking_label=tracking_label,
    )


@bp.route("/<int:product_id>/archive", methods=["POST"])
@login_required
def archive(product_id):
    product = get_product(product_id)
    _ensure_product_access(product)
    archive_product(product_id)
    flash("Product archived and hidden from store", "success")
    return redirect(url_for("inventory.index"))
