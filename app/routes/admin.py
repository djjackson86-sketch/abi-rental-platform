from flask import Blueprint, render_template, redirect, url_for
from app.routes.auth import login_required
from app.db import get_db
from app.services.settings import get_company_settings

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
    return render_template("admin/dashboard.html", settings=get_company_settings(), metrics=metrics)


def render_placeholder(name, title, description, primary_label=None):
    return render_template("admin/placeholder.html", settings=get_company_settings(), name=name, title=title, description=description, primary_label=primary_label)


@bp.route("/calendar")
@login_required
def calendar():
    return render_placeholder("Calendar", "Calendar", "Timeline view for reservations, pickups, returns and availability checks.", "Check availability")


@bp.route("/orders")
@login_required
def orders():
    return render_placeholder("Orders", "Orders", "Create, reserve, pick up, return and invoice rental orders with live availability.", "Add order")


@bp.route("/customers")
@login_required
def customers():
    return render_placeholder("Customers", "Customers", "Manage individuals and companies, balances, marketing status and order history.", "Add customer")


@bp.route("/inventory")
@login_required
def inventory():
    return render_placeholder("Inventory", "Inventory", "Manage rental products, sale items, services, bundles, stock and public visibility.", "Add product")


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
