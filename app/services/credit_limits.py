"""ABI-341953166: outstanding order receivables reserve the credit facility.

Account allocations are historical non-cash records, not receipts. Prepaid
Customer Credit applied to an order remains a payment. No history is rewritten.
SQLite/libSQL triggers enforce capacity atomically, including receipt reversals.
"""
from app.db import get_db

CREDIT_LIMIT_ERROR = 'Customer credit limit reached or this order would exceed the maximum credit allowed'
ACCOUNT_ERROR = 'Account is no longer a payment option'
PAYMENT_REQUIRED_ERROR = 'Pay the invoice balance in full before finalising an invoice without an active credit facility'
DUPLICATE_INVOICE_ERROR = 'A finalized invoice already exists for this order'


def _paid_sql(order='o.id', account=False, excluded='-1'):
    method = "= 'account'" if account else "<> 'account'"
    return f"""COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.order_id = {order}
        AND p.id != {excluded} AND COALESCE(p.status, 'paid') = 'paid'
        AND COALESCE(p.deleted_at, '') = '' AND LOWER(COALESCE(p.method, '')) {method}), 0)"""


def _exposure_sql(order='o.id', total='o.total', excluded='-1', extra_cash='0'):
    return f"MAX(ROUND({total} - ({_paid_sql(order, excluded=excluded)} + {extra_cash}), 2), 0)"


def order_paid_sql(alias='o'):
    return _paid_sql(f'{alias}.id')


def debt_sql(customer='NEW.customer_id', excluded='-1'):
    # All outstanding orders, not just finalised invoices. An invoice never
    # reserves the same order twice; archiving is not repayment.
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
    row = get_db().execute('SELECT credit_allowed, credit_limit FROM customers WHERE id = ?', (customer_id,)).fetchone()
    if not row or not row['credit_allowed']:
        return
    paid = 0
    if order_id:
        from app.services.payments import payment_total
        paid = payment_total(order_id)
    debt = outstanding_debt(customer_id, order_id or -1)
    proposed = round(max(float(total) - paid, 0), 2)
    if order_id:
        existing = get_db().execute('SELECT customer_id,total FROM orders WHERE id=?', (order_id,)).fetchone()
        if existing and existing['customer_id'] == customer_id and proposed <= round(max(float(existing['total']) - paid, 0), 2):
            return  # Harmless/decreasing edits remain possible after a limit reduction.
    if (not order_id and debt >= round(float(row['credit_limit']), 2)) or round(debt + proposed, 2) > round(float(row['credit_limit']), 2):
        raise ValueError(CREDIT_LIMIT_ERROR)


def ensure_invoice_finalisation(order_id):
    row = get_db().execute('SELECT o.total, o.customer_id, COALESCE(c.credit_allowed,0) credit_allowed FROM orders o LEFT JOIN customers c ON c.id=o.customer_id WHERE o.id=?', (order_id,)).fetchone()
    if not row:
        raise ValueError('Order not found')
    from app.services.payments import payment_total
    if round(float(row['total']) - payment_total(order_id), 2) > 0:
        if not row['credit_allowed']:
            raise ValueError(PAYMENT_REQUIRED_ERROR)
        facility = facility_summary(row['customer_id'])
        if facility['used'] > round(facility['limit'], 2):
            raise ValueError(CREDIT_LIMIT_ERROR)


def execute_credit_checked(db, sql, params):
    try:
        return db.execute(sql, params)
    except Exception as exc:
        for message in (CREDIT_LIMIT_ERROR, ACCOUNT_ERROR, PAYMENT_REQUIRED_ERROR, DUPLICATE_INVOICE_ERROR):
            if message in str(exc):
                raise ValueError(message) from exc
        raise


