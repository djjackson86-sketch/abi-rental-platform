from datetime import datetime, date, time
from math import ceil

from app.db import get_db, now

STATUS_LABELS = {
    "draft": "Draft",
    "reserved": "Reserved",
    "started": "Started",
    "returned": "Returned",
    "archived": "Archived",
    "canceled": "Canceled",
}


def list_orders(query="", status="", payment_status=""):
    sql = """SELECT o.*, c.name AS customer_name, c.email AS customer_email,
        (SELECT COALESCE(SUM(quantity), 0) FROM order_items oi WHERE oi.order_id = o.id) AS item_count
        FROM orders o LEFT JOIN customers c ON c.id = o.customer_id WHERE 1=1"""
    params = []
    if query:
        sql += " AND (LOWER(o.order_number) LIKE ? OR LOWER(c.name) LIKE ? OR LOWER(c.email) LIKE ?)"
        needle = f"%{query.lower()}%"
        params.extend([needle, needle, needle])
    if status:
        sql += " AND o.status = ?"
        params.append(status)
    if payment_status:
        sql += " AND o.payment_status = ?"
        params.append(payment_status)
    sql += " ORDER BY o.created_at DESC, o.id DESC"
    return get_db().execute(sql, params).fetchall()


def order_counts():
    row = get_db().execute("SELECT COUNT(*) total, COALESCE(SUM(total),0) revenue, COALESCE(SUM(due_total),0) due FROM orders").fetchone()
    item_row = get_db().execute("SELECT COALESCE(SUM(quantity),0) items FROM order_items").fetchone()
    return {"total": row["total"] or 0, "revenue": row["revenue"] or 0, "due": row["due"] or 0, "items": item_row["items"] or 0}


def get_order(order_id):
    return get_db().execute(
        """SELECT o.*, c.name AS customer_name, c.email AS customer_email, c.phone AS customer_phone
        FROM orders o LEFT JOIN customers c ON c.id = o.customer_id WHERE o.id = ?""",
        (order_id,),
    ).fetchone()


def order_items(order_id):
    return get_db().execute(
        """SELECT oi.*, p.name AS product_name, p.sku AS product_sku, p.product_type, p.security_deposit
        FROM order_items oi LEFT JOIN products p ON p.id = oi.product_id WHERE oi.order_id = ? ORDER BY oi.id""",
        (order_id,),
    ).fetchall()


def next_order_number():
    row = get_db().execute("SELECT COUNT(*) c FROM orders").fetchone()
    return f"ORD-{(row['c'] or 0) + 1:05d}"


def _parse_dt(date_value, time_value, fallback_time):
    if not date_value:
        return None
    t = time_value or fallback_time
    return datetime.fromisoformat(f"{date_value}T{t}")


def rental_days(start_at, end_at):
    if not start_at or not end_at or end_at <= start_at:
        return 1
    hours = (end_at - start_at).total_seconds() / 3600
    return max(1, ceil(hours / 24))


def calculate_line(product, quantity, days, tax_mode="exclusive"):
    qty = max(1, int(quantity or 1))
    base = float(product["price_amount"] or 0) * qty
    if product["price_unit"] in {"day", "week", "month", "hour"}:
        # v1 pricing is day-equivalent for all duration units; advanced structures come later.
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
    deposit = float(product["security_deposit"] or 0) * qty
    return {"quantity": qty, "line_subtotal": round(line_subtotal, 2), "line_tax": round(line_tax, 2), "line_total": round(line_total, 2), "deposit": round(deposit, 2)}


def create_order(form):
    customer_id = int(form.get("customer_id") or 0)
    product_id = int(form.get("product_id") or 0)
    if not customer_id:
        raise ValueError("Customer is required")
    if not product_id:
        raise ValueError("Product is required")
    customer = get_db().execute("SELECT id FROM customers WHERE id = ?", (customer_id,)).fetchone()
    product = get_db().execute(
        """SELECT p.*, COALESCE(t.rate, 0) AS tax_rate FROM products p LEFT JOIN tax_profiles t ON t.id = p.tax_profile_id
        WHERE p.id = ? AND p.active = 1""",
        (product_id,),
    ).fetchone()
    if not customer:
        raise ValueError("Selected customer was not found")
    if not product:
        raise ValueError("Selected product was not found or is archived")

    settings = get_db().execute("SELECT * FROM company_settings WHERE id = 1").fetchone()
    start_dt = _parse_dt(form.get("start_date"), form.get("start_time"), settings["default_pickup_time"])
    end_dt = _parse_dt(form.get("end_date"), form.get("end_time"), settings["default_return_time"])
    if not start_dt or not end_dt:
        raise ValueError("Pickup and return dates are required")
    if end_dt <= start_dt:
        raise ValueError("Return must be after pickup")

    days = rental_days(start_dt, end_dt)
    line = calculate_line(product, form.get("quantity", 1), days, settings["tax_mode"])
    subtotal = line["line_subtotal"]
    tax_total = line["line_tax"]
    total = line["line_total"]
    deposit_total = line["deposit"]
    db = get_db()
    order_number = next_order_number()
    cur = db.execute(
        """INSERT INTO orders (order_number, customer_id, status, payment_status, start_at, end_at, subtotal, discount_total, tax_total, deposit_total, total, due_total, notes, created_at)
        VALUES (?, ?, 'draft', 'payment_due', ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)""",
        (order_number, customer_id, start_dt.isoformat(timespec="minutes"), end_dt.isoformat(timespec="minutes"), subtotal, tax_total, deposit_total, total, total, form.get("notes", "").strip(), now()),
    )
    order_id = cur.lastrowid
    db.execute(
        """INSERT INTO order_items (order_id, product_id, custom_name, quantity, unit_price, line_subtotal, line_tax, line_total)
        VALUES (?, ?, '', ?, ?, ?, ?, ?)""",
        (order_id, product_id, line["quantity"], float(product["price_amount"] or 0), subtotal, tax_total, total),
    )
    db.commit()
    return order_id
