from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.routes.auth import login_required
from app.services.products import archive_product, create_product, get_product, list_products, product_counts, update_product
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
        filters={"query": query, "product_type": product_type, "visibility": visibility},
    )


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
    return render_template("admin/inventory/form.html", settings=get_company_settings(), product=None, tax_profiles=list_tax_profiles())


@bp.route("/<int:product_id>/edit", methods=["GET", "POST"])
@login_required
def edit(product_id):
    product = get_product(product_id)
    if not product:
        flash("Product not found", "error")
        return redirect(url_for("inventory.index"))
    if request.method == "POST":
        try:
            update_product(product_id, request.form)
            flash("Product saved", "success")
            return redirect(url_for("inventory.edit", product_id=product_id))
        except ValueError as exc:
            flash(str(exc), "error")
    product = get_product(product_id)
    return render_template("admin/inventory/form.html", settings=get_company_settings(), product=product, tax_profiles=list_tax_profiles())


@bp.route("/<int:product_id>/archive", methods=["POST"])
@login_required
def archive(product_id):
    archive_product(product_id)
    flash("Product archived and hidden from store", "success")
    return redirect(url_for("inventory.index"))
