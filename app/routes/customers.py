import csv
import uuid
from io import StringIO

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, session, url_for

from app.routes.auth import login_required
from app.services.access import user_can_module
from app.services.customers import can_set_customer_credit, client_verified_label, create_customer, credit_fields_submitted, custom_field_label, custom_fields_for, customer_counts, customer_filter_counts, customer_filtered_total, customer_has_history, customer_orders, customer_outstanding_balances, customer_statement, delete_customer, get_customer, list_customers, update_customer
from app.services.pdf_documents import customer_statement_pdf_bytes
from app.services.payments import PAYMENT_METHODS, normalise_payment_date_filter
from app.services.settings import get_company_settings
from app.services.customer_credits import customer_credit_balance, customer_credit_entries
from app.services.legacy_balances import legacy_balance_summary, record_legacy_payment

bp = Blueprint("customers", __name__, url_prefix="/customers")

PAGE_SIZE = 25

# Methods staff may use to settle an imported legacy balance. Customer credit is
# excluded on purpose: it is money the customer already has with us, not cash
# collected against an old debt, and manual is a legacy ledger value only.
LEGACY_PAYMENT_METHODS = ("cash", "eft", "card", "other")
LEGACY_PAYMENT_METHOD_LABELS = {
    "cash": "Cash",
    "eft": "EFT",
    "card": "Card",
    "other": "Other",
}
assert set(LEGACY_PAYMENT_METHODS) <= set(PAYMENT_METHODS)


def can_settle_legacy_balances():
    """Only the owner or staff with both Orders and Customers may settle old balances."""
    return user_can_module(session, "orders") and user_can_module(session, "customers")


def can_manage_customer_credit():
    """Ticket ABI-341953129: only the main profile may set a customer's credit."""
    return can_set_customer_credit()


def _enforce_owner_only_credit(form):
    """Refuse a crafted credit payload from anything but the main profile.

    The service already ignores the credit fields for a non-main caller (which is
    also what protects the order form's inline customer card, a path this route
    does not own), so a stored facility can never be changed by a forged field.
    This route-level check makes the refusal explicit instead of silent for the
    customer form itself: a POST carrying the credit panel from a staff session
    is a 403, not a quietly-ignored field.
    """
    if credit_fields_submitted(form) and not can_manage_customer_credit():
        abort(403)


def _display_limit():
    try:
        requested = int(request.args.get("limit") or PAGE_SIZE)
    except (TypeError, ValueError):
        requested = PAGE_SIZE
    return max(PAGE_SIZE, min(requested, 5000))


@bp.route("")
@login_required
def index():
    query = request.args.get("query", "").strip()
    customer_type = request.args.get("customer_type", "")
    marketing = request.args.get("marketing", "")
    display_limit = _display_limit()
    customers = list_customers(query=query, customer_type=customer_type, marketing=marketing, limit=display_limit)
    total = customer_filtered_total(query=query, customer_type=customer_type, marketing=marketing)
    return render_template(
        "admin/customers/index.html",
        settings=get_company_settings(),
        customers=customers,
        total_customers=total,
        display_limit=display_limit,
        previous_limit=max(display_limit - PAGE_SIZE, 0),
        next_limit=display_limit + PAGE_SIZE,
        counts=customer_counts(),
        filter_counts=customer_filter_counts(),
        filters={"query": query, "customer_type": customer_type, "marketing": marketing},
        client_verified_label=client_verified_label,
    )


@bp.route("/export.csv")
@login_required
def export_csv():
    query = request.args.get("query", "").strip()
    customer_type = request.args.get("customer_type", "")
    marketing = request.args.get("marketing", "")
    customers = list_customers(query=query, customer_type=customer_type, marketing=marketing)
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["name", "customer_type", "email", "phone", "marketing_opt_in", "client_verified", "orders", "balance_due", "created_at"])
    for customer in customers:
        writer.writerow([
            customer["name"],
            customer["customer_type"],
            customer["email"],
            customer["phone"],
            customer["marketing_opt_in"],
            client_verified_label(customer["client_verified"]),
            customer["order_count"],
            customer["outstanding_balance"],
            customer["created_at"],
        ])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=customers.csv"})


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        try:
            _enforce_owner_only_credit(request.form)
            customer_id = create_customer(request.form)
            flash("Customer created", "success")
            return redirect(url_for("customers.detail", customer_id=customer_id))
        except ValueError as exc:
            flash(str(exc), "error")
    return render_template("admin/customers/form.html", settings=get_company_settings(), customer=None, custom_fields={}, custom_field_label=custom_field_label, can_set_credit=can_manage_customer_credit())


@bp.route("/<int:customer_id>")
@login_required
def detail(customer_id):
    customer = get_customer(customer_id)
    if not customer:
        flash("Customer not found", "error")
        return redirect(url_for("customers.index"))
    orders = customer_orders(customer_id)
    return render_template(
        "admin/customers/detail.html",
        settings=get_company_settings(),
        customer=customer,
        custom_fields=custom_fields_for(customer),
        orders=orders,
        outstanding_balance=customer_outstanding_balances([customer_id])[customer_id],
        customer_has_history=bool(orders) or customer_has_history(customer_id),
        custom_field_label=custom_field_label,
        client_verified_label=client_verified_label,
        customer_credit_balance=customer_credit_balance(customer_id),
        customer_credit_entries=customer_credit_entries(customer_id),
        legacy_balance=legacy_balance_summary(customer_id),
        legacy_payment_methods=[(method, LEGACY_PAYMENT_METHOD_LABELS[method]) for method in LEGACY_PAYMENT_METHODS],
        can_settle_legacy_balance=can_settle_legacy_balances(),
        funding_request_key=uuid.uuid4().hex,
    )


