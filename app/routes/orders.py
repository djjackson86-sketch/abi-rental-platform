from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.routes.auth import login_required
from app.db import get_db
from app.services.orders import create_order, get_order, list_orders, order_counts, order_items
from app.services.settings import get_company_settings

bp = Blueprint("orders", __name__, url_prefix="/orders")


def _customers():
    return get_db().execute("SELECT id, name, email FROM customers ORDER BY name").fetchall()


def _products():
    return get_db().execute("SELECT id, name, sku, price_amount, price_unit, quantity FROM products WHERE active = 1 ORDER BY name").fetchall()


@bp.route("")
@login_required
def index():
    query = request.args.get("query", "").strip()
    status = request.args.get("status", "")
    payment_status = request.args.get("payment_status", "")
    orders = list_orders(query=query, status=status, payment_status=payment_status)
    return render_template("admin/orders/index.html", settings=get_company_settings(), orders=orders, counts=order_counts(), filters={"query": query, "status": status, "payment_status": payment_status})


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        try:
            order_id = create_order(request.form)
            flash("Draft order created", "success")
            return redirect(url_for("orders.detail", order_id=order_id))
        except ValueError as exc:
            flash(str(exc), "error")
    return render_template("admin/orders/form.html", settings=get_company_settings(), customers=_customers(), products=_products())


@bp.route("/<int:order_id>")
@login_required
def detail(order_id):
    order = get_order(order_id)
    if not order:
        flash("Order not found", "error")
        return redirect(url_for("orders.index"))
    return render_template("admin/orders/detail.html", settings=get_company_settings(), order=order, items=order_items(order_id))
