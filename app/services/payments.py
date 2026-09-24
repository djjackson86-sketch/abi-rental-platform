from datetime import datetime

from app.db import get_db, now
from app.services.access import order_branch_clause
from app.services.orders import get_order


def _active_payment_clause(alias=""):
    prefix = f"{alias}." if alias else ""
    return f"COALESCE({prefix}deleted_at, '') = '' AND COALESCE({prefix}status, 'paid') = 'paid'"

PAYMENT_LABELS = {
    "payment_due": "Payment due",
    "partially_paid": "Partially paid",
    "paid": "Paid",
    "overpaid": "Overpaid",
}


def parse_payment_date(value):
    value = (value or "").strip()
    if not value:
        return now()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Payment date must be a valid date and time") from exc
    return parsed.isoformat(timespec="seconds")


def display_payment_date(payment):
    return (payment["payment_date"] or payment["created_at"] or "")[:10]


def payments_for_order(order_id, include_archived=False):
    where = "order_id = ?" if include_archived else f"order_id = ? AND {_active_payment_clause()}"
    return get_db().execute(
        f"""SELECT * FROM payments WHERE {where}
        ORDER BY COALESCE(NULLIF(payment_date, ''), created_at) DESC, created_at DESC, id DESC""",
        (order_id,),
    ).fetchall()


def get_payment(payment_id):
    return get_db().execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()


def payment_total(order_id):
    row = get_db().execute(
        f"SELECT COALESCE(SUM(amount), 0) AS paid FROM payments WHERE order_id = ? AND {_active_payment_clause()}",
        (order_id,),
    ).fetchone()
    return float(row["paid"] or 0)


def status_for(order_total, paid_total):
    order_total = float(order_total or 0)
    paid_total = float(paid_total or 0)
    if paid_total <= 0:
        return "payment_due"
    if paid_total < order_total:
        return "partially_paid"
    if paid_total == order_total:
        return "paid"
    return "overpaid"


def recalculate_order_payment(order_id):
    order = get_order(order_id)
    if not order:
        raise ValueError("Order not found")
    paid_total = payment_total(order_id)
    due_total = round(float(order["total"] or 0) - paid_total, 2)
    status = status_for(order["total"], paid_total)
    db = get_db()
    db.execute("UPDATE orders SET payment_status = ?, due_total = ? WHERE id = ?", (status, due_total, order_id))
    db.commit()
    return {"paid_total": round(paid_total, 2), "due_total": due_total, "payment_status": status}


def payment_summary(order_id):
    order = get_order(order_id)
    if not order:
        return {"paid_total": 0, "due_total": 0, "payment_status": "payment_due"}
    paid_total = payment_total(order_id)
    due_total = round(float(order["total"] or 0) - paid_total, 2)
    return {"paid_total": round(paid_total, 2), "due_total": due_total, "payment_status": status_for(order["total"], paid_total)}


def record_payment(order_id, form):
    order = get_order(order_id)
    if not order:
        raise ValueError("Order not found")
    amount = _parse_payment_amount(form)
    method = form.get("method", "manual").strip() or "manual"
    reference = form.get("reference", "").strip()
    payment_date = parse_payment_date(form.get("payment_date"))
    created_at = now()
    db = get_db()
    db.execute(
        """INSERT INTO payments (order_id, amount, method, reference, status, payment_date, created_at)
        VALUES (?, ?, ?, ?, 'paid', ?, ?)""",
        (order_id, amount, method, reference, payment_date, created_at),
    )
    db.commit()
    return recalculate_order_payment(order_id)


def _parse_payment_amount(form):
    try:
        amount = float(form.get("amount", 0) or 0)
    except ValueError as exc:
        raise ValueError("Payment amount must be a number") from exc
    if amount <= 0:
        raise ValueError("Payment amount must be greater than zero")
    return round(amount, 2)


def update_payment(payment_id, form):
    payment = get_payment(payment_id)
    if not payment or payment["deleted_at"]:
        raise ValueError("Payment not found")
    amount = _parse_payment_amount(form)
    method = form.get("method", "manual").strip() or "manual"
    reference = form.get("reference", "").strip()
    payment_date = parse_payment_date(form.get("payment_date"))
    db = get_db()
    db.execute(
        "UPDATE payments SET amount = ?, method = ?, reference = ?, payment_date = ?, status = 'paid' WHERE id = ?",
        (amount, method, reference, payment_date, payment_id),
    )
    db.commit()
    return recalculate_order_payment(payment["order_id"])


