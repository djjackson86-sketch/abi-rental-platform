from app.db import get_db
from app.services.access import customer_branch_clause, order_branch_clause, product_branch_clause
from app.services.timezone import local_now_iso


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


def dashboard_period_metrics(start_date=None, end_date=None, branch_id=None):
    """The four headline dashboard cards, restricted to a period (and branch).

    Definitions are the ones the dashboard has always used, only windowed:

    * ``orders``   — orders **created** in the period (``orders.created_at``)
    * ``revenue``  — the sum of those orders' totals
    * ``products`` — catalogue rows **added** in the period (``products.created_at``);
      like the pre-ticket card this counts every row, archived included
    * ``customers``— customer records **added** in the period (``customers.created_at``)

    With ``start_date``/``end_date`` of ``None`` the window is off and the four
    numbers are byte-for-byte the all-time figures the page showed before the
    quick ranges existed, so the "All time" pill is a true baseline.

    Rows come back as plain ints/float — production returns libsql tuple rows,
    which is why nothing here builds a dict from a raw row without ``row_dict``.
    """
    db = get_db()

    order_sql = "SELECT COUNT(*) AS count, COALESCE(SUM(o.total), 0) AS revenue FROM orders o WHERE 1=1"
    order_params = []
    window_sql, window_params = _window("o.created_at", start_date, end_date)
    order_sql += window_sql
    order_params.extend(window_params)
    scope_sql, scope_params = order_branch_clause("o", branch_id=branch_id)
    order_sql += scope_sql
    order_params.extend(scope_params)
    orders = row_dict(db.execute(order_sql, order_params).fetchone())

    product_sql = "SELECT COUNT(*) AS count FROM products p WHERE 1=1"
    product_params = []
    window_sql, window_params = _window("p.created_at", start_date, end_date)
    product_sql += window_sql
    product_params.extend(window_params)
    scope_sql, scope_params = product_branch_clause("p", include_unassigned=True, branch_id=branch_id)
    product_sql += scope_sql
    product_params.extend(scope_params)
    products = row_dict(db.execute(product_sql, product_params).fetchone())

    customer_sql = "SELECT COUNT(*) AS count FROM customers WHERE 1=1"
    customer_params = []
    window_sql, window_params = _window("created_at", start_date, end_date)
    customer_sql += window_sql
    customer_params.extend(window_params)
    customers = row_dict(db.execute(customer_sql, customer_params).fetchone())

    return {
        "orders": int(orders.get("count") or 0),
        "revenue": money(orders.get("revenue")),
        "products": int(products.get("count") or 0),
        "customers": int(customers.get("count") or 0),
    }


# Rental stock is counted per item, not per booking line, and the client's
# "Other Rental Products" group (ratchets, straps, the non-trailer hire extras)
# is deliberately excluded from the trailer cards.
TRAILER_GROUPS_EXCLUDED = ("Other Rental Products",)

# "New orders for the day" excludes everything that has not actually started:
# a draft is not an order yet and a reservation is counted by its own card.
_NOT_NEW_ORDER_STATUSES = ("draft", "reserved", "canceled", "cancelled", "archived")
# Reserved earlier and collected today: the order exists and has been picked up.
_PICKED_UP_STATUSES = ("started", "returned")


