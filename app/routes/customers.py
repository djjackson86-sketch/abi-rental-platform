from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.routes.auth import login_required
from app.services.customers import create_customer, customer_counts, customer_orders, get_customer, list_customers, update_customer
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
        filters={"query": query, "customer_type": customer_type, "marketing": marketing},
    )


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
