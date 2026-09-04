from datetime import datetime, date, time, timedelta
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

BLOCKED_EDIT_STATUSES = {"archived", "canceled", "cancelled"}
BLOCKED_EDIT_MESSAGE = "Canceled and archived orders cannot be edited"


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
        f"{prefix}status = 'returned' "
        f"AND COALESCE({prefix}deposit_total, 0) > 0 "
        f"AND COALESCE({prefix}deposit_processed_at, '') = '' "
        f"AND COALESCE({prefix}deposit_process_method, '') = '' "
        f"AND COALESCE({prefix}deposit_refund_amount, 0) = 0 "
        f"AND COALESCE({prefix}deposit_applied_amount, 0) = 0"
    )


def list_orders(query="", status="", payment_status="", return_status=""):
    sql = """SELECT o.*, c.name AS customer_name, c.email AS customer_email, cb.name AS collect_branch_name, rb.name AS return_branch_name,
        (SELECT COALESCE(SUM(quantity), 0) FROM order_items oi WHERE oi.order_id = o.id) AS item_count
        FROM orders o LEFT JOIN customers c ON c.id = o.customer_id
        LEFT JOIN branches cb ON cb.id = o.collect_branch_id
        LEFT JOIN branches rb ON rb.id = o.return_branch_id WHERE 1=1"""
    params = []
    if query:
        sql += " AND (LOWER(o.order_number) LIKE ? OR LOWER(c.name) LIKE ? OR LOWER(c.email) LIKE ?)"
        needle = f"%{query.lower()}%"
        params.extend([needle, needle, needle])
    if status:
        sql += " AND o.status = ?"
        params.append(status)
    if return_status == "late":
        sql += " AND o.status = 'started' AND o.end_at < ?"
        params.append(now())
    if payment_status == "process_deposit":
        sql += f" AND {_process_deposit_clause('o')}"
    elif payment_status:
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
    process_deposit_row = db.execute(f"SELECT COUNT(*) AS count FROM orders WHERE {_process_deposit_clause('')}").fetchone()
    late_return_row = db.execute("SELECT COUNT(*) AS count FROM orders WHERE status = 'started' AND end_at < ?", (now(),)).fetchone()
    payment_counts = {row["payment_status"]: row["count"] for row in payment_rows}
    payment_counts["process_deposit"] = process_deposit_row["count"] if process_deposit_row else 0
    return {
        "status": {row["status"]: row["count"] for row in status_rows},
        "payment_status": payment_counts,
        "return_status": {"late": late_return_row["count"] if late_return_row else 0},
    }