def dashboard_day_metrics(day=None, branch_id=None):
    """Every "for the day" figure the dashboard shows, as finished scalars.

    ``day`` is the business day (Africa/Johannesburg) and is compared as a
    ``YYYY-MM-DD`` prefix on the stored timestamp, matching how the dashboard
    has always decided "today". Orders, payments and the new-customer count are
    branch-scoped, so these cards follow the session scope and the dashboard's own
    ``branch_id`` filter exactly like the rest of the dashboard — through
    ``order_branch_clause`` / ``product_branch_clause`` /
    ``customer_branch_clause``, where the session scope always wins, so the filter
    can only ever NARROW. "New customers for the day" counts the customers added
    by the branch in view (ticket ABI-341952962); an unrestricted view still
    counts the whole company.

    Revenue for the day is money RECEIVED today (paid payments, by payment date),
    not the value of the orders raised today — the client asked for "actual
    payments received, not just created orders". The three method cards add up to
    it exactly.

    The two trailer cards are a snapshot, not a day figure: **out** is what is on
    hire (a ``started`` order), and **in** is the rest of the yard — the active
    rental fleet less what is currently out. "Other Rental Products" (ratchets,
    straps) is not trailer stock and is excluded from both.

    Plain values only — production rows are libsql tuples, so nothing here
    hands a row object to the template.
    """
    db = get_db()
    day = day or local_now_iso(timespec="seconds")[:10]
    scope_sql, scope_params = order_branch_clause("o", branch_id=branch_id)
    product_scope_sql, product_scope_params = product_branch_clause("p", include_unassigned=True, branch_id=branch_id)

    def count(sql, params):
        row = db.execute(sql, params).fetchone()
        return int(row["c"] or 0)

    def total(sql, params):
        row = db.execute(sql, params).fetchone()
        return money(row["s"])

    def payment_total(method=None):
        """Money RECEIVED for the day: paid, not archived, by payment date.

        With no ``method`` this is the day's revenue (ticket ABI-341952962:
        "actual payments received, not just created orders"), so the revenue card
        is exactly the method cards added up and the two can never disagree. One
        depot's takings stay that depot's: the order join carries the same branch
        scope as every other day card.
        """
        method_sql = " AND LOWER(pay.method) = ?" if method else ""
        method_params = [method] if method else []
        return total(
            f"""SELECT COALESCE(SUM(pay.amount), 0) s
            FROM payments pay JOIN orders o ON o.id = pay.order_id
            WHERE pay.status = 'paid' AND COALESCE(pay.deleted_at, '') = ''
              {method_sql}
              AND substr(COALESCE(NULLIF(pay.payment_date, ''), pay.created_at), 1, 10) = ?{scope_sql}""",
            [*method_params, day, *scope_params],
        )

    new_orders = count(
        f"""SELECT COUNT(*) c FROM orders o
        WHERE substr(o.created_at, 1, 10) = ?
          AND o.status NOT IN (?, ?, ?, ?, ?){scope_sql}""",
        [day, *_NOT_NEW_ORDER_STATUSES, *scope_params],
    )
    # Customers added by THIS branch (ticket ABI-341952962): a depot's figure is
    # the customers created at that depot. An unrestricted view keeps the
    # company-wide count it always had, and the branch filter can only narrow it.
    customer_scope_sql, customer_scope_params = customer_branch_clause("c", branch_id=branch_id)
    new_customers = count(
        f"""SELECT COUNT(*) c FROM customers c
        WHERE substr(c.created_at, 1, 10) = ?{customer_scope_sql}""",
        [day, *customer_scope_params],
    )
    # Revenue for the day = money actually received today (paid payments, by
    # payment date), not the value of the orders raised today.
    revenue = payment_total()
    reservations = count(
        f"""SELECT COUNT(*) c FROM orders o
        WHERE substr(o.created_at, 1, 10) = ? AND o.status = 'reserved'{scope_sql}""",
        [day, *scope_params],
    )
    # Created before today, collected today. ``picked_up_at`` is only written
    # from the day it shipped, so an order that predates the column falls back
    # to its scheduled pickup — a documented proxy, not a second date source.
    reservation_pickups = count(
        f"""SELECT COUNT(*) c FROM orders o
        WHERE substr(o.created_at, 1, 10) < ?
          AND o.status IN (?, ?)
          AND substr(COALESCE(NULLIF(o.picked_up_at, ''), o.start_at), 1, 10) = ?{scope_sql}""",
        [day, *_PICKED_UP_STATUSES, day, *scope_params],
    )

    def trailer_count(statuses):
        marks = ", ".join("?" for _ in statuses)
        return count(
            f"""SELECT COALESCE(SUM(oi.quantity), 0) c
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            JOIN products p ON p.id = oi.product_id
            LEFT JOIN product_groups pg ON pg.id = p.product_group_id
            WHERE o.status IN ({marks}) AND p.product_type = 'rental'
              AND COALESCE(pg.name, '') <> ?{scope_sql}""",
            [*statuses, *TRAILER_GROUPS_EXCLUDED, *scope_params],
        )

    # The fleet: every active rental unit the business owns, in units (a row may
    # carry more than one), minus the non-trailer hire extras. Branch-scoped with
    # the same clause the dashboard's product count uses, and the session scope
    # still wins, so a depot-scoped account sees its own yard.
    fleet = count(
        f"""SELECT COALESCE(SUM(p.quantity), 0) c
        FROM products p
        LEFT JOIN product_groups pg ON pg.id = p.product_group_id
        WHERE p.product_type = 'rental' AND p.active = 1
          AND COALESCE(pg.name, '') <> ?{product_scope_sql}""",
        [*TRAILER_GROUPS_EXCLUDED, *product_scope_params],
    )
    # On hire: a started order has the trailer out; a returned one is back in the
    # yard, and a reserved-but-uncollected booking is still standing in it.
    on_hire = trailer_count(("started",))

    return {
        "day": day,
        "orders": new_orders,
        "customers": new_customers,
        "revenue": revenue,
        "card_payments": payment_total("card"),
        "cash_payments": payment_total("cash"),
        "eft_payments": payment_total("eft"),
        "reservations": reservations,
        "reservation_pickups": reservation_pickups,
        "trailers_out": on_hire,
        # Never negative: bad data (more on hire than on the books) shows 0, not a
        # negative count of trailers.
        "trailers_in": max(0, fleet - on_hire),
        # Both plain ints, for the tests and any future card that wants them.
        "fleet": fleet,
        "on_hire": on_hire,
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
