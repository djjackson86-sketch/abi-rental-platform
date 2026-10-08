"""Explicit Account borrowing, separate from prepaid/refund credit.

Opening exposure is snapshotted once without rewriting historical records.
Per-order exposure is the greater of opening and active Account allocations,
capped by unpaid actual receivables. Receipts release capacity; new unpaid
invoices do not consume it. Triggers protect SQLite and remote autocommits.
"""
from app.db import get_db

CREDIT_LIMIT_ERROR = 'Customer credit limit reached or this order would exceed the maximum credit allowed'
ACCOUNT_ERROR = 'Account requires an enabled customer credit facility and an amount within the order balance'
PAYMENT_REQUIRED_ERROR = 'Record at least one payment before finalising an invoice'


def _paid_sql(order='o.id', account=False, excluded='-1'):
    method = "= 'account'" if account else "<> 'account'"
    return f"""COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.order_id = {order}
        AND p.id != {excluded} AND COALESCE(p.status, 'paid') = 'paid'
        AND COALESCE(p.deleted_at, '') = '' AND LOWER(COALESCE(p.method, '')) {method}), 0)"""


def _opening_sql(order='o.id'):
    return f"COALESCE((SELECT amount FROM credit_facility_opening WHERE order_id = {order}), 0)"


def _exposure_sql(order='o.id', total='o.total', excluded='-1', extra_account='0', extra_cash='0'):
    allocation = f"MAX({_opening_sql(order)}, {_paid_sql(order, True, excluded)} + {extra_account})"
    receivable = f"MAX(ROUND({total} - ({_paid_sql(order, False, excluded)} + {extra_cash}), 2), 0)"
    return f"MIN({allocation}, {receivable})"


def order_paid_sql(alias="o"):
    cash = _paid_sql(f"{alias}.id")
    account = _paid_sql(f"{alias}.id", True)
    return f"({cash} + MIN({account}, MAX({alias}.total - {cash}, 0)))"


def debt_sql(customer='NEW.customer_id', excluded='-1'):
    return f"COALESCE((SELECT SUM({_exposure_sql('debt_order.id', 'debt_order.total')}) FROM orders debt_order WHERE debt_order.customer_id = {customer} AND debt_order.id != {excluded}), 0)"


def outstanding_debt(customer_id, exclude_order_id=-1):
    return round(float(get_db().execute(
        f'SELECT {debt_sql("?", "?")} AS debt', (customer_id, exclude_order_id)
    ).fetchone()['debt']), 2)


def facility_summary(customer_id):
    row = get_db().execute('SELECT credit_allowed, credit_limit FROM customers WHERE id = ?', (customer_id,)).fetchone()
    used = outstanding_debt(customer_id) if row else 0
    limit = float(row['credit_limit'] or 0) if row else 0
    return {'enabled': bool(row and row['credit_allowed']), 'limit': limit,
            'used': used, 'available': round(max(limit - used, 0), 2)}


def ensure_credit_capacity(customer_id, total, order_id=None):
    # Creating/editing orders or finalising invoices is not borrowing.
    return


def execute_credit_checked(db, sql, params):
    try:
        return db.execute(sql, params)
    except Exception as exc:
        for message in (CREDIT_LIMIT_ERROR, ACCOUNT_ERROR, PAYMENT_REQUIRED_ERROR,
                        'This total change would restore settled Account borrowing; reverse the Account allocation before increasing the total',
                        'Reverse Account allocations before changing the customer; historical account orders cannot be reassigned'):
            if message in str(exc):
                raise ValueError(message) from exc
        raise


