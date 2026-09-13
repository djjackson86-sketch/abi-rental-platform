import csv
from io import StringIO

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from werkzeug.datastructures import MultiDict

from app.routes.auth import login_required
from app.services.products import (
    archive_product,
    create_product,
    create_product_group,
    delete_product,
    duplicate_product,
    format_stock_breakdown,
    get_product,
    get_product_group,
    group_products_for_display,
    list_product_groups,
    list_products,
    product_branch_stock,
    product_counts,
    product_filter_counts,
    product_has_order_history,
    stock_breakdown_rows,
    tracking_label,
    update_product,
    update_product_group,
)
from app.services.settings import get_company_settings, global_vat_rate, list_tax_profiles
from app.services.branches import branch_options
from app.services.access import main_required, resolve_branch_filter, session_branch_scope_ids, session_primary_branch_id

bp = Blueprint("inventory", __name__, url_prefix="/inventory")


def _scoped_branch_options():
    scope_ids = session_branch_scope_ids()
    branches = branch_options()
    if scope_ids is None:
        return branches
    return [branch for branch in branches if branch["id"] in scope_ids]


def _force_staff_product_branch(form):
    """Keep a branch-limited session inside its own depots (server-side)."""
    scope_ids = session_branch_scope_ids()
    if not scope_ids:
        return form
    mutable = MultiDict(form)
    try:
        submitted = int(mutable.get("branch_id") or 0)
    except (TypeError, ValueError):
        submitted = 0
    primary = session_primary_branch_id()
    chosen = submitted if submitted in scope_ids else (primary if primary in scope_ids else scope_ids[0])
    mutable["branch_id"] = str(chosen)
    return mutable


def _ensure_product_access(product):
    scope_ids = session_branch_scope_ids()
    if not scope_ids or not product:
        return
    current = product["branch_id"]
    if current is None:
        # Unassigned stock is visible to every branch.
        return
    try:
        current = int(current)
    except (TypeError, ValueError):
        return
    if current not in scope_ids:
        abort(404)


@bp.route("")
@login_required
def index():
    query = request.args.get("query", "").strip()
    product_type = request.args.get("product_type", "")
    visibility = request.args.get("visibility", "")
    product_group_id = request.args.get("product_group_id", "")
    # Branch filter. The shared resolver keeps the session scope authoritative, so
    # a ?branch= outside it is ignored (and a single-depot account is pinned).
    selected_branch, branch_id, branch_label, branches, branch_scope = resolve_branch_filter(request.args.get("branch", ""))
    products = list_products(query=query, product_type=product_type, visibility=visibility,
                             product_group_id=product_group_id, branch_id=branch_id)
    breakdown = stock_breakdown_rows([product["id"] for product in products])
    return render_template(
        "admin/inventory/index.html",
        settings=get_company_settings(),
        products=products,
        grouped_products=group_products_for_display(products),
        product_groups=list_product_groups(),
        counts=product_counts(),
        filter_counts=product_filter_counts(),
        branches=branches,
        branch_scope=branch_scope,
        branch_label=branch_label,
        filters={"query": query, "product_type": product_type, "visibility": visibility, "product_group_id": product_group_id, "branch": selected_branch},
        tracking_label=tracking_label,
        stock_breakdown={pid: format_stock_breakdown(rows) for pid, rows in breakdown.items()},
        branch_split={pid for pid, rows in breakdown.items() if len(rows) > 1},
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
    # Same resolver as the list, so the file matches the view exactly.
    _selected_branch, branch_id, _label, _branches, _scope = resolve_branch_filter(request.args.get("branch", ""))
    products = list_products(query=query, product_type=product_type, visibility=visibility,
                             product_group_id=product_group_id, branch_id=branch_id)
    breakdown = stock_breakdown_rows([product["id"] for product in products])
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["group", "name", "sku", "product_type", "branch", "price_amount", "price_unit", "quantity", "stock_by_branch", "security_deposit", "hourly_extra_rate", "active", "public_visible"])
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
            format_stock_breakdown(breakdown.get(product["id"])),
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
        global_vat_rate=global_vat_rate(),
        branches=_scoped_branch_options(),
        product_groups=list_product_groups(include_inactive=False),
        tracking_label=tracking_label,
        branch_stock={},
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
            blocked_change = update_product(product_id, _force_staff_product_branch(request.form))
            flash("Product saved", "success")
            if blocked_change:
                flash(
                    "Product type and tracking method were not changed: this product has been used on an order",
                    "info",
                )
            return redirect(url_for("inventory.edit", product_id=product_id))
        except ValueError as exc:
            flash(str(exc), "error")
    product = get_product(product_id)
    return render_template(
        "admin/inventory/form.html",
        settings=get_company_settings(),
        product=product,
        product_used_on_orders=product_has_order_history(product["id"]),
        tax_profiles=list_tax_profiles(),
        global_vat_rate=global_vat_rate(),
        branches=_scoped_branch_options(),
        product_groups=list_product_groups(include_inactive=False),
        tracking_label=tracking_label,
        branch_stock=product_branch_stock(product_id),
    )


@bp.route("/<int:product_id>/archive", methods=["POST"])
@login_required
def archive(product_id):
    product = get_product(product_id)
    _ensure_product_access(product)
    archive_product(product_id)
    flash("Product archived and hidden from store", "success")
    return redirect(url_for("inventory.index"))


@bp.route("/<int:product_id>/duplicate", methods=["POST"])
@login_required
def duplicate(product_id):
    """Copy a product into a new row (ABI-341952945).

    Additive: the source product is untouched and the copy is created with a
    blank SKU, so there is no risk of an identifier or an order history being
    cloned. The ``inventory.`` blueprint prefix already gates this to the
    Inventory module; a crafted POST without it is refused by that gate.
    """
    product = get_product(product_id)
    if not product:
        flash("Product not found", "error")
        return redirect(url_for("inventory.index"))
    _ensure_product_access(product)
    try:
        new_id = duplicate_product(product_id)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("inventory.edit", product_id=product_id))
    flash("Product duplicated", "success")
    return redirect(url_for("inventory.edit", product_id=new_id))


@bp.route("/<int:product_id>/delete", methods=["POST"])
@login_required
@main_required
def delete(product_id):
    """Permanently remove an unused product. Main profile only.

    Products that an order already uses must be archived instead — the service
    refuses them, and the guard is server-side so a crafted POST cannot bypass a
    hidden button.
    """
    product = get_product(product_id)
    if not product:
        flash("Product not found", "error")
        return redirect(url_for("inventory.index"))
    _ensure_product_access(product)
    try:
        delete_product(product_id)
        flash("Product deleted permanently", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("inventory.index"))
