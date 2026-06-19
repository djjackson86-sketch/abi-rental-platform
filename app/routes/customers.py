import csv
from io import StringIO

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for

from app.routes.auth import login_required
from app.services.customers import create_customer, customer_counts, customer_filter_counts, customer_orders, get_customer, list_customers, update_customer
from app.services.settings import get_company_settings

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
    writer.writerow(["name", "customer_type", "email", "phone", "marketing_opt_in", "orders", "balance_due", "created_at"])
    for customer in customers:
        writer.writerow([
            customer["name"],
            customer["customer_type"],
            customer["email"],
            customer["phone"],
            customer["marketing_opt_in"],
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
    return render_template("admin/customers/form.html", settings=get_company_settings(), customer=None)


@bp.route("/<int:customer_id>")
@login_required
def detail(customer_id):
    customer = get_customer(customer_id)
    if not customer:
        flash("Customer not found", "error")
        return redirect(url_for("customers.index"))
    return render_template("admin/customers/detail.html", settings=get_company_settings(), customer=customer, orders=customer_orders(customer_id))


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
    return render_template("admin/customers/form.html", settings=get_company_settings(), customer=get_customer(customer_id))
