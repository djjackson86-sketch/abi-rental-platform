"""Legacy balance helpers for imported Sano/Booqable customer balances."""

from app.db import get_db, now

ACTIVE_STATUS = "active"
COLLECTED_STATUS = "collected"
WAIVED_STATUS = "waived"
ADJUSTED_STATUS = "adjusted"


def legacy_balance_total(customer_id):
    if not customer_id:
        return 0.0
    row = get_db().execute(
        """SELECT COALESCE(SUM(amount_due), 0) AS balance
        FROM legacy_customer_balances
        WHERE customer_id = ? AND status = ?""",
        (customer_id, ACTIVE_STATUS),
    ).fetchone()
    return round(float(row["balance"] or 0), 2) if row else 0.0


def legacy_balance_entries(customer_id):
    if not customer_id:
        return []
    return get_db().execute(
        """SELECT * FROM legacy_customer_balances
        WHERE customer_id = ?
        ORDER BY COALESCE(NULLIF(source_started_at, ''), source_created_at) DESC, id DESC""",
        (customer_id,),
    ).fetchall()


def legacy_balance_summary(customer_id):
    entries = legacy_balance_entries(customer_id)
    active = [row for row in entries if row["status"] == ACTIVE_STATUS]
    return {
        "total": round(sum(float(row["amount_due"] or 0) for row in active), 2),
        "active_count": len(active),
        "entries": entries,
    }


def upsert_legacy_balance(customer_id, source_customer_id, source_order_number, amount_due,
                          source_payment_status="", source_order_status="", source_total=0,
                          source_paid=0, invoice_count=0, source_started_at="",
                          source_created_at="", note="Imported from previous Sano system"):
    if not customer_id:
        raise ValueError("customer_id is required")
    source_order_number = str(source_order_number or "").strip()
    if not source_order_number:
        raise ValueError("source_order_number is required")
    amount_due = round(float(amount_due or 0), 2)
    if amount_due <= 0:
        raise ValueError("amount_due must be positive")
    db = get_db()
    db.execute(
        """INSERT INTO legacy_customer_balances
        (customer_id, source_system, source_customer_id, source_order_number,
         source_payment_status, source_order_status, source_total, source_paid,
         amount_due, invoice_count, source_started_at, source_created_at, note,
         status, created_at, updated_at)
        VALUES (?, 'booqable', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_system, source_order_number) DO UPDATE SET
          customer_id=excluded.customer_id,
          source_customer_id=excluded.source_customer_id,
          source_payment_status=excluded.source_payment_status,
          source_order_status=excluded.source_order_status,
          source_total=excluded.source_total,
          source_paid=excluded.source_paid,
          amount_due=excluded.amount_due,
          invoice_count=excluded.invoice_count,
          source_started_at=excluded.source_started_at,
          source_created_at=excluded.source_created_at,
          note=excluded.note,
          status=excluded.status,
          updated_at=excluded.updated_at""",
        (
            customer_id, str(source_customer_id or "").strip(), source_order_number,
            str(source_payment_status or "").strip(), str(source_order_status or "").strip(),
            round(float(source_total or 0), 2), round(float(source_paid or 0), 2), amount_due,
            int(invoice_count or 0), str(source_started_at or "")[:10], str(source_created_at or "")[:10],
            str(note or "").strip(), ACTIVE_STATUS, now(), now(),
        ),
    )
