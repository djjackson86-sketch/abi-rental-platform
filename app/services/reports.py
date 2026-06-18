from app.db import get_db


def money(value):
    return round(float(value or 0), 2)


def summary_metrics():
    db = get_db()
    orders = db.execute(
        "SELECT COUNT(*) AS count, COALESCE(SUM(total), 0) AS revenue, COALESCE(SUM(due_total), 0) AS due FROM orders"
    ).fetchone()
    payments = db.execute("SELECT COUNT(*) AS count, COALESCE(SUM(amount), 0) AS paid FROM payments WHERE status = 'paid'").fetchone()
    customers = db.execute("SELECT COUNT(*) AS count FROM customers").fetchone()
    products = db.execute("SELECT COUNT(*) AS count FROM products WHERE active = 1").fetchone()
    return {
        "orders": orders["count"] or 0,
        "revenue": money(orders["revenue"]),
        "due": money(orders["due"]),
        "payments": payments["count"] or 0,
        "paid": money(payments["paid"]),
        "customers": customers["count"] or 0,
        "products": products["count"] or 0,
    }


def orders_by_status():
    return get_db().execute(
        "SELECT status, COUNT(*) AS count, COALESCE(SUM(total), 0) AS total FROM orders GROUP BY status ORDER BY count DESC, status"
    ).fetchall()


def payments_by_method():
    return get_db().execute(
        "SELECT method, COUNT(*) AS count, COALESCE(SUM(amount), 0) AS total FROM payments WHERE status = 'paid' GROUP BY method ORDER BY total DESC, method"
    ).fetchall()


def product_performance():
    return get_db().execute(
        """SELECT COALESCE(p.name, oi.custom_name, 'Custom line') AS product_name,
            COALESCE(SUM(oi.quantity), 0) AS quantity,
            COALESCE(SUM(oi.line_total), 0) AS total
        FROM order_items oi
        LEFT JOIN products p ON p.id = oi.product_id
        GROUP BY product_name
        ORDER BY total DESC, quantity DESC
        LIMIT 10"""
    ).fetchall()


def customer_summary():
    return get_db().execute(
        """SELECT c.name AS customer_name, COUNT(o.id) AS orders, COALESCE(SUM(o.total), 0) AS total
        FROM customers c
        LEFT JOIN orders o ON o.customer_id = c.id
        GROUP BY c.id, c.name
        ORDER BY total DESC, orders DESC, c.name
        LIMIT 10"""
    ).fetchall()


def orders_export_rows():
    return get_db().execute(
        """SELECT o.order_number, COALESCE(c.name, '') AS customer, o.status, o.payment_status, o.total, o.due_total
        FROM orders o
        LEFT JOIN customers c ON c.id = o.customer_id
        ORDER BY o.created_at DESC, o.id DESC"""
    ).fetchall()
