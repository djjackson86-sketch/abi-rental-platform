from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.db import get_db
from app.services.customers import create_customer
from app.services.orders import create_order, get_order, order_items
from app.services.settings import get_company_settings

bp = Blueprint("public", __name__)


def _public_products():
    return get_db().execute("SELECT * FROM products WHERE active = 1 AND public_visible = 1 ORDER BY name").fetchall()


def _public_product(product_id):
    return get_db().execute("SELECT * FROM products WHERE id = ? AND active = 1 AND public_visible = 1", (product_id,)).fetchone()


@bp.route("/store")
def store():
    products = _public_products()
    return render_template("public/store.html", settings=get_company_settings(), products=products)


@bp.route("/store/products/<int:product_id>")
def product_detail(product_id):
    product = _public_product(product_id)
    if not product:
        flash("Product not found", "error")
        return redirect(url_for("public.store"))
    return render_template("public/product.html", settings=get_company_settings(), product=product)


@bp.post("/store/products/<int:product_id>/book")
def book_product(product_id):
    product = _public_product(product_id)
    if not product:
        flash("Product not found", "error")
        return redirect(url_for("public.store"))
    customer_name = request.form.get("customer_name", "").strip()
    customer_email = request.form.get("customer_email", "").strip().lower()
    if not customer_name or not customer_email:
        flash("Name and email are required", "error")
        return render_template("public/product.html", settings=get_company_settings(), product=product), 400
    try:
        customer_id = create_customer({
            "customer_type": "individual",
            "name": customer_name,
            "email": customer_email,
            "phone": request.form.get("customer_phone", ""),
            "marketing_opt_in": request.form.get("marketing_opt_in", ""),
        })
        order_id = create_order({
            "customer_id": str(customer_id),
            "product_id": str(product_id),
            "quantity": request.form.get("quantity", "1"),
            "start_date": request.form.get("start_date", ""),
            "start_time": request.form.get("start_time", ""),
            "end_date": request.form.get("end_date", ""),
            "end_time": request.form.get("end_time", ""),
            "notes": f"Public booking request. {request.form.get('notes', '').strip()}".strip(),
        })
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
