from flask import Blueprint, render_template, redirect, request, url_for, Response, flash
import csv
from io import StringIO
from app.routes.auth import login_required
from app.db import get_db
from app.services.settings import get_company_settings, update_online_store_settings
from app.services.orders import dashboard_schedule, scheduled_events
from app.services.reports import customer_summary, orders_by_status, orders_export_rows, payments_by_method, product_performance, summary_metrics

bp = Blueprint("admin", __name__)


@bp.route("/health")
def health():
    return {"ok": True, "app": "abi-rental-platform"}


@bp.route("/")
def index():
    return redirect(url_for("admin.setup"))


@bp.route("/setup")
@login_required
def setup():
    db = get_db()
    counts = {
        "products": db.execute("SELECT COUNT(*) c FROM products").fetchone()["c"],
        "customers": db.execute("SELECT COUNT(*) c FROM customers").fetchone()["c"],
        "orders": db.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"],
        "tax_profiles": db.execute("SELECT COUNT(*) c FROM tax_profiles WHERE active = 1").fetchone()["c"],
    }
    completed = sum([counts["tax_profiles"] > 0, counts["products"] > 0, counts["customers"] > 0, counts["orders"] > 0])
    return render_template("admin/setup.html", settings=get_company_settings(), counts=counts, completed=completed)


@bp.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    metrics = {
        "orders": db.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"],
        "products": db.execute("SELECT COUNT(*) c FROM products").fetchone()["c"],
        "customers": db.execute("SELECT COUNT(*) c FROM customers").fetchone()["c"],
        "revenue": db.execute("SELECT COALESCE(SUM(total),0) s FROM orders").fetchone()["s"],
    }
    return render_template("admin/dashboard.html", settings=get_company_settings(), metrics=metrics, schedule=dashboard_schedule())


def render_placeholder(name, title, description, primary_label=None):
    return render_template("admin/placeholder.html", settings=get_company_settings(), name=name, title=title, description=description, primary_label=primary_label)


@bp.route("/calendar")
@login_required
def calendar():
    return render_template("admin/calendar.html", settings=get_company_settings(), events=scheduled_events())



@bp.route("/online-store", methods=["GET", "POST"])
@login_required
def online_store():
    if request.method == "POST":
        update_online_store_settings(request.form)
        flash("Online store settings saved", "success")
        return redirect(url_for("admin.online_store"))
    return render_template("admin/online_store.html", settings=get_company_settings())


@bp.route("/reports")
@login_required
def reports():
    return render_template(
        "admin/reports.html",
        settings=get_company_settings(),
        metrics=summary_metrics(),
        status_rows=orders_by_status(),
        payment_rows=payments_by_method(),
        product_rows=product_performance(),
        customer_rows=customer_summary(),
    )


@bp.route("/reports/orders.csv")
@login_required
def reports_orders_csv():
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["order_number", "customer", "status", "payment_status", "total", "due_total"])
    for row in orders_export_rows():
        writer.writerow([
            row["order_number"],
            row["customer"],
            row["status"],
            row["payment_status"],
            f"{float(row['total'] or 0):.2f}",
            f"{float(row['due_total'] or 0):.2f}",
        ])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=orders.csv"},
    )
