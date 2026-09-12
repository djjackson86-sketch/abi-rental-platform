from datetime import datetime, timedelta

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from werkzeug.datastructures import MultiDict

from app.routes.auth import login_required
from app.db import get_db
from app.services.orders import _build_order_payload, add_return_charges, apply_order_discount, billed_rental_days, can_process_return_deposit, create_order, delete_order, deposit_to_process_amount, draft_order_form, get_order, has_finalized_invoice, list_orders, order_counts, order_filter_counts, order_items, next_time_slot, rental_days, return_charge_defaults, return_damage_total, revise_started_return, settle_return_deposit, status_actions, transition_order, update_draft_order, update_return_checklist, use_return_deposit
from app.services.documents import create_document, documents_for_order, document_type_options, label_for
from app.services.payments import display_payment_date, label_for as payment_label_for, payment_summary, payments_for_order, record_payment, record_refund
from app.services.settings import get_company_settings
from app.services.customers import create_customer, customer_fields_changed, customer_summary_for, custom_field_label, custom_fields_for, get_customer, update_customer
from app.services.branches import branch_options, default_branch_id
from app.services.timezone import local_now_iso
from app.services.access import main_required, resolve_branch_filter, session_branch_scope, user_can_access_order

bp = Blueprint("orders", __name__, url_prefix="/orders")

# Flat names of the editable attached-customer fields on the new-order form.
CUSTOMER_EDIT_FIELD_KEYS = [
    "customer_type", "name", "email", "phone", "marketing_opt_in",
    "address_line1", "address_line2", "suburb", "city", "province", "postal_code", "country",
    "vehicle_make", "vehicle_color", "vehicle_reg_no",
    "alternative_contact_name", "alternative_contact_number", "alternative_contact_relationship",
    "vat_number", "company_reg_no", "standard_discount_percent",
]


def _submitted_customer_values(form):
    return {key: (form.get(key) or "") for key in CUSTOMER_EDIT_FIELD_KEYS}