def get_order(order_id):
    return get_db().execute(
        """SELECT o.*, c.name AS customer_name, c.email AS customer_email, c.phone AS customer_phone,
            c.address_line1 AS customer_address_line1, c.address_line2 AS customer_address_line2, c.suburb AS customer_suburb,
            c.city AS customer_city, c.province AS customer_province, c.postal_code AS customer_postal_code, c.country AS customer_country,
            c.custom_fields_json AS custom_fields_json,
            cb.name AS collect_branch_name, rb.name AS return_branch_name
        FROM orders o LEFT JOIN customers c ON c.id = o.customer_id
        LEFT JOIN branches cb ON cb.id = o.collect_branch_id
        LEFT JOIN branches rb ON rb.id = o.return_branch_id WHERE o.id = ?""",
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



def next_time_slot(now_dt=None, increment_minutes=15):
    now_dt = now_dt or datetime.now()
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


def _build_order_payload(form):
    db = get_db()
    customer_id = int(form.get("customer_id") or 0) or None
    if customer_id:
        customer = db.execute("SELECT id FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if not customer:
            raise ValueError("Selected customer was not found")
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
            line = calculate_custom_line(custom_name, quantity, custom_price or 0, custom_mode, days, 0, settings["tax_mode"])
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
    # Coupon entry has been retired from reachable staff/admin workflows.
    # Keep the historical order columns/display intact, but do not apply
    # submitted coupon codes to newly saved/recalculated orders.
    coupon_code = ""
    discount_total = 0
    total = round(subtotal + tax_total, 2)
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


def create_order(form):
    payload = _build_order_payload(form)
    order_number = next_order_number()
    db = get_db()
    cur = db.execute(
        """INSERT INTO orders (order_number, customer_id, booking_type, collect_branch_id, return_branch_id, status, payment_status, start_at, end_at, subtotal, discount_total, coupon_code, tax_total, deposit_total, deposit_option, damage_waiver_amount, total, due_total, notes, created_at)
        VALUES (?, ?, ?, ?, ?, 'draft', 'payment_due', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (order_number, payload["customer_id"], payload["booking_type"], payload["collect_branch_id"], payload["return_branch_id"], payload["start_at"], payload["end_at"], payload["subtotal"], payload["discount_total"], payload["coupon_code"], payload["tax_total"], payload["deposit_total"], payload["deposit_option"], payload["damage_waiver_amount"], payload["total"], payload["total"], payload["notes"], now()),
    )
    order_id = cur.lastrowid
    _insert_order_items(order_id, payload["lines"])
    db.commit()
    try:
        from app.services.telegram import send_new_order_notification
        send_new_order_notification(order_id)
    except Exception:
        pass
    return order_id


def update_draft_order(order_id, form):
    order = get_order(order_id)
    if not order:
        raise ValueError("Order not found")
    if not can_edit_order_status(order["status"]):
        raise ValueError(BLOCKED_EDIT_MESSAGE)
    payload = _build_order_payload(form)
    db = get_db()
    paid_row = db.execute("SELECT COALESCE(SUM(amount), 0) AS paid FROM payments WHERE order_id = ? AND status = 'paid'", (order_id,)).fetchone()
    paid_total = float(paid_row["paid"] or 0) if paid_row else 0
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
        start_at = ?, end_at = ?, subtotal = ?, discount_total = ?, coupon_code = ?, tax_total = ?,
        deposit_total = ?, deposit_option = ?, damage_waiver_amount = ?, total = ?, due_total = ?,
        payment_status = ?, notes = ? WHERE id = ?""",
        (payload["customer_id"], payload["booking_type"], payload["collect_branch_id"], payload["return_branch_id"], payload["start_at"], payload["end_at"], payload["subtotal"], payload["discount_total"], payload["coupon_code"], payload["tax_total"], payload["deposit_total"], payload["deposit_option"], payload["damage_waiver_amount"], payload["total"], due_total, payment_status, payload["notes"], order_id),
    )
    db.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
    _insert_order_items(order_id, payload["lines"])
    db.commit()
    return order_id


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
    while len(lines) < 4:
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
        product = db.execute("SELECT name, quantity, branch_id FROM products WHERE id = ?", (item["product_id"],)).fetchone()
        if not product:
            errors.append("One of the products on this order is no longer available")
            continue
        booked = db.execute(
            """SELECT COALESCE(SUM(oi.quantity), 0) AS booked
            FROM order_items oi JOIN orders o ON o.id = oi.order_id
            WHERE oi.product_id = ?
              AND o.id != ?
              AND o.status IN ('reserved', 'started')
              AND COALESCE(o.collect_branch_id, 0) = COALESCE(?, 0)
              AND o.start_at < ?
              AND o.end_at > ?""",
            (item["product_id"], order_id, order["collect_branch_id"], order["end_at"], order["start_at"]),
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
    if action in {"reserve", "start"}:
        if not order["customer_id"]:
            raise ValueError("Add customer details before reserving or pickup")
        errors = availability_errors(order_id)
        if errors:
            raise ValueError(errors[0])
    db = get_db()
    db.execute("UPDATE orders SET status = ? WHERE id = ?", (transition["to"], order_id))
    if action == "return" and order["booking_type"] == "oneway" and order["return_branch_id"]:
        for item in order_items(order_id):
            if item["product_id"]:
                db.execute("UPDATE products SET branch_id = ? WHERE id = ?", (order["return_branch_id"], item["product_id"]))
    db.commit()
    return transition["message"]


def status_actions(status):
    actions = []
    if status == "draft":
        actions.append(("reserve", "Reserve order", "primary"))
        actions.append(("start", "Pick up now", "ghost"))
        actions.append(("cancel", "Cancel order", "danger"))
    elif status == "reserved":
        actions.append(("start", "Start order", "primary"))
        actions.append(("cancel", "Cancel order", "danger"))
    elif status == "started":
        actions.append(("return", "Return order", "primary"))
    elif status == "returned":
        actions.append(("archive", "Archive order", "ghost"))
    return actions


def _parse_deposit_processed_at(value):
    value = (value or "").strip()
    if not value:
        return now()
    try:
        processed_at = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Deposit refund date must be a valid date and time") from exc
    if processed_at > datetime.utcnow().replace(microsecond=0):
        raise ValueError("Deposit refund date cannot be in the future")
    return processed_at.isoformat(timespec="seconds")


def settle_return_deposit(order_id, form):
    order = get_order(order_id)
    if not order:
        raise ValueError("Order not found")
    try:
        extra_hours = max(0, float(form.get("extra_hours") or 0))
        hourly_rate = max(0, float(form.get("extra_hourly_rate") or 0))
    except ValueError as exc:
        raise ValueError("Extra hours and hourly rate must be numbers") from exc
    deposit_process_method = (form.get("deposit_process_method") or "").strip().lower()
    if deposit_process_method not in {"eft", "card", "cash"}:
        raise ValueError("Deposit process method must be EFT, Card, or Cash")
    note = form.get("deposit_note", "").strip()
    deposit_processed_at = _parse_deposit_processed_at(form.get("deposit_processed_at"))
    extra_charge = round(extra_hours * hourly_rate, 2)
    deposit_available = float(order["deposit_total"] or 0)
    deposit_applied = round(min(deposit_available, extra_charge), 2)
    deposit_refund = round(max(deposit_available - deposit_applied, 0), 2)
    db = get_db()
    if extra_charge > 0:
        new_total = round(float(order["total"] or 0) + extra_charge, 2)
        db.execute("UPDATE orders SET total = ? WHERE id = ?", (new_total, order_id))
    db.execute(
        """UPDATE orders SET extra_hours = ?, deposit_applied_amount = ?, deposit_refund_amount = ?,
        deposit_process_method = ?, deposit_processed_at = ?, deposit_note = ? WHERE id = ?""",
        (extra_hours, deposit_applied, deposit_refund, deposit_process_method, deposit_processed_at, note, order_id),
    )
    if deposit_applied > 0:
        existing = db.execute("SELECT id FROM payments WHERE order_id = ? AND method = 'deposit_applied' AND reference = 'DEPOSIT-SETTLEMENT'", (order_id,)).fetchone()
        if existing:
            db.execute("UPDATE payments SET amount = ?, created_at = ? WHERE id = ?", (deposit_applied, now(), existing["id"]))
        else:
            db.execute(
                "INSERT INTO payments (order_id, amount, method, reference, status, created_at) VALUES (?, ?, 'deposit_applied', 'DEPOSIT-SETTLEMENT', 'paid', ?)",
                (order_id, deposit_applied, now()),
            )
    db.commit()
    from app.services.payments import recalculate_order_payment
    recalculate_order_payment(order_id)
    if extra_charge and deposit_applied:
        return f"Return settled: R{deposit_applied:.2f} used from deposit; refund R{deposit_refund:.2f}"
    if extra_charge:
        return "Extra hours charge added"
    return f"Deposit refund marked: R{deposit_refund:.2f}"


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


def calendar_group_availability(start_date=None, end_date=None):
    range_start, range_end = _calendar_range(start_date, end_date)
    db = get_db()
    products = db.execute(
        """SELECT p.id, p.name, p.sku, p.quantity, p.tracking_method,
               COALESCE(pg.id, 0) AS group_id,
               COALESCE(pg.name, 'Ungrouped trailers') AS group_name
        FROM products p
        LEFT JOIN product_groups pg ON pg.id = p.product_group_id AND pg.active = 1
        WHERE p.active = 1 AND p.product_type = 'rental'
        ORDER BY CASE WHEN pg.id IS NULL THEN 1 ELSE 0 END, pg.sort_order, pg.name, p.name"""
    ).fetchall()

    groups = []
    groups_by_id = {}
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

        booked = db.execute(
            """SELECT COALESCE(SUM(oi.quantity), 0) AS booked
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            WHERE oi.product_id = ?
              AND o.status IN ('reserved', 'started')
              AND o.start_at < ?
              AND o.end_at > ?""",
            (product["id"], range_end.isoformat(timespec="seconds"), range_start.isoformat(timespec="seconds")),
        ).fetchone()["booked"] or 0
        reservations = db.execute(
            """SELECT o.id, o.order_number, o.status, o.start_at, o.end_at, oi.quantity
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            WHERE oi.product_id = ?
              AND o.status IN ('reserved', 'started')
              AND o.start_at < ?
              AND o.end_at > ?
            ORDER BY o.start_at, o.id""",
            (product["id"], range_end.isoformat(timespec="seconds"), range_start.isoformat(timespec="seconds")),
        ).fetchall()

        total = int(product["quantity"] or 0)
        booked = int(booked or 0)
        available = max(total - booked, 0)
        product_row = {
            "id": product["id"],
            "name": product["name"],
            "sku": product["sku"],
            "tracking_method": product["tracking_method"],
            "total_quantity": total,
            "booked_quantity": booked,
            "available_quantity": available,
            "reservations": reservations,
        }
        group["products"].append(product_row)
        group["total_quantity"] += total
        group["booked_quantity"] += booked
        group["available_quantity"] += available

    return {
        "start_date": range_start.date().isoformat(),
        "end_date": (range_end.date() - timedelta(days=1)).isoformat(),
        "groups": groups,
    }


def scheduled_events(limit=50, start_date=None, end_date=None):
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