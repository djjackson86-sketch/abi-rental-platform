from app.db import get_db

def money(value):
    return round(float(value or 0), 2)


def summary_metrics(start_date=None, end_date=None):
    db = get_db()
    sql = """
        SELECT COUNT(*) AS count, COALESCE(SUM(total), 0) AS revenue, COALESCE(SUM(due_total), 0) AS due FROM orders
        WHERE 1=1
    """
    params = []
    if start_date:
        sql += " AND DATE(created_at) >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND DATE(created_at) <= ?"
        params.append(end_date)
    orders = db.execute(sql, params).fetchone()
    
    payments_sql = "SELECT COUNT(*) AS count, COALESCE(SUM(amount), 0) AS paid FROM payments WHERE status = 'paid'"
    payments_params = []
    if start_date:
        payments_sql += " AND DATE(created_at) >= ?"
        payments_params.append(start_date)
    if end_date:
        payments_sql += " AND DATE(created_at) <= ?"
        payments_params.append(end_date)
    payments = db.execute(payments_sql, payments_params).fetchone()
    
    customers_sql = "SELECT COUNT(*) AS count FROM customers WHERE 1=1"
    customers_params = []
    if start_date:
        # For customers, we might want to filter by when they were created or when they had orders
        # For simplicity, let's not filter customers by date for now
        pass
    if end_date:
        pass
    customers = db.execute(customers_sql, customers_params).fetchone()
    
    products_sql = "SELECT COUNT(*) AS count FROM products WHERE active = 1"
    products_params = []
    if start_date:
        # Products don't have a direct date filter, but we could filter by when they were created
        # For simplicity, let's not filter products by date for now
        pass
    if end_date:
        pass
    products = db.execute(products_sql, products_params).fetchone()
    
    return {
        "orders": orders["count"] or 0,
        "revenue": money(orders["revenue"]),
        "due": money(orders["due"]),
        "payments": payments["count"] or 0,
        "paid": money(payments["paid"]),
        "customers": customers["count"] or 0,
        "products": products["count"] or 0,
    }


def orders_by_status(start_date=None, end_date=None):
    db = get_db()
    sql = """
        SELECT status, COUNT(*) AS count, COALESCE(SUM(total), 0) AS total FROM orders
        WHERE 1=1
    """
    params = []
    if start_date:
        sql += " AND DATE(created_at) >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND DATE(created_at) <= ?"
        params.append(end_date)
    sql += " GROUP BY status ORDER BY count DESC, status"
    return db.execute(sql, params).fetchall()


def payments_by_method(start_date=None, end_date=None):
    db = get_db()
    sql = """
        SELECT method, COUNT(*) AS count, COALESCE(SUM(amount), 0) AS total FROM payments
        WHERE status = 'paid'
    """
    params = []
    if start_date:
        sql += " AND DATE(created_at) >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND DATE(created_at) <= ?"
        params.append(end_date)
    sql += " GROUP BY method ORDER BY total DESC, method"
    return db.execute(sql, params).fetchall()


def product_performance(start_date=None, end_date=None, limit=10):
    db = get_db()
    sql = """
        SELECT COALESCE(p.name, oi.custom_name, 'Custom line') AS product_name,
            COALESCE(SUM(oi.quantity), 0) AS quantity,
            COALESCE(SUM(oi.line_total), 0) AS total
        FROM order_items oi
        LEFT JOIN products p ON p.id = oi.product_id
        WHERE 1=1
    """
    params = []
    if start_date:
        sql += " AND DATE(oi.created_at) >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND DATE(oi.created_at) <= ?"
        params.append(end_date)
    sql += """
        GROUP BY product_name
        ORDER BY total DESC, quantity DESC
        LIMIT ?
    """
    params.append(limit)
    return db.execute(sql, params).fetchall()


def customer_summary(start_date=None, end_date=None, limit=10):
    db = get_db()
    sql = """
        SELECT c.name AS customer_name, COUNT(o.id) AS orders, COALESCE(SUM(o.total), 0) AS total
        FROM customers c
        LEFT JOIN orders o ON o.customer_id = c.id
        WHERE 1=1
    """
    params = []
    if start_date:
        sql += " AND DATE(o.created_at) >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND DATE(o.created_at) <= ?"
        params.append(end_date)
    sql += """
        GROUP BY c.id, c.name
        ORDER BY total DESC, orders DESC, c.name
        LIMIT ?
    """
    params.append(limit)
    return db.execute(sql, params).fetchall()


def orders_export_rows(start_date=None, end_date=None):
    db = get_db()
    sql = """
        SELECT o.order_number, COALESCE(c.name, '') AS customer, o.status, o.payment_status, o.total, o.due_total
        FROM orders o
        LEFT JOIN customers c ON c.id = o.customer_id
        WHERE 1=1
    """
    params = []
    if start_date:
        sql += " AND DATE(o.created_at) >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND DATE(o.created_at) <= ?"
        params.append(end_date)
    sql += " ORDER BY o.created_at DESC, o.id DESC"
    return db.execute(sql, params).fetchall()