"""Upfront customer funds: cash receipt now, revenue only when used on an order."""
import math
from app.db import get_db, now
from app.services.payments import parse_payment_date


def install_guards(db):
    db.execute("""CREATE TABLE IF NOT EXISTS prepaid_fundings (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
      amount REAL NOT NULL CHECK(amount > 0), method TEXT NOT NULL,
      reference TEXT NOT NULL DEFAULT '', payment_date TEXT NOT NULL,
      created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'paid',
      deleted_at TEXT NOT NULL DEFAULT '', request_key TEXT NOT NULL DEFAULT '',
      branch_id INTEGER REFERENCES branches(id) ON DELETE SET NULL,
      created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL
    )""")
    from app.db import ensure_column
    ensure_column(db, 'customer_credits', 'funding_id', 'INTEGER REFERENCES prepaid_fundings(id) ON DELETE RESTRICT')
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS prepaid_funding_request ON prepaid_fundings(request_key) WHERE request_key<>''")
    db.execute("""CREATE TRIGGER IF NOT EXISTS prepaid_funding_credit AFTER INSERT ON prepaid_fundings
      WHEN NEW.status='paid'
      BEGIN INSERT INTO customer_credits(customer_id,amount,source_type,note,status,created_at,funding_id)
      VALUES(NEW.customer_id,NEW.amount,'prepaid_funding',NEW.reference,'active',NEW.created_at,NEW.id); END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS prepaid_funding_immutable BEFORE UPDATE ON prepaid_fundings
      WHEN NEW.amount IS NOT OLD.amount OR NEW.customer_id IS NOT OLD.customer_id OR NEW.method IS NOT OLD.method
      OR NEW.branch_id IS NOT OLD.branch_id OR NEW.payment_date IS NOT OLD.payment_date
      BEGIN SELECT RAISE(ABORT,'Reverse the prepaid funding instead of editing it'); END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS prepaid_funding_reversal_guard BEFORE UPDATE OF status ON prepaid_fundings
      WHEN OLD.status='paid' AND NEW.status<>'paid' AND ROUND(OLD.amount,2) > ROUND((SELECT COALESCE(SUM(amount),0)
        FROM customer_credits WHERE customer_id=OLD.customer_id AND status='active'),2)
      BEGIN SELECT RAISE(ABORT,'Cannot reverse prepaid funds already used on orders'); END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS prepaid_funding_reverse AFTER UPDATE OF status ON prepaid_fundings
      WHEN OLD.status='paid' AND NEW.status<>'paid'
      BEGIN UPDATE customer_credits SET status='reversed' WHERE funding_id=OLD.id; END""")
    # Link and debit are one atomic payment statement, including on Turso.
    db.execute("""CREATE TRIGGER IF NOT EXISTS prepaid_payment_balance_guard BEFORE INSERT ON payments
      WHEN NEW.method='customer_credit' AND NEW.amount>0 AND NEW.status='paid'
        AND ROUND(NEW.amount,2)>ROUND(COALESCE((SELECT SUM(cc.amount) FROM customer_credits cc
          JOIN orders o ON o.customer_id=cc.customer_id WHERE o.id=NEW.order_id AND cc.status='active'),0),2)
      BEGIN SELECT RAISE(ABORT,'Customer credit cannot exceed the available balance'); END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS prepaid_payment_debit AFTER INSERT ON payments
      WHEN NEW.method='customer_credit' AND NEW.amount>0 AND NEW.status='paid'
      BEGIN INSERT INTO customer_credits(customer_id,amount,source_type,note,status,applied_order_id,applied_payment_id,created_at)
        SELECT customer_id,-NEW.amount,'order_payment',NEW.reference,'active',NEW.order_id,NEW.id,NEW.created_at
        FROM orders WHERE id=NEW.order_id; END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS prepaid_payment_immutable BEFORE UPDATE ON payments
      WHEN (OLD.method='customer_credit' OR NEW.method='customer_credit') AND OLD.amount>0
      AND (NEW.amount IS NOT OLD.amount OR NEW.method IS NOT OLD.method OR NEW.order_id IS NOT OLD.order_id)
      BEGIN SELECT RAISE(ABORT,'Reverse the customer-credit settlement instead of editing it'); END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS prepaid_payment_reverse AFTER UPDATE OF status,deleted_at ON payments
      WHEN OLD.method='customer_credit' AND OLD.amount>0 AND (NEW.status<>'paid' OR NEW.deleted_at<>'')
      BEGIN UPDATE customer_credits SET status='reversed' WHERE applied_payment_id=OLD.id AND source_type='order_payment'; END""")


def record_funding(customer_id, form):
    from flask import has_request_context, session
    from app.services.access import session_primary_branch_id
    db=get_db()
    customer=db.execute('SELECT branch_id FROM customers WHERE id=?',(customer_id,)).fetchone()
    if not customer:
        raise ValueError('Customer not found')
    key=(form.get('request_key') or '').strip()
    if key:
        existing=db.execute('SELECT id,customer_id FROM prepaid_fundings WHERE request_key=?',(key,)).fetchone()
        if existing:
            if existing['customer_id']!=customer_id:
                raise ValueError('Funding belongs to another customer')
            return existing['id']
    try:
        amount=round(float(form.get('amount') or 0),2)
    except (ValueError,TypeError):
        raise ValueError('Funding amount must be a number')
    if not math.isfinite(amount) or amount<=0:
        raise ValueError('Funding amount must be positive and finite')
    method=(form.get('method') or '').strip().lower()
    if method not in ('cash','card','eft'):
        raise ValueError('Choose a funding method: Cash, Card, or EFT')
    branch_id=(session_primary_branch_id() if has_request_context() else None) or customer['branch_id']
    user_id=session.get('user_id') if has_request_context() else None
    cur=db.execute("""INSERT INTO prepaid_fundings(customer_id,amount,method,reference,payment_date,created_at,request_key,branch_id,created_by_user_id)
      VALUES(?,?,?,?,?,?,?,?,?)""",(customer_id,amount,method,(form.get('reference') or '').strip(),parse_payment_date(form.get('payment_date')),now(),key,branch_id,user_id))
    db.commit()
    return cur.lastrowid
