from flask import Blueprint, render_template, redirect, request, url_for, Response, flash, abort
import csv
from datetime import date, timedelta
from io import StringIO
from app.routes.auth import login_required
from app.db import get_db
from app.services.settings import get_company_settings, update_online_store_settings
from app.services.orders import calendar_group_availability, calendar_month_overview, dashboard_schedule, scheduled_events
from app.services.reports import customer_summary, orders_by_status, orders_export_rows, payments_by_method, product_performance, summary_metrics
from app.services.app_store import list_app_store_items, update_app_store_item, seed_app_store_items
from app.services.timezone import local_now_iso
from app.services.access import order_branch_clause, product_branch_clause, resolve_branch_filter, session_branch_scope
from app.services.branches import branch_options

bp = Blueprint("admin", __name__)


def _scoped_branch_options():
    """Branches this session may filter by: all of them, or just the staff branch."""
    branch_id = session_branch_scope()
    branches = branch_options()
    if not branch_id:
        return branches
    return [branch for branch in branches if branch["id"] == branch_id]

def _branch_filter():
    """Resolve the ``?branch=`` filter for a branch-aware screen.

    Thin wrapper over the shared resolver in ``app.services.access`` so
    /calendar, /reports and /orders cannot drift apart. Returns
    ``(selected, branch_id, label, branches, scope)``.
    """
    return resolve_branch_filter(request.args.get("branch", ""))


@bp.route("/health")
def health():
    return {"ok": True, "app": "abi-rental-platform"}

@bp.route("/")
def index():
    return redirect(url_for("admin.dashboard"))

@bp.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    order_scope_sql, order_scope_params = order_branch_clause("o")
    product_scope_sql, product_scope_params = product_branch_clause("p", include_unassigned=True)
    metrics = {
        "orders": db.execute(f"SELECT COUNT(*) c FROM orders o WHERE 1=1{order_scope_sql}", order_scope_params).fetchone()[ "c"],
        "products": db.execute(f"SELECT COUNT(*) c FROM products p WHERE 1=1{product_scope_sql}", product_scope_params).fetchone()[ "c"],
        "customers": db.execute("SELECT COUNT(*) c FROM customers").fetchone()[ "c"],
        "revenue": db.execute(f"SELECT COALESCE(SUM(o.total),0) s FROM orders o WHERE 1=1{order_scope_sql}", order_scope_params).fetchone()[ "s"],
    }
    day_prefix = local_now_iso(timespec="seconds")[:10]
    day_metrics = {
        "orders": db.execute(f"SELECT COUNT(*) c FROM orders o WHERE substr(o.created_at, 1, 10) = ?{order_scope_sql}", [day_prefix, *order_scope_params]).fetchone()["c"],
        "customers": db.execute("SELECT COUNT(*) c FROM customers WHERE substr(created_at, 1, 10) = ?", (day_prefix,)).fetchone()["c"],
        "revenue": db.execute(f"SELECT COALESCE(SUM(o.total),0) s FROM orders o WHERE substr(o.created_at, 1, 10) = ?{order_scope_sql}", [day_prefix, *order_scope_params]).fetchone()["s"],
        "day": day_prefix,
    }
    return render_template(
        "admin/dashboard.html",
        settings=get_company_settings(),
        metrics=metrics,
        day_metrics=day_metrics,
        schedule=dashboard_schedule(),
    )

@bp.route("/coupons", methods=["GET", "POST"])
@bp.route("/coupons/<int:coupon_id>/edit", methods=["GET", "POST"])
@login_required
def coupons(coupon_id=None):
    abort(404)


@bp.route("/app-store", methods=["GET", "POST"])
@login_required
def app_store():
    # Seed default items if none exist
    seed_app_store_items()
    if request.method == "POST":
        item_id = request.form.get("item_id")
        if item_id:
            is_active = 1 if request.form.get("is_active") else 0
            try:
                update_app_store_item(item_id, is_active=is_active)
                flash("App store item updated", "success")
            except Exception as exc:
                flash(str(exc), "error")
        return redirect(url_for("admin.app_store"))
    items = list_app_store_items()
    return render_template("admin/app_store.html", items=items)


@bp.route("/scan-barcode", methods=["GET", "POST"])
@login_required
def scan_barcode():
    if request.method == "POST":
        barcode = (request.form.get("barcode") or "").strip()
        if not barcode:
            flash("Barcode is required", "error")
            return redirect(url_for("admin.scan_barcode"))
        # Look for product by SKU (barcode)
        product = get_db().execute("SELECT id FROM products WHERE sku = ? AND active = 1", (barcode,)).fetchone()
        if product:
            return redirect(url_for("inventory.edit", product_id=product["id"]))
        else:
            flash(f"No active product found with barcode '{barcode}'", "error")
            return redirect(url_for("admin.scan_barcode"))
    return render_template("admin/scan_barcode.html", settings=get_company_settings())

