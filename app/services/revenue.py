"""Receipt-date revenue, separating refundable security from rental earnings.

Historical receipts have no rent/deposit allocation. For security-deposit orders
we conservatively reserve the contractual deposit first across chronological
receipts, then recognise rent. Applied funds release that reserve on the recorded
application date. Neither a deposit payout nor a transfer to customer credit is
an expense/revenue event. No stored transactions are rewritten.
"""
from app.db import get_db
from app.services.access import order_branch_clause


def order_revenue_events(start_date=None, end_date=None, branch_id=None,
                         *, order_where="1=1", order_params=(), detail=False):
    """Return daily/category totals with one bounded SQL query (SQLite/libSQL).

    All earlier receipts must enter the windows, even for a one-day report.
    Current order charges cap recognised security-deposit revenue: return charges
    already live in total, so applying a deposit must not add a second charge.
    Historical edits restate reports; this is not an immutable event ledger.

    ``detail=True`` returns the individual receipt events instead of the
    day/category aggregate, each with the revenue it recognised that day (the
    per-order running total's change). It is the same query and the same
    arithmetic as the totals, so a breakdown built from these rows cannot drift
    from the figure the dashboard shows (ticket ABI-341953335).
    """
    scope, scope_params = order_branch_clause("o", branch_id=branch_id)
    # One line per contributing receipt, or the day/category aggregate the
    # reports and dashboard cards read. Both come from the same CTEs.
    TOTAL_SELECT = "SELECT day, category, SUM(amount) AS total FROM changes"
    DETAIL_SELECT = (
        "SELECT day, category, amount, id, order_id, order_number, customer_name,"
        " method, reference, payment_date, receipt, applied, deposit, charges,"
        " received, used, revenue FROM changes"
    )
    sql = f"""WITH events AS (
        SELECT pay.id, pay.order_id,
            substr(COALESCE(NULLIF(pay.payment_date, ''), pay.created_at), 1, 10) AS day,
            CASE WHEN LOWER(COALESCE(pay.method, '')) = 'deposit_applied'
                 THEN 0 ELSE pay.amount END AS receipt,
            CASE WHEN LOWER(COALESCE(pay.method, '')) = 'deposit_applied'
                 THEN pay.amount ELSE 0 END AS applied,
            CASE WHEN COALESCE(o.deposit_option, 'security_deposit') = 'security_deposit'
                 THEN MAX(COALESCE(o.deposit_total, 0), 0) ELSE 0 END AS deposit,
            MAX(COALESCE(o.total, 0) - CASE
                WHEN COALESCE(o.deposit_option, 'security_deposit') = 'security_deposit'
                THEN COALESCE(o.deposit_total, 0) ELSE 0 END, 0) AS charges,
            CASE WHEN LOWER(COALESCE(pay.method, '')) = 'deposit_applied'
                 THEN 'trailer_rentals'
                 WHEN (CASE WHEN o.status = 'archived' THEN o.status_before_archive
                       WHEN o.status IN ('canceled', 'cancelled') THEN o.status_before_cancel
                       ELSE o.status END) = 'sales_repairs' THEN 'sales_repairs'
                 WHEN (CASE WHEN o.status = 'archived' THEN o.status_before_archive
                       WHEN o.status IN ('canceled', 'cancelled') THEN o.status_before_cancel
                       ELSE o.status END) IN ('reserved', 'started', 'returned')
                 THEN 'trailer_rentals' ELSE 'unattributed' END AS category,
            o.order_number AS order_number,
            c.name AS customer_name,
            pay.method AS method,
            pay.reference AS reference,
            COALESCE(NULLIF(pay.payment_date, ''), pay.created_at) AS payment_date
        FROM payments pay JOIN orders o ON o.id = pay.order_id
        LEFT JOIN customers c ON c.id = o.customer_id
        WHERE pay.status = 'paid' AND COALESCE(pay.deleted_at, '') = ''
            AND LOWER(COALESCE(pay.method, '')) <> 'account'
            AND ({order_where}){scope}
    ), running AS (
        SELECT *, SUM(receipt) OVER w AS received,
            SUM(applied) OVER w AS used
        FROM events WINDOW w AS (PARTITION BY order_id ORDER BY day, id
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
    ), earned AS (
        SELECT *, CASE WHEN deposit > 0
            THEN MIN(charges, MAX(received - deposit, 0) + used)
            ELSE received + used END AS revenue FROM running
    ), changes AS (
        SELECT *, revenue - COALESCE(LAG(revenue) OVER
            (PARTITION BY order_id ORDER BY day, id), 0) AS amount FROM earned
    ) {DETAIL_SELECT if detail else TOTAL_SELECT} WHERE 1=1"""
    params = [*order_params, *scope_params]
    if start_date:
        sql += " AND day >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND day <= ?"
        params.append(end_date)
    sql += " ORDER BY day, id" if detail else " GROUP BY day, category"
    return get_db().execute(sql, params).fetchall()
