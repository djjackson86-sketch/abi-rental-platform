from app.db import get_db, now
from app.services.orders import get_order

PAYMENT_LABELS = {
    "payment_due": "Payment due",
    "partially_paid": "Partially paid",
    "paid": "Paid",
    "overpaid": "Overpaid",
}


def payments_for_order(order_id):
    return get_db().execute(
        "SELECT * FROM payments WHERE order_id = ? ORDER BY created_at DESC, id DESC",
        (order_id,),
    ).fetchall()


def payment_total(order_id):
    row = get_db().execute("SELECT COALESCE(SUM(amount), 0) AS paid FROM payments WHERE order_id = ? AND status = 'paid'", (order_id,)).fetchone()
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
    db.execute("UPDATE orders SET payment_status = ?, due_total = ? WHERE id = ?", (status, max(due_total, 0), order_id))
    db.commit()
    return {"paid_total": round(paid_total, 2), "due_total": round(max(due_total, 0), 2), "payment_status": status}


def payment_summary(order_id):
    order = get_order(order_id)
    if not order:
        return {"paid_total": 0, "due_total": 0, "payment_status": "payment_due"}
    paid_total = payment_total(order_id)
    due_total = max(round(float(order["total"] or 0) - paid_total, 2), 0)
    return {"paid_total": round(paid_total, 2), "due_total": due_total, "payment_status": status_for(order["total"], paid_total)}


def record_payment(order_id, form):
    order = get_order(order_id)
    if not order:
        raise ValueError("Order not found")
    try:
        amount = float(form.get("amount", 0) or 0)
    except ValueError as exc:
        raise ValueError("Payment amount must be a number") from exc
    if amount <= 0:
        raise ValueError("Payment amount must be greater than zero")
    method = form.get("method", "manual").strip() or "manual"
    reference = form.get("reference", "").strip()
    db = get_db()
    db.execute(
        """INSERT INTO payments (order_id, amount, method, reference, status, created_at)
        VALUES (?, ?, ?, ?, 'paid', ?)""",
        (order_id, round(amount, 2), method, reference, now()),
    )
    db.commit()
    return recalculate_order_payment(order_id)


def list_payments():
    return get_db().execute(
        """SELECT p.*, o.order_number, c.name AS customer_name
        FROM payments p
        LEFT JOIN orders o ON o.id = p.order_id
        LEFT JOIN customers c ON c.id = o.customer_id
        ORDER BY p.created_at DESC, p.id DESC"""
    ).fetchall()


def label_for(payment_status):
    return PAYMENT_LABELS.get(payment_status, payment_status.replace("_", " ").title())