@bp.route("/calendar")
@login_required
def calendar():
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    selected_branch, branch_id, branch_label, branches, branch_scope = _branch_filter()
    month = request.args.get('month', '').strip()
    if not month and start_date:
        # Following the filtered period keeps the grid on the month you are
        # actually looking at instead of snapping back to today.
        month = start_date[:7]
    month_grid = calendar_month_overview(
        month=month or None, branch_id=branch_id, start_date=start_date or None, end_date=end_date or None
    )
    return render_template(
        "admin/calendar.html",
        settings=get_company_settings(),
        branches=branches,
        branch_scope=branch_scope,
        branch_label=branch_label,
        month_grid=month_grid,
        events=scheduled_events(start_date=start_date or None, end_date=end_date or None, branch_id=branch_id),
        availability=calendar_group_availability(start_date=start_date or None, end_date=end_date or None, branch_id=branch_id),
        filters={"start_date": start_date, "end_date": end_date, "branch": selected_branch},
    )

@bp.route("/online-store", methods=["GET", "POST"])
@login_required
def online_store():
    if request.method == "POST":
        update_online_store_settings(request.form)
        flash("Online store settings saved", "success")
        return redirect(url_for("admin.online_store"))
    return render_template("admin/online_store.html", settings=get_company_settings())

def _period_label(start_date, end_date, branch_label=''):
    """Human label for the active report filters, e.g. '1 Jul 2026 – 11 Sep 2026 · Midrand'."""
    def pretty(value):
        try:
            return date.fromisoformat(str(value)).strftime('%d %b %Y').lstrip('0')
        except (TypeError, ValueError):
            return ''

    start, end = pretty(start_date), pretty(end_date)
    if start and end:
        label = start if start == end else f'{start} – {end}'
    elif start:
        label = f'From {start}'
    elif end:
        label = f'Up to {end}'
    else:
        label = 'All time'
    return f'{label} · {branch_label}' if branch_label else label


def _report_presets(today=None):
    """Quick date ranges for the report filter rail."""
    today = today or date.today()
    first_of_month = today.replace(day=1)
    last_month_end = first_of_month - timedelta(days=1)
    return [
        {"label": "All time", "start": "", "end": ""},
        {"label": "Today", "start": today.isoformat(), "end": today.isoformat()},
        {"label": "Last 7 days", "start": (today - timedelta(days=6)).isoformat(), "end": today.isoformat()},
        {"label": "Last 30 days", "start": (today - timedelta(days=29)).isoformat(), "end": today.isoformat()},
        {"label": "This month", "start": first_of_month.isoformat(), "end": today.isoformat()},
        {"label": "Last month", "start": last_month_end.replace(day=1).isoformat(), "end": last_month_end.isoformat()},
        {"label": "Year to date", "start": date(today.year, 1, 1).isoformat(), "end": today.isoformat()},
    ]


@bp.route("/reports")
@login_required
def reports():
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    selected_branch, branch_id, branch_label, branches, branch_scope = _branch_filter()
    return render_template(
        "admin/reports.html",
        settings=get_company_settings(),
        branches=branches,
        branch_scope=branch_scope,
        branch_label=branch_label,
        period_label=_period_label(start_date, end_date, branch_label),
        presets=_report_presets(),
        metrics=summary_metrics(start_date=start_date or None, end_date=end_date or None, branch_id=branch_id),
        status_rows=orders_by_status(start_date=start_date or None, end_date=end_date or None, branch_id=branch_id),
        payment_rows=payments_by_method(start_date=start_date or None, end_date=end_date or None, branch_id=branch_id),
        product_rows=product_performance(start_date=start_date or None, end_date=end_date or None, branch_id=branch_id),
        customer_rows=customer_summary(start_date=start_date or None, end_date=end_date or None, branch_id=branch_id),
        filters={"start_date": start_date, "end_date": end_date, "branch": selected_branch},
    )

@bp.route("/reports/orders.csv")
@login_required
def reports_orders_csv():
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    _selected_branch, branch_id, _label, _branches, _scope = _branch_filter()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["order_number", "customer", "status", "payment_status", "total", "due_total"])
    for row in orders_export_rows(start_date=start_date or None, end_date=end_date or None, branch_id=branch_id):
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