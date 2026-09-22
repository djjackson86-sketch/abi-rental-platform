from datetime import datetime, date, time, timedelta
from math import ceil
import calendar as _month_calendar
import json
import os

from app.db import get_db, now
from app.services.numbering import next_in_sequence
from app.services.access import current_session_user_id, order_branch_clause, product_branch_clause as scoped_product_branch_clause, session_branch_scope_ids
from app.services.branches import pickup_hours_error
from app.services.settings import global_vat_rate
from app.services.products import product_branch_stock
from app.services.reports import collectible_due_expr
from app.services.timezone import local_now, local_now_iso

# "Sales/Repairs" is a REAL stored order status (ticket ABI-341952962): a draft
# order that hires nothing out is marked Sales/Repairs the moment the
# Sales/Repairs button is selected. It is deliberately NOT derived from the order
# lines any more — an order whose lines are all sales/service/custom work stays a
# plain draft until someone marks it.
SALES_REPAIRS_STATUS = "sales_repairs"
SALES_REPAIRS_LABEL = "Sales/Repairs"

STATUS_LABELS = {
    "draft": "Draft",
    "sales_repairs": SALES_REPAIRS_LABEL,
    "reserved": "Reserved",
    "started": "Started",
    "returned": "Returned",
    "archived": "Archived",
    "canceled": "Canceled",
}

BLOCKED_EDIT_STATUSES = {"archived", "canceled", "cancelled"}
BLOCKED_EDIT_MESSAGE = "Canceled and archived orders cannot be edited"


def status_label(status):
    """Display label for a stored status ("sales_repairs" -> "Sales/Repairs")."""
    value = (status or "").strip()
    return STATUS_LABELS.get(value) or value.replace("_", " ").title()


def can_edit_order_status(status):
    return status not in BLOCKED_EDIT_STATUSES


def _process_deposit_clause(alias="o"):
    """Returned orders that still need the refundable deposit action completed.

    This is deliberately separate from payment due. A returned order can still
    have rental money outstanding, but if its deposit has already been marked as
    refunded/used it should not remain in the staff "Process deposit" folder.
    """
    prefix = f"{alias}." if alias else ""
    return (
        f"{prefix}status IN ('started', 'returned', 'canceled', 'cancelled') "
        f"AND COALESCE({prefix}deposit_total, 0) > 0 "
        f"AND COALESCE({prefix}deposit_processed_at, '') = '' "
        f"AND COALESCE({prefix}deposit_process_method, '') = '' "
        f"AND (COALESCE({prefix}deposit_refund_amount, 0) = 0 OR COALESCE({prefix}deposit_applied_amount, 0) > 0)"
    )


def _status_clause(status, alias="o"):
    """(sql, params) for one Orders status filter value.

    Every value is a stored status now — ``sales_repairs`` included — so the
    list, the count badges, the CSV export and the rail all read the same column.
    """
    return f"{alias}.status = ?", [status]


def list_orders(query="", status="", payment_status="", return_status="", start_date="", end_date="", branch_id=None):
    sql = """SELECT o.*, c.name AS customer_name, c.email AS customer_email, cb.name AS collect_branch_name, rb.name AS return_branch_name,
        cu.name AS created_by_name, cu.email AS created_by_email,
        (SELECT COALESCE(SUM(quantity), 0) FROM order_items oi WHERE oi.order_id = o.id) AS item_count
        FROM orders o LEFT JOIN customers c ON c.id = o.customer_id
        LEFT JOIN branches cb ON cb.id = o.collect_branch_id
        LEFT JOIN branches rb ON rb.id = o.return_branch_id
        LEFT JOIN users cu ON cu.id = o.created_by_user_id WHERE 1=1"""
    params = []
    scope_sql, scope_params = order_branch_clause("o", branch_id=branch_id)
    sql += scope_sql
    params.extend(scope_params)
    if query:
        sql += " AND (LOWER(o.order_number) LIKE ? OR LOWER(c.name) LIKE ? OR LOWER(c.email) LIKE ?)"
        needle = f"%{query.lower()}%"
        params.extend([needle, needle, needle])
    if status:
        status_clause, status_params = _status_clause(status, "o")
        sql += f" AND {status_clause}"
        params.extend(status_params)
    if return_status == "late":
        sql += " AND o.status = 'started' AND o.end_at < ?"
        params.append(now())
    if payment_status == "process_deposit":
        sql += f" AND {_process_deposit_clause('o')}"
    elif payment_status:
        sql += " AND o.payment_status = ?"
        params.append(payment_status)
    if start_date:
        sql += " AND DATE(o.start_at) >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND DATE(o.start_at) <= ?"
        params.append(end_date)
    sql += " ORDER BY o.created_at DESC, o.id DESC"
    return get_db().execute(sql, params).fetchall()


def _order_filter_where(query="", status="", payment_status="", return_status="", start_date="", end_date="", branch_id=None):
    clauses = ["1=1"]
    params = []
    scope_sql, scope_params = order_branch_clause("o", branch_id=branch_id)
    if scope_sql:
        clauses.append(scope_sql.replace(" AND ", "", 1))
        params.extend(scope_params)
    if query:
        clauses.append("(LOWER(o.order_number) LIKE ? OR LOWER(c.name) LIKE ? OR LOWER(c.email) LIKE ?)")
        needle = f"%{query.lower()}%"
        params.extend([needle, needle, needle])
    if status:
        status_clause, status_params = _status_clause(status, "o")
        clauses.append(status_clause)
        params.extend(status_params)
    if return_status == "late":
        clauses.append("o.status = 'started' AND o.end_at < ?")
        params.append(now())
    if payment_status == "process_deposit":
        clauses.append(_process_deposit_clause('o'))
    elif payment_status:
        clauses.append("o.payment_status = ?")
        params.append(payment_status)
    if start_date:
        clauses.append("DATE(o.start_at) >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("DATE(o.start_at) <= ?")
        params.append(end_date)
    return " AND ".join(clauses), params


def order_counts(query="", status="", payment_status="", return_status="", start_date="", end_date="", branch_id=None):
    """Totals for the Orders page metric cards.

    Revenue is money RECEIVED (ticket ABI-341952993) — paid, non-archived payments
    dated inside the same window as the order list, taken on orders that survive
    the page's own filters — so the card agrees with the dashboard's "Revenue for
    the day" when the page is filtered to Today. The recognised (booked) value of
    the same orders is a deliberately different number and lives on Reports, which
    is relabelled "recognised (booked)".
    """
    where, params = _order_filter_where(query, status, payment_status, return_status, start_date, end_date, branch_id=branch_id)
    db = get_db()
    due_expr = collectible_due_expr("o")
    row = db.execute(f"""SELECT COUNT(*) total, COALESCE(SUM({due_expr}),0) due
        FROM orders o LEFT JOIN customers c ON c.id = o.customer_id WHERE {where}""", params).fetchone()
    item_row = db.execute(f"""SELECT COALESCE(SUM(oi.quantity),0) items FROM order_items oi
        JOIN orders o ON o.id = oi.order_id LEFT JOIN customers c ON c.id = o.customer_id WHERE {where}""", params).fetchone()
    # Money received for those orders. ``where`` already carries the branch,
    # status, payment-status and pickup-date filters, so the payment window below
    # is the only extra restriction: with no dates picked the card is everything
    # ever received against the orders on screen.
    payment_window_sql = ""
    payment_window_params = []
    if start_date:
        payment_window_sql += " AND substr(COALESCE(NULLIF(pay.payment_date, ''), pay.created_at), 1, 10) >= ?"
        payment_window_params.append(start_date)
    if end_date:
        payment_window_sql += " AND substr(COALESCE(NULLIF(pay.payment_date, ''), pay.created_at), 1, 10) <= ?"
        payment_window_params.append(end_date)
    received_row = db.execute(
        f"""SELECT COALESCE(SUM(pay.amount),0) revenue
        FROM payments pay
        JOIN orders o ON o.id = pay.order_id
        LEFT JOIN customers c ON c.id = o.customer_id
        WHERE {where} AND pay.status = 'paid' AND COALESCE(pay.deleted_at, '') = ''
        {payment_window_sql}""",
        [*params, *payment_window_params],
    ).fetchone()
    return {
        "total": row["total"] or 0,
        "revenue": received_row["revenue"] or 0,
        "due": row["due"] or 0,
        "items": item_row["items"] or 0,
    }


def deposit_to_process_amount(order):
    if not order or float(order["deposit_total"] or 0) <= 0:
        return 0
    if (order["deposit_process_method"] or "") or (order["deposit_processed_at"] or ""):
        return 0
    status = (order["status"] or "").lower()
    if status not in {"returned", "canceled", "cancelled"}:
        return 0
    if float(order["deposit_applied_amount"] or 0) or float(order["deposit_refund_amount"] or 0):
        return round(max(float(order["deposit_refund_amount"] or 0), 0), 2)
    return round(float(order["deposit_total"] or 0), 2)


def order_filter_counts(branch_id=None):
    """Counts for the Orders filter rail, honouring the branch filter."""
    db = get_db()
    scope_sql, scope_params = order_branch_clause("o", branch_id=branch_id)
    status_rows = db.execute(f"SELECT o.status AS status, COUNT(*) count FROM orders o WHERE 1=1{scope_sql} GROUP BY o.status", scope_params).fetchall()
    payment_rows = db.execute(f"SELECT o.payment_status AS payment_status, COUNT(*) count FROM orders o WHERE 1=1{scope_sql} GROUP BY o.payment_status", scope_params).fetchall()
    process_deposit_row = db.execute(f"SELECT COUNT(*) AS count FROM orders o WHERE 1=1{scope_sql} AND {_process_deposit_clause('o')}", scope_params).fetchone()
    late_return_row = db.execute(f"SELECT COUNT(*) AS count FROM orders o WHERE 1=1{scope_sql} AND o.status = 'started' AND o.end_at < ?", [*scope_params, now()]).fetchone()
    sales_repairs_row = db.execute(f"SELECT COUNT(*) AS count FROM orders o WHERE 1=1{scope_sql} AND o.status = ?", [*scope_params, SALES_REPAIRS_STATUS]).fetchone()
    payment_counts = {row["payment_status"]: row["count"] for row in payment_rows}
    payment_counts["process_deposit"] = process_deposit_row["count"] if process_deposit_row else 0
    status_counts = {row["status"]: row["count"] for row in status_rows}
    # Sales/Repairs is a stored status, so the GROUP BY above already carries it;
    # the explicit read only guarantees a 0 badge before anything is marked.
    status_counts[SALES_REPAIRS_STATUS] = sales_repairs_row["count"] if sales_repairs_row else 0
    return {
        "status": status_counts,
        "payment_status": payment_counts,
        "return_status": {"late": late_return_row["count"] if late_return_row else 0},
    }


def get_order(order_id):
    return get_db().execute(
        """SELECT o.*, c.name AS customer_name, c.email AS customer_email, c.phone AS customer_phone,
            c.address_line1 AS customer_address_line1, c.address_line2 AS customer_address_line2, c.suburb AS customer_suburb,
            c.city AS customer_city, c.province AS customer_province, c.postal_code AS customer_postal_code, c.country AS customer_country,
            c.custom_fields_json AS custom_fields_json, c.standard_discount_percent AS customer_standard_discount_percent,
            c.client_verified AS customer_client_verified,
            cb.name AS collect_branch_name, rb.name AS return_branch_name,
            cu.name AS created_by_name, cu.email AS created_by_email
        FROM orders o LEFT JOIN customers c ON c.id = o.customer_id
        LEFT JOIN branches cb ON cb.id = o.collect_branch_id
        LEFT JOIN branches rb ON rb.id = o.return_branch_id
        LEFT JOIN users cu ON cu.id = o.created_by_user_id WHERE o.id = ?""",
        (order_id,),
    ).fetchone()


def order_items(order_id):
    return get_db().execute(
        """SELECT oi.*, p.name AS product_name, p.sku AS product_sku, p.product_type, p.price_unit, p.security_deposit, p.hourly_extra_rate
        FROM order_items oi LEFT JOIN products p ON p.id = oi.product_id WHERE oi.order_id = ? ORDER BY oi.id""",
        (order_id,),
    ).fetchall()


def line_uses_order_days(item):
    """True when a line's price/display should use the order day count."""
    def item_value(key):
        if isinstance(item, dict):
            return item.get(key, "")
        try:
            return item[key]
        except (KeyError, IndexError, TypeError):
            return ""

    billing_mode = item_value("billing_mode") or ""
    product_type = item_value("product_type") or ""
    price_unit = item_value("price_unit") or ""
    return (
        billing_mode == "rental_day"
        or (
            billing_mode == "catalog"
            and product_type in {"rental", "service"}
            and price_unit in DURATION_PRICE_UNITS
        )
    )


def order_has_rental_items(items):
    """True when an order/document contains at least one hire-period line.

    Catalogue rental products are rental items. A custom line explicitly set to
    multiply by rental days is also treated as rental-like, because its price and
    displayed days depend on the pickup/return period. Sales and services are not.
    """
    def item_value(item, key):
        if isinstance(item, dict):
            return item.get(key, "")
        try:
            return item[key]
        except (KeyError, IndexError, TypeError):
            return ""

    return any(
        (item_value(item, "product_type") or "") == "rental"
        or (item_value(item, "billing_mode") or "") == "rental_day"
        for item in items
    )


ORDER_DELETE_BACKUP_DIR = os.path.join(os.path.expanduser("~"), "abi-backups")


def _row_dict(row):
    """Normalise a sqlite3/libsql row into a plain dict for the JSON snapshot."""
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    asdict = getattr(row, "asdict", None)
    if callable(asdict):
        return dict(asdict())
    fields = getattr(row, "_fields", None)
    if fields:
        return {name: row[name] for name in fields}
    keys = getattr(row, "keys", None)
    if callable(keys):
        return {key: row[key] for key in keys()}
    raise TypeError(f"cannot convert row of type {type(row)!r} to a dict")


def _backup_order_before_delete(order, items, payments, documents):
    """Write everything about to be deleted to ~/abi-backups/ (best effort).

    The libsql adapter autocommits every statement, so the deletes in
    delete_order() cannot be rolled back — this JSON file is the only recovery
    path. A failing backup must never block the caller's explicit delete, so the
    error is swallowed and reported as an empty path.
    """
    try:
        os.makedirs(ORDER_DELETE_BACKUP_DIR, exist_ok=True)
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        label = str(order["order_number"] or order["id"]).replace("/", "-")
        path = os.path.join(ORDER_DELETE_BACKUP_DIR, f"order-delete-{label}-{stamp}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "deleted_at_utc": now(),
                    "order": _row_dict(order),
                    "order_items": [_row_dict(row) for row in items],
                    "payments": [_row_dict(row) for row in payments],
                    "documents": [_row_dict(row) for row in documents],
                },
                handle,
                indent=2,
                default=str,
            )
        return path
    except Exception:
        return ""


