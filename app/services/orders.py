from datetime import datetime, date, time
from math import ceil

from app.db import get_db, now
from app.services.coupons import calculate_discount, get_coupon_by_code

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


def order_filter_counts():
    db = get_db()
    status_rows = db.execute("SELECT status, COUNT(*) count FROM orders GROUP BY status").fetchall()
    payment_rows = db.execute("SELECT payment_status, COUNT(*) count FROM orders GROUP BY payment_status").fetchall()
    return {
        "status": {row["status"]: row["count"] for row in status_rows},
        "payment_status": {row["payment_status"]: row["count"] for row in payment_rows},
    }


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


def _form_list(form, name):
    if hasattr(form, "getlist"):
        return [value for value in form.getlist(name) if str(value).strip()]
    value = form.get(name)
    if isinstance(value, (list, tuple)):
        return [item for item in value if str(item).strip()]
    return [value] if value else []


def create_order(form):
    customer_id = int(form.get("customer_id") or 0)
    product_ids = _form_list(form, "product_id")
    quantities = _form_list(form, "quantity")
    if not customer_id:
        raise ValueError("Customer is required")
    if not product_ids:
        raise ValueError("Product is required")
    customer = get_db().execute("SELECT id FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if not customer:
        raise ValueError("Selected customer was not found")

    settings = get_db().execute("SELECT * FROM company_settings WHERE id = 1").fetchone()
    start_dt = _parse_dt(form.get("start_date"), form.get("start_time"), settings["default_pickup_time"])
    end_dt = _parse_dt(form.get("end_date"), form.get("end_time"), settings["default_return_time"])
    if not start_dt or not end_dt:
        raise ValueError("Pickup and return dates are required")
    if end_dt <= start_dt:
        raise ValueError("Return must be after pickup")

    days = rental_days(start_dt, end_dt)
    lines = []
    subtotal = tax_total = total = deposit_total = 0
    db = get_db()
    for index, product_id_value in enumerate(product_ids):
        product_id = int(product_id_value or 0)
        product = db.execute(
            """SELECT p.*, COALESCE(t.rate, 0) AS tax_rate FROM products p LEFT JOIN tax_profiles t ON t.id = p.tax_profile_id
            WHERE p.id = ? AND p.active = 1""",
            (product_id,),
        ).fetchone()
        if not product:
            raise ValueError("Selected product was not found or is archived")
        quantity = quantities[index] if index < len(quantities) else 1
        line = calculate_line(product, quantity, days, settings["tax_mode"])
        lines.append((product, line))
        subtotal += line["line_subtotal"]
        tax_total += line["line_tax"]
        total += line["line_total"]
        deposit_total += line["deposit"]

    subtotal = round(subtotal, 2)
    tax_total = round(tax_total, 2)
    coupon_code = (form.get("coupon_code") or "").strip().upper()
    coupon = get_coupon_by_code(coupon_code)
    discount_total = calculate_discount(coupon, subtotal) if coupon_code else 0
    if coupon_code and not coupon:
        raise ValueError("Coupon code was not found or is inactive")
    total = round(max(0, subtotal - discount_total) + tax_total, 2)
    deposit_total = round(deposit_total, 2)
    order_number = next_order_number()
    cur = db.execute(
        """INSERT INTO orders (order_number, customer_id, status, payment_status, start_at, end_at, subtotal, discount_total, coupon_code, tax_total, deposit_total, total, due_total, notes, created_at)
        VALUES (?, ?, 'draft', 'payment_due', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (order_number, customer_id, start_dt.isoformat(timespec="minutes"), end_dt.isoformat(timespec="minutes"), subtotal, discount_total, coupon_code if coupon else "", tax_total, deposit_total, total, total, form.get("notes", "").strip(), now()),
    )
    order_id = cur.lastrowid
    for product, line in lines:
        db.execute(
            """INSERT INTO order_items (order_id, product_id, custom_name, quantity, unit_price, line_subtotal, line_tax, line_total)
            VALUES (?, ?, '', ?, ?, ?, ?, ?)""",
            (order_id, product["id"], line["quantity"], float(product["price_amount"] or 0), line["line_subtotal"], line["line_tax"], line["line_total"]),
        )
    db.commit()
    try:
        from app.services.telegram import send_new_order_notification
        send_new_order_notification(order_id)
    except Exception:
        pass
    return order_id


TRANSITIONS = {
    "reserve": {"from": {"draft"}, "to": "reserved", "message": "Order reserved"},
    "start": {"from": {"reserved"}, "to": "started", "message": "Order started"},
    "return": {"from": {"started"}, "to": "returned", "message": "Order returned"},
    "archive": {"from": {"returned"}, "to": "archived", "message": "Order archived"},
    "cancel": {"from": {"draft", "reserved"}, "to": "canceled", "message": "Order canceled"},
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
        product = db.execute("SELECT name, quantity FROM products WHERE id = ?", (item["product_id"],)).fetchone()
        if not product:
            errors.append("One of the products on this order is no longer available")
            continue
        booked = db.execute(
            """SELECT COALESCE(SUM(oi.quantity), 0) AS booked
            FROM order_items oi JOIN orders o ON o.id = oi.order_id
            WHERE oi.product_id = ?
              AND o.id != ?
              AND o.status IN ('reserved', 'started')
              AND o.start_at < ?
              AND o.end_at > ?""",
            (item["product_id"], order_id, order["end_at"], order["start_at"]),
        ).fetchone()["booked"] or 0
        available = int(product["quantity"] or 0) - int(booked)
        if item["quantity"] > available:
            errors.append(f"Only {available} available for {product['name']} during this rental period")
    return errors


def transition_order(order_id, action):
    if action not in TRANSITIONS:
        raise ValueError("Unknown order action")
    order = get_order(order_id)
    if not order:
        raise ValueError("Order not found")
    transition = TRANSITIONS[action]
    if order["status"] not in transition["from"]:
        raise ValueError(f"Cannot {action} an order with status {STATUS_LABELS.get(order['status'], order['status'])}")
    if action == "reserve":
        errors = availability_errors(order_id)
        if errors:
            raise ValueError(errors[0])
    db = get_db()
    db.execute("UPDATE orders SET status = ? WHERE id = ?", (transition["to"], order_id))
    db.commit()
    return transition["message"]


def status_actions(status):
    actions = []
    if status == "draft":
        actions.append(("reserve", "Reserve order", "primary"))
        actions.append(("cancel", "Cancel order", "danger"))
    elif status == "reserved":
        actions.append(("start", "Start order", "primary"))
        actions.append(("cancel", "Cancel order", "danger"))
    elif status == "started":
        actions.append(("return", "Return order", "primary"))
    elif status == "returned":
        actions.append(("archive", "Archive order", "ghost"))
    return actions


def scheduled_events(limit=50, start_date=None, end_date=None):
    db = get_db()
    sql = """SELECT o.*, c.name AS customer_name,
            (SELECT GROUP_CONCAT(COALESCE(p.name, oi.custom_name), ', ')
             FROM order_items oi LEFT JOIN products p ON p.id = oi.product_id
             WHERE oi.order_id = o.id) AS product_names
        FROM orders o LEFT JOIN customers c ON c.id = o.customer_id
        WHERE o.status IN ('reserved', 'started')"""
    params = []
    if start_date:
        sql += " AND DATE(o.start_at) >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND DATE(o.start_at) <= ?"
        params.append(end_date)
    sql += " ORDER BY o.start_at ASC, o.end_at ASC LIMIT ?"
    params.append(limit)
    return db.execute(sql, params).fetchall()
def dashboard_schedule():
    events = scheduled_events(limit=100)
    return {
        "going_out": [event for event in events if event["status"] == "reserved"][:5],
        "coming_back": [event for event in events if event["status"] in {"reserved", "started"}][:5],
    }