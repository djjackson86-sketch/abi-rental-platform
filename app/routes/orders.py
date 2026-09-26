from datetime import datetime, timedelta

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.datastructures import MultiDict

from app.routes.auth import login_required
from app.db import get_db
from app.services.orders import SALES_REPAIRS_LABEL, SALES_REPAIRS_STATUS, _build_order_payload, add_return_charges, apply_order_discount, billed_rental_days, can_process_return_deposit, create_order, delete_deposit_refund, delete_order, deposit_to_process_amount, draft_order_form, get_order, has_finalized_invoice, list_orders, order_counts, order_filter_counts, order_items, order_has_rental_items, next_time_slot, rental_days, return_charge_defaults, return_damage_total, revise_started_return, settle_return_deposit, status_actions, status_label, transition_order, update_deposit_refund, update_draft_order, update_return_checklist, use_return_deposit
from app.services.documents import create_document, documents_for_order, document_type_options, label_for
from app.services.payments import display_payment_date, label_for as payment_label_for, payment_summary, payments_for_order, record_payment, record_refund
from app.services.settings import get_company_settings
from app.services.customers import create_customer, customer_fields_changed, customer_summary_for, custom_field_label, custom_fields_for, get_customer, update_customer
from app.services.branches import branch_hours_summaries, branch_options, default_branch_id
from app.services.timezone import local_now_iso
from app.services.access import is_main_session, main_required, resolve_branch_filter, session_branch_scope_ids, session_primary_branch_id, user_can_access_order

bp = Blueprint("orders", __name__, url_prefix="/orders")
PAGE_SIZE = 25


def _display_limit():
    try:
        requested = int(request.args.get("limit") or PAGE_SIZE)
    except (TypeError, ValueError):
        requested = PAGE_SIZE
    return max(PAGE_SIZE, min(requested, 5000))


# Flat names of the editable attached-customer fields on the new-order form.
CUSTOMER_EDIT_FIELD_KEYS = [
    "customer_type", "name", "email", "phone", "marketing_opt_in",
    "address_line1", "address_line2", "suburb", "city", "province", "postal_code", "country",
    "vehicle_make", "vehicle_color", "vehicle_reg_no",
    "alternative_contact_name", "alternative_contact_number", "alternative_contact_relationship",
    "vat_number", "company_reg_no", "standard_discount_percent", "client_verified",
]


def _submitted_customer_values(form):
    return {key: (form.get(key) or "") for key in CUSTOMER_EDIT_FIELD_KEYS}