def delete_order(order_id):
    """Permanently delete an order and everything attached to it.

    Children are deleted first and explicitly (documents -> payments ->
    order_items -> orders) because the libsql adapter autocommits: this is not one
    transaction, so the FK cascade cannot be relied on and the order of the
    statements is what keeps the run deterministic. Every affected row is dumped
    to ~/abi-backups/ first — there is no rollback. Returns a dict of rowcounts
    plus the backup path. The main-profile-only rule lives on the route.
    """
    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        raise ValueError("Order not found")
    items = db.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,)).fetchall()
    payments = db.execute("SELECT * FROM payments WHERE order_id = ?", (order_id,)).fetchall()
    documents = db.execute("SELECT * FROM documents WHERE order_id = ?", (order_id,)).fetchall()
    counts = {
        "backup_path": _backup_order_before_delete(order, items, payments, documents),
        "order_items": len(items),
        "payments": len(payments),
        "documents": len(documents),
    }
    for table in ("documents", "payments", "order_items", "orders"):
        cursor = db.execute(f"DELETE FROM {table} WHERE order_id = ?" if table != "orders" else "DELETE FROM orders WHERE id = ?",
                            (order_id,))
        db.commit()
        if table != "orders" and cursor is not None and cursor.rowcount is not None:
            counts[table] = cursor.rowcount
    counts["orders"] = 1
    return counts


def next_order_number():
    """Next order number.

    Follows the highest number actually in use rather than counting rows, so
    deleting or cancelling an order can never hand out a number twice, and
    continues the client's Booqable sequence (see app/services/numbering.py).
    """
    rows = get_db().execute(
        "SELECT order_number FROM orders WHERE COALESCE(order_number, '') != ''"
    ).fetchall()
    return next_in_sequence([row["order_number"] for row in rows], "ORD")


def _parse_dt(date_value, time_value, fallback_time):
    if not date_value:
        return None
    t = time_value or fallback_time
    return datetime.fromisoformat(f"{date_value}T{t}")



def next_time_slot(now_dt=None, increment_minutes=15):
    now_dt = now_dt or local_now()
    if increment_minutes <= 0:
        increment_minutes = 15
    minute = now_dt.minute
    remainder = minute % increment_minutes
    if remainder:
        now_dt = now_dt + timedelta(minutes=increment_minutes - remainder)
    return now_dt.replace(second=0, microsecond=0)

def rental_days(start_at, end_at):
    if not start_at or not end_at or end_at <= start_at:
        return 1
    hours = (end_at - start_at).total_seconds() / 3600
    return max(1, ceil(hours / 24))


def billed_rental_days(start_at, end_at, extra_hours=0, revised=False):
    """The day count a hire line was actually CHARGED for.

    A normal booking bills ``rental_days()`` - whole 24h blocks with a partial
    day rounding up. Once the actual return is revised the order is re-priced to
    full 24h blocks plus extra hours (``late_return_breakdown()``), so the day
    count shown must be the floor-based one or it disagrees with the money by a
    day (06 08:00 -> 09 10:00 = 3 days + 2 hours, billed R600 + R100, while
    ceil() would claim 4 days).

    ``extra_hours`` and ``revised`` are the evidence that a revision happened.
    ``revised`` (orders.return_revised_at) matters on its own because saving a
    damage settlement rewrites orders.extra_hours back to 0 on a returned order.
    """
    if revised or float(extra_hours or 0) > 0:
        return late_return_breakdown(start_at, end_at)["days"]
    return rental_days(start_at, end_at)


DURATION_PRICE_UNITS = {"day", "week", "month", "hour"}


def calculate_line(product, quantity, days, tax_mode="exclusive"):
    product_type = product["product_type"] or "rental"
    qty = 1 if product_type == "service" else max(1, int(quantity or 1))
    base = float(product["price_amount"] or 0) * qty
    if product_type in {"rental", "service"} and product["price_unit"] in DURATION_PRICE_UNITS:
        # v1 pricing is day-equivalent for duration-priced rental/service items;
        # service quantity stays 1 and service deposits stay zero.
        base *= days
    tax_rate = float(product["tax_rate"] or 0) / 100
    if tax_mode == "inclusive" and tax_rate:
        line_tax = base - (base / (1 + tax_rate))
        line_total = base
        line_subtotal = base - line_tax
    else:
        line_subtotal = base
        line_tax = base * tax_rate
        line_total = line_subtotal + line_tax
    deposit = 0 if product_type == "service" else float(product["security_deposit"] or 0) * qty
    return {"quantity": qty, "line_subtotal": round(line_subtotal, 2), "line_tax": round(line_tax, 2), "line_total": round(line_total, 2), "deposit": round(deposit, 2)}


def _form_list(form, name):
    if hasattr(form, "getlist"):
        return [value for value in form.getlist(name) if str(value).strip()]
    value = form.get(name)
    if isinstance(value, (list, tuple)):
        return [item for item in value if str(item).strip()]
    return [value] if value else []


def calculate_custom_line(name, quantity, unit_price, billing_mode, days, tax_rate=0, tax_mode="exclusive"):
    qty = max(1, int(quantity or 1))
    price = max(0, float(unit_price or 0))
    multiplier = days if billing_mode == "rental_day" else 1
    base = price * qty * multiplier
    rate = max(0, float(tax_rate or 0)) / 100
    if tax_mode == "inclusive" and rate:
        line_tax = base - (base / (1 + rate))
        line_total = base
        line_subtotal = base - line_tax
    else:
        line_subtotal = base
        line_tax = base * rate
        line_total = line_subtotal + line_tax
    return {"quantity": qty, "line_subtotal": round(line_subtotal, 2), "line_tax": round(line_tax, 2), "line_total": round(line_total, 2), "deposit": 0, "unit_price": price, "billing_mode": billing_mode}


def computed_discount(mode, value, subtotal, tax_total):
    """Money discount for a staff-applied order discount.

    mode is 'percent' or 'amount'. Percent discounts apply to the taxable
    subtotal only (tax, refundable deposit and damage waiver are not
    discounted). The discount is clamped so it can never exceed the
    subtotal + tax money base of the order.
    """
    subtotal = round(max(float(subtotal or 0), 0), 2)
    limit = round(subtotal + max(float(tax_total or 0), 0), 2)
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        return 0
    if value < 0:
        return 0
    if mode == "percent":
        discount = round(subtotal * value / 100, 2)
    else:
        discount = round(value, 2)
    return min(discount, limit)