def _customers():
    rows = get_db().execute("""
        SELECT id, customer_type, name, email, phone, marketing_opt_in,
               address_line1, address_line2, suburb, city, province, postal_code, country, custom_fields_json, standard_discount_percent,
               (SELECT COALESCE(SUM(o.total - COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.order_id=o.id AND p.status='paid' AND COALESCE(p.deleted_at,'')=''), 0)), 0)
                FROM orders o WHERE o.customer_id = customers.id AND o.status NOT IN ('canceled','cancelled','archived')) AS previous_orders_balance
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
    branch_id = session_branch_scope()
    scope_sql = " AND (p.branch_id = ? OR p.branch_id IS NULL)" if branch_id else ""
    params = [branch_id] if branch_id else []
    return get_db().execute(f"""
        SELECT p.id, p.name, p.sku, p.price_amount, p.price_unit, p.quantity, p.branch_id,
               p.security_deposit, p.hourly_extra_rate, p.product_type, COALESCE(t.rate, 0) AS tax_rate, b.name AS branch_name
        FROM products p
        LEFT JOIN branches b ON b.id = p.branch_id
        LEFT JOIN tax_profiles t ON t.id = p.tax_profile_id
        WHERE p.active = 1{scope_sql}
        ORDER BY p.name
    """, params).fetchall()




def _scoped_branch_options():
    branch_id = session_branch_scope()
    branches = branch_options()
    if not branch_id:
        return branches
    return [branch for branch in branches if branch["id"] == branch_id]


def _scoped_default_branch_id(fallback=None):
    return session_branch_scope() or fallback or default_branch_id()


def _force_staff_collection_branch(form):
    branch_id = session_branch_scope()
    if not branch_id:
        return form
    mutable = MultiDict(form)
    mutable["collect_branch_id"] = str(branch_id)
    if mutable.get("booking_type") != "oneway":
        mutable["return_branch_id"] = str(branch_id)
    return mutable


def _ensure_order_access(order_id):
    order = get_order(order_id)
    if not order:
        return None
    if not user_can_access_order(order):
        abort(404)
    return order

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
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    selected_branch, branch_id, branch_label, branches, branch_scope = resolve_branch_filter(request.args.get("branch", ""))
    orders = list_orders(query=query, status=status, payment_status=payment_status, return_status=return_status, start_date=start_date, end_date=end_date, branch_id=branch_id)
    return render_template(
        "admin/orders/index.html",
        settings=get_company_settings(),
        orders=orders,
        counts=order_counts(query=query, status=status, payment_status=payment_status, return_status=return_status, start_date=start_date, end_date=end_date, branch_id=branch_id),
        filter_counts=order_filter_counts(branch_id=branch_id),
        branches=branches,
        branch_label=branch_label,
        branch_scope=branch_scope,
        filters={"query": query, "status": status, "payment_status": payment_status, "return_status": return_status, "start_date": start_date, "end_date": end_date, "branch": selected_branch},
        deposit_to_process_amount=deposit_to_process_amount,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    settings = get_company_settings()
    selected_customer_id = request.args.get("customer_id", "")
    submitted_customer_values = None
    if request.method == "POST":
        selected_customer_id = (request.form.get("customer_id") or "").strip()
        if request.form.get("order_action") == "create_customer_continue":
            try:
                customer_id = create_customer(request.form)
                flash("Customer created — continue the order", "success")
                return redirect(url_for("orders.new", customer_id=customer_id))
            except ValueError as exc:
                flash(str(exc), "error")
                submitted_customer_values = _submitted_customer_values(request.form)
        else:
            try:
                # When a saved customer is attached and its editable card was
                # submitted (customer_edit_active), persist any changed customer
                # fields to the saved record before creating the draft so the
                # draft joins fresh values. customer_edit_active is only present
                # when the card was actually enabled, so no-JS fallback-select
                # saves and plain customerless drafts are untouched.
                if selected_customer_id and request.form.get("customer_edit_active"):
                    try:
                        wanted = int(selected_customer_id)
                    except (TypeError, ValueError):
                        wanted = None
                    stored = get_customer(wanted) if wanted else None
                    if stored:
                        # Validate order fields first so a failing draft does not
                        # silently rewrite the saved customer record.
                        _build_order_payload(request.form)
                        if customer_fields_changed(stored, request.form):
                            update_customer(stored["id"], request.form)
                form = _force_staff_collection_branch(_form_with_inline_customer(request.form))
                order_id = create_order(form)
                flash("Draft order created", "success")
                return redirect(url_for("orders.detail", order_id=order_id))
            except ValueError as exc:
                flash(str(exc), "error")
                submitted_customer_values = _submitted_customer_values(request.form)
    slot = next_time_slot(increment_minutes=15)
    customers = _customers()
    selected_customer_summary = _selected_customer_summary(customers, selected_customer_id)
    # On POST errors keep the typed customer card values; otherwise the template
    # falls back to the stored summary's own form values for GET rendering.
    if request.method != "POST" or submitted_customer_values is None:
        submitted_customer_values = {}
    return render_template(
        "admin/orders/form.html",
        settings=settings,
        customers=customers,
        products=_products(),
        selected_customer_id=selected_customer_id,
        selected_customer_summary=selected_customer_summary,
        customer_values=submitted_customer_values if selected_customer_summary else {},
        default_start_date=slot.date().isoformat(),
        default_return_date=(slot + timedelta(days=1)).date().isoformat(),
        default_start_time=slot.strftime("%H:%M"),
        time_options=_time_options(15),
        branches=_scoped_branch_options(),
        default_branch_id=_scoped_default_branch_id(),
        form_mode="new",
        form_action=url_for("orders.new"),
        custom_field_label=custom_field_label,
    )


@bp.route("/<int:order_id>/edit", methods=["GET", "POST"])
@login_required
def edit(order_id):
    try:
        _ensure_order_access(order_id)
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
                form = _force_staff_collection_branch(_form_with_inline_customer(request.form))
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
            form_data = {"order": _ensure_order_access(order_id), "lines": []}
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
        branches=_scoped_branch_options(),
        default_branch_id=_scoped_default_branch_id(form_data.get("collect_branch_id")),
        form_mode="edit",
        form_action=url_for("orders.edit", order_id=order_id),
        custom_field_label=custom_field_label,
        order_form=form_data,
    )


@bp.route("/<int:order_id>")
@login_required
def detail(order_id):
    order = _ensure_order_access(order_id)
    if not order:
        flash("Order not found", "error")
        return redirect(url_for("orders.index"))
    try:
        start_at = datetime.fromisoformat(order["start_at"]) if order["start_at"] else None
        end_at = datetime.fromisoformat(order["end_at"]) if order["end_at"] else None
        # A revised return is billed as full 24h blocks + extra hours, so the Days
        # column must use the floor-based count, not the ceil() used at booking.
        order_rental_days = billed_rental_days(
            start_at,
            end_at,
            extra_hours=order["extra_hours"] or 0,
            revised=bool(order["return_revised_at"] or ""),
        ) if start_at and end_at else 1
    except ValueError:
        order_rental_days = 1
    documents = documents_for_order(order_id)
    has_invoice = any(document["document_type"] == "invoice" for document in documents)
    finalized_invoice_exists = has_finalized_invoice(order_id)
    return render_template(
        "admin/orders/detail.html",
        settings=get_company_settings(),
        order=order,
        items=order_items(order_id),
        actions=status_actions(order["status"]),
        documents=documents,
        has_invoice=has_invoice,
        finalized_invoice_exists=finalized_invoice_exists,
        document_types=document_type_options(),
        label_for=label_for,
        payments=payments_for_order(order_id),
        payment_summary=payment_summary(order_id),
        payment_label_for=payment_label_for,
        display_payment_date=display_payment_date,
        customer_custom_fields=custom_fields_for(order),
        return_charge_defaults=return_charge_defaults(order_id),
        return_damage_total=return_damage_total(order_id),
        order_rental_days=order_rental_days,
        can_process_return_deposit=can_process_return_deposit(order),
        default_payment_date=local_now_iso(timespec="minutes"),
        default_deposit_processed_at=(order["deposit_processed_at"] or local_now_iso(timespec="minutes"))[:16],
    )


@bp.post("/<int:order_id>/revise-return")
@login_required
def revise_return(order_id):
    _ensure_order_access(order_id)
    try:
        message = revise_started_return(order_id, request.form)
        flash(message, "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("orders.detail", order_id=order_id))


@bp.post("/<int:order_id>/add-return-charges")
@login_required
def add_charges(order_id):
    _ensure_order_access(order_id)
    try:
        message = add_return_charges(order_id, request.form)
        flash(message, "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("orders.detail", order_id=order_id))


@bp.post("/<int:order_id>/settle-return")
@login_required
def settle_return(order_id):
    _ensure_order_access(order_id)
    try:
        message = settle_return_deposit(order_id, request.form)
        flash(message, "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("orders.detail", order_id=order_id))


@bp.post("/<int:order_id>/use-deposit")
@login_required
def use_deposit(order_id):
    _ensure_order_access(order_id)
    try:
        message = use_return_deposit(order_id, request.form)
        flash(message, "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("orders.detail", order_id=order_id))


@bp.post("/<int:order_id>/return-checklist")
@login_required
def return_checklist(order_id):
    _ensure_order_access(order_id)
    wants_json = request.headers.get("X-Requested-With") == "fetch" or "application/json" in (request.headers.get("Accept") or "")
    try:
        message = update_return_checklist(order_id, request.form)
        if wants_json:
            return jsonify({"ok": True, "message": message})
        flash(message, "success")
    except ValueError as exc:
        if wants_json:
            return jsonify({"ok": False, "message": str(exc)}), 400
        flash(str(exc), "error")
    return redirect(url_for("orders.detail", order_id=order_id))


@bp.post("/<int:order_id>/refund")
@login_required
def refund_order(order_id):
    _ensure_order_access(order_id)
    try:
        record_refund(order_id, request.form)
        flash("Refund recorded", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("orders.detail", order_id=order_id))


@bp.post("/<int:order_id>/payments")
@login_required
def record_order_payment(order_id):
    _ensure_order_access(order_id)
    try:
        record_payment(order_id, request.form)
        flash("Payment recorded", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("orders.detail", order_id=order_id))


@bp.post("/<int:order_id>/documents")
@login_required
def create_document_for_order(order_id):
    _ensure_order_access(order_id)
    try:
        document_id = create_document(order_id, request.form.get("document_type", ""))
        flash("Document created", "success")
        return redirect(url_for("documents.detail", document_id=document_id))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("orders.detail", order_id=order_id))


@bp.post("/<int:order_id>/discount")
@login_required
def apply_discount(order_id):
    _ensure_order_access(order_id)
    wants_json = request.headers.get("X-Requested-With") == "fetch" or "application/json" in (request.headers.get("Accept") or "")
    try:
        message = apply_order_discount(order_id, request.form.get("discount_mode", ""), request.form.get("discount_value", ""))
        if wants_json:
            order = get_order(order_id)
            summary = payment_summary(order_id)
            return jsonify({
                "ok": True,
                "message": message,
                "discount_total": float(order["discount_total"] or 0),
                "tax_total": float(order["tax_total"] or 0),
                "total": float(order["total"] or 0),
                "paid_total": float(summary["paid_total"] or 0),
                "due_total": float(summary["due_total"] or 0),
                "payment_status": summary["payment_status"],
            })
        flash(message, "success")
    except ValueError as exc:
        if wants_json:
            return jsonify({"ok": False, "message": str(exc)}), 400
        flash(str(exc), "error")
    return redirect(url_for("orders.detail", order_id=order_id))


@bp.post("/<int:order_id>/delete")
@login_required
@main_required
def delete(order_id):
    """Permanently delete an order with its items, payments and documents.

    Main profile only — the endpoint carries the gate, not the template. Staff
    keep every other order action. This is irreversible: delete_order() dumps the
    rows to ~/abi-backups/ first, and the client's confirm dialog is the second
    barrier.
    """
    order = _ensure_order_access(order_id)
    if not order:
        flash("Order not found", "error")
        return redirect(url_for("orders.index"))
    order_number = order["order_number"] or order_id
    try:
        counts = delete_order(order_id)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("orders.detail", order_id=order_id))
    flash(
        f"{order_number} deleted permanently — {counts['order_items']} item(s), "
        f"{counts['payments']} payment(s), {counts['documents']} document(s) removed",
        "success",
    )
    return redirect(url_for("orders.index"))


@bp.post("/<int:order_id>/<action>")
@login_required
def change_status(order_id, action):
    _ensure_order_access(order_id)
    try:
        message = transition_order(order_id, action)
        flash(message, "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("orders.detail", order_id=order_id))
