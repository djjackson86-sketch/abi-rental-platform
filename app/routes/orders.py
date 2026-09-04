from datetime import datetime, timedelta

from flask import Blueprint, flash, redirect, render_template, request, url_for
from werkzeug.datastructures import MultiDict

from app.routes.auth import login_required
from app.db import get_db
from app.services.orders import create_order, draft_order_form, get_order, list_orders, order_counts, order_filter_counts, order_items, next_time_slot, settle_return_deposit, status_actions, transition_order, update_draft_order
from app.services.documents import create_document, documents_for_order, document_type_options, label_for
from app.services.payments import display_payment_date, label_for as payment_label_for, payment_summary, payments_for_order, record_payment
from app.services.settings import get_company_settings
from app.services.customers import create_customer, customer_summary_for, custom_field_label, custom_fields_for
from app.services.branches import branch_options, default_branch_id

bp = Blueprint("orders", __name__, url_prefix="/orders")


def _customers():
    rows = get_db().execute("""
        SELECT id, customer_type, name, email, phone, address_line1, address_line2, suburb, city, province, postal_code, country, custom_fields_json
        FROM customers
        ORDER BY name
    """).fetchall()
    return [customer_summary_for(row) for row in rows]


def _selected_customer_summary(customers, selected_customer_id):
    if not selected_customer_id:
        return None
    try:
        wanted = int(selected_customer_id)
    except (TypeError, ValueError):
        return None
    return next((customer for customer in customers if customer["id"] == wanted), None)


def _products():
    return get_db().execute("""
        SELECT p.id, p.name, p.sku, p.price_amount, p.price_unit, p.quantity, p.branch_id,
               p.security_deposit, COALESCE(t.rate, 0) AS tax_rate, b.name AS branch_name
        FROM products p
        LEFT JOIN branches b ON b.id = p.branch_id
        LEFT JOIN tax_profiles t ON t.id = p.tax_profile_id
        WHERE p.active = 1
        ORDER BY p.name
    """).fetchall()


def _form_with_inline_customer(form):
    """Create/attach the inline customer when saving an order draft directly."""
    selected_customer_id = (form.get("customer_id") or "").strip()
    if selected_customer_id:
        return form
    inline_customer_name = (form.get("name") or "").strip()
    if not inline_customer_name:
        return form
    mutable_form = MultiDict(form)
    customer_id = create_customer(mutable_form)
    mutable_form["customer_id"] = str(customer_id)
    return mutable_form


def _time_options(increment=15):
    return [f"{hour:02d}:{minute:02d}" for hour in range(24) for minute in range(0, 60, increment)]


@bp.route("")
@login_required
def index():
    query = request.args.get("query", "").strip()
    status = request.args.get("status", "")
    payment_status = request.args.get("payment_status", "")
    return_status = request.args.get("return_status", "")
    orders = list_orders(query=query, status=status, payment_status=payment_status, return_status=return_status)
    return render_template(
        "admin/orders/index.html",
        settings=get_company_settings(),
        orders=orders,
        counts=order_counts(),
        filter_counts=order_filter_counts(),
        filters={"query": query, "status": status, "payment_status": payment_status, "return_status": return_status},
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
                form = _form_with_inline_customer(request.form)
                order_id = create_order(form)
                flash("Draft order created", "success")
                return redirect(url_for("orders.detail", order_id=order_id))
            except ValueError as exc:
                flash(str(exc), "error")
    slot = next_time_slot(increment_minutes=15)
    customers = _customers()
    return render_template(
        "admin/orders/form.html",
        settings=settings,
        customers=customers,
        products=_products(),
        selected_customer_id=selected_customer_id,
        selected_customer_summary=_selected_customer_summary(customers, selected_customer_id),
        default_start_date=slot.date().isoformat(),
        default_return_date=(slot + timedelta(days=1)).date().isoformat(),
        default_start_time=slot.strftime("%H:%M"),
        time_options=_time_options(15),
        branches=branch_options(),
        default_branch_id=default_branch_id(),
        form_mode="new",
        form_action=url_for("orders.new"),
        custom_field_label=custom_field_label,
    )


@bp.route("/<int:order_id>/edit", methods=["GET", "POST"])
@login_required
def edit(order_id):
    try:
        form_data = draft_order_form(order_id)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("orders.detail", order_id=order_id))
    if request.method == "POST":
        if request.form.get("order_action") == "create_customer_continue":
            try:
                customer_id = create_customer(request.form)
                get_db().execute("UPDATE orders SET customer_id = ? WHERE id = ?", (customer_id, order_id))
                get_db().commit()
                flash("Customer created and attached — continue editing the order", "success")
                return redirect(url_for("orders.edit", order_id=order_id))
            except ValueError as exc:
                flash(str(exc), "error")
                form_data = None
        else:
            try:
                form = _form_with_inline_customer(request.form)
                update_draft_order(order_id, form)
                flash("Order saved", "success")
                return redirect(url_for("orders.detail", order_id=order_id))
            except ValueError as exc:
                flash(str(exc), "error")
                form_data = None
    if form_data is None:
        try:
            form_data = draft_order_form(order_id)
        except ValueError:
            form_data = {"order": get_order(order_id), "lines": []}
    return render_template(
        "admin/orders/form.html",
        settings=get_company_settings(),
        customers=_customers(),
        products=_products(),
        selected_customer_id=form_data.get("selected_customer_id", ""),
        default_start_date=form_data.get("start_date", ""),
        default_return_date=form_data.get("end_date", ""),
        default_start_time=form_data.get("start_time", ""),
        default_return_time=form_data.get("end_time", ""),
        time_options=_time_options(15),
        branches=branch_options(),
        default_branch_id=form_data.get("collect_branch_id") or default_branch_id(),
        form_mode="edit",
        form_action=url_for("orders.edit", order_id=order_id),
        custom_field_label=custom_field_label,
        order_form=form_data,
    )


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
        display_payment_date=display_payment_date,
        customer_custom_fields=custom_fields_for(order),
        default_payment_date=datetime.utcnow().isoformat(timespec="minutes"),
        default_deposit_processed_at=(order["deposit_processed_at"] or datetime.utcnow().isoformat(timespec="minutes"))[:16],
    )


@bp.post("/<int:order_id>/settle-return")
@login_required
def settle_return(order_id):
    try:
        message = settle_return_deposit(order_id, request.form)
        flash(message, "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("orders.detail", order_id=order_id))


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