@bp.get('/<int:customer_id>/account-statement.pdf')
@login_required
def account_statement_pdf(customer_id):
    from app.services.account_statements import account_statement
    view=account_statement(customer_id,request.args.get('date_from',''),request.args.get('date_to',''))
    if view is None:
        abort(404)
    return Response(customer_statement_pdf_bytes(view),mimetype='application/pdf',
        headers={'Content-Disposition':f'inline; filename=account-statement-{customer_id}.pdf',
                 'Cache-Control':'no-store, no-cache, max-age=0, must-revalidate'})


@bp.post('/<int:customer_id>/prepaid-funding')
@login_required
def record_prepaid_funding(customer_id):
    if not can_settle_legacy_balances():
        abort(403)
    if not get_customer(customer_id):
        abort(404)
    if not request.form.get('request_key'):
        abort(400)
    from app.services.prepaid_funding import record_funding
    try:
        record_funding(customer_id,request.form)
        flash('Prepaid funds stored; revenue is recognised when used on an order','success')
    except ValueError as exc:
        flash(str(exc),'error')
    return redirect(url_for('customers.detail',customer_id=customer_id))


@bp.post('/<int:customer_id>/prepaid-funding/<int:funding_id>/reverse')
@login_required
def reverse_prepaid_funding(customer_id,funding_id):
    if not user_can_module(session,'payments'):
        abort(403)
    customer=get_customer(customer_id)
    if not customer:
        abort(404)
    from app.db import get_db,now
    from app.services.access import session_branch_scope_ids
    db=get_db()
    row=db.execute('SELECT * FROM prepaid_fundings WHERE id=? AND customer_id=?',(funding_id,customer_id)).fetchone()
    if not row:
        abort(404)
    scope=session_branch_scope_ids()
    if scope is not None and row['branch_id'] not in scope:
        abort(404)
    try:
        db.execute("UPDATE prepaid_fundings SET status='archived',deleted_at=? WHERE id=? AND status='paid'",(now(),funding_id))
        db.commit()
        flash('Prepaid funding reversed; original audit record retained','success')
    except Exception as exc:
        if 'Cannot reverse prepaid funds already used' not in str(exc):
            raise
        db.rollback()
        flash('Cannot reverse prepaid funds already used on orders; reverse the settlements first','error')
    return redirect(url_for('customers.detail',customer_id=customer_id))


@bp.post("/<int:customer_id>/legacy-payments")
@login_required
def record_legacy_balance_payment(customer_id):
    """Settle part or all of a customer's imported legacy balance.

    Ticket follow-up: the balance is only useful if staff can take the money on
    the spot, so the customer page (and the order form's link to it) posts here.
    """
    if not can_settle_legacy_balances():
        abort(403)
    customer = get_customer(customer_id)
    if not customer:
        flash("Customer not found", "error")
        return redirect(url_for("customers.index"))
    try:
        result = record_legacy_payment(customer_id, request.form)
        flash(f"R{result['amount']:.2f} recorded against previous orders. R{result['remaining']:.2f} still outstanding.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("customers.detail", customer_id=customer_id) + "#legacy-balance")


@bp.route("/<int:customer_id>/statement")
@login_required
def statement(customer_id):
    """Ticket ABI-341953085: the customer's statement of account as a PDF.

    Read-only: it renders finalised invoices and active payments for the
    customer, optionally limited to an inclusive ``date_from``/``date_to``
    range, and never writes anything.
    """
    customer = get_customer(customer_id)
    if not customer:
        flash("Customer not found", "error")
        return redirect(url_for("customers.index"))
    view = customer_statement(
        customer_id,
        date_from=normalise_payment_date_filter(request.args.get("date_from")),
        date_to=normalise_payment_date_filter(request.args.get("date_to")),
    )
    response = Response(
        customer_statement_pdf_bytes(view),
        mimetype="application/pdf",
        headers={"Content-Disposition": f"inline; filename=CUSTOMER-STATEMENT-{customer_id}.pdf"},
    )
    # Statements are re-rendered from the current ledger on every request, so a
    # browser/proxy must never reuse an older copy under this URL.
    response.headers["Cache-Control"] = "no-store, no-cache, max-age=0, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@bp.route("/<int:customer_id>/delete", methods=["POST"])
@login_required
def delete(customer_id):
    customer = get_customer(customer_id)
    if not customer:
        flash("Customer not found", "error")
        return redirect(url_for("customers.index"))
    try:
        deleted = delete_customer(customer_id)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("customers.detail", customer_id=customer_id))
    if deleted:
        flash("Customer deleted", "success")
    else:
        flash("Customer not found", "error")
    return redirect(url_for("customers.index"))


@bp.route("/<int:customer_id>/edit", methods=["GET", "POST"])
@login_required
def edit(customer_id):
    customer = get_customer(customer_id)
    if not customer:
        flash("Customer not found", "error")
        return redirect(url_for("customers.index"))
    if request.method == "POST":
        try:
            _enforce_owner_only_credit(request.form)
            update_customer(customer_id, request.form)
            flash("Customer saved", "success")
            return redirect(url_for("customers.detail", customer_id=customer_id))
        except ValueError as exc:
            flash(str(exc), "error")
    customer = get_customer(customer_id)
    return render_template("admin/customers/form.html", settings=get_company_settings(), customer=customer, custom_fields=custom_fields_for(customer), custom_field_label=custom_field_label, can_set_credit=can_manage_customer_credit())
