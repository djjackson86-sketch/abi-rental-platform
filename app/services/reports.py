from app.db import get_db
from app.services.access import order_branch_clause


def money(value):
    return round(float(value or 0), 2)


def row_dict(row):
    """Normalise a database row into a plain dict.

    Production runs on libSQL rows while local dev runs on sqlite3 rows, and the
    two expose completely different interfaces. libsql_client.Row is a *tuple
    subclass*, so ``row.count`` resolves to ``tuple.count`` (a bound method, not
    the column) and ``dict(row)`` misreads the values as key/value pairs. That
    difference produced a production-only 500:

        TypeError: '>' not supported between instances of 'method' and 'int'

    Never read report columns off a raw row object — convert first.
    """
    if isinstance(row, dict):
        return dict(row)
    asdict = getattr(row, "asdict", None)  # libsql_client.Row
    if callable(asdict):
        return dict(asdict())
    fields = getattr(row, "_fields", None)  # libsql_client.Row, older versions
    if fields:
        return {name: row[name] for name in fields}
    keys = getattr(row, "keys", None)  # sqlite3.Row
    if callable(keys):
        return {key: row[key] for key in keys()}
    raise TypeError(f"cannot convert row of type {type(row)!r} to a dict")


def rows_with_bars(rows, count_key="count"):
    """Convert rows to dicts and attach a 0-100 ``bar`` share to each.

    The bar scale is computed here, not in the template: arithmetic in Jinja is
    unverifiable locally (it can pass every test and still fail on the deployed
    page), so templates only ever render finished values.
    """
    materialised = [row_dict(row) for row in rows]
    top = max((int(row.get(count_key) or 0) for row in materialised), default=0)
    for row in materialised:
        count = int(row.get(count_key) or 0)
        row["bar"] = round(count / top * 100, 1) if top else 0.0
    return materialised


def payment_split(revenue, paid):
    """Paid vs outstanding as percentages, or ``None`` when nothing was invoiced."""
    revenue = float(revenue or 0)
    paid = float(paid or 0)
    if revenue <= 0:
        return None
    paid_share = min(max(paid / revenue * 100, 0.0), 100.0)
    paid_pct = int(round(paid_share))
    return {
        "paid_share": round(paid_share, 1),
        "due_share": round(100 - paid_share, 1),
        "paid_pct": paid_pct,
        "due_pct": 100 - paid_pct,
    }


def _window(column, start_date, end_date):
    """Shared date restriction for one DATE()-comparable column."""
    sql = ""
    params = []
    if start_date:
        sql += f" AND DATE({column}) >= ?"
        params.append(start_date)
    if end_date:
        sql += f" AND DATE({column}) <= ?"
        params.append(end_date)
    return sql, params


def summary_metrics(start_date=None, end_date=None, branch_id=None):
    """Headline figures. The branch restriction applies to orders and payments.

    Customer and product totals stay company-wide: they count master data, not
    activity, so scoping them to one branch would understate the business.
    """
    db = get_db()
    sql = """
        SELECT COUNT(*) AS count, COALESCE(SUM(o.total), 0) AS revenue, COALESCE(SUM(o.due_total), 0) AS due
        FROM orders o
        WHERE 1=1
    """
    params = []
    window_sql, window_params = _window("o.created_at", start_date, end_date)
    sql += window_sql
    params.extend(window_params)
    scope_sql, scope_params = order_branch_clause("o", branch_id=branch_id)
    sql += scope_sql
    params.extend(scope_params)
    orders = db.execute(sql, params).fetchone()

    payments_sql = """
        SELECT COUNT(*) AS count, COALESCE(SUM(pay.amount), 0) AS paid
        FROM payments pay JOIN orders o ON o.id = pay.order_id
        WHERE pay.status = 'paid' AND COALESCE(pay.deleted_at, '') = ''
    """
    payments_params = []
    window_sql, window_params = _window("pay.created_at", start_date, end_date)
    payments_sql += window_sql
    payments_params.extend(window_params)
    scope_sql, scope_params = order_branch_clause("o", branch_id=branch_id)
    payments_sql += scope_sql
    payments_params.extend(scope_params)
    payments = db.execute(payments_sql, payments_params).fetchone()

    customers = db.execute("SELECT COUNT(*) AS count FROM customers WHERE 1=1").fetchone()
    products = db.execute("SELECT COUNT(*) AS count FROM products WHERE active = 1").fetchone()

    return {
        "orders": orders["count"] or 0,
        "revenue": money(orders["revenue"]),
        "due": money(orders["due"]),
        "payments": payments["count"] or 0,
        "paid": money(payments["paid"]),
        "customers": customers["count"] or 0,
        "products": products["count"] or 0,
        "payment_split": payment_split(orders["revenue"], payments["paid"]),
    }