def _customers():
    rows = get_db().execute("""
        SELECT id, customer_type, name, email, phone, marketing_opt_in,
               address_line1, address_line2, suburb, city, province, postal_code, country, custom_fields_json, standard_discount_percent,
               client_verified, is_blocked, blocked_reason,
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


def _customer_summary_by_id(customer_id):
    row = get_db().execute("""
        SELECT id, customer_type, name, email, phone, marketing_opt_in,
               address_line1, address_line2, suburb, city, province, postal_code, country, custom_fields_json, standard_discount_percent,
               client_verified, is_blocked, blocked_reason,
               (SELECT COALESCE(SUM(o.total - COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.order_id=o.id AND p.status='paid' AND COALESCE(p.deleted_at,'')=''), 0)), 0)
                FROM orders o WHERE o.customer_id = customers.id AND o.status NOT IN ('canceled','cancelled','archived')) AS previous_orders_balance
        FROM customers
        WHERE id = ?
    """, (customer_id,)).fetchone()
    return customer_summary_for(row) if row else None


def _wants_json():
    return request.is_json or request.headers.get("X-Requested-With") == "fetch" or "application/json" in request.headers.get("Accept", "")


def _create_inline_customer_response(customer_id, message):
    summary = _customer_summary_by_id(customer_id)
    if _wants_json():
        return jsonify({"ok": True, "message": message, "customer": summary})
    return None


def _products():
    scope_ids = session_branch_scope_ids()
    if scope_ids is None:
        scope_sql, params = "", []
    elif not scope_ids:
        scope_sql, params = " AND 0=1", []
    else:
        marks = ",".join("?" for _ in scope_ids)
        scope_sql = f" AND (p.branch_id IN ({marks}) OR p.branch_id IS NULL)"
        params = list(scope_ids)
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
    """Branches this session may choose from — its own depots, or every branch."""
    scope_ids = session_branch_scope_ids()
    branches = branch_options()
    if scope_ids is None:
        return branches
    return [branch for branch in branches if branch["id"] in scope_ids]


def _scoped_default_branch_id(fallback=None):
    scope_ids = session_branch_scope_ids()
    if not scope_ids:
        return fallback or default_branch_id()
    primary = session_primary_branch_id()
    if primary and primary in scope_ids:
        return primary
    return scope_ids[0]


def _force_staff_collection_branch(form):
    """Keep a branch-limited session inside its own depots.

    One depot behaves exactly as before (the branch is forced). With several
    depots the submitted collection branch is honoured when it is one of them and
    otherwise replaced by the session's primary branch, so a crafted POST cannot
    book against a depot the account cannot see. Validation is server-side; the
    narrowed dropdown is only a convenience.
    """
    scope_ids = session_branch_scope_ids()
    if not scope_ids:
        return form
    mutable = MultiDict(form)
    try:
        submitted = int(mutable.get("collect_branch_id") or 0)
    except (TypeError, ValueError):
        submitted = 0
    chosen = submitted if submitted in scope_ids else (session_primary_branch_id() or scope_ids[0])
    mutable["collect_branch_id"] = str(chosen)
    if mutable.get("booking_type") == "oneway":
        if len(scope_ids) > 1:
            try:
                submitted_return = int(mutable.get("return_branch_id") or 0)
            except (TypeError, ValueError):
                submitted_return = 0
            if submitted_return not in scope_ids:
                mutable["return_branch_id"] = str(chosen)
    else:
        mutable["return_branch_id"] = str(chosen)
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


def _order_customer_id(form_data):
    """The customer_id already stored on the order being edited (may be None)."""
    order = form_data.get("order") if isinstance(form_data, dict) else None
    if order is None:
        return None
    try:
        return order["customer_id"]
    except (KeyError, IndexError, TypeError):
        return None


def _wants_sales_repairs(form, order_id):
    """True when the order form was submitted with the Sales/Repairs button.

    Server-side guard on top of the hidden button: the Sales/Repairs status is
    only ever applied to an order that hires nothing out — the same rule the
    Sales/Repairs folder has always used — so a crafted POST cannot label a
    rental order as sales/repairs (ticket ABI-341952962).
    """
    if (form.get("order_action") or "").strip() != SALES_REPAIRS_STATUS:
        return False
    return not order_has_rental_items(order_items(order_id))


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
    display_limit = _display_limit()
    counts = order_counts(query=query, status=status, payment_status=payment_status, return_status=return_status, start_date=start_date, end_date=end_date, branch_id=branch_id)
    orders = list_orders(query=query, status=status, payment_status=payment_status, return_status=return_status, start_date=start_date, end_date=end_date, branch_id=branch_id, limit=display_limit)
    return render_template(
        "admin/orders/index.html",
        settings=get_company_settings(),
        orders=orders,
        counts=counts,
        total_orders=counts["total"],
        display_limit=display_limit,
        previous_limit=max(display_limit - PAGE_SIZE, 0),
        next_limit=display_limit + PAGE_SIZE,
        filter_counts=order_filter_counts(branch_id=branch_id),
        branches=branches,
        branch_label=branch_label,
        branch_scope=branch_scope,
        filters={"query": query, "status": status, "payment_status": payment_status, "return_status": return_status, "start_date": start_date, "end_date": end_date, "branch": selected_branch},
        deposit_to_process_amount=deposit_to_process_amount,
        sales_repairs_label=SALES_REPAIRS_LABEL,
        status_label=status_label,
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
                json_response = _create_inline_customer_response(customer_id, "Customer created — continue the order")
                if json_response:
                    return json_response
                flash("Customer created — continue the order", "success")
                return redirect(url_for("orders.new", customer_id=customer_id))
            except ValueError as exc:
                if _wants_json():
                    return jsonify({"ok": False, "message": str(exc)}), 400
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
                form = _force_staff_collection_branch(request.form)
                # Validate before the inline customer is created: create_order()
                # validates the same payload anyway, but by then a new customer
                # would already exist, so a refused draft (a pickup outside the
                # branch's trading hours, a return before the pickup) would leave
                # a stray customer record with no order behind it.
                _build_order_payload(form)
                form = _form_with_inline_customer(form)
                # Ticket ABI-341952988: admin-app drafts stay silent on creation
                # (notify=False). The "New order" message is sent by the status
                # change below (Save as Sales/Repairs) or later from
                # transition_order() when the order is reserved / picked up.
                order_id = create_order(form, notify=False)
                if _wants_sales_repairs(form, order_id):
                    # "Save as Sales/Repairs" on the form: the order is stored as
                    # a draft first, then the real Sales/Repairs status is applied
                    # (ticket ABI-341952962). "Save as draft" is untouched.
                    transition_order(order_id, SALES_REPAIRS_STATUS)
                    flash(f"Order saved as {SALES_REPAIRS_LABEL}", "success")
                else:
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
        branch_hours=branch_hours_summaries(),
        form_mode="new",
        form_action=url_for("orders.new"),
        custom_field_label=custom_field_label,
        # A brand-new order is always a draft, so Sales/Repairs can be selected
        # straight away (ticket ABI-341952962); "Save as draft" stays beside it.
        sales_repairs_available=True,
        sales_repairs_label=SALES_REPAIRS_LABEL,
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
                json_response = _create_inline_customer_response(customer_id, "Customer created and attached — continue editing the order")
                if json_response:
                    return json_response
                flash("Customer created and attached — continue editing the order", "success")
                return redirect(url_for("orders.edit", order_id=order_id))
            except ValueError as exc:
                if _wants_json():
                    return jsonify({"ok": False, "message": str(exc)}), 400
                flash(str(exc), "error")
                form_data = None
        else:
            try:
                form = _force_staff_collection_branch(request.form)
                # Same guard as /orders/new: validate the order fields before the
                # inline customer row is created, so a refused save cannot leave a
                # customer with no order behind it. Ticket ABI-341953028: the
                # order's OWN customer may be blocked (a block applied after the
                # order was raised) — only re-pointing it at a blocked customer is
                # refused.
                _build_order_payload(form, allow_blocked_customer_id=_order_customer_id(form_data))
                form = _form_with_inline_customer(form)
                update_draft_order(order_id, form)
                if _wants_sales_repairs(form, order_id):
                    transition_order(order_id, SALES_REPAIRS_STATUS)
                    flash(f"Order saved as {SALES_REPAIRS_LABEL}", "success")
                else:
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
    has_rental_items = order_has_rental_items(order_items(order_id))
    # The form's Sales/Repairs button stores the status, so it is only offered
    # while the order is still a draft and hires nothing out (ticket ABI-341952962).
    form_order = form_data.get("order") if isinstance(form_data, dict) else None
    try:
        form_order_status = form_order["status"] if form_order is not None else ""
    except (KeyError, IndexError, TypeError):
        form_order_status = ""
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
        branch_hours=branch_hours_summaries(),
        form_mode="edit",
        form_action=url_for("orders.edit", order_id=order_id),
        custom_field_label=custom_field_label,
        order_form=form_data,
        has_rental_items=has_rental_items,
        sales_repairs_available=(form_order_status == "draft") and not has_rental_items,
        sales_repairs_label=SALES_REPAIRS_LABEL,
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
    items = order_items(order_id)
    has_rental_items = order_has_rental_items(items)
    documents = documents_for_order(order_id)
    has_invoice = any(document["document_type"] == "invoice" for document in documents)
    finalized_invoice_exists = has_finalized_invoice(order_id)
    return render_template(
        "admin/orders/detail.html",
        settings=get_company_settings(),
        order=order,
        items=items,
        has_rental_items=has_rental_items,
        # The badge means the real stored status now (ticket ABI-341952962) — a
        # draft sale-only order gets the Sales/Repairs ACTION button instead.
        is_sales_repairs_order=(order["status"] or "") == SALES_REPAIRS_STATUS,
        sales_repairs_label=SALES_REPAIRS_LABEL,
        status_label=status_label,
        actions=status_actions(order["status"], has_rental_items=has_rental_items),
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


@bp.route("/<int:order_id>/deposit-refund/edit", methods=["GET", "POST"])
@login_required
def deposit_refund_edit(order_id):
    """Edit the recorded deposit refund payout for an order (ticket ABI-341953057)."""
    order = _ensure_order_access(order_id)
    if not order:
        flash("Order not found", "error")
        return redirect(url_for("orders.index"))
    if request.method == "POST":
        try:
            message = update_deposit_refund(order_id, request.form)
            flash(message, "success")
            return redirect(url_for("orders.detail", order_id=order_id))
        except ValueError as exc:
            flash(str(exc), "error")
    elif not can_process_return_deposit(order):
        flash("Return and deposit settlement is available after pickup or cancelation", "error")
        return redirect(url_for("orders.detail", order_id=order_id))
    elif round(float(order["deposit_refund_amount"] or 0), 2) <= 0:
        flash("There is no deposit refund recorded on this order to edit", "error")
        return redirect(url_for("orders.detail", order_id=order_id))
    return render_template(
        "admin/orders/deposit_refund_edit.html",
        order=order,
        can_process_return_deposit=can_process_return_deposit(order),
        default_deposit_processed_at=(order["deposit_processed_at"] or local_now_iso(timespec="minutes"))[:16],
    )


@bp.post("/<int:order_id>/deposit-refund/delete")
@login_required
def deposit_refund_delete(order_id):
    """Reverse (soft delete) the recorded deposit refund payout (ticket ABI-341953057)."""
    _ensure_order_access(order_id)
    try:
        message = delete_deposit_refund(order_id)
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


@bp.post("/<int:order_id>/revert-draft")
@login_required
@main_required
def revert_draft(order_id):
    """Pull a live order back to draft (ticket ABI-341952993).

    Main profile only — the gate is on the endpoint, never on the button. The
    stock the order held is released by the status change itself (availability
    only counts reserved and started orders) while payments, quotes and invoices
    are deliberately left intact, so nothing financial is rewritten or removed.
    Cancelled and archived orders are refused by the transition table.
    """
    order = _ensure_order_access(order_id)
    if not order:
        flash("Order not found", "error")
        return redirect(url_for("orders.index"))
    try:
        message = transition_order(order_id, "revert_draft")
        flash(message, "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("orders.detail", order_id=order_id))


@bp.post("/<int:order_id>/unarchive")
@login_required
@main_required
def unarchive(order_id):
    """Put an archived order back on the books (ticket ABI-341953038, item 1).

    Main profile only — the gate is on the endpoint, never on the button. The
    order returns to the status it held before it was archived (the client's
    clarification), so an archived hire order comes back as Returned and an
    archived sale or repair comes back as Sales/Repairs. Payments, quotes and
    invoices are deliberately left intact: nothing financial is rewritten, added
    or removed, exactly like Revert to Draft.
    """
    order = _ensure_order_access(order_id)
    if not order:
        flash("Order not found", "error")
        return redirect(url_for("orders.index"))
    try:
        message = transition_order(order_id, "unarchive")
        flash(message, "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("orders.detail", order_id=order_id))


@bp.post("/<int:order_id>/<action>")
@login_required
def change_status(order_id, action):
    _ensure_order_access(order_id)
    if action in {"revert_draft", "unarchive"} and not is_main_session(session):
        # Belt and braces: the dedicated routes above carry the gate, and this
        # refuses the same actions through the generic catch-all (a crafted URL
        # must not be able to sidestep the main-profile rule).
        abort(403)
    try:
        message = transition_order(order_id, action)
        flash(message, "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("orders.detail", order_id=order_id))
