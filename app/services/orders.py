from datetime import datetime, date, time, timedelta
from math import ceil
import calendar as _month_calendar

from app.db import get_db, now
from app.services.access import current_session_user_id, order_branch_clause, product_branch_clause as scoped_product_branch_clause
from app.services.settings import global_vat_rate
from app.services.timezone import local_now, local_now_iso

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
        f"{prefix}status IN ('started', 'returned', 'canceled', 'cancelled') "
        f"AND COALESCE({prefix}deposit_total, 0) > 0 "
        f"AND COALESCE({prefix}deposit_processed_at, '') = '' "
        f"AND COALESCE({prefix}deposit_process_method, '') = '' "
        f"AND (COALESCE({prefix}deposit_refund_amount, 0) = 0 OR COALESCE({prefix}deposit_applied_amount, 0) > 0)"
    )


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
        clauses.append("o.status = ?")
        params.append(status)
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
    where, params = _order_filter_where(query, status, payment_status, return_status, start_date, end_date, branch_id=branch_id)
    db = get_db()
    row = db.execute(f"""SELECT COUNT(*) total, COALESCE(SUM(o.total),0) revenue, COALESCE(SUM(o.due_total),0) due
        FROM orders o LEFT JOIN customers c ON c.id = o.customer_id WHERE {where}""", params).fetchone()
    item_row = db.execute(f"""SELECT COALESCE(SUM(oi.quantity),0) items FROM order_items oi
        JOIN orders o ON o.id = oi.order_id LEFT JOIN customers c ON c.id = o.customer_id WHERE {where}""", params).fetchone()
    return {"total": row["total"] or 0, "revenue": row["revenue"] or 0, "due": row["due"] or 0, "items": item_row["items"] or 0}


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
            c.custom_fields_json AS custom_fields_json, c.standard_discount_percent AS customer_standard_discount_percent,
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
        """SELECT oi.*, p.name AS product_name, p.sku AS product_sku, p.product_type, p.security_deposit, p.hourly_extra_rate
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


def calculate_line(product, quantity, days, tax_mode="exclusive"):
    product_type = product["product_type"] or "rental"
    qty = 1 if product_type == "service" else max(1, int(quantity or 1))
    base = float(product["price_amount"] or 0) * qty
    if product_type == "rental" and product["price_unit"] in {"day", "week", "month", "hour"}:
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


def create_order(form):
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
            payload["total"] = round(float(payload["total"] or 0) - float(payload["discount_total"] or 0), 2)
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
        product = db.execute("SELECT name, quantity, branch_id, product_type FROM products WHERE id = ?", (item["product_id"],)).fetchone()
        if not product:
            errors.append("One of the products on this order is no longer available")
            continue
        if (product["product_type"] or "") == "service":
            continue
        branch_clause = "" if product["branch_id"] is None else "AND COALESCE(o.collect_branch_id, 0) = COALESCE(?, 0)"
        params = [item["product_id"], order_id]
        if product["branch_id"] is not None:
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
    if action == "return":
        validate_return_ready(order_id)
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
    multiplier = days if (billing_mode == "rental_day" or (billing_mode == "catalog" and product_type == "rental" and price_unit in {"day", "week", "month", "hour"})) else 1
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


def calendar_group_availability(start_date=None, end_date=None, branch_id=None):
    range_start, range_end = _calendar_range(start_date, end_date)
    db = get_db()
    product_branch_clause, product_branch_params = scoped_product_branch_clause(
        "p", include_unassigned=True, branch_id=branch_id
    )
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


def dashboard_schedule():
    events = scheduled_events(limit=100)
    return {
        "going_out": [event for event in events if event["status"] == "reserved"][:5],
        "coming_back": [event for event in events if event["status"] in {"reserved", "started"}][:5],
    }