def install_credit_guards(db):
    # Replace superseded guards, retaining opening snapshots and all payments.
    names = [r['name'] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND (name LIKE 'account_%' OR name LIKE 'customer_credit_%' OR name LIKE 'invoice_payment_required_%')").fetchall()]
    for name in names:
        db.execute('DROP TRIGGER IF EXISTS "' + name.replace('"', '""') + '"')
    # Existing Account records may be annotated or archived, never created,
    # increased, reassigned, or restored as new allocations.
    db.execute(f"""CREATE TRIGGER account_payment_retired_insert BEFORE INSERT ON payments
        WHEN LOWER(COALESCE(NEW.method,''))='account'
        BEGIN SELECT RAISE(ABORT, '{ACCOUNT_ERROR}'); END""")
    db.execute(f"""CREATE TRIGGER account_payment_retired_update BEFORE UPDATE ON payments
        WHEN LOWER(COALESCE(NEW.method,''))='account' AND
        (LOWER(COALESCE(OLD.method,''))<>'account' OR NEW.amount IS NOT OLD.amount
        OR NEW.order_id IS NOT OLD.order_id OR (NEW.status='paid' AND OLD.status IS NOT 'paid')
        OR (COALESCE(NEW.deleted_at,'')='' AND COALESCE(OLD.deleted_at,'')<>''))
        BEGIN SELECT RAISE(ABORT, '{ACCOUNT_ERROR}'); END""")
    db.execute(f"""CREATE TRIGGER customer_credit_order_insert BEFORE INSERT ON orders
        WHEN EXISTS (SELECT 1 FROM customers c WHERE c.id=NEW.customer_id AND c.credit_allowed=1
        AND ({debt_sql()} >= ROUND(c.credit_limit,2) OR
        ROUND({debt_sql()} + MAX(ROUND(NEW.total,2),0),2)>ROUND(c.credit_limit,2)))
        BEGIN SELECT RAISE(ABORT, '{CREDIT_LIMIT_ERROR}'); END""")
    db.execute(f"""CREATE TRIGGER customer_credit_order_update BEFORE UPDATE OF total,customer_id ON orders
        WHEN (NEW.customer_id IS NOT OLD.customer_id OR NEW.total>OLD.total)
        AND EXISTS (SELECT 1 FROM customers c WHERE c.id=NEW.customer_id AND c.credit_allowed=1
        AND ROUND({debt_sql(excluded='OLD.id')} + {_exposure_sql('OLD.id','NEW.total')},2)>ROUND(c.credit_limit,2))
        BEGIN SELECT RAISE(ABORT, '{CREDIT_LIMIT_ERROR}'); END""")
    # AFTER guards see the hypothetical final state in the same atomic statement.
    for event in ('INSERT', 'UPDATE', 'DELETE'):
        for ref in (('NEW',) if event == 'INSERT' else ('OLD',) if event == 'DELETE' else ('OLD','NEW')):
            active = f"COALESCE({ref}.status,'paid')='paid' AND COALESCE({ref}.deleted_at,'')='' AND LOWER(COALESCE({ref}.method,''))<>'account'"
            previous = f"CASE WHEN {active} THEN {ref}.amount ELSE 0 END"
            # Undo NEW or restore OLD to compare with the prior exposure.
            if ref == 'NEW':
                before = _exposure_sql(excluded='NEW.id')
                if event == 'UPDATE':
                    old_cash = "CASE WHEN OLD.order_id=NEW.order_id AND COALESCE(OLD.status,'paid')='paid' AND COALESCE(OLD.deleted_at,'')='' AND LOWER(COALESCE(OLD.method,''))<>'account' THEN OLD.amount ELSE 0 END"
                    before = _exposure_sql(excluded='NEW.id', extra_cash=old_cash)
            else:
                before = _exposure_sql(excluded='OLD.id', extra_cash=previous)
            db.execute(f"""CREATE TRIGGER customer_credit_receipt_{event.lower()}_{ref.lower()}
                AFTER {event} ON payments WHEN EXISTS (SELECT 1 FROM orders o JOIN customers c ON c.id=o.customer_id
                WHERE o.id={ref}.order_id AND c.credit_allowed=1 AND {_exposure_sql()} > {before}
                AND ROUND({debt_sql('o.customer_id')},2)>ROUND(c.credit_limit,2))
                BEGIN SELECT RAISE(ABORT, '{CREDIT_LIMIT_ERROR}'); END""")
    # Historical imports can insert finalised documents; application invoices
    # always begin draft. Guard finalisation and reassignment of final invoices.
    unpaid = f"{_exposure_sql()} > 0"
    db.execute(f"""CREATE TRIGGER customer_credit_invoice_update
        BEFORE UPDATE OF status,order_id,document_type ON documents
        WHEN NEW.document_type='invoice' AND NEW.status='finalized' AND
        (OLD.status IS NOT 'finalized' OR OLD.order_id IS NOT NEW.order_id OR OLD.document_type IS NOT 'invoice')
        BEGIN
        SELECT CASE WHEN EXISTS (SELECT 1 FROM orders o LEFT JOIN customers c ON c.id=o.customer_id
            WHERE o.id=NEW.order_id AND {unpaid} AND COALESCE(c.credit_allowed,0)<>1)
            THEN RAISE(ABORT, '{PAYMENT_REQUIRED_ERROR}') END;
        SELECT CASE WHEN EXISTS (SELECT 1 FROM orders o JOIN customers c ON c.id=o.customer_id
            WHERE o.id=NEW.order_id AND {unpaid} AND c.credit_allowed=1
            AND ROUND({debt_sql('o.customer_id')},2)>ROUND(c.credit_limit,2))
            THEN RAISE(ABORT, '{CREDIT_LIMIT_ERROR}') END;
        SELECT CASE WHEN EXISTS (SELECT 1 FROM documents d WHERE d.order_id=NEW.order_id
            AND d.document_type='invoice' AND d.status='finalized' AND d.id<>NEW.id)
            THEN RAISE(ABORT, '{DUPLICATE_INVOICE_ERROR}') END;
        END""")
    # Rebuild only derived order payment fields for historical Account orders;
    # payment rows, invoice history, totals and prepaid balances remain intact.
    paid = order_paid_sql('orders')
    due = f'ROUND(total - ({paid}), 2)'
    status = f"CASE WHEN ({paid}) <= 0 THEN 'payment_due' WHEN ({paid}) < total THEN 'partially_paid' WHEN ({paid}) = total THEN 'paid' ELSE 'overpaid' END"
    db.execute(f"""UPDATE orders SET due_total={due}, payment_status={status}
        WHERE EXISTS (SELECT 1 FROM payments p WHERE p.order_id=orders.id AND LOWER(COALESCE(p.method,''))='account')
        AND (due_total IS NOT {due} OR payment_status IS NOT ({status}))""")
