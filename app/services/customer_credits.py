"""Customer credit ledger helpers (ticket ABI-341953068).

Credits are additive ledger entries:
* positive rows grant customer credit from a refund/deposit refund;
* negative rows consume credit on an order payment.
Only rows with ``status='active'`` count toward the customer's available balance.
"""

from app.db import get_db, now

CREDIT_STATUS_ACTIVE = "active"
SOURCE_ORDER_REFUND = "order_refund"
SOURCE_DEPOSIT_REFUND = "deposit_refund"
SOURCE_ORDER_PAYMENT = "order_payment"


def customer_credit_balance(customer_id):
    if not customer_id:
        return 0.0
    row = get_db().execute(
        """SELECT COALESCE(SUM(amount), 0) AS balance
        FROM customer_credits
        WHERE customer_id = ? AND status = ?""",
        (customer_id, CREDIT_STATUS_ACTIVE),
    ).fetchone()
    return round(float(row["balance"] or 0), 2) if row else 0.0


def customer_credit_entries(customer_id):
    if not customer_id:
        return []
    return get_db().execute(
        """SELECT cc.*, so.order_number AS source_order_number, ao.order_number AS applied_order_number
        FROM customer_credits cc
        LEFT JOIN orders so ON so.id = cc.source_order_id
        LEFT JOIN orders ao ON ao.id = cc.applied_order_id
        WHERE cc.customer_id = ?
        ORDER BY cc.created_at DESC, cc.id DESC""",
        (customer_id,),
    ).fetchall()


def create_customer_credit(customer_id, amount, source_order_id, source_type, note=""):
    amount = round(float(amount or 0), 2)
    if not customer_id:
        raise ValueError("A customer is required before credit can be stored")
    if amount <= 0:
        raise ValueError("Credit amount must be greater than zero")
    source_type = (source_type or "").strip() or SOURCE_ORDER_REFUND
    note = (note or "").strip()
    cur = get_db().execute(
        """INSERT INTO customer_credits
        (customer_id, source_order_id, amount, source_type, note, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (customer_id, source_order_id, amount, source_type, note, CREDIT_STATUS_ACTIVE, now()),
    )
    return cur.lastrowid


def apply_customer_credit(customer_id, amount, applied_order_id, applied_payment_id=None, note=""):
    amount = round(float(amount or 0), 2)
    if not customer_id:
        raise ValueError("A customer is required before credit can be used")
    if amount <= 0:
        raise ValueError("Customer credit amount must be greater than zero")
    available = customer_credit_balance(customer_id)
    if amount > available:
        raise ValueError(f"Customer credit cannot exceed the available R{available:.2f}")
    cur = get_db().execute(
        """INSERT INTO customer_credits
        (customer_id, source_order_id, amount, source_type, note, status, applied_order_id, applied_payment_id, created_at)
        VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?)""",
        (customer_id, -amount, SOURCE_ORDER_PAYMENT, (note or "").strip(), CREDIT_STATUS_ACTIVE, applied_order_id, applied_payment_id, now()),
    )
    return cur.lastrowid


def link_applied_payment(credit_entry_id, payment_id):
    get_db().execute(
        "UPDATE customer_credits SET applied_payment_id = ? WHERE id = ?",
        (payment_id, credit_entry_id),
    )


def replace_source_credit(customer_id, amount, source_order_id, source_type, note=""):
    """Replace active credit for one source with a fresh audit row.

    Used when a deposit refund is edited/deleted: old credit is not hard-deleted,
    it is marked reversed and the current refund state gets the only active row.
    """
    db = get_db()
    db.execute(
        """UPDATE customer_credits
        SET status = 'reversed'
        WHERE source_order_id = ? AND source_type = ? AND status = ?""",
        (source_order_id, source_type, CREDIT_STATUS_ACTIVE),
    )
    if round(float(amount or 0), 2) > 0:
        return create_customer_credit(customer_id, amount, source_order_id, source_type, note=note)
    return None
