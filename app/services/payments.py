from datetime import date, datetime

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

# Every method the payment forms may write (ticket ABI-341953066 added "other").
# "manual" is kept for legacy ledger rows and imports. Anything else is either a
# legacy row's own method (allowed through on edit below) or junk, and is
# refused: the dashboard and the day report decide which money line a payment
# lands on from this stored value, so a typo here would drop money from a total.
PAYMENT_METHODS = ("cash", "eft", "card", "other", "customer_credit", "manual")
PAYMENT_METHOD_ERROR = "Payment method must be Cash, EFT, Card, Other, Use Customer Credit, or Manual"


def normalise_payment_method(value, fallback="manual"):
    """Lower-case a submitted method and check it against ``PAYMENT_METHODS``.

    ``fallback`` (the historic default) applies only when the form hands us
    nothing at all, so a blank method still stores "manual" exactly as before.
    """
    method = (value or "").strip().lower() or fallback
    if method not in PAYMENT_METHODS:
        raise ValueError(PAYMENT_METHOD_ERROR)
    return method


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


def normalise_payment_date_filter(value):
    value = (value or "").strip()
    if not value:
        return ""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return ""


def is_refund(payment):
    return float(payment["amount"] or 0) < 0


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
    method = normalise_payment_method(form.get("method"))
    reference = form.get("reference", "").strip()
    payment_date = parse_payment_date(form.get("payment_date"))
    if method == "customer_credit":
        return _record_customer_credit_payment(order, amount, reference, payment_date)
    created_at = now()
    db = get_db()
    db.execute(
        """INSERT INTO payments (order_id, amount, method, reference, status, payment_date, created_at)
        VALUES (?, ?, ?, ?, 'paid', ?, ?)""",
        (order_id, amount, method, reference, payment_date, created_at),
    )
    db.commit()
    return recalculate_order_payment(order_id)


def _row_get(row, key, default=None):
    """Read one field from any DB row driver.

    sqlite3.Row has ``keys()``, libSQL/Turso rows do NOT, so never probe a row
    with ``"x" in row.keys()`` - it works locally and raises AttributeError in
    production (ABI-341953068: 'Row' object has no attribute 'keys' on the live
    customer-credit payment route).
    """
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _record_customer_credit_payment(order, amount, reference, payment_date):
    customer_id = order["customer_id"]
    if not customer_id:
        raise ValueError("A customer is required before customer credit can be used")
    due_value = _row_get(order, "due_total", None)
    if due_value is None:
        due_value = _row_get(order, "total", 0)
    due = round(max(float(due_value or 0), 0), 2)
    if due <= 0:
        raise ValueError("This order has no due amount to pay with customer credit")
    if amount > due:
        raise ValueError(f"Customer credit payment cannot be more than the order due R{due:.2f}")
    from app.services.customer_credits import apply_customer_credit, link_applied_payment

    db = get_db()
    available_entry_id = apply_customer_credit(
        customer_id,
        amount,
        order["id"],
        note=reference or f"Customer credit used on {order['order_number']}",
    )
    cur = db.execute(
        """INSERT INTO payments (order_id, amount, method, reference, status, payment_date, created_at)
        VALUES (?, ?, 'customer_credit', ?, 'paid', ?, ?)""",
        (order["id"], amount, reference or "Customer credit used", payment_date, now()),
    )
    link_applied_payment(available_entry_id, cur.lastrowid)
    db.commit()
    return recalculate_order_payment(order["id"])


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
    if is_refund(payment):
        raise ValueError("Refund rows cannot be edited from the payments ledger")
    amount = _parse_payment_amount(form)
    existing_method = (payment["method"] or "").strip().lower()
    method = (form.get("method") or existing_method or "manual").strip().lower()
    # A legacy ledger row (an import, or one of the deposit rows) can carry a
    # method the form cannot offer. Editing that row must never be blocked, so
    # its own value is allowed through while anything else unknown is refused.
    if method not in PAYMENT_METHODS and method != existing_method:
        raise ValueError(PAYMENT_METHOD_ERROR)
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
    "date": "COALESCE(NULLIF(payment_date, ''), created_at)",
    "branch": "LOWER(COALESCE(branch_label, ''))",
    "order": "LOWER(COALESCE(order_number, ''))",
    "customer": "LOWER(COALESCE(customer_name, ''))",
    "amount": "amount",
    "method": "LOWER(COALESCE(method, ''))",
    "status": "LOWER(COALESCE(status, ''))",
}


