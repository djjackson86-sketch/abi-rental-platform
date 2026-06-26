import csv
from io import StringIO

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for

from app.routes.auth import login_required
from app.services.products import archive_product, create_product, get_product, list_products, product_counts, product_filter_counts, tracking_label, update_product
from app.services.settings import get_company_settings, list_tax_profiles

bp = Blueprint("inventory", __name__, url_prefix="/inventory")


@bp.route("")
@login_required
def index():
    query = request.args.get("query", "").strip()
    product_type = request.args.get("product_type", "")
    visibility = request.args.get("visibility", "")
    products = list_products(query=query, product_type=product_type, visibility=visibility)
    return render_template(
        "admin/inventory/index.html",
        settings=get_company_settings(),
        products=products,
        counts=product_counts(),
        filter_counts=product_filter_counts(),
        filters={"query": query, "product_type": product_type, "visibility": visibility},
        tracking_label=tracking_label,
    )


@bp.route("/export.csv")
@login_required
def export_csv():
    query = request.args.get("query", "").strip()
    product_type = request.args.get("product_type", "")
    visibility = request.args.get("visibility", "")
    products = list_products(query=query, product_type=product_type, visibility=visibility)
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["name", "sku", "product_type", "price_amount", "price_unit", "quantity", "security_deposit", "active", "public_visible"])
    for product in products:
        writer.writerow([
            product["name"],
            product["sku"],
            product["product_type"],
            product["price_amount"],
            product["price_unit"],
            product["quantity"],
            product["security_deposit"],
            product["active"],
            product["public_visible"],
        ])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=products.csv"})


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        try:
            product_id = create_product(request.form)
            flash("Product created", "success")
            return redirect(url_for("inventory.edit", product_id=product_id))
        except ValueError as exc:
            flash(str(exc), "error")
    return render_template("admin/inventory/form.html", settings=get_company_settings(), product=None, tax_profiles=list_tax_profiles(), tracking_label=tracking_label)


@bp.route("/<int:product_id>/edit", methods=["GET", "POST"])
@login_required
def edit(product_id):
    product = get_product(product_id)
    if not product:
        flash("Product not found", "error")
        return redirect(url_for("inventory.index"))
    if request.method == "POST":
        try:
            immutable_change_requested = update_product(product_id, request.form)
            flash("Product saved", "success")
            if immutable_change_requested:
                flash("Product type and tracking method cannot be changed after saving", "info")
            return redirect(url_for("inventory.edit", product_id=product_id))
        except ValueError as exc:
            flash(str(exc), "error")
    product = get_product(product_id)
    return render_template("admin/inventory/form.html", settings=get_company_settings(), product=product, tax_profiles=list_tax_profiles(), tracking_label=tracking_label)


@bp.route("/<int:product_id>/archive", methods=["POST"])
@login_required
def archive(product_id):
    archive_product(product_id)
    flash("Product archived and hidden from store", "success")
    return redirect(url_for("inventory.index"))