def orders_by_status(start_date=None, end_date=None, branch_id=None):
    db = get_db()
    sql = """
        SELECT o.status, COUNT(*) AS count, COALESCE(SUM(o.total), 0) AS total FROM orders o
        WHERE 1=1
    """
    params = []
    window_sql, window_params = _window("o.created_at", start_date, end_date)
    sql += window_sql
    params.extend(window_params)
    scope_sql, scope_params = order_branch_clause("o", branch_id=branch_id)
    sql += scope_sql
    params.extend(scope_params)
    sql += " GROUP BY o.status ORDER BY count DESC, o.status"
    return rows_with_bars(db.execute(sql, params).fetchall())


def payments_by_method(start_date=None, end_date=None, branch_id=None):
    db = get_db()
    sql = """
        SELECT pay.method AS method, COUNT(*) AS count, COALESCE(SUM(pay.amount), 0) AS total
        FROM payments pay JOIN orders o ON o.id = pay.order_id
        WHERE pay.status = 'paid' AND COALESCE(pay.deleted_at, '') = ''
    """
    params = []
    window_sql, window_params = _window("pay.created_at", start_date, end_date)
    sql += window_sql
    params.extend(window_params)
    scope_sql, scope_params = order_branch_clause("o", branch_id=branch_id)
    sql += scope_sql
    params.extend(scope_params)
    sql += " GROUP BY pay.method ORDER BY total DESC, pay.method"
    return rows_with_bars(db.execute(sql, params).fetchall())


def product_performance(start_date=None, end_date=None, limit=10, branch_id=None):
    db = get_db()
    sql = """
        SELECT COALESCE(p.name, oi.custom_name, 'Custom line') AS product_name,
            COALESCE(SUM(oi.quantity), 0) AS quantity,
            COALESCE(SUM(oi.line_total), 0) AS total
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        LEFT JOIN products p ON p.id = oi.product_id
        WHERE 1=1
    """
    params = []
    # order_items has no created_at: the order's own date is the reporting date,
    # which also matches every other query in this module (and fixes a 500 that
    # any report date filter used to raise).
    window_sql, window_params = _window("o.created_at", start_date, end_date)
    sql += window_sql
    params.extend(window_params)
    scope_sql, scope_params = order_branch_clause("o", branch_id=branch_id)
    sql += scope_sql
    params.extend(scope_params)
    sql += """
        GROUP BY product_name
        ORDER BY total DESC, quantity DESC
        LIMIT ?
    """
    params.append(limit)
    return [row_dict(row) for row in db.execute(sql, params).fetchall()]


def customer_summary(start_date=None, end_date=None, limit=10, branch_id=None):
    db = get_db()
    sql = """
        SELECT c.name AS customer_name, COUNT(o.id) AS orders, COALESCE(SUM(o.total), 0) AS total
        FROM customers c
        LEFT JOIN orders o ON o.customer_id = c.id
        WHERE 1=1
    """
    params = []
    window_sql, window_params = _window("o.created_at", start_date, end_date)
    sql += window_sql
    params.extend(window_params)
    # Only ever applied when there IS a restriction: with none, the LEFT JOIN
    # still lists customers who have not ordered yet.
    scope_sql, scope_params = order_branch_clause("o", branch_id=branch_id)
    sql += scope_sql
    params.extend(scope_params)
    sql += """
        GROUP BY c.id, c.name
        ORDER BY total DESC, orders DESC, c.name
        LIMIT ?
    """
    params.append(limit)
    return [row_dict(row) for row in db.execute(sql, params).fetchall()]


def orders_export_rows(start_date=None, end_date=None, branch_id=None):
    db = get_db()
    sql = """
        SELECT o.order_number, COALESCE(c.name, '') AS customer, o.status, o.payment_status, o.total, o.due_total
        FROM orders o
        LEFT JOIN customers c ON c.id = o.customer_id
        WHERE 1=1
    """
    params = []
    window_sql, window_params = _window("o.created_at", start_date, end_date)
    sql += window_sql
    params.extend(window_params)
    scope_sql, scope_params = order_branch_clause("o", branch_id=branch_id)
    sql += scope_sql
    params.extend(scope_params)
    sql += " ORDER BY o.created_at DESC, o.id DESC"
    return db.execute(sql, params).fetchall()