def _payment_order_clause(sort="date", direction="desc"):
    sort = sort if sort in PAYMENT_SORTS else "date"
    direction = "asc" if direction == "asc" else "desc"
    expr = PAYMENT_SORTS[sort]
    clauses = [f"{expr} {direction.upper()}"]
    if sort != "date":
        clauses.append("COALESCE(NULLIF(payment_date, ''), created_at) DESC")
    clauses.extend(["created_at DESC", "COALESCE(id, 0) DESC"])
    return ", ".join(clauses)


def _date_filter_parts(date_expr, date_from="", date_to=""):
    where_parts = []
    params = []
    date_from = normalise_payment_date_filter(date_from)
    date_to = normalise_payment_date_filter(date_to)
    if date_from:
        where_parts.append(f"{date_expr} >= ?")
        params.append(date_from)
    if date_to:
        where_parts.append(f"{date_expr} <= ?")
        params.append(date_to)
    return where_parts, params


def _payment_where(include_archived=False, branch_id=None, date_from="", date_to=""):
    where_parts = ["1=1" if include_archived else _active_payment_clause("p")]
    branch_sql, params = order_branch_clause("o", branch_id=branch_id)
    if branch_sql:
        where_parts.append(branch_sql[5:] if branch_sql.startswith(" AND ") else branch_sql)
    date_parts, date_params = _date_filter_parts("date(COALESCE(NULLIF(p.payment_date, ''), p.created_at))", date_from, date_to)
    where_parts.extend(date_parts)
    params.extend(date_params)
    return where_parts, params


def _deposit_refund_where(include_archived=False, branch_id=None, date_from="", date_to=""):
    # Deposit refunds are stored on orders, not in payments, because counting them
    # as real negative payments would make fully settled returned orders look due.
    # They still belong in the Payments & refunds ledger as read-only payout rows.
    where_parts = [
        "0=1" if include_archived else "COALESCE(o.deposit_refund_amount, 0) > 0",
        "(COALESCE(o.deposit_process_method, '') <> '' OR COALESCE(o.deposit_processed_at, '') <> '')",
    ]
    branch_sql, params = order_branch_clause("o", branch_id=branch_id)
    if branch_sql:
        where_parts.append(branch_sql[5:] if branch_sql.startswith(" AND ") else branch_sql)
    date_parts, date_params = _date_filter_parts("date(COALESCE(NULLIF(o.deposit_processed_at, ''), o.created_at))", date_from, date_to)
    where_parts.extend(date_parts)
    params.extend(date_params)
    return where_parts, params


def _payments_ledger_sql(real_where, deposit_where):
    branch_label = """CASE
                    WHEN cb.name IS NOT NULL AND rb.name IS NOT NULL AND cb.id <> rb.id THEN cb.name || ' → ' || rb.name
                    WHEN cb.name IS NOT NULL THEN cb.name
                    WHEN rb.name IS NOT NULL THEN rb.name
                    ELSE ''
                  END"""
    return f"""
        SELECT p.id, p.order_id, p.amount, p.method, p.reference, p.status, p.payment_date,
               p.created_at, p.deleted_at, NULL AS synthetic_kind,
               o.order_number, o.collect_branch_id, o.return_branch_id,
               c.name AS customer_name, cb.name AS collect_branch_name, rb.name AS return_branch_name,
               {branch_label} AS branch_label
        FROM payments p
        LEFT JOIN orders o ON o.id = p.order_id
        LEFT JOIN customers c ON c.id = o.customer_id
        LEFT JOIN branches cb ON cb.id = o.collect_branch_id
        LEFT JOIN branches rb ON rb.id = o.return_branch_id
        WHERE {' AND '.join(real_where)}
        UNION ALL
        SELECT NULL AS id, o.id AS order_id, -ROUND(COALESCE(o.deposit_refund_amount, 0), 2) AS amount,
               COALESCE(NULLIF(o.deposit_process_method, ''), 'deposit_refund') AS method,
               'Refunded deposit' AS reference, 'paid' AS status,
               COALESCE(NULLIF(o.deposit_processed_at, ''), o.created_at) AS payment_date,
               COALESCE(NULLIF(o.deposit_processed_at, ''), o.created_at) AS created_at,
               '' AS deleted_at, 'deposit_refund' AS synthetic_kind,
               o.order_number, o.collect_branch_id, o.return_branch_id,
               c.name AS customer_name, cb.name AS collect_branch_name, rb.name AS return_branch_name,
               {branch_label} AS branch_label
        FROM orders o
        LEFT JOIN customers c ON c.id = o.customer_id
        LEFT JOIN branches cb ON cb.id = o.collect_branch_id
        LEFT JOIN branches rb ON rb.id = o.return_branch_id
        WHERE {' AND '.join(deposit_where)}
    """


