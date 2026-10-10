from flask import Blueprint, abort, flash, make_response, redirect, render_template, request, url_for

from app.db import get_db
from app.services import group_images
from app.services.customers import create_customer
from app.services.orders import _build_order_payload, create_order, get_order, order_items
from app.services.settings import get_company_settings

bp = Blueprint("public", __name__)
CATEGORY_IMAGE_MAX_AGE_SECONDS = 3600


def _public_products():
    # Ticket ABI-341953038(3): a trailer flagged "Trailer under maintenance" is
    # not offered in the online store until the flag is released. It stays fully
    # visible in the back office inventory.
    return get_db().execute(
        "SELECT * FROM products WHERE active = 1 AND public_visible = 1 "
        "AND COALESCE(under_maintenance, 0) = 0 ORDER BY name"
    ).fetchall()


def _public_product(product_id):
    return get_db().execute(
        "SELECT * FROM products WHERE id = ? AND active = 1 AND public_visible = 1 "
        "AND COALESCE(under_maintenance, 0) = 0",
        (product_id,),
    ).fetchone()


def _store_sections():
    db = get_db()
    groups = db.execute(
        "SELECT * FROM product_groups WHERE active = 1 AND becomes_store_visible = 1 "
        "ORDER BY sort_order ASC, name ASC"
    ).fetchall()
    visible_group_ids = {group["id"] for group in groups}
    by_group = {}
    other = []
    for product in _public_products():
        if product["product_group_id"] is not None and product["product_group_id"] in visible_group_ids:
            by_group.setdefault(product["product_group_id"], []).append(product)
        else:
            other.append(product)
    sections = []
    for group in groups:
        products = by_group.get(group["id"], [])
        if not products:
            continue
        sections.append({
            "id": group["id"],
            "name": group["name"],
            "description": group["description"],
            "has_image": group["image_blob"] is not None,
            "products": products,
        })
    if other:
        sections.append({"id": None, "name": "Other", "description": "", "has_image": False, "products": other})
    return sections


@bp.route("/store")
def store():
    settings = get_company_settings()
    if not settings["store_enabled"]:
        return render_template("public/store_unavailable.html", settings=settings)
    return render_template("public/store.html", settings=settings, sections=_store_sections())


@bp.route("/store/category-image/<int:group_id>")
def category_image(group_id):
    data, mime = group_images.group_image_bytes(group_id)
    if data is None:
        abort(404)
    response = make_response(data)
    response.headers["Content-Type"] = mime
    response.headers["Cache-Control"] = f"public, max-age={CATEGORY_IMAGE_MAX_AGE_SECONDS}"
    return response


@bp.route("/store/products/<int:product_id>")
def product_detail(product_id):
    settings = get_company_settings()
    if not settings["store_enabled"]:
        return render_template("public/store_unavailable.html", settings=settings)
    product = _public_product(product_id)
    if not product:
        flash("Product not found", "error")
        return redirect(url_for("public.store"))
    return render_template("public/product.html", settings=settings, product=product)


@bp.post("/store/products/<int:product_id>/book")
def book_product(product_id):
    settings = get_company_settings()
    if not settings["store_enabled"]:
        return render_template("public/store_unavailable.html", settings=settings), 403
    product = _public_product(product_id)
    if not product:
        flash("Product not found", "error")
        return redirect(url_for("public.store"))
    customer_name = request.form.get("customer_name", "").strip()
    customer_email = request.form.get("customer_email", "").strip().lower()
    if not customer_name or not customer_email:
        flash("Name and email are required", "error")
        return render_template("public/product.html", settings=get_company_settings(), product=product), 400
    order_form = {
        "product_id": str(product_id),
        "quantity": request.form.get("quantity", "1"),
        "start_date": request.form.get("start_date", ""),
        "start_time": request.form.get("start_time", ""),
        "end_date": request.form.get("end_date", ""),
        "end_time": request.form.get("end_time", ""),
        "notes": f"Public booking request. {request.form.get('notes', '').strip()}".strip(),
    }
    try:
        # Validate the booking before the customer row is created, so a refused
        # request (e.g. a pickup outside the branch's trading hours) leaves no
        # stray customer behind.
        _build_order_payload(order_form)
        customer_id = create_customer({
            "customer_type": "individual",
            "name": customer_name,
            "email": customer_email,
            "phone": request.form.get("customer_phone", ""),
            "marketing_opt_in": request.form.get("marketing_opt_in", ""),
        })
        order_form["customer_id"] = str(customer_id)
        order_id = create_order(order_form)
    except ValueError as exc:
        flash(str(exc), "error")
        return render_template("public/product.html", settings=get_company_settings(), product=product), 400
    return redirect(url_for("public.booking_confirmation", order_id=order_id))


@bp.route("/store/booking/<int:order_id>")
def booking_confirmation(order_id):
    order = get_order(order_id)
    if not order:
        flash("Booking request not found", "error")
        return redirect(url_for("public.store"))
    return render_template("public/confirmation.html", settings=get_company_settings(), order=order, items=order_items(order_id))
