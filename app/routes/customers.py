import csv
from io import StringIO

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for

from app.routes.auth import login_required
from app.services.customers import client_verified_label, create_customer, custom_field_label, custom_fields_for, customer_counts, customer_filter_counts, customer_has_history, customer_orders, delete_customer, get_customer, list_customers, update_customer
from app.services.settings import get_company_settings
from app.services.vehicles import list_vehicles

bp = Blueprint("customers", __name__, url_prefix="/customers")


@bp.route("")
@login_required
def index():
    query = request.args.get("query", "").strip()
    customer_type = request.args.get("customer_type", "")
    marketing = request.args.get("marketing", "")
    customers = list_customers(query=query, customer_type=customer_type, marketing=marketing)
    return render_template(
        "admin/customers/index.html",
        settings=get_company_settings(),
        customers=customers,
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
            customer["balance_due"],
            customer["created_at"],
        ])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=customers.csv"})


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        try:
            customer_id = create_customer(request.form)
            flash("Customer created", "success")
            return redirect(url_for("customers.detail", customer_id=customer_id))
        except ValueError as exc:
            flash(str(exc), "error")
    return render_template("admin/customers/form.html", settings=get_company_settings(), customer=None, custom_fields={}, custom_field_label=custom_field_label)


@bp.route("/<int:customer_id>")
@login_required
def detail(customer_id):
    customer = get_customer(customer_id)
    if not customer:
        flash("Customer not found", "error")
        return redirect(url_for("customers.index"))
    orders = customer_orders(customer_id)
    # Programme phase 7 (feature P §P1): the POPIA evidence line. Read-only, no extra
    # permission — it is one line of the client's own contact card, and a client with no
    # acceptance recorded says so rather than showing nothing.
    from app.services.consent import consent_summary
    # Programme phase 4 (feature A / A4): the client page owns the Vehicles panel, so the rows are
    # read here (same service A2/A3 use, same order) rather than fetched by the panel over JSON —
    # the page renders exactly what the database holds, with no second round trip to drift out of
    # step with it. `/customers/<id>/vehicles` stays as the feed for anything that needs JSON.
    return render_template(
        "admin/customers/detail.html",
        settings=get_company_settings(),
        customer=customer,
        custom_fields=custom_fields_for(customer),
        orders=orders,
        vehicles=list_vehicles(customer_id),
        consent_summary=consent_summary(customer_id),
        customer_has_history=bool(orders) or customer_has_history(customer_id),
        custom_field_label=custom_field_label,
        client_verified_label=client_verified_label,
    )


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
            update_customer(customer_id, request.form)
            flash("Customer saved", "success")
            return redirect(url_for("customers.detail", customer_id=customer_id))
        except ValueError as exc:
            flash(str(exc), "error")
    customer = get_customer(customer_id)
    return render_template("admin/customers/form.html", settings=get_company_settings(), customer=customer, custom_fields=custom_fields_for(customer), custom_field_label=custom_field_label)