def list_payments(include_archived=False, branch_id=None, sort="date", direction="desc", date_from="", date_to="", limit=None, offset=0):
    real_where, real_params = _payment_where(include_archived, branch_id, date_from, date_to)
    deposit_where, deposit_params = _deposit_refund_where(include_archived, branch_id, date_from, date_to)
    params = [*real_params, *deposit_params]
    order_clause = _payment_order_clause(sort, direction)
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset or 0)])
    return get_db().execute(
        f"""SELECT * FROM ({_payments_ledger_sql(real_where, deposit_where)}) ledger
        ORDER BY {order_clause}{limit_sql}""",
        params,
    ).fetchall()


def payment_count(include_archived=False, branch_id=None, date_from="", date_to=""):
    real_where, real_params = _payment_where(include_archived, branch_id, date_from, date_to)
    deposit_where, deposit_params = _deposit_refund_where(include_archived, branch_id, date_from, date_to)
    row = get_db().execute(
        f"""SELECT COUNT(*) AS total FROM ({_payments_ledger_sql(real_where, deposit_where)}) ledger""",
        [*real_params, *deposit_params],
    ).fetchone()
    return int(row["total"] if row else 0)


def payment_method_totals(include_archived=False, branch_id=None, date_from="", date_to=""):
    # These cards are payment-method takings, matching the dashboard day cards and
    # cash-up "received" figures.  Deposit-refund payout rows stay visible in the
    # payments ledger, but they are order-settlement payouts stored on ``orders``
    # rather than captured payment rows; subtracting them here made the payments
    # page card disagree with the dashboard for the same branch/day (ABI-341953122).
    real_where, real_params = _payment_where(include_archived, branch_id, date_from, date_to)
    totals = {"card": 0.0, "cash": 0.0, "eft": 0.0}
    rows = get_db().execute(
        f"""SELECT LOWER(COALESCE(p.method, '')) AS method, COALESCE(SUM(p.amount), 0) AS total
        FROM payments p
        LEFT JOIN orders o ON o.id = p.order_id
        WHERE {' AND '.join(real_where)}
          AND LOWER(COALESCE(p.method, '')) IN ('card', 'cash', 'eft')
        GROUP BY LOWER(COALESCE(p.method, ''))""",
        real_params,
    ).fetchall()
    for row in rows:
        totals[row["method"]] = round(float(row["total"] or 0), 2)
    return totals


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
    if method not in {"eft", "card", "cash", "customer_credit"}:
        raise ValueError("Refund method must be EFT, Card, Cash, or Credit to Customer")
    payment_date = parse_payment_date(form.get("refund_date") or form.get("deposit_processed_at"))
    reference = (form.get("reference") or "Customer refund").strip()
    if method == "customer_credit" and not order["customer_id"]:
        raise ValueError("A customer is required before credit can be stored")
    db = get_db()
    db.execute("""INSERT INTO payments (order_id, amount, method, reference, status, payment_date, created_at)
        VALUES (?, ?, ?, ?, 'paid', ?, ?)""", (order_id, -amount, method, reference, payment_date, now()))
    if method == "customer_credit":
        from app.services.customer_credits import create_customer_credit, SOURCE_ORDER_REFUND
        create_customer_credit(
            order["customer_id"],
            amount,
            order_id,
            SOURCE_ORDER_REFUND,
            note=reference or f"Credit from refund on {order['order_number']}",
        )
    db.commit()
    return recalculate_order_payment(order_id)