def order_total_from_payload(payload):
    """The order total as the SUM OF ITS PARTS, never as a running delta.

    ``subtotal + tax - discount + refundable deposit + damage waiver`` is exactly
    how :func:`_build_order_payload` composes the total, so re-deriving it is safe
    at any point in a save. The update path used to subtract a carried-forward
    discount from the builder's total instead, which deducted it a SECOND time
    whenever the customer carries a standing standard discount - every re-save of
    such an order lost the discount again (ORD-10169: R49.70 off a R635.00 order,
    printed as a phantom "Amount due -R49.70" and an Overpaid status).
    """
    return round(
        float(payload.get("subtotal") or 0)
        + float(payload.get("tax_total") or 0)
        - float(payload.get("discount_total") or 0)
        + float(payload.get("deposit_total") or 0)
        + float(payload.get("damage_waiver_amount") or 0),
        2,
    )


def blocked_customer_error(customer):
    """Ticket ABI-341953028: the refusal message for a blocked customer."""
    try:
        reason = (customer["blocked_reason"] or "").strip()
    except (KeyError, IndexError, TypeError):
        reason = ""
    return f"Customer blocked — {reason or 'no reason recorded'}"


def _blocked_customer_allowance(value):
    """Normalise an "this order already had that customer" allowance to an id."""
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _build_order_payload(form, allow_blocked_customer_id=None):
    db = get_db()
    customer_id = int(form.get("customer_id") or 0) or None
    if customer_id:
        customer = db.execute("SELECT id, is_blocked, blocked_reason FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if not customer:
            raise ValueError("Selected customer was not found")
        # Ticket ABI-341953028: the server-side gate for the whole feature. A
        # blocked customer can never take a new order, whatever the browser did —
        # the "Are you sure / Customer Blocked" dialogs are only the visible half.
        # allow_blocked_customer_id lets an order that ALREADY had this customer
        # attached (a draft saved before the block) still be saved/edited; only
        # attaching a blocked customer to an order is refused.
        blocked = 0
        try:
            blocked = int(customer["is_blocked"] or 0)
        except (KeyError, IndexError, TypeError, ValueError):
            blocked = 0
        if blocked and customer_id != _blocked_customer_allowance(allow_blocked_customer_id):
            raise ValueError(blocked_customer_error(customer))
    booking_type = form.get("booking_type") if form.get("booking_type") in {"return", "oneway"} else "return"
    collect_branch_id = int(form.get("collect_branch_id") or 0) or None
    return_branch_id = int(form.get("return_branch_id") or 0) or collect_branch_id
    if booking_type == "return":
        return_branch_id = collect_branch_id
    if not collect_branch_id:
        default_branch = db.execute("SELECT id FROM branches WHERE active = 1 ORDER BY id LIMIT 1").fetchone()
        collect_branch_id = default_branch["id"] if default_branch else None
        return_branch_id = return_branch_id or collect_branch_id

    settings = db.execute("SELECT * FROM company_settings WHERE id = 1").fetchone()
    start_dt = _parse_dt(form.get("start_date"), form.get("start_time"), settings["default_pickup_time"])
    end_dt = _parse_dt(form.get("end_date"), form.get("end_time"), settings["default_return_time"])
    if not start_dt or not end_dt:
        raise ValueError("Pickup and return dates are required")
    if end_dt <= start_dt:
        raise ValueError("Return must be after pickup")

    # The pickup has to fall inside the collection branch's trading hours. Only
    # branches that have SAVED hours enforce this, so nothing changes for a depot
    # the client has not set hours for yet. Applies to staff orders and to public
    # storefront bookings alike, because both routes end up here.
    hours_error = pickup_hours_error(collect_branch_id, start_dt)
    if hours_error:
        raise ValueError(hours_error)

    days = rental_days(start_dt, end_dt)
    lines = []
    subtotal = tax_total = total = deposit_total = 0
    product_ids = form.getlist("product_id") if hasattr(form, "getlist") else _form_list(form, "product_id")
    quantities = form.getlist("quantity") if hasattr(form, "getlist") else _form_list(form, "quantity")
    custom_names = form.getlist("custom_name") if hasattr(form, "getlist") else _form_list(form, "custom_name")
    custom_prices = form.getlist("custom_unit_price") if hasattr(form, "getlist") else _form_list(form, "custom_unit_price")
    custom_modes = form.getlist("custom_billing_mode") if hasattr(form, "getlist") else _form_list(form, "custom_billing_mode")
    max_lines = max(len(product_ids), len(quantities), len(custom_names), len(custom_prices), len(custom_modes), 1)
    for index in range(max_lines):
        product_id_value = product_ids[index].strip() if index < len(product_ids) and product_ids[index] else ""
        quantity = quantities[index] if index < len(quantities) and quantities[index] else 1
        custom_name = custom_names[index].strip() if index < len(custom_names) and custom_names[index] else ""
        custom_price = custom_prices[index] if index < len(custom_prices) and custom_prices[index] else ""
        custom_mode = custom_modes[index] if index < len(custom_modes) and custom_modes[index] in {"fixed", "rental_day"} else "fixed"
        if product_id_value:
            product_id = int(product_id_value or 0)
            product = db.execute(
                """SELECT p.*, COALESCE(t.rate, 0) AS tax_rate FROM products p LEFT JOIN tax_profiles t ON t.id = p.tax_profile_id
                WHERE p.id = ? AND p.active = 1""",
                (product_id,),
            ).fetchone()
            if not product:
                raise ValueError("Selected product was not found or is archived")
            if collect_branch_id and product["branch_id"] and product["branch_id"] != collect_branch_id:
                raise ValueError("Selected product is not assigned to the collection branch")
            line = calculate_line(product, quantity, days, settings["tax_mode"])
            line["billing_mode"] = "catalog"
            lines.append({"product": product, "custom_name": "", "line": line})
        elif custom_name:
            line = calculate_custom_line(custom_name, quantity, custom_price or 0, custom_mode, days,
                                        global_vat_rate(), settings["tax_mode"])
            lines.append({"product": None, "custom_name": custom_name, "line": line})
        else:
            continue
        subtotal += line["line_subtotal"]
        tax_total += line["line_tax"]
        total += line["line_total"]
        deposit_total += line["deposit"]

    if not lines:
        raise ValueError("At least one product or custom line is required")

    subtotal = round(subtotal, 2)
    tax_total = round(tax_total, 2)
    standard_discount_percent = 0.0
    if customer_id:
        customer_discount_row = db.execute("SELECT standard_discount_percent FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if customer_discount_row:
            standard_discount_percent = float(customer_discount_row["standard_discount_percent"] or 0)
    # Coupon entry has been retired from reachable staff/admin workflows.
    # Keep the historical order columns/display intact, but do not apply
    # submitted coupon codes to newly saved/recalculated orders.
    coupon_code = ""
    discount_mode = "percent" if standard_discount_percent > 0 else ""
    discount_value = standard_discount_percent if standard_discount_percent > 0 else 0
    discount_total = computed_discount(discount_mode, discount_value, subtotal, tax_total) if discount_mode else 0
    total = round(subtotal + tax_total - discount_total, 2)
    deposit_option = form.get("deposit_option", "security_deposit")
    if deposit_option not in {"security_deposit", "damage_waiver", "no_deposit"}:
        deposit_option = "security_deposit"
    try:
        damage_waiver_amount = max(0, float(form.get("damage_waiver_amount") or 0)) if deposit_option == "damage_waiver" else 0
    except ValueError as exc:
        raise ValueError("Damage waiver amount must be a number") from exc
    deposit_total = round(deposit_total, 2) if deposit_option == "security_deposit" else 0
    total = round(total + damage_waiver_amount + deposit_total, 2)
    return {
        "customer_id": customer_id,
        "booking_type": booking_type,
        "collect_branch_id": collect_branch_id,
        "return_branch_id": return_branch_id,
        "start_at": start_dt.isoformat(timespec="minutes"),
        "end_at": end_dt.isoformat(timespec="minutes"),
        "subtotal": subtotal,
        "discount_total": discount_total,
        "discount_mode": discount_mode,
        "discount_value": discount_value,
        "coupon_code": coupon_code,
        "tax_total": tax_total,
        "deposit_total": deposit_total,
        "deposit_option": deposit_option,
        "damage_waiver_amount": damage_waiver_amount,
        "total": total,
        "notes": form.get("notes", "").strip(),
        "lines": lines,
    }


def _insert_order_items(order_id, lines):
    db = get_db()
    for entry in lines:
        product = entry["product"]
        line = entry["line"]
        product_id = product["id"] if product else None
        unit_price = float(product["price_amount"] or 0) if product else line["unit_price"]
        db.execute(
            """INSERT INTO order_items (order_id, product_id, custom_name, quantity, unit_price, line_subtotal, line_tax, line_total, billing_mode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (order_id, product_id, entry["custom_name"], line["quantity"], unit_price, line["line_subtotal"], line["line_tax"], line["line_total"], line["billing_mode"]),
        )


def _notify_new_order(order_id):
    """Best-effort Telegram "new order / booking request" message.

    A Telegram failure (or a disabled / unconfigured bot) must never break the
    order write, so the swallow is kept in one place. Ticket ABI-341952988 also
    routes BOTH notification moments — creation for public bookings, first status
    change for admin drafts — through here.

    Ticket ABI-341952993: a delivered message stamps ``new_order_notified_at``, so
    an order that is reverted to draft and then reserved again is never announced
    to the client group a second time. A failed send leaves the stamp empty, which
    only ever means the next departure from draft may try once more — never a
    duplicate of a message that did go out.
    """
    try:
        from app.services.telegram import send_new_order_notification
        send_new_order_notification(order_id)
    except Exception:
        return
    try:
        db = get_db()
        db.execute(
            "UPDATE orders SET new_order_notified_at = ? WHERE id = ? AND COALESCE(new_order_notified_at, '') = ''",
            (now(), order_id),
        )
        db.commit()
    except Exception:
        # Never let bookkeeping of the flag break the caller either.
        pass


def create_order(form, notify=True):
    payload = _build_order_payload(form)
    order_number = next_order_number()
    db = get_db()
    cur = db.execute(
        """INSERT INTO orders (order_number, customer_id, booking_type, collect_branch_id, return_branch_id, status, payment_status, start_at, end_at, subtotal, discount_total, discount_mode, discount_value, coupon_code, tax_total, deposit_total, deposit_option, damage_waiver_amount, total, due_total, notes, created_by_user_id, created_at)
        VALUES (?, ?, ?, ?, ?, 'draft', 'payment_due', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (order_number, payload["customer_id"], payload["booking_type"], payload["collect_branch_id"], payload["return_branch_id"], payload["start_at"], payload["end_at"], payload["subtotal"], payload["discount_total"], payload["discount_mode"], payload["discount_value"], payload["coupon_code"], payload["tax_total"], payload["deposit_total"], payload["deposit_option"], payload["damage_waiver_amount"], payload["total"], payload["total"], payload["notes"], current_session_user_id(), now()),
    )
    order_id = cur.lastrowid
    _insert_order_items(order_id, payload["lines"])
    db.commit()
    if notify:
        # Ticket ABI-341952988: an order created in the admin app is always
        # stored as a draft first, so it is NOT announced here — its "New order"
        # message goes out from transition_order() the moment its status leaves
        # draft. Public store booking requests keep notifying immediately, which
        # is why the default stays True and app/routes/public.py is untouched.
        _notify_new_order(order_id)
    return order_id


class _OrderFormWithLineLists:
    """Read-only view of a submitted form whose line lists can be patched.

    ``update_draft_order`` feeds the submitted form to ``_build_order_payload``.
    A data-safety net sometimes has to adjust one line value before that happens,
    and submitted lines arrive as parallel lists (product_id / custom_name /
    custom_unit_price / custom_billing_mode / quantity), so this wrapper hands
    out mutable copies of those lists while every other field (customer, dates,
    notes, deposit option) is read straight from the original form.
    """

    def __init__(self, form):
        self._form = form
        self._lists = {}

    def getlist(self, name):
        if name not in self._lists:
            if hasattr(self._form, "getlist"):
                self._lists[name] = [value for value in self._form.getlist(name)]
            else:
                self._lists[name] = list(_form_list(self._form, name))
        return list(self._lists[name])

    def setlist(self, name, values):
        self._lists[name] = list(values)

    def get(self, name, default=None):
        return self._form.get(name, default)


def _line_list(form, name):
    """Submitted line values, blanks included (unlike ``_form_list``)."""
    if hasattr(form, "getlist"):
        return [value for value in form.getlist(name)]
    return list(_form_list(form, name))


def _restore_custom_line_prices(form, order_id):
    """Refill a blank custom-line price from the order's stored line (ABI-341952987).

    The order form's line JS used to blank the unit price of any custom line
    whose product-search box was empty — and every saved custom line reopens with
    an empty product-search box — so a save-then-edit round trip silently zeroed
    the custom price and shrank the order total. The template is fixed, but the
    server no longer trusts a blank price on a line whose price it already knows:
    a blank entry on a custom line is refilled from that order's own order_items
    row (matched on the custom name, preferring the same billing mode). An
    explicit price — including an explicit 0 — is left exactly as submitted.
    """
    stored = {}
    for item in get_db().execute(
        """SELECT custom_name, billing_mode, unit_price FROM order_items
        WHERE order_id = ? AND COALESCE(product_id, 0) = 0 ORDER BY id""",
        (order_id,),
    ).fetchall():
        name = (item["custom_name"] or "").strip().lower()
        if not name:
            continue
        stored.setdefault(name, []).append(((item["billing_mode"] or "fixed"), float(item["unit_price"] or 0)))
    if not stored:
        return form

    product_ids = _line_list(form, "product_id")
    names = _line_list(form, "custom_name")
    prices = _line_list(form, "custom_unit_price")
    modes = _line_list(form, "custom_billing_mode")
    width = max(len(product_ids), len(names), len(prices), len(modes), 1)

    def value_at(values, index):
        return values[index] if index < len(values) else ""

    patched = [value_at(prices, index) for index in range(width)]
    changed = False
    for index in range(width):
        if str(value_at(product_ids, index) or "").strip():
            continue  # catalogue line: the price comes from the product
        name = str(value_at(names, index) or "").strip()
        if not name:
            continue  # blank row, nothing to keep
        if str(patched[index] or "").strip():
            continue  # a submitted price (including an explicit 0) is respected
        candidates = stored.get(name.lower()) or []
        if not candidates:
            continue
        mode = str(value_at(modes, index) or "fixed")
        stored_price = next((price for stored_mode, price in candidates if stored_mode == mode), candidates[0][1])
        patched[index] = f"{stored_price:g}"
        changed = True
    if not changed:
        return form
    patched_form = _OrderFormWithLineLists(form)
    patched_form.setlist("custom_unit_price", patched)
    return patched_form


def update_draft_order(order_id, form):
    order = get_order(order_id)
    if not order:
        raise ValueError("Order not found")
    if not can_edit_order_status(order["status"]):
        raise ValueError(BLOCKED_EDIT_MESSAGE)
    # Data-safety net (update path only): never let a blank custom-line price
    # wipe the stored price and shrink the order total.
    form = _restore_custom_line_prices(form, order_id)
    # Ticket ABI-341953028: a customer blocked AFTER this order was created can
    # still be kept on the order it is already attached to — only pointing the
    # order at a different blocked customer is refused.
    payload = _build_order_payload(form, allow_blocked_customer_id=order["customer_id"])
    db = get_db()
    # Data-safety net (update path only): the order form no longer exposes an
    # editable "Damage waiver amount" box, so a re-save of an existing
    # damage-waiver order may submit no amount (or an empty one). Preserve the
    # order's stored waiver fee instead of silently zeroing the fee and
    # shrinking the order total. The amount is only preserved while the order
    # still uses the damage_waiver deposit option.
    if payload["deposit_option"] == "damage_waiver" and float(order["damage_waiver_amount"] or 0) > 0:
        raw_amount = form.get("damage_waiver_amount") if hasattr(form, "get") else None
        if raw_amount is None or str(raw_amount).strip() == "":
            stored_waiver = round(float(order["damage_waiver_amount"]), 2)
            payload["damage_waiver_amount"] = stored_waiver
            payload["total"] = round(float(payload["total"] or 0) + stored_waiver, 2)
    # Data-safety net (update path only): the new/edit form no longer exposes
    # any discount option, so a re-save of a discounted order submits no
    # discount fields. Without this net the re-save would zero the applied
    # discount and silently raise the order total back up. Carry the stored
    # discount forward, recomputing percentage discounts against the edited
    # order's new subtotal while keeping flat R discounts fixed.
    stored_discount_mode = (order["discount_mode"] or "").strip().lower()
    if stored_discount_mode in ("percent", "amount"):
        try:
            stored_discount_value = float(order["discount_value"] or 0)
        except (TypeError, ValueError):
            stored_discount_value = 0.0
        if stored_discount_value > 0:
            payload["discount_mode"] = stored_discount_mode
            payload["discount_value"] = stored_discount_value
            payload["discount_total"] = computed_discount(
                stored_discount_mode, stored_discount_value, payload["subtotal"], payload["tax_total"]
            )
            # Re-derive the total from its parts. The payload builder already
            # deducted the CUSTOMER's standing standard discount, so subtracting
            # the stored discount from payload["total"] deducted it twice for
            # every customer carrying a standard discount (ORD-10169).
            payload["total"] = order_total_from_payload(payload)
    from app.services.payments import payment_total
    paid_total = payment_total(order_id)
    due_total = round(max(float(payload["total"] or 0) - paid_total, 0), 2)
    if paid_total <= 0:
        payment_status = "payment_due"
    elif paid_total < float(payload["total"] or 0):
        payment_status = "partially_paid"
    elif paid_total == float(payload["total"] or 0):
        payment_status = "paid"
    else:
        payment_status = "overpaid"
    db.execute(
        """UPDATE orders SET customer_id = ?, booking_type = ?, collect_branch_id = ?, return_branch_id = ?,
        start_at = ?, end_at = ?, subtotal = ?, discount_total = ?, discount_mode = ?, discount_value = ?, coupon_code = ?, tax_total = ?,
        deposit_total = ?, deposit_option = ?, damage_waiver_amount = ?, total = ?, due_total = ?,
        payment_status = ?, notes = ? WHERE id = ?""",
        (payload["customer_id"], payload["booking_type"], payload["collect_branch_id"], payload["return_branch_id"], payload["start_at"], payload["end_at"], payload["subtotal"], payload["discount_total"], payload["discount_mode"], payload["discount_value"], payload["coupon_code"], payload["tax_total"], payload["deposit_total"], payload["deposit_option"], payload["damage_waiver_amount"], payload["total"], due_total, payment_status, payload["notes"], order_id),
    )
    db.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
    _insert_order_items(order_id, payload["lines"])
    db.commit()
    return order_id


def apply_order_discount(order_id, mode, value):
    """Apply (or clear) a staff discount on the saved order page.

    mode is 'percent' (value = % of the taxable subtotal) or 'amount'
    (value = flat R discount). The discount is deducted from the order total
    without touching the stored subtotal/tax/deposit/damage-waiver parts, so
    existing extra-hours charges and refundable deposit settlement stay
    intact. Archived and canceled orders cannot be changed.
    """
    order = get_order(order_id)
    if not order:
        raise ValueError("Order not found")
    if not can_edit_order_status(order["status"]):
        raise ValueError(BLOCKED_EDIT_MESSAGE)
    mode = (mode or "").strip().lower()
    if mode in ("%", "percent", "percentage"):
        mode = "percent"
    elif mode in ("r", "rand", "amount", "fixed"):
        mode = "amount"
    else:
        raise ValueError("Discount type must be % or R")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError("Discount value must be a number")
    if value < 0:
        raise ValueError("Discount value cannot be negative")
    if mode == "percent" and value > 100:
        raise ValueError("Percentage discount cannot exceed 100")
    discount_total = computed_discount(mode, value, order["subtotal"], order["tax_total"])
    previous = float(order["discount_total"] or 0)
    new_total = round(float(order["total"] or 0) - (discount_total - previous), 2)
    if new_total < 0:
        new_total = 0
    db = get_db()
    db.execute(
        "UPDATE orders SET discount_mode = ?, discount_value = ?, discount_total = ?, total = ? WHERE id = ?",
        (mode, value, discount_total, new_total, order_id),
    )
    db.commit()
    from app.services.payments import recalculate_order_payment

    recalculate_order_payment(order_id)
    unit = "%" if mode == "percent" else "R"
    if discount_total:
        return f"Discount applied: {unit}{value:g} = R{discount_total:.2f}"
    return "Discount cleared"


def draft_order_form(order_id):
    order = get_order(order_id)
    if not order:
        raise ValueError("Order not found")
    if not can_edit_order_status(order["status"]):
        raise ValueError(BLOCKED_EDIT_MESSAGE)
    from app.services.customers import customer_summary_for
    start_at = datetime.fromisoformat(order["start_at"]) if order["start_at"] else None
    end_at = datetime.fromisoformat(order["end_at"]) if order["end_at"] else None
    customer_summary = None
    if order["customer_id"]:
        # Ticket ABI-341953028: the edit form shows the same blocked banner the
        # new-order form does, so the customer's block state rides along with the
        # summary built from the order's joined customer columns.
        block_row = get_db().execute(
            "SELECT is_blocked, blocked_reason FROM customers WHERE id = ?", (order["customer_id"],)
        ).fetchone()
        customer_summary = customer_summary_for({
            "id": order["customer_id"],
            "customer_type": "individual",
            "name": order["customer_name"],
            "email": order["customer_email"],
            "phone": order["customer_phone"],
            "address_line1": order["customer_address_line1"],
            "address_line2": order["customer_address_line2"],
            "suburb": order["customer_suburb"],
            "city": order["customer_city"],
            "province": order["customer_province"],
            "postal_code": order["customer_postal_code"],
            "country": order["customer_country"],
            "custom_fields_json": order["custom_fields_json"],
            "client_verified": order["customer_client_verified"],
            "is_blocked": block_row["is_blocked"] if block_row else 0,
            "blocked_reason": (block_row["blocked_reason"] if block_row else "") or "",
        })
    lines = []
    for item in order_items(order_id):
        product_display = ""
        if item["product_id"]:
            product_display = item["product_name"] or ""
            if item["product_sku"]:
                product_display += f" — {item['product_sku']}"
        lines.append({
            "product_id": item["product_id"] or "",
            "product_display": product_display,
            "custom_name": item["custom_name"] or "",
            "custom_billing_mode": item["billing_mode"] if item["billing_mode"] in {"fixed", "rental_day"} else "fixed",
            "custom_unit_price": item["unit_price"] if not item["product_id"] else "",
            "quantity": item["quantity"] or 1,
        })
    if not lines:
        lines.append({"product_id": "", "product_display": "", "custom_name": "", "custom_billing_mode": "fixed", "custom_unit_price": "", "quantity": 1})
    return {
        "order": order,
        "selected_customer_id": order["customer_id"] or "",
        "customer_display": order["customer_name"] + (f" — {order['customer_email']}" if order["customer_email"] else "") if order["customer_name"] else "",
        "customer_summary": customer_summary,
        "booking_type": order["booking_type"] or "return",
        "collect_branch_id": order["collect_branch_id"],
        "return_branch_id": order["return_branch_id"] or order["collect_branch_id"],
        "start_date": start_at.date().isoformat() if start_at else "",
        "start_time": start_at.strftime("%H:%M") if start_at else "",
        "end_date": end_at.date().isoformat() if end_at else "",
        "end_time": end_at.strftime("%H:%M") if end_at else "",
        "deposit_option": order["deposit_option"] or "security_deposit",
        "damage_waiver_amount": order["damage_waiver_amount"] or "",
        "coupon_code": order["coupon_code"] or "",
        "notes": order["notes"] or "",
        "lines": lines,
    }


TRANSITIONS = {
    "reserve": {"from": {"draft"}, "to": "reserved", "message": "Order reserved"},
    "start": {"from": {"draft", "reserved"}, "to": "started", "message": "Order started / picked up"},
    # Ticket ABI-341952962: selecting Sales/Repairs STORES the status, and the
    # inverse keeps "Save as draft" available on a Sales/Repairs order.
    "sales_repairs": {"from": {"draft"}, "to": SALES_REPAIRS_STATUS, "message": f"Order marked {SALES_REPAIRS_LABEL}"},
    "draft": {"from": {SALES_REPAIRS_STATUS}, "to": "draft", "message": "Order saved as draft"},
    "return": {"from": {"started"}, "to": "returned", "message": "Order returned"},
    "archive": {"from": {"returned", SALES_REPAIRS_STATUS}, "to": "archived", "message": "Order archived"},
    "cancel": {"from": {"draft", "reserved"}, "to": "canceled", "message": "Order canceled"},
    # Ticket ABI-341952993: the main profile can pull a live order back to draft.
    # Only orders that hold stock or are active are offered (reserved, picked up,
    # returned, Sales/Repairs) — a cancelled or archived order is never
    # resurrected. Reservations only count `reserved`/`started` orders, so the
    # stock a reserved or picked-up order held is released by the status change
    # itself; payments, quotes and invoices are deliberately left intact.
    "revert_draft": {
        "from": {"reserved", "started", "returned", SALES_REPAIRS_STATUS},
        "to": "draft",
        "message": "Order reverted to draft",
    },
}


def availability_errors(order_id):
    order = get_order(order_id)
    if not order or not order["start_at"] or not order["end_at"]:
        return ["Order needs a pickup and return date before it can be reserved"]
    errors = []
    db = get_db()
    for item in order_items(order_id):
        if not item["product_id"]:
            continue
        product = db.execute("SELECT name, quantity, branch_id, product_type, tracking_method FROM products WHERE id = ?", (item["product_id"],)).fetchone()
        if not product:
            errors.append("One of the products on this order is no longer available")
            continue
        # Services and untracked products keep no stock count, so they are always
        # bookable and must never be blocked by the availability check.
        if (product["product_type"] or "") == "service" or (product["tracking_method"] or "") == "none":
            continue
        # A product that holds per-branch counts is gated by the count for the
        # branch the booking is collected from — a branch with no row (or a 0
        # row) is not available from that branch. Products with no branch rows
        # keep the legacy single shared pool, bookable from every branch.
        branch_stock = product_branch_stock(item["product_id"])
        branch_scoped = product["branch_id"] is not None or bool(branch_stock)
        branch_clause = "AND COALESCE(o.collect_branch_id, 0) = COALESCE(?, 0)" if branch_scoped else ""
        params = [item["product_id"], order_id]
        if branch_scoped:
            params.append(order["collect_branch_id"])
        params.extend([order["end_at"], order["start_at"]])
        booked = db.execute(
            f"""SELECT COALESCE(SUM(oi.quantity), 0) AS booked
            FROM order_items oi JOIN orders o ON o.id = oi.order_id
            WHERE oi.product_id = ?
              AND o.id != ?
              AND o.status IN ('reserved', 'started')
              {branch_clause}
              AND o.start_at < ?
              AND o.end_at > ?""",
            params,
        ).fetchone()["booked"] or 0
        # Ticket ABI-341952987(2): a unit that is picked up and never returned
        # cannot be picked up again on another order. A started order whose
        # return time has passed is still physically out ("overrunning"), so it
        # holds one unit even for a later, non-overlapping hire — which is the
        # case the date-overlap check above cannot see. Started units that DO
        # overlap this order's period are already counted in `booked`, so they
        # are not counted twice here. Quantities, not orders, are counted, so
        # spare units on the same product stay hireable.
        out_params = [item["product_id"], order_id]
        if branch_scoped:
            out_params.append(order["collect_branch_id"])
        overdue_units = 0
        overdue_overlap_units = 0
        overdue_order_number = ""
        for out_row in db.execute(
            f"""SELECT o.id AS id, o.order_number AS order_number, o.start_at AS start_at,
                   o.end_at AS end_at, COALESCE(SUM(oi.quantity), 0) AS quantity
            FROM order_items oi JOIN orders o ON o.id = oi.order_id
            WHERE oi.product_id = ?
              AND o.id != ?
              AND o.status = 'started'
              {branch_clause}
            GROUP BY o.id, o.order_number, o.start_at, o.end_at
            ORDER BY o.id""",
            out_params,
        ).fetchall():
            out_quantity = int(out_row["quantity"] or 0)
            out_end = out_row["end_at"] or ""
            if out_end and out_end >= now():
                continue  # still inside its hire period: the overlap check above covers it
            if out_row["start_at"] and out_end and out_row["start_at"] < order["end_at"] and out_end > order["start_at"]:
                overdue_overlap_units += out_quantity
            elif not overdue_order_number:
                overdue_order_number = out_row["order_number"] or ""
            overdue_units += out_quantity
        still_out_units = max(0, overdue_units - overdue_overlap_units)
        busy = int(booked) + still_out_units

        def shortage_message(name):
            if branch_stock:
                message = f"Only {available} available for {name} at this collection branch during this rental period"
            else:
                message = f"Only {available} available for {name} during this rental period"
            if still_out_units > 0 and overdue_order_number:
                out_note = f"{name} is still out on {overdue_order_number} — return it before picking it up again."
                if available + still_out_units >= item["quantity"]:
                    # The unit that never came back is the whole reason this
                    # pickup is short — say that instead of a stock count.
                    return out_note
                message += f" {out_note}"
            return message

        if branch_stock:
            stock_total = int(branch_stock.get(int(order["collect_branch_id"] or 0), 0))
            available = stock_total - busy
            if item["quantity"] > available:
                errors.append(shortage_message(product["name"]))
        else:
            available = int(product["quantity"] or 0) - busy
            if item["quantity"] > available:
                errors.append(shortage_message(product["name"]))
    return errors


def transition_order(order_id, action):
    if action not in TRANSITIONS:
        raise ValueError("Unknown order action")
    order = get_order(order_id)
    if not order:
        raise ValueError("Order not found")
    transition = TRANSITIONS[action]
    if action == "start" and order["status"] == "started":
        # Ticket ABI-341952987(2): the same order is never picked up twice.
        # Name the collection moment so staff know which order must come back
        # first instead of reading a bare "status Started" refusal.
        picked_up_at = (order["picked_up_at"] or "").replace("T", " ").strip()
        detail = f" already picked up on {picked_up_at}" if picked_up_at else " already picked up"
        raise ValueError(f"This order is{detail} — return it before picking it up again.")
    if order["status"] not in transition["from"]:
        if action == "revert_draft":
            # Name the button and the eligible statuses: "Cannot revert_draft"
            # leaks the internal action key at the client.
            current = STATUS_LABELS.get(order["status"], order["status"])
            raise ValueError(
                f"Cannot revert an order with status {current} — Revert to Draft is available "
                "for reserved, picked up, returned and Sales/Repairs orders"
            )
        raise ValueError(f"Cannot {action} an order with status {STATUS_LABELS.get(order['status'], order['status'])}")
    if action == "return":
        validate_return_ready(order_id)
    if action == "sales_repairs" and order_has_rental_items(order_items(order_id)):
        # Server-side rule, not just a hidden button: a hire order is never a
        # sales/repairs order (ticket ABI-341952960/962).
        raise ValueError(f"{SALES_REPAIRS_LABEL} applies to orders without rental items")
    if action in {"reserve", "start"}:
        if not order["customer_id"]:
            raise ValueError("Add customer details before reserving or pickup")
        errors = availability_errors(order_id)
        if errors:
            raise ValueError(errors[0])
    db = get_db()
    previous_status = order["status"]
    if action == "start":
        # Record the real collection moment: "reservation pick ups for the day"
        # is a day figure, and the scheduled pickup is only a proxy for it.
        db.execute("UPDATE orders SET status = ?, picked_up_at = ? WHERE id = ?",
                   (transition["to"], now(), order_id))
    else:
        db.execute("UPDATE orders SET status = ? WHERE id = ?", (transition["to"], order_id))
    if action == "return" and order["booking_type"] == "oneway" and order["return_branch_id"]:
        for item in order_items(order_id):
            if item["product_id"]:
                db.execute("UPDATE products SET branch_id = ? WHERE id = ?", (order["return_branch_id"], item["product_id"]))
    if (
        action == "revert_draft"
        and previous_status == "returned"
        and order["booking_type"] == "oneway"
        and order["return_branch_id"]
    ):
        # Ticket ABI-341952993: the mirror of the return-leg move above. A
        # completed one-way hire left its units at the return branch; pulling the
        # order back to draft releases that stock, so the units have to sit in
        # the yard the booking was collected from — otherwise the released units
        # would be bookable at the wrong depot. Nothing else about the order (its
        # payments, its quotes and invoices, its picked_up_at history) moves.
        for item in order_items(order_id):
            if item["product_id"] and order["collect_branch_id"]:
                db.execute("UPDATE products SET branch_id = ? WHERE id = ?", (order["collect_branch_id"], item["product_id"]))
    db.commit()
    if (
        previous_status == "draft"
        and transition["to"] not in {"draft", "canceled"}
        and order["created_by_user_id"]
        and not order["new_order_notified_at"]
    ):
        # Ticket ABI-341952988: a draft raised in the admin app skipped its
        # creation notification, so this first move out of draft (Reserved,
        # Started or Sales/Repairs) is the moment the "New order" message goes
        # out — exactly once. A public/background order has
        # created_by_user_id = NULL and already notified at creation, so the id
        # check stops it ever being announced twice. Un-marking a Sales/Repairs
        # order back to draft and cancelling a draft stay silent; the send is
        # swallowed so messaging can never block a status change.
        #
        # Ticket ABI-341952993: "once" is per ORDER, not per departure from draft.
        # Reverting a live order to draft and reserving it again is now a normal
        # workflow, and it must not re-announce the same order to the client
        # group — hence the new_order_notified_at guard (stamped by the send).
        _notify_new_order(order_id)
    return transition["message"]


def status_actions(status, has_rental_items=False):
    """Order-detail buttons for a stored status, as (action, label, style).

    ``Sales/Repairs`` is offered on a draft that hires nothing out and stores the
    Sales/Repairs status (ticket ABI-341952962). Draft stays a valid status, so an
    unmarked sale order is still a draft — and a Sales/Repairs order keeps
    "Save as draft" (plus archive once the sale is done).

    Ticket ABI-341952993 adds ``revert_draft`` ("Revert to Draft") to every status
    that holds stock or is active. The button is only RENDERED for the main
    profile (``current_user_is_main`` in templates/admin/orders/detail.html) and
    the endpoint itself is main-gated, so a staff account is never offered it and
    cannot post it either. Cancelled and archived orders get nothing: a cancelled
    or archived order is not resurrected.
    """
    actions = []
    if status == "draft":
        actions.append(("reserve", "Reserve order", "primary"))
        if not has_rental_items:
            actions.append(("sales_repairs", SALES_REPAIRS_LABEL, "ghost"))
        actions.append(("start", "Pick up now", "ghost"))
        actions.append(("cancel", "Cancel order", "danger"))
    elif status == SALES_REPAIRS_STATUS:
        actions.append(("draft", "Save as draft", "ghost"))
        actions.append(("revert_draft", "Revert to Draft", "ghost"))
        actions.append(("archive", "Archive order", "ghost"))
    elif status == "reserved":
        actions.append(("start", "Start order", "primary"))
        actions.append(("revert_draft", "Revert to Draft", "ghost"))
        actions.append(("cancel", "Cancel order", "danger"))
    elif status == "started":
        actions.append(("return", "Return order", "primary"))
        actions.append(("revert_draft", "Revert to Draft", "ghost"))
    elif status == "returned":
        actions.append(("archive", "Archive order", "ghost"))
        actions.append(("revert_draft", "Revert to Draft", "ghost"))
    return actions


def can_process_return_deposit(order):
    return bool(order) and order["status"] in {"started", "returned", "canceled", "cancelled"}


def _require_return_deposit_allowed(order):
    if not can_process_return_deposit(order):
        raise ValueError("Return and deposit settlement is available after pickup or cancelation")


def _parse_deposit_processed_at(value):
    value = (value or "").strip()
    if not value:
        return now()
    try:
        processed_at = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Deposit refund date must be a valid date and time") from exc
    if processed_at > local_now().replace(microsecond=0):
        raise ValueError("Deposit refund date cannot be in the future")
    return processed_at.isoformat(timespec="seconds")


RETURN_CHARGE_NAMES = {"Extra hours", "Damage charge"}


def return_damage_total(order_id):
    row = get_db().execute("SELECT COALESCE(SUM(line_total), 0) AS total FROM order_items WHERE order_id = ? AND custom_name = 'Damage charge'", (order_id,)).fetchone()
    return round(float(row["total"] or 0), 2) if row else 0.0


def has_finalized_invoice(order_id):
    row = get_db().execute(
        """SELECT id FROM documents
        WHERE order_id = ? AND document_type = 'invoice' AND status = 'finalized'
        LIMIT 1""",
        (order_id,),
    ).fetchone()
    return bool(row)


def validate_return_ready(order_id):
    order = get_order(order_id)
    if not order:
        raise ValueError("Order not found")
    if not has_finalized_invoice(order_id):
        raise ValueError("Finalize the invoice before returning this order")
    damage_ok = bool(order["no_damages"]) or return_damage_total(order_id) > 0
    revision_ok = bool(order["no_revision_required"]) or bool(order["return_revised_at"] or "")
    if not damage_ok or not revision_ok:
        raise ValueError("Before returning, tick No Damages or record a damage charge, and tick No Revision Required or revise the actual return date/time")


def update_return_checklist(order_id, form):
    order = get_order(order_id)
    if not order:
        raise ValueError("Order not found")
    _require_return_deposit_allowed(order)
    updates = []
    params = []
    if "no_damages" in form:
        updates.append("no_damages = ?")
        params.append(1 if form.get("no_damages") else 0)
    if "no_revision_required" in form:
        updates.append("no_revision_required = ?")
        params.append(1 if form.get("no_revision_required") else 0)
    if updates:
        params.append(order_id)
        get_db().execute(f"UPDATE orders SET {', '.join(updates)} WHERE id = ?", params)
        get_db().commit()
    return "Return checklist saved"


def late_return_breakdown(start_at, actual_return_at):
    """Return billable full 24h rental days plus extra hours after pickup time.

    Sano Trailers rents in 24-hour blocks. Once the actual return passes the original
    pickup time on a later date, the completed 24h blocks are full rental days
    and only the remainder is extra hours (06 08:00 -> 09 10:00 = 3d + 2h).
    """
    if not start_at or not actual_return_at or actual_return_at <= start_at:
        return {"days": 1, "extra_hours": 0.0}
    total_hours = (actual_return_at - start_at).total_seconds() / 3600
    days = max(1, int(total_hours // 24))
    extra_hours = round(total_hours - (days * 24), 2)
    if extra_hours < 0.01:
        extra_hours = 0.0
    return {"days": days, "extra_hours": extra_hours}


def _line_recalc(item, days, tax_mode):
    qty = max(1, int(item["quantity"] or 1))
    unit_price = float(item["unit_price"] or 0)
    billing_mode = item["billing_mode"] or "catalog"
    product_type = item["product_type"] or ""
    price_unit = item["price_unit"] or ""
    multiplier = days if line_uses_order_days(item) else 1
    base = unit_price * qty * multiplier
    tax_rate = float(item["tax_rate"] or 0) / 100
    if tax_mode == "inclusive" and tax_rate:
        tax = base - (base / (1 + tax_rate))
        total = base
        subtotal = base - tax
    else:
        subtotal = base
        tax = base * tax_rate
        total = subtotal + tax
    return round(subtotal, 2), round(tax, 2), round(total, 2)


def _return_charge_tax(order_id, amount, settings):
    row = get_db().execute("""SELECT COALESCE(t.rate, 0) AS tax_rate FROM order_items oi
        LEFT JOIN products p ON p.id = oi.product_id
        LEFT JOIN tax_profiles t ON t.id = p.tax_profile_id
        WHERE oi.order_id = ? AND oi.product_id IS NOT NULL ORDER BY oi.id LIMIT 1""", (order_id,)).fetchone()
    rate = float(row["tax_rate"] or 0) / 100 if row else 0
    amount = round(float(amount or 0), 2)
    if settings["tax_mode"] == "inclusive" and rate:
        tax = amount - (amount / (1 + rate))
        return round(amount - tax, 2), round(tax, 2), amount
    return amount, round(amount * rate, 2), round(amount + (amount * rate), 2)


def revise_started_return(order_id, form):
    order = get_order(order_id)
    if not order:
        raise ValueError("Order not found")
    if order["status"] != "started":
        raise ValueError("Return date and time can only be revised after pickup and before return")
    if form.get("no_revision_required"):
        get_db().execute("UPDATE orders SET no_revision_required = 1 WHERE id = ?", (order_id,))
        get_db().commit()
        return "No revision required saved"
    start_at = datetime.fromisoformat(order["start_at"])
    settings = get_db().execute("SELECT * FROM company_settings WHERE id = 1").fetchone()
    actual_return_at = _parse_dt(form.get("end_date"), form.get("end_time"), settings["default_return_time"])
    if not actual_return_at or actual_return_at <= start_at:
        raise ValueError("Return must be after pickup")
    breakdown = late_return_breakdown(start_at, actual_return_at)
    days = breakdown["days"]
    extra_hours = breakdown["extra_hours"]
    db = get_db()
    rows = db.execute(
        """SELECT oi.*, p.product_type, p.price_unit, COALESCE(t.rate, 0) AS tax_rate
        FROM order_items oi
        LEFT JOIN products p ON p.id = oi.product_id
        LEFT JOIN tax_profiles t ON t.id = p.tax_profile_id
        WHERE oi.order_id = ? AND oi.custom_name NOT IN ('Extra hours', 'Damage charge')
        ORDER BY oi.id""",
        (order_id,),
    ).fetchall()
    subtotal = tax_total = line_total = 0.0
    for item in rows:
        line_subtotal, line_tax, total = _line_recalc(item, days, settings["tax_mode"])
        db.execute(
            "UPDATE order_items SET line_subtotal = ?, line_tax = ?, line_total = ? WHERE id = ?",
            (line_subtotal, line_tax, total, item["id"]),
        )
        subtotal += line_subtotal
        tax_total += line_tax
        line_total += total
    damage_rows = db.execute("SELECT line_subtotal, line_tax, line_total FROM order_items WHERE order_id = ? AND custom_name = 'Damage charge'", (order_id,)).fetchall()
    for row in damage_rows:
        subtotal += float(row["line_subtotal"] or 0)
        tax_total += float(row["line_tax"] or 0)
        line_total += float(row["line_total"] or 0)
    db.execute("DELETE FROM order_items WHERE order_id = ? AND custom_name = 'Extra hours'", (order_id,))
    hourly_rate = return_charge_defaults(order_id)["hourly_rate"]
    extra_charge = round(extra_hours * hourly_rate, 2)
    if extra_charge > 0:
        extra_subtotal, extra_tax, extra_total = _return_charge_tax(order_id, extra_charge, settings)
        db.execute(
            """INSERT INTO order_items (order_id, product_id, custom_name, quantity, unit_price, line_subtotal, line_tax, line_total, billing_mode)
            VALUES (?, NULL, 'Extra hours', ?, ?, ?, ?, ?, 'fixed')""",
            (order_id, extra_hours, hourly_rate, extra_subtotal, extra_tax, extra_total),
        )
        subtotal += extra_subtotal
        tax_total += extra_tax
        line_total += extra_total
    discount_total = computed_discount(order["discount_mode"], order["discount_value"], subtotal, tax_total) if (order["discount_mode"] or "") in {"percent", "amount"} else float(order["discount_total"] or 0)
    total = round(line_total - discount_total + float(order["deposit_total"] or 0) + float(order["damage_waiver_amount"] or 0), 2)
    db.execute(
        """UPDATE orders SET end_at = ?, extra_hours = ?, subtotal = ?, tax_total = ?, discount_total = ?, total = ?, no_revision_required = 0, return_revised_at = ? WHERE id = ?""",
        (actual_return_at.isoformat(timespec="minutes"), extra_hours, round(subtotal, 2), round(tax_total, 2), round(discount_total, 2), total, now(), order_id),
    )
    db.commit()
    from app.services.payments import recalculate_order_payment
    recalculate_order_payment(order_id)
    return f"Return revised: {days} rental day{'s' if days != 1 else ''} + {extra_hours:g} extra hour{'s' if extra_hours != 1 else ''}"


def return_charge_defaults(order_id):
    """Automatic rates for the return/deposit panel from order inventory."""
    for item in order_items(order_id):
        if item["product_id"]:
            hourly_rate = item["hourly_extra_rate"] if float(item["hourly_extra_rate"] or 0) > 0 else item["unit_price"]
            return {"hourly_rate": round(float(hourly_rate or 0), 2)}
    return {"hourly_rate": 0.0}


def add_return_charges(order_id, form):
    order = get_order(order_id)
    if not order:
        raise ValueError("Order not found")
    _require_return_deposit_allowed(order)
    try:
        extra_hours = 0
        hourly_rate = 0
        damage_charge = max(0, float(form.get("damage_charge") or 0))
    except ValueError as exc:
        raise ValueError("Extra hours, hourly rate and damage charge must be numbers") from exc
    extra_charge = round(extra_hours * hourly_rate, 2)
    damage_charge = round(damage_charge, 2)
    no_damages = 1 if form.get("no_damages") else 0
    if extra_charge <= 0 and damage_charge <= 0 and not no_damages:
        raise ValueError("Enter a damage charge or tick No Damages before saving damage settlement")
    db = get_db()
    existing_rows = db.execute(
        "SELECT id, line_subtotal, line_tax, line_total FROM order_items WHERE order_id = ? AND custom_name = 'Damage charge'",
        (order_id,),
    ).fetchall()
    removed_subtotal = round(sum(float(row["line_subtotal"] or 0) for row in existing_rows), 2)
    removed_tax = round(sum(float(row["line_tax"] or 0) for row in existing_rows), 2)
    removed_total = round(sum(float(row["line_total"] or 0) for row in existing_rows), 2)
    if existing_rows:
        db.execute("DELETE FROM order_items WHERE order_id = ? AND custom_name = 'Damage charge'", (order_id,))
    if extra_charge > 0:
        db.execute(
            """INSERT INTO order_items (order_id, product_id, custom_name, quantity, unit_price, line_subtotal, line_tax, line_total, billing_mode)
            VALUES (?, NULL, 'Extra hours', ?, ?, ?, 0, ?, 'fixed')""",
            (order_id, extra_hours, hourly_rate, extra_charge, extra_charge),
        )
    added_subtotal = added_tax = added_line_total = 0.0
    settings = db.execute("SELECT * FROM company_settings WHERE id = 1").fetchone()
    if damage_charge > 0:
        damage_subtotal, damage_tax, damage_total = _return_charge_tax(order_id, damage_charge, settings)
        db.execute(
            """INSERT INTO order_items (order_id, product_id, custom_name, quantity, unit_price, line_subtotal, line_tax, line_total, billing_mode)
            VALUES (?, NULL, 'Damage charge', 1, ?, ?, ?, ?, 'fixed')""",
            (order_id, damage_charge, damage_subtotal, damage_tax, damage_total),
        )
        added_subtotal += damage_subtotal
        added_tax += damage_tax
        added_line_total += damage_total
    new_subtotal = round(float(order["subtotal"] or 0) - removed_subtotal + added_subtotal, 2)
    new_tax = round(float(order["tax_total"] or 0) - removed_tax + added_tax, 2)
    new_total = round(float(order["total"] or 0) - removed_total + added_line_total, 2)
    db.execute(
        "UPDATE orders SET extra_hours = ?, subtotal = ?, tax_total = ?, total = ?, no_damages = ? WHERE id = ?",
        (extra_hours, new_subtotal, new_tax, new_total, no_damages if no_damages else 0, order_id),
    )
    db.commit()
    from app.services.payments import recalculate_order_payment
    recalculate_order_payment(order_id)
    return "Return charges added to order items"


DEPOSIT_UTILISED_REFERENCE = "Deposit Amount Utilised"


def _deposit_applied_payment(order_id):
    return get_db().execute(
        "SELECT id FROM payments WHERE order_id = ? AND method = 'deposit_applied' AND COALESCE(deleted_at, '') = '' ORDER BY id DESC LIMIT 1",
        (order_id,),
    ).fetchone()


def _non_deposit_paid_total(order_id):
    row = get_db().execute(
        """SELECT COALESCE(SUM(amount), 0) AS paid FROM payments
        WHERE order_id = ? AND status = 'paid' AND COALESCE(deleted_at, '') = ''
        AND method != 'deposit_applied'""",
        (order_id,),
    ).fetchone()
    return float(row["paid"] or 0) if row else 0.0


def _upsert_deposit_applied_payment(order_id, amount):
    db = get_db()
    existing = _deposit_applied_payment(order_id)
    if amount > 0:
        if existing:
            db.execute("UPDATE payments SET amount = ?, reference = ?, status = 'paid', deleted_at = '', created_at = ? WHERE id = ?", (amount, DEPOSIT_UTILISED_REFERENCE, now(), existing["id"]))
        else:
            db.execute(
                "INSERT INTO payments (order_id, amount, method, reference, status, created_at) VALUES (?, ?, 'deposit_applied', ?, 'paid', ?)",
                (order_id, amount, DEPOSIT_UTILISED_REFERENCE, now()),
            )
    elif existing:
        db.execute("UPDATE payments SET status = 'archived', deleted_at = ? WHERE id = ?", (now(), existing["id"]))


def settle_return_deposit(order_id, form):
    order = get_order(order_id)
    if not order:
        raise ValueError("Order not found")
    _require_return_deposit_allowed(order)
    deposit_process_method = (form.get("deposit_process_method") or "").strip().lower()
    if deposit_process_method not in {"eft", "card", "cash"}:
        raise ValueError("Deposit process method must be EFT, Card, or Cash")
    note = form.get("deposit_note", "").strip()
    deposit_processed_at = _parse_deposit_processed_at(form.get("deposit_processed_at"))
    deposit_available = round(float(order["deposit_total"] or 0), 2)
    non_deposit_paid = _non_deposit_paid_total(order_id)
    balance = round(max(float(order["total"] or 0) - non_deposit_paid, 0), 2)
    # If rental money is still due, the security deposit must settle that first;
    # only the true remainder can be paid out to the customer.
    applied_amount = round(min(deposit_available, balance), 2)
    refund_amount = round(max(deposit_available - applied_amount, 0), 2)
    db = get_db()
    _upsert_deposit_applied_payment(order_id, applied_amount)
    db.execute(
        """UPDATE orders SET deposit_applied_amount = ?, deposit_refund_amount = ?,
        deposit_process_method = ?, deposit_processed_at = ?, deposit_note = ? WHERE id = ?""",
        (applied_amount, refund_amount, deposit_process_method, deposit_processed_at, note, order_id),
    )
    db.commit()
    from app.services.payments import recalculate_order_payment
    recalculate_order_payment(order_id)
    if applied_amount > 0 and refund_amount > 0:
        return f"Deposit settled: R{applied_amount:.2f} used; R{refund_amount:.2f} refunded"
    if applied_amount > 0:
        return f"Deposit settled: R{applied_amount:.2f} used; no refund remaining"
    return f"Deposit refund processed: R{refund_amount:.2f}"

def use_return_deposit(order_id, form):
    order = get_order(order_id)
    if not order:
        raise ValueError("Order not found")
    _require_return_deposit_allowed(order)
    note = form.get("deposit_note", "").strip()
    deposit_available = round(float(order["deposit_total"] or 0), 2)
    db = get_db()
    non_deposit_paid = _non_deposit_paid_total(order_id)
    balance = round(max(float(order["total"] or 0) - non_deposit_paid, 0), 2)
    deposit_applied = round(min(deposit_available, balance), 2)
    deposit_refund = round(max(deposit_available - deposit_applied, 0), 2)
    _upsert_deposit_applied_payment(order_id, deposit_applied)
    db.execute(
        """UPDATE orders SET deposit_applied_amount = ?, deposit_refund_amount = ?,
        deposit_process_method = '', deposit_processed_at = '', deposit_note = ? WHERE id = ?""",
        (deposit_applied, deposit_refund, note, order_id),
    )
    db.commit()
    from app.services.payments import recalculate_order_payment
    recalculate_order_payment(order_id)
    return f"Security deposit used: R{deposit_applied:.2f}; refund R{deposit_refund:.2f}"


def _calendar_range(start_date=None, end_date=None):
    today = date.today()
    if start_date:
        range_start_date = date.fromisoformat(start_date)
    elif end_date:
        range_start_date = date.fromisoformat(end_date)
    else:
        range_start_date = today

    if end_date:
        range_end_date = date.fromisoformat(end_date)
    elif start_date:
        range_end_date = date.fromisoformat(start_date)
    else:
        range_end_date = range_start_date

    if range_end_date < range_start_date:
        range_start_date, range_end_date = range_end_date, range_start_date

    range_start = datetime.combine(range_start_date, time.min)
    range_end = datetime.combine(range_end_date + timedelta(days=1), time.min)
    return range_start, range_end


def _chunked(items, size):
    """Yield ``items`` in slices of ``size`` (keeps an IN (...) list inside SQLite's
    bound-parameter limit without turning it back into one query per row)."""
    for start in range(0, len(items), size):
        yield items[start:start + size]


def calendar_group_availability(start_date=None, end_date=None, branch_id=None):
    range_start, range_end = _calendar_range(start_date, end_date)
    db = get_db()
    product_branch_clause, product_branch_params = scoped_product_branch_clause(
        "p", include_unassigned=True, branch_id=branch_id
    )
    # With a branch selected, a product that holds a per-branch count for that
    # branch is stock for it even when the product's own branch differs — the
    # session scope still wins, so this can only ever narrow. A session limited to
    # several depots matches any of them. Products without per-branch rows keep
    # the plain clause (unchanged behaviour).
    branch_targets = session_branch_scope_ids()
    if branch_targets is None:
        branch_targets = [branch_id] if branch_id else []
    if branch_targets and product_branch_clause:
        marks = ",".join("?" for _ in branch_targets)
        product_branch_clause = (
            f" AND (p.branch_id IN ({marks}) OR EXISTS (SELECT 1 FROM product_branch_stock s"
            f" WHERE s.product_id = p.id AND s.branch_id IN ({marks})))"
        )
        product_branch_params = [*branch_targets, *branch_targets]
    products = db.execute(
        f"""SELECT p.id, p.name, p.sku, p.quantity, p.tracking_method,
               COALESCE(pg.id, 0) AS group_id,
               COALESCE(pg.name, 'Ungrouped trailers') AS group_name
        FROM products p
        LEFT JOIN product_groups pg ON pg.id = p.product_group_id AND pg.active = 1
        WHERE p.active = 1 AND p.product_type = 'rental'{product_branch_clause}
        ORDER BY CASE WHEN pg.id IS NULL THEN 1 ELSE 0 END, pg.sort_order, pg.name, p.name""",
        product_branch_params,
    ).fetchall()

    groups = []
    groups_by_id = {}
    product_ids = [int(product["id"]) for product in products]

    # Per-branch counts, booked totals and the reservation rows are all fetched in ONE
    # pass for the whole view. Until 2026-09-13 this looped per product and ran two
    # queries each, which cost ~180 Turso round trips per page view (~30s live) even
    # though production held 3 orders - the cost was round trips, not data. The list is
    # chunked only to stay inside SQLite's bound-parameter limit.
    branch_stock = {}
    branch_keys = {}
    booked_by_product = {}
    reservations_by_product = {}
    for chunk in _chunked(product_ids, 400):
        marks = ",".join("?" for _ in chunk)
        for row in db.execute(
            f"SELECT product_id, branch_id, quantity FROM product_branch_stock WHERE product_id IN ({marks})",
            chunk,
        ).fetchall():
            branch_stock.setdefault(int(row["product_id"]), {})[int(row["branch_id"])] = int(row["quantity"] or 0)
        # A product holding per-branch counts is counted per collection branch; every
        # other product keeps the all-branch behaviour, exactly as before.
        for product_id in chunk:
            branch_keys[product_id] = int(branch_id) if (branch_stock.get(product_id) and branch_id) else None

        booking_params = [*chunk, range_end.isoformat(timespec="seconds"),
                          range_start.isoformat(timespec="seconds")]
        for row in db.execute(
            f"""SELECT oi.product_id,
                       COALESCE(o.collect_branch_id, 0) AS collect_branch_id,
                       COALESCE(SUM(oi.quantity), 0) AS booked
                FROM order_items oi
                JOIN orders o ON o.id = oi.order_id
                WHERE oi.product_id IN ({marks})
                  AND o.status IN ('reserved', 'started')
                  AND o.start_at < ?
                  AND o.end_at > ?
                GROUP BY oi.product_id, COALESCE(o.collect_branch_id, 0)""",
            booking_params,
        ).fetchall():
            booked_by_product.setdefault(int(row["product_id"]), {})[
                int(row["collect_branch_id"])
            ] = int(row["booked"] or 0)

        for row in db.execute(
            f"""SELECT oi.product_id, o.id, o.order_number, o.status, o.start_at, o.end_at,
                       oi.quantity, COALESCE(o.collect_branch_id, 0) AS collect_branch_id
                FROM order_items oi
                JOIN orders o ON o.id = oi.order_id
                WHERE oi.product_id IN ({marks})
                  AND o.status IN ('reserved', 'started')
                  AND o.start_at < ?
                  AND o.end_at > ?
                ORDER BY o.start_at, o.id""",
            booking_params,
        ).fetchall():
            product_key = int(row["product_id"])
            branch_key = branch_keys.get(product_key)
            if branch_key is not None and int(row["collect_branch_id"]) != branch_key:
                continue
            reservations_by_product.setdefault(product_key, []).append({
                "id": row["id"],
                "order_number": row["order_number"],
                "status": row["status"],
                "start_at": row["start_at"],
                "end_at": row["end_at"],
                "quantity": row["quantity"],
            })

    for product in products:
        group_id = product["group_id"]
        group = groups_by_id.get(group_id)
        if not group:
            group = {
                "id": group_id,
                "name": product["group_name"],
                "products": [],
                "total_quantity": 0,
                "booked_quantity": 0,
                "available_quantity": 0,
            }
            groups_by_id[group_id] = group
            groups.append(group)

        product_key = int(product["id"])
        stock_rows = branch_stock.get(product_key)
        # Bookings and reservations came from the single pass above: a branch-managed
        # product counts only the selected branch's bookings, everything else counts
        # across branches exactly as it did when this ran a query per product.
        branch_key = branch_keys.get(product_key)
        if branch_key is None:
            booked = sum(booked_by_product.get(product_key, {}).values())
        else:
            booked = booked_by_product.get(product_key, {}).get(branch_key, 0)
        reservations = reservations_by_product.get(product_key, [])

        if stock_rows:
            # Selected branch -> that branch's count; no filter -> the total of
            # all branch counts.
            total = int(stock_rows.get(int(branch_id), 0)) if branch_id else sum(stock_rows.values())
        else:
            total = int(product["quantity"] or 0)
        booked = int(booked or 0)
        available = max(total - booked, 0)
        # Products set to "Don't track quantities" hold no stock count, so the
        # calendar reports them as not tracked instead of 0 of 0.
        untracked = (product["tracking_method"] or "") == "none"
        product_row = {
            "id": product["id"],
            "name": product["name"],
            "sku": product["sku"],
            "tracking_method": product["tracking_method"],
            "untracked": untracked,
            "branch_split": bool(stock_rows),
            "total_quantity": total,
            "booked_quantity": booked,
            "available_quantity": available,
            "reservations": reservations,
        }
        group["products"].append(product_row)
        if not untracked:
            group["total_quantity"] += total
            group["booked_quantity"] += booked
            group["available_quantity"] += available

    return {
        "start_date": range_start.date().isoformat(),
        "end_date": (range_end.date() - timedelta(days=1)).isoformat(),
        "groups": groups,
    }


def scheduled_events(limit=50, start_date=None, end_date=None, branch_id=None):
    db = get_db()
    sql = """SELECT o.*, c.name AS customer_name, cb.name AS collect_branch_name, rb.name AS return_branch_name,
            (SELECT GROUP_CONCAT(COALESCE(p.name, oi.custom_name), ', ')
             FROM order_items oi LEFT JOIN products p ON p.id = oi.product_id
             WHERE oi.order_id = o.id) AS product_names
        FROM orders o LEFT JOIN customers c ON c.id = o.customer_id
        LEFT JOIN branches cb ON cb.id = o.collect_branch_id
        LEFT JOIN branches rb ON rb.id = o.return_branch_id
        WHERE o.status IN ('reserved', 'started')"""
    params = []
    scope_sql, scope_params = order_branch_clause("o", branch_id=branch_id)
    sql += scope_sql
    params.extend(scope_params)
    if start_date:
        sql += " AND DATE(o.start_at) >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND DATE(o.start_at) <= ?"
        params.append(end_date)
    sql += " ORDER BY o.start_at ASC, o.end_at ASC LIMIT ?"
    params.append(limit)
    return db.execute(sql, params).fetchall()
def calendar_month_overview(month=None, branch_id=None, start_date=None, end_date=None):
    """Monday-first month grid with per-day pickup and return counts.

    Backs the visual month calendar above the reservation filters. The counts
    come from exactly the same orders the timeline lists, so the grid and the
    list underneath it can never contradict each other. ``branch_id`` obeys the
    usual rule: the session scope wins, a filter only narrows.
    """
    today = date.today()
    try:
        year, month_number = (int(part) for part in str(month).split("-"))
        first_day = date(year, month_number, 1)
    except (AttributeError, TypeError, ValueError):
        first_day = today.replace(day=1)
    last_day = date(
        first_day.year,
        first_day.month,
        _month_calendar.monthrange(first_day.year, first_day.month)[1],
    )

    if start_date and end_date and end_date < start_date:
        start_date, end_date = end_date, start_date

    db = get_db()
    sql = """SELECT o.start_at, o.end_at FROM orders o
        WHERE o.status IN ('reserved', 'started')
          AND DATE(o.start_at) <= ? AND DATE(o.end_at) >= ?"""
    params = [last_day.isoformat(), first_day.isoformat()]
    scope_sql, scope_params = order_branch_clause("o", branch_id=branch_id)
    sql += scope_sql
    params.extend(scope_params)

    pickups = {}
    returns = {}
    for row in db.execute(sql, params).fetchall():
        pickup_day = str(row["start_at"] or "")[:10]
        return_day = str(row["end_at"] or "")[:10]
        if pickup_day:
            pickups[pickup_day] = pickups.get(pickup_day, 0) + 1
        if return_day:
            returns[return_day] = returns.get(return_day, 0) + 1

    weeks = []
    for week in _month_calendar.Calendar(firstweekday=0).monthdatescalendar(first_day.year, first_day.month):
        days = []
        for day in week:
            key = day.isoformat()
            days.append({
                "date": key,
                "day": day.day,
                "in_month": day.month == first_day.month,
                "pickups": pickups.get(key, 0),
                "returns": returns.get(key, 0),
                "is_today": day == today,
                "selected": key == start_date or key == end_date
                            or bool(start_date and end_date and start_date < key < end_date),
            })
        weeks.append(days)

    return {
        "month": first_day.strftime("%Y-%m"),
        "label": first_day.strftime("%B %Y"),
        "weeks": weeks,
        "previous_month": (first_day - timedelta(days=1)).strftime("%Y-%m"),
        "next_month": (last_day + timedelta(days=1)).strftime("%Y-%m"),
        "is_current_month": first_day.year == today.year and first_day.month == today.month,
    }


def dashboard_schedule(branch_id=None):
    """Today's going-out / coming-back lists for the dashboard.

    ``branch_id`` is the dashboard's own branch filter; it goes through
    ``order_branch_clause`` like every other branch-aware query, so the session
    scope still wins and the filter can only narrow.
    """
    events = scheduled_events(limit=100, branch_id=branch_id)
    return {
        "going_out": [event for event in events if event["status"] == "reserved"][:5],
        "coming_back": [event for event in events if event["status"] in {"reserved", "started"}][:5],
    }