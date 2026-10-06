"""Customer borrowing limits, separate from the prepaid credit ledger.

Database guards deliberately use one statement: the remote adapter autocommits,
so a Python read/check/write alone cannot protect concurrent finalisations.
Debt is per allocated order, across all branches; unallocated/prepaid funds and
payments on drafts do not offset another invoice's debt.
"""
from app.db import get_db

CREDIT_LIMIT_ERROR = 'Customer credit limit reached or this order would exceed the maximum credit allowed'


def debt_sql(customer='NEW.customer_id', excluded='-1'):
    return f"""COALESCE((SELECT SUM(MAX(ROUND(debt_order.total - COALESCE((
        SELECT SUM(p.amount) FROM payments p WHERE p.order_id = debt_order.id
        AND COALESCE(p.status, 'paid') = 'paid' AND COALESCE(p.deleted_at, '') = ''
    ), 0), 2), 0)) FROM orders debt_order WHERE debt_order.customer_id = {customer}
    AND debt_order.id != {excluded} AND EXISTS (SELECT 1 FROM documents d
        WHERE d.order_id = debt_order.id AND d.document_type = 'invoice' AND d.status = 'finalized')), 0)"""


def outstanding_debt(customer_id, exclude_order_id=-1):
    return float(get_db().execute(
        f'SELECT {debt_sql("?", "?")} AS debt', (customer_id, exclude_order_id)
    ).fetchone()['debt'])


def ensure_credit_capacity(customer_id, total, order_id=None):
    row = get_db().execute('SELECT credit_allowed, credit_limit FROM customers WHERE id = ?', (customer_id,)).fetchone()
    if not row or not row['credit_allowed']:
        return
    paid = 0
    if order_id:
        from app.services.payments import payment_total
        paid = payment_total(order_id)
    debt = outstanding_debt(customer_id, order_id or -1)
    proposed = round(max(float(total) - paid, 0), 2)
    if round(debt + proposed, 2) > round(float(row['credit_limit']), 2) or (not order_id and debt >= float(row['credit_limit'])):
        raise ValueError(CREDIT_LIMIT_ERROR)


def execute_credit_checked(db, sql, params):
    """Translate only our trigger rejection into the routes' validation error."""
    try:
        return db.execute(sql, params)
    except Exception as exc:
        if CREDIT_LIMIT_ERROR in str(exc):
            raise ValueError(CREDIT_LIMIT_ERROR) from exc
        raise


def install_credit_guards(db):
    debt = debt_sql()
    edit_debt = debt_sql(excluded='NEW.id')
    paid = "COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.order_id = NEW.id AND COALESCE(p.status, 'paid') = 'paid' AND COALESCE(p.deleted_at, '') = ''), 0)"
    # SQLite triggers also execute atomically in Turso/libSQL.
    db.execute(f"""CREATE TRIGGER IF NOT EXISTS customer_credit_order_insert
        BEFORE INSERT ON orders WHEN EXISTS (SELECT 1 FROM customers c
        WHERE c.id = NEW.customer_id AND c.credit_allowed = 1
        AND ({debt} >= ROUND(c.credit_limit, 2) OR ROUND({debt} + MAX(NEW.total, 0), 2) > ROUND(c.credit_limit, 2)))
        BEGIN SELECT RAISE(ABORT, '{CREDIT_LIMIT_ERROR}'); END""")
    db.execute(f"""CREATE TRIGGER IF NOT EXISTS customer_credit_order_update
        BEFORE UPDATE OF total, customer_id ON orders
        WHEN (NEW.customer_id IS NOT OLD.customer_id OR NEW.total > OLD.total)
        AND EXISTS (SELECT 1 FROM customers c WHERE c.id = NEW.customer_id AND c.credit_allowed = 1
        AND ROUND({edit_debt} + MAX(ROUND(NEW.total - {paid}, 2), 0), 2) > ROUND(c.credit_limit, 2))
        BEGIN SELECT RAISE(ABORT, '{CREDIT_LIMIT_ERROR}'); END""")
    invoice_debt = debt_sql('o.customer_id', 'o.id')
    invoice_paid = "COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.order_id = o.id AND COALESCE(p.status, 'paid') = 'paid' AND COALESCE(p.deleted_at, '') = ''), 0)"
    condition = f"""EXISTS (SELECT 1 FROM orders o JOIN customers c ON c.id = o.customer_id
        WHERE o.id = NEW.order_id AND c.credit_allowed = 1 AND
        ROUND({invoice_debt} + MAX(ROUND(o.total - {invoice_paid}, 2), 0), 2) > ROUND(c.credit_limit, 2))"""
    for event, suffix in [('INSERT', 'insert'), ('UPDATE OF status, order_id, document_type', 'update')]:
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS customer_credit_invoice_{suffix}
            BEFORE {event} ON documents WHEN NEW.document_type = 'invoice'
            AND NEW.status = 'finalized' AND {condition}
            BEGIN SELECT RAISE(ABORT, '{CREDIT_LIMIT_ERROR}'); END""")