def install_credit_guards(db):
    db.execute("""CREATE TABLE IF NOT EXISTS credit_facility_opening (
        snapshot_key TEXT PRIMARY KEY, order_id INTEGER UNIQUE REFERENCES orders(id) ON DELETE CASCADE,
        amount REAL NOT NULL DEFAULT 0)""")
    historical_debt = f"MAX(ROUND(o.total - {_paid_sql()}, 2), 0)"
    # Sentinel and snapshot in one statement: restarts and concurrent workers
    # cannot resnapshot or add newly created invoices to opening exposure.
    db.execute(f"""INSERT OR IGNORE INTO credit_facility_opening (snapshot_key, order_id, amount)
        SELECT snapshot_key, order_id, amount FROM (
            SELECT 'complete' AS snapshot_key, NULL AS order_id, 0 AS amount
            UNION ALL SELECT CAST(o.id AS TEXT), o.id, {historical_debt}
            FROM orders o JOIN customers c ON c.id = o.customer_id
            WHERE c.credit_allowed = 1 AND EXISTS (SELECT 1 FROM documents d
                WHERE d.order_id = o.id AND d.document_type = 'invoice' AND d.status = 'finalized')
        ) WHERE NOT EXISTS (SELECT 1 FROM credit_facility_opening WHERE snapshot_key = 'complete')""")
    for suffix in ('order_insert', 'order_update', 'invoice_insert', 'invoice_update'):
        db.execute('DROP TRIGGER IF EXISTS customer_credit_' + suffix)
    active = "COALESCE(NEW.status, 'paid') = 'paid' AND COALESCE(NEW.deleted_at, '') = ''"
    for event, suffix, excluded in [('INSERT', 'insert', '-1'), ('UPDATE', 'update_v2', 'OLD.id')]:
        unchanged = "" if event == 'INSERT' else "AND NOT (NEW.order_id = OLD.order_id AND NEW.amount = OLD.amount AND LOWER(COALESCE(OLD.method, '')) = 'account' AND NEW.status IS OLD.status AND NEW.deleted_at IS OLD.deleted_at)"
        paid = f"({_paid_sql('o.id', True, excluded)} + {_paid_sql('o.id', False, excluded)})"
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS account_payment_{suffix}
            BEFORE {event} ON payments WHEN LOWER(COALESCE(NEW.method, '')) = 'account' AND {active} {unchanged}
            BEGIN
                SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM orders o JOIN customers c ON c.id = o.customer_id
                    WHERE o.id = NEW.order_id AND c.credit_allowed = 1 AND NEW.amount > 0
                    AND ROUND(NEW.amount, 2) <= ROUND(o.total - {paid}, 2))
                    THEN RAISE(ABORT, '{ACCOUNT_ERROR}') END;
                SELECT CASE WHEN EXISTS (SELECT 1 FROM orders o JOIN customers c ON c.id = o.customer_id
                    WHERE o.id = NEW.order_id AND ROUND({debt_sql('o.customer_id', 'o.id')} +
                    {_exposure_sql(excluded=excluded, extra_account='NEW.amount')}, 2) > ROUND(c.credit_limit, 2))
                    THEN RAISE(ABORT, '{CREDIT_LIMIT_ERROR}') END;
            END""")
    db.execute('DROP TRIGGER IF EXISTS account_payment_update')
    # Receipt reversals/edits must not restore more borrowing than is available.
    # Compare exposure so repayments still work after an owner lowers the limit.
    for event, suffix in [('UPDATE', 'update'), ('DELETE', 'delete')]:
        old_active = "COALESCE(OLD.status, 'paid') = 'paid' AND COALESCE(OLD.deleted_at, '') = ''"
        old_account = f"CASE WHEN LOWER(COALESCE(OLD.method, '')) = 'account' AND {old_active} THEN OLD.amount ELSE 0 END"
        old_cash = f"CASE WHEN LOWER(COALESCE(OLD.method, '')) <> 'account' AND {old_active} THEN OLD.amount ELSE 0 END"
        before = _exposure_sql(excluded='OLD.id', extra_account=old_account, extra_cash=old_cash)
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS account_receipt_{suffix}
            AFTER {event} ON payments WHEN EXISTS (SELECT 1 FROM orders o JOIN customers c ON c.id = o.customer_id
                WHERE o.id = OLD.order_id AND {_exposure_sql()} > {before}
                AND ROUND({debt_sql('o.customer_id')}, 2) > ROUND(c.credit_limit, 2))
            BEGIN SELECT RAISE(ABORT, '{CREDIT_LIMIT_ERROR}'); END""")
    before_insert = _exposure_sql(excluded='NEW.id')
    db.execute(f"""CREATE TRIGGER IF NOT EXISTS account_receipt_insert
        AFTER INSERT ON payments WHEN EXISTS (SELECT 1 FROM orders o JOIN customers c ON c.id = o.customer_id
            WHERE o.id = NEW.order_id AND {_exposure_sql()} > {before_insert}
            AND ROUND({debt_sql('o.customer_id')}, 2) > ROUND(c.credit_limit, 2))
        BEGIN SELECT RAISE(ABORT, '{CREDIT_LIMIT_ERROR}'); END""")
    db.execute(f"""CREATE TRIGGER IF NOT EXISTS account_order_total_guard
        BEFORE UPDATE OF total ON orders WHEN NEW.total > OLD.total
        AND {_exposure_sql('OLD.id', 'NEW.total')} > {_exposure_sql('OLD.id', 'OLD.total')}
        BEGIN SELECT RAISE(ABORT, 'This total change would restore settled Account borrowing; reverse the Account allocation before increasing the total'); END""")
    db.execute(f"""CREATE TRIGGER IF NOT EXISTS account_order_customer_guard
        BEFORE UPDATE OF customer_id ON orders WHEN NEW.customer_id IS NOT OLD.customer_id
        AND ({_opening_sql('OLD.id')} > 0 OR {_paid_sql('OLD.id', True)} > 0)
        BEGIN SELECT RAISE(ABORT, 'Reverse Account allocations before changing the customer; historical account orders cannot be reassigned'); END""")
    # Historical imports may insert already-finalised documents. Application
    # documents always start draft; guard the actual finalisation transition.
    for event, suffix in [('UPDATE OF status, order_id, document_type', 'update')]:
        transition = "AND OLD.status != 'finalized'" if suffix == 'update' else ''
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS invoice_payment_required_{suffix}
            BEFORE {event} ON documents WHEN NEW.document_type = 'invoice' AND NEW.status = 'finalized'
            {transition}
            AND COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.order_id = NEW.order_id
                AND COALESCE(p.status, 'paid') = 'paid' AND COALESCE(p.deleted_at, '') = ''), 0) <= 0
            BEGIN SELECT RAISE(ABORT, '{PAYMENT_REQUIRED_ERROR}'); END""")
