from flask import Blueprint, render_template, redirect, url_for
from app.routes.auth import login_required
from app.db import get_db
from app.services.settings import get_company_settings
from app.services.orders import dashboard_schedule, scheduled_events

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




@bp.route("/documents")
@login_required
def documents():
    return render_placeholder("Documents", "Documents", "Generate invoices, contracts, quotes and packing slips from orders.", "New document")


@bp.route("/online-store")
@login_required
def online_store():
    return render_placeholder("Online store", "Online store", "Configure public booking page, checkout, rental periods, availability and SEO.", "View store")


@bp.route("/reports")
@login_required
def reports():
    return render_placeholder("Reports", "Reports", "Company performance, product performance, availability, fulfillment and customer reports.", "Run report")