def archive_payment(payment_id):
    payment = get_payment(payment_id)
    if not payment or payment["deleted_at"]:
        raise ValueError("Payment not found")
    db = get_db()
    db.execute("UPDATE payments SET status = 'archived', deleted_at = ? WHERE id = ?", (now(), payment_id))
    db.commit()
    return recalculate_order_payment(payment["order_id"])


PAYMENT_SORTS = {
    "date": "COALESCE(NULLIF(p.payment_date, ''), p.created_at)",
    "branch": "LOWER(COALESCE(cb.name, rb.name, ''))",
    "order": "LOWER(COALESCE(o.order_number, ''))",
    "customer": "LOWER(COALESCE(c.name, ''))",
    "amount": "p.amount",
    "method": "LOWER(COALESCE(p.method, ''))",
    "status": "LOWER(COALESCE(p.status, ''))",
}


def _payment_order_clause(sort="date", direction="desc"):
    sort = sort if sort in PAYMENT_SORTS else "date"
    direction = "asc" if direction == "asc" else "desc"
    expr = PAYMENT_SORTS[sort]
    clauses = [f"{expr} {direction.upper()}"]
    if sort == "branch":
        clauses.append(f"LOWER(COALESCE(rb.name, '')) {direction.upper()}")
    if sort != "date":
        clauses.append("COALESCE(NULLIF(p.payment_date, ''), p.created_at) DESC")
    clauses.extend(["p.created_at DESC", "p.id DESC"])
    return ", ".join(clauses)


def list_payments(include_archived=False, branch_id=None, sort="date", direction="desc"):
    where_parts = ["1=1" if include_archived else _active_payment_clause("p")]
    branch_sql, params = order_branch_clause("o", branch_id=branch_id)
    if branch_sql:
        where_parts.append(branch_sql[5:] if branch_sql.startswith(" AND ") else branch_sql)
    order_clause = _payment_order_clause(sort, direction)
    return get_db().execute(
        f"""SELECT p.*, o.order_number, o.collect_branch_id, o.return_branch_id,
                  c.name AS customer_name, cb.name AS collect_branch_name, rb.name AS return_branch_name,
                  CASE
                    WHEN cb.name IS NOT NULL AND rb.name IS NOT NULL AND cb.id <> rb.id THEN cb.name || ' → ' || rb.name
                    WHEN cb.name IS NOT NULL THEN cb.name
                    WHEN rb.name IS NOT NULL THEN rb.name
                    ELSE ''
                  END AS branch_label
        FROM payments p
        LEFT JOIN orders o ON o.id = p.order_id
        LEFT JOIN customers c ON c.id = o.customer_id
        LEFT JOIN branches cb ON cb.id = o.collect_branch_id
        LEFT JOIN branches rb ON rb.id = o.return_branch_id
        WHERE {' AND '.join(where_parts)}
        ORDER BY {order_clause}""",
        params,
    ).fetchall()


def normalise_payment_sort(sort, direction):
    sort = sort if sort in PAYMENT_SORTS else "date"
    direction = "asc" if direction == "asc" else "desc"
    return sort, direction


def label_for(payment_status):
    return PAYMENT_LABELS.get(payment_status, payment_status.replace("_", " ").title())


def record_refund(order_id, form):
    order = get_order(order_id)
    if not order:
        raise ValueError("Order not found")
    paid_total = payment_total(order_id)
    credit = round(paid_total - float(order["total"] or 0), 2)
    if credit <= 0:
        raise ValueError("This order does not have a credit to refund")
    try:
        amount = round(float(form.get("refund_amount") or credit), 2)
    except ValueError as exc:
        raise ValueError("Refund amount must be a number") from exc
    if amount <= 0 or amount > credit:
        raise ValueError("Refund amount must be greater than zero and not more than the credit")
    method = (form.get("refund_method") or form.get("deposit_process_method") or "").strip().lower()
    if method not in {"eft", "card", "cash"}:
        raise ValueError("Refund method must be EFT, Card, or Cash")
    payment_date = parse_payment_date(form.get("refund_date") or form.get("deposit_processed_at"))
    reference = (form.get("reference") or "Customer refund").strip()
    db = get_db()
    db.execute("""INSERT INTO payments (order_id, amount, method, reference, status, payment_date, created_at)
        VALUES (?, ?, ?, ?, 'paid', ?, ?)""", (order_id, -amount, method, reference, payment_date, now()))
    db.commit()
    return recalculate_order_payment(order_id)
