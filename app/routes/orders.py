from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.routes.auth import login_required
from app.db import get_db
from app.services.orders import create_order, get_order, list_orders, order_counts, order_filter_counts, order_items, next_time_slot, status_actions, transition_order
from app.services.documents import create_document, documents_for_order, document_type_options, label_for
from app.services.payments import label_for as payment_label_for, payment_summary, payments_for_order, record_payment
from app.services.settings import get_company_settings
from app.services.coupons import list_coupons
from app.services.customers import create_customer
from app.services.branches import branch_options, default_branch_id

bp = Blueprint("orders", __name__, url_prefix="/orders")


def _customers():
    return get_db().execute("SELECT id, name, email FROM customers ORDER BY name").fetchall()


def _products():
    return get_db().execute("SELECT p.id, p.name, p.sku, p.price_amount, p.price_unit, p.quantity, p.branch_id, b.name AS branch_name FROM products p LEFT JOIN branches b ON b.id = p.branch_id WHERE p.active = 1 ORDER BY p.name").fetchall()


@bp.route("")
@login_required
def index():
    query = request.args.get("query", "").strip()
    status = request.args.get("status", "")
    payment_status = request.args.get("payment_status", "")
    orders = list_orders(query=query, status=status, payment_status=payment_status)
    return render_template(
        "admin/orders/index.html",
        settings=get_company_settings(),
        orders=orders,
        counts=order_counts(),
        filter_counts=order_filter_counts(),
        filters={"query": query, "status": status, "payment_status": payment_status},
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    settings = get_company_settings()
    selected_customer_id = request.args.get("customer_id", "")
    if request.method == "POST":
        if request.form.get("order_action") == "create_customer_continue":
            try:
                customer_id = create_customer(request.form)
                flash("Customer created — continue the order", "success")
                return redirect(url_for("orders.new", customer_id=customer_id))
            except ValueError as exc:
                flash(str(exc), "error")
        else:
            try:
                order_id = create_order(request.form)
                flash("Draft order created", "success")
                return redirect(url_for("orders.detail", order_id=order_id))
            except ValueError as exc:
                flash(str(exc), "error")
    slot = next_time_slot(increment_minutes=15)
    return render_template("admin/orders/form.html", settings=settings, customers=_customers(), products=_products(), coupons=list_coupons(status="active"), selected_customer_id=selected_customer_id, default_start_date=slot.date().isoformat(), default_start_time=slot.strftime("%H:%M"), branches=branch_options(), default_branch_id=default_branch_id())


@bp.route("/<int:order_id>")
@login_required
def detail(order_id):
    order = get_order(order_id)
    if not order:
        flash("Order not found", "error")
        return redirect(url_for("orders.index"))
    return render_template(
        "admin/orders/detail.html",
        settings=get_company_settings(),
        order=order,
        items=order_items(order_id),
        actions=status_actions(order["status"]),
        documents=documents_for_order(order_id),
        document_types=document_type_options(),
        label_for=label_for,
        payments=payments_for_order(order_id),
        payment_summary=payment_summary(order_id),
        payment_label_for=payment_label_for,
    )


@bp.post("/<int:order_id>/payments")
@login_required
def record_order_payment(order_id):
    try:
        record_payment(order_id, request.form)
        flash("Payment recorded", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("orders.detail", order_id=order_id))


@bp.post("/<int:order_id>/documents")
@login_required
def create_document_for_order(order_id):
    try:
        document_id = create_document(order_id, request.form.get("document_type", ""))
        flash("Document created", "success")
        return redirect(url_for("documents.detail", document_id=document_id))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("orders.detail", order_id=order_id))


@bp.post("/<int:order_id>/<action>")
@login_required
def change_status(order_id, action):
    try:
        message = transition_order(order_id, action)
        flash(message, "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("orders.detail", order_id=order_id))
