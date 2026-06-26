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
        "products": db.execute("SELECT COUNT(*) c FROM products").fetchone()[ "c"],
        "customers": db.execute("SELECT COUNT(*) c FROM customers").fetchone()[ "c"],
        "orders": db.execute("SELECT COUNT(*) c FROM orders").fetchone()[ "c"],
        "tax_profiles": db.execute("SELECT COUNT(*) c FROM tax_profiles WHERE active = 1").fetchone()[ "c"],
    }
    completed = sum([counts["tax_profiles"] > 0, counts["products"] > 0, counts["customers"] > 0, counts["orders"] > 0])
    return render_template("admin/setup.html", settings=get_company_settings(), counts=counts, completed=completed)

@bp.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    metrics = {
        "orders": db.execute("SELECT COUNT(*) c FROM orders").fetchone()[ "c"],
        "products": db.execute("SELECT COUNT(*) c FROM products").fetchone()[ "c"],
        "customers": db.execute("SELECT COUNT(*) c FROM customers").fetchone()[ "c"],
        "revenue": db.execute("SELECT COALESCE(SUM(total),0) s FROM orders").fetchone()[ "s"],
    }
    return render_template("admin/dashboard.html", settings=get_company_settings(), metrics=metrics, schedule=dashboard_schedule())

def render_placeholder(name, title, description, primary_label=None):
    return render_template("admin/placeholder.html", settings=get_company_settings(), name=name, title=title, description=description, primary_label=primary_label)


@bp.route("/coupons")
@login_required
def coupons():
    return render_placeholder("Coupons", "Coupons", "Create and track coupon codes for online checkout discounts. Full coupon rules are planned for a parity pass.")


@bp.route("/app-store")
@login_required
def app_store():
    return render_placeholder("App store", "App store", "Review available integrations and add-ons for payments, documents, messaging and store extensions.")


@bp.route("/ask-bo")
@login_required
def ask_bo():
    return render_placeholder("Ask Bo", "Ask Bo", "Ask operational questions about rentals, inventory, orders and reports. AI assistant functionality is planned.")


@bp.route("/scan-barcode")
@login_required
def scan_barcode():
    return render_placeholder("Scan a barcode", "Scan a barcode", "Scan or enter product barcodes to find inventory, pick up orders and process returns.")


@bp.route("/help")
@login_required
def help_page():
    return render_placeholder("Help", "Help", "Find setup guidance, workflow help and support resources for the rental platform.")

@bp.route("/calendar")
@login_required
def calendar():
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    return render_template(
        "admin/calendar.html",
        settings=get_company_settings(),
        events=scheduled_events(start_date=start_date or None, end_date=end_date or None),
        filters={"start_date": start_date, "end_date": end_date},
    )

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
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    return render_template(
        "admin/reports.html",
        settings=get_company_settings(),
        metrics=summary_metrics(start_date=start_date or None, end_date=end_date or None),
        status_rows=orders_by_status(start_date=start_date or None, end_date=end_date or None),
        payment_rows=payments_by_method(start_date=start_date or None, end_date=end_date or None),
        product_rows=product_performance(start_date=start_date or None, end_date=end_date or None),
        customer_rows=customer_summary(start_date=start_date or None, end_date=end_date or None),
        filters={"start_date": start_date, "end_date": end_date},
    )

@bp.route("/reports/orders.csv")
@login_required
def reports_orders_csv():
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["order_number", "customer", "status", "payment_status", "total", "due_total"])
    for row in orders_export_rows(start_date=start_date or None, end_date=end_date or None):
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