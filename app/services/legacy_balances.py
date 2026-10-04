"""Legacy balance helpers for imported Sano/Booqable customer balances.

These are customer-level opening balances carried over from the previous system.
They do not recreate old orders, invoices or product lines.

Money collected against them is tracked separately (``settled_amount`` on each
imported row plus a ``legacy_balance_payments`` receipt row), so the imported
figure is never edited and the original import stays auditable. Outstanding is
always ``amount_due - settled_amount``.

One customer payment is allocated oldest-first across that customer's still-open
imported rows, which is why a single receipt can settle several old orders.
"""

from app.db import get_db, now
from app.services.payments import normalise_payment_method, parse_payment_date

ACTIVE_STATUS = "active"
COLLECTED_STATUS = "collected"

PAYMENT_ACTIVE = "active"
PAYMENT_ARCHIVED = "archived"


def _outstanding(row):
    """Outstanding on one imported row, never below zero."""
    return round(max(float(row["amount_due"] or 0) - float(row["settled_amount"] or 0), 0), 2)


def _decorate(row):
    """A plain dict copy of a legacy row with its outstanding amount resolved."""
    entry = dict(row)
    entry["outstanding"] = _outstanding(row)
    return entry


def legacy_balance_entries(customer_id):
    if not customer_id:
        return []
    rows = get_db().execute(
        """SELECT * FROM legacy_customer_balances
        WHERE customer_id = ?
        ORDER BY COALESCE(NULLIF(source_started_at, ''), source_created_at) ASC, id ASC""",
        (customer_id,),
    ).fetchall()
    return [_decorate(row) for row in rows]


def legacy_balance_payments(customer_id):
    """Receipts recorded against this customer's legacy balance, newest first."""
    if not customer_id:
        return []
    return get_db().execute(
        """SELECT * FROM legacy_balance_payments
        WHERE customer_id = ? AND status = ?
        ORDER BY COALESCE(NULLIF(payment_date, ''), created_at) DESC, id DESC""",
        (customer_id, PAYMENT_ACTIVE),
    ).fetchall()


def legacy_balance_summary(customer_id):
    entries = legacy_balance_entries(customer_id)
    # Open means still marked active AND still carrying a balance, so a row
    # settled to zero (or closed by hand) never inflates the outstanding total.
    open_entries = [
        entry for entry in entries
        if entry["status"] == ACTIVE_STATUS and entry["outstanding"] > 0
    ]
    return {
        "total": round(sum(entry["outstanding"] for entry in open_entries), 2),
        "imported_total": round(sum(float(entry["amount_due"] or 0) for entry in entries), 2),
        "settled_total": round(sum(float(entry["settled_amount"] or 0) for entry in entries), 2),
        "active_count": len(open_entries),
        "entries": entries,
        "open_entries": open_entries,
        "payments": legacy_balance_payments(customer_id),
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


def _parse_amount(form):
    try:
        amount = float(form.get("amount", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("Payment amount must be a number") from exc
    if amount <= 0:
        raise ValueError("Payment amount must be greater than zero")
    return round(amount, 2)


def record_legacy_payment(customer_id, form):
    """Record money collected against a customer's imported legacy balance.

    The payment is allocated oldest-first across the customer's still-open
    imported rows. A row that reaches zero is marked collected, which keeps the
    original imported amount on the record while removing it from the
    outstanding total.
    """
    if not customer_id:
        raise ValueError("A customer is required before recording a payment")
    summary = legacy_balance_summary(customer_id)
    outstanding = summary["total"]
    if outstanding <= 0:
        raise ValueError("This customer has no outstanding legacy balance to settle")
    amount = _parse_amount(form)
    if amount > outstanding:
        raise ValueError(f"Payment cannot be more than the outstanding legacy balance R{outstanding:.2f}")
    method = normalise_payment_method(form.get("method"), fallback="cash")
    if method == "customer_credit":
        raise ValueError("Customer credit cannot be used on an imported legacy balance")
    reference = (form.get("reference") or "").strip()
    note = (form.get("note") or "").strip()
    payment_date = parse_payment_date(form.get("payment_date"))

    db = get_db()
    remaining = amount
    for entry in summary["open_entries"]:
        if remaining <= 0:
            break
        applied = round(min(remaining, entry["outstanding"]), 2)
        settled = round(float(entry["settled_amount"] or 0) + applied, 2)
        status = COLLECTED_STATUS if settled >= float(entry["amount_due"] or 0) - 0.005 else ACTIVE_STATUS
        db.execute(
            "UPDATE legacy_customer_balances SET settled_amount = ?, status = ?, updated_at = ? WHERE id = ?",
            (settled, status, now(), entry["id"]),
        )
        remaining = round(remaining - applied, 2)

    db.execute(
        """INSERT INTO legacy_balance_payments
        (customer_id, amount, method, reference, payment_date, note, status, deleted_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, '', ?)""",
        (customer_id, amount, method, reference, payment_date, note, PAYMENT_ACTIVE, now()),
    )
    db.commit()
    return {"amount": amount, "remaining": round(outstanding - amount, 2)}
