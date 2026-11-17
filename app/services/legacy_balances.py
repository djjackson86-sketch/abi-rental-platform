"""Legacy balance helpers for imported Sano/Booqable customer balances.

These are customer-level opening balances carried over from the previous system.
They do not recreate old orders, invoices or product lines.

Money collected against them is tracked separately (``settled_amount`` on each
imported row plus a ``legacy_balance_payments`` receipt row), so the imported
figure is never edited and the original import stays auditable. Outstanding is
always ``amount_due - settled_amount - written off``.

Ticket ABI-341953292: debt may also be written off without collecting cash. A
write-off is never a receipt — it lives in ``legacy_balance_writeoffs``, keeps
``amount_due``/``settled_amount`` untouched, and is unique per ticket and
imported row so a replayed approval cannot double-write. A row whose remaining
balance is written off is marked ``waived``; every active-status filter
therefore stops counting it as debt.

One customer payment is allocated oldest-first across that customer's still-open
imported rows, which is why a single receipt can settle several old orders.
"""

from app.services.currency import format_currency

from app.db import get_db, now

from app.services.access import customer_branch_clause, session_branch_scope_ids, session_primary_branch_id


def legacy_payment_totals(day, branch_id=None):
    """Use the ledger's receipt eligibility and recorded-depot/customer fallback.

    Import lazily: payments imports orders/reports/cash at module load time.
    One receipt can settle several balances, but is counted only once here.
    """
    from app.services.payments import legacy_receipt_totals
    return legacy_receipt_totals(branch_id=branch_id, date_from=day, date_to=day)

ACTIVE_STATUS = "active"
COLLECTED_STATUS = "collected"
WAIVED_STATUS = "waived"

PAYMENT_ACTIVE = "active"
PAYMENT_ARCHIVED = "archived"


def _row_value(row, key, default=None):
    """Read one column from a sqlite3.Row, libsql row or plain dict."""
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _outstanding(row):
    """Outstanding on one imported row, never below zero.

    The imported amount is reduced by money collected (``settled_amount``) and
    then by any noncash write-off. A row closed by hand (``waived``) is never
    debt, whatever the arithmetic says.
    """
    if _row_value(row, "status", "") == WAIVED_STATUS:
        return 0.0
    written_off = float(_row_value(row, "writeoff_amount", 0) or 0)
    return round(max(
        float(_row_value(row, "amount_due", 0) or 0)
        - float(_row_value(row, "settled_amount", 0) or 0)
        - written_off,
        0,
    ), 2)


def _row_dict(row):
    """Plain dict copy for sqlite3.Row and libsql rows."""
    try:
        return dict(row)
    except (TypeError, ValueError, KeyError):
        if hasattr(row, "asdict"):
            return row.asdict()
        raise


def _decorate(row, writeoff_amount=None):
    """A plain dict copy of a legacy row with its outstanding amount resolved."""
    entry = _row_dict(row)
    if writeoff_amount is not None:
        entry["writeoff_amount"] = writeoff_amount
    entry["writeoff_amount"] = round(float(entry.get("writeoff_amount") or 0), 2)
    entry["outstanding"] = _outstanding(entry)
    return entry


def can_open_customer_record():
    """Whether this session may open a customer page at all.

    Mirrors the module gate on ``customers.*`` server-side: the dashboard's
    company-wide legacy list is visible to every signed-in account (ticket
    ABI-341953323), but the customer page it links to is not. Callers render a
    plain name instead of a dead link when this is False, and the customer
    route still refuses the crafted URL itself.
    """
    from flask import has_request_context, session as flask_session

    if not has_request_context() or not flask_session.get("user_id"):
        return False
    from app.services.access import user_can_module
    return user_can_module(flask_session, "customers")


def _can_open_order(order):
    """Order links obey the orders module AND the session's existing branch scope.

    ``user_can_access_order`` is the same predicate ``orders.detail`` enforces,
    so a link is only rendered where the route would actually open.
    """
    from flask import has_request_context, session as flask_session

    if not has_request_context() or not flask_session.get("user_id"):
        return False
    from app.services.access import user_can_access_order, user_can_module
    return user_can_module(flask_session, "orders") and user_can_access_order(order)


def _attach_order_references(entries):
    """Point imported rows at the real platform order they correspond to, if any.

    The only honest match is an exact (trimmed) ``orders.order_number`` that
    belongs to the SAME customer. The previous system's number is a source
    reference, not a promise that the order was re-created here: when nothing
    matches, ``order_id`` stays None and the templates keep showing the
    imported source fields instead of inventing an order. A number reused by a
    different client's order is likewise not a match.

    A match is only exposed as a link when this session could actually open that
    order (orders module + branch scope). The route enforces the same rule, so
    hiding the link is convenience, never the boundary.
    """
    entries = list(entries or [])
    numbers = {str(entry.get("source_order_number") or "").strip() for entry in entries}
    numbers.discard("")
    by_number = {}
    if numbers:
        placeholders = ", ".join("?" for _ in numbers)
        rows = get_db().execute(
            f"""SELECT id, order_number, customer_id, collect_branch_id, return_branch_id
            FROM orders WHERE TRIM(order_number) IN ({placeholders})""",
            sorted(numbers),
        ).fetchall()
        by_number = {str(row["order_number"]).strip(): row for row in rows}
    for entry in entries:
        entry["order_id"] = None
        entry["order_link_allowed"] = False
        match = by_number.get(str(entry.get("source_order_number") or "").strip())
        if match is None:
            continue
        if str(match["customer_id"]) != str(entry.get("customer_id")):
            # Same reference text, different client: not this imported row's order.
            continue
        if _can_open_order(match):
            entry["order_id"] = match["id"]
            entry["order_link_allowed"] = True
    return entries


def legacy_balance_writeoffs(customer_id):
    """Noncash write-offs recorded against this customer, oldest first."""
    if not customer_id:
        return []
    return get_db().execute(
        """SELECT * FROM legacy_balance_writeoffs
        WHERE customer_id = ?
        ORDER BY COALESCE(NULLIF(writeoff_date, ''), created_at) ASC, id ASC""",
        (customer_id,),
    ).fetchall()


def legacy_balance_entries(customer_id):
    if not customer_id:
        return []
    rows = get_db().execute(
        """SELECT * FROM legacy_customer_balances
        WHERE customer_id = ?
        ORDER BY COALESCE(NULLIF(source_started_at, ''), source_created_at) ASC, id ASC""",
        (customer_id,),
    ).fetchall()
    writeoffs_by_balance = {}
    for writeoff in legacy_balance_writeoffs(customer_id):
        writeoffs_by_balance.setdefault(writeoff["legacy_balance_id"], []).append(writeoff)
    entries = []
    for row in rows:
        writeoffs = writeoffs_by_balance.get(row["id"], [])
        entry = _decorate(row, sum(float(w["amount"] or 0) for w in writeoffs))
        entry["writeoffs"] = writeoffs
        if writeoffs:
            # The newest write-off supplies the label; the total above is the sum.
            latest = writeoffs[-1]
            entry["writeoff_ticket_id"] = latest["ticket_id"]
            entry["writeoff_reason"] = latest["reason"]
            entry["writeoff_date"] = latest["writeoff_date"]
        entries.append(entry)
    return _attach_order_references(entries)


def company_outstanding_legacy_total():
    """Company-wide, read-only aggregate approved for every dashboard user.

    Intentionally independent of branch filters. Select monetary fields only;
    customer/reference details remain behind the existing scoped row helper.
    Reuse the settlement/write-off calculation, rounding each balance first.
    """
    rows = get_db().execute(
        """SELECT l.amount_due, l.settled_amount, l.status,
                  COALESCE(w.writeoff_amount, 0) AS writeoff_amount
        FROM legacy_customer_balances l
        JOIN customers c ON c.id = l.customer_id
        LEFT JOIN (
            SELECT legacy_balance_id, SUM(amount) AS writeoff_amount
            FROM legacy_balance_writeoffs GROUP BY legacy_balance_id
        ) w ON w.legacy_balance_id = l.id
        WHERE l.status = ?""",
        (ACTIVE_STATUS,),
    ).fetchall()
    return round(sum(_outstanding(row) for row in rows), 2)


def outstanding_legacy_balances(branch_id=None, *, company_wide=False):
    """Read-only dashboard rows, scoped by the owning customer's depot.

    Imported balances have no platform order/collection branch. Unassigned
    customers appear only in the unrestricted company-wide view, matching the
    shared customer scope rather than guessing an order's depot.
    """
    # Only the authenticated dashboard legacy list opts into company-wide rows.
    clause, params = ("", []) if company_wide else customer_branch_clause("c", branch_id=branch_id)
    rows = get_db().execute(
        f"""SELECT l.*, c.name AS customer_name, b.name AS branch_name,
                   COALESCE(w.writeoff_amount, 0) AS writeoff_amount
        FROM legacy_customer_balances l
        JOIN customers c ON c.id = l.customer_id
        LEFT JOIN branches b ON b.id = c.branch_id
        LEFT JOIN (
            SELECT legacy_balance_id, SUM(amount) AS writeoff_amount
            FROM legacy_balance_writeoffs GROUP BY legacy_balance_id
        ) w ON w.legacy_balance_id = l.id
        WHERE l.status = ? AND ROUND(l.amount_due - COALESCE(l.settled_amount, 0)
              - COALESCE(w.writeoff_amount, 0), 2) > 0
        {clause}
        ORDER BY (l.amount_due - COALESCE(l.settled_amount, 0)
                  - COALESCE(w.writeoff_amount, 0)) DESC,
                 COALESCE(NULLIF(l.source_started_at, ''), l.source_created_at), l.id""",
        [ACTIVE_STATUS, *params],
    ).fetchall()
    entries = _attach_order_references([_decorate(row) for row in rows])
    # The dashboard list is company-wide, but its customer link is not a grant:
    # only a session that may use the customers module gets one at all.
    customer_links = can_open_customer_record()
    for entry in entries:
        entry["customer_link_allowed"] = bool(entry.get("customer_id")) and customer_links
    return entries


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
    writeoffs = [writeoff for entry in entries for writeoff in entry["writeoffs"]]
    return {
        "total": round(sum(entry["outstanding"] for entry in open_entries), 2),
        "imported_total": round(sum(float(entry["amount_due"] or 0) for entry in entries), 2),
        "settled_total": round(sum(float(entry["settled_amount"] or 0) for entry in entries), 2),
        # Noncash forgiveness: money never collected, kept separate from settled cash.
        "writeoff_total": round(sum(entry["writeoff_amount"] for entry in entries), 2),
        "writeoff_count": len(writeoffs),
        "writeoffs": writeoffs,
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
          -- A re-import must never reopen debt that was written off (or waived)
          -- by a later decision, and must not overwrite the note that records it.
          note=CASE WHEN legacy_customer_balances.status = 'waived'
                    THEN legacy_customer_balances.note ELSE excluded.note END,
          status=CASE WHEN legacy_customer_balances.status = 'waived'
                      THEN 'waived' ELSE excluded.status END,
          updated_at=excluded.updated_at
        WHERE NOT EXISTS (
            SELECT 1 FROM legacy_balance_writeoffs w
            WHERE w.legacy_balance_id = legacy_customer_balances.id
        )""",
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
    from app.services.payments import normalise_payment_method, parse_payment_date

    if not customer_id:
        raise ValueError("A customer is required before recording a payment")
    summary = legacy_balance_summary(customer_id)
    outstanding = summary["total"]
    if outstanding <= 0:
        raise ValueError("This customer has no outstanding legacy balance to settle")
    amount = _parse_amount(form)
    if amount > outstanding:
        raise ValueError(f'Payment cannot be more than the outstanding legacy balance {format_currency(outstanding)}')
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

    # New receipts retain their collecting depot independently of customer moves.
    # Historical receipts use customer ownership only; unknown stays unassigned.
    scope = session_branch_scope_ids()
    branch_id = session_primary_branch_id()
    if scope is not None and branch_id not in scope:
        branch_id = scope[0] if len(scope) == 1 else None
    if branch_id is None:
        customer = db.execute("SELECT branch_id FROM customers WHERE id = ?", (customer_id,)).fetchone()
        candidate = customer["branch_id"] if customer else None
        if scope is None or candidate in scope:
            branch_id = candidate
    db.execute(
        """INSERT INTO legacy_balance_payments
        (customer_id, amount, method, reference, payment_date, note, status, deleted_at, created_at, branch_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?)""",
        (customer_id, amount, method, reference, payment_date, note, PAYMENT_ACTIVE, now(), branch_id),
    )
    db.commit()
    return {"amount": amount, "remaining": round(outstanding - amount, 2)}


def record_legacy_writeoff(customer_id, expected_rows, ticket_id, reason="", writeoff_date=""):
    """Write off imported legacy debt without collecting any money.

    ``expected_rows`` is ``{legacy_balance_id: expected outstanding}``. Every row
    is validated before anything is written: it must belong to this customer, be
    still open, and still carry exactly the expected outstanding, so a stale
    screen cannot forgive the wrong amount after a payment landed.

    The write-off keeps ``amount_due`` (the import) and ``settled_amount`` (money
    actually received) untouched. It adds one ``legacy_balance_writeoffs`` row per
    imported row and marks that row ``waived``, which removes it from every
    active-status debt filter. No payment, receipt or cash row is created.

    Replaying the same ``ticket_id`` is idempotent: rows already written off under
    that ticket are skipped rather than duplicated. This function never commits —
    the caller owns the transaction.
    """
    from datetime import datetime

    if not customer_id:
        raise ValueError("A customer is required before writing off a legacy balance")
    ticket_id = str(ticket_id or "").strip()
    if not ticket_id:
        raise ValueError("A ticket reference is required for a noncash writeoff")
    if not expected_rows:
        raise ValueError("At least one old order must be selected for the writeoff")
    reason = str(reason or "").strip()
    writeoff_date = str(writeoff_date or "").strip()[:10] or now()[:10]
    try:
        datetime.strptime(writeoff_date, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("Writeoff date must be a date (YYYY-MM-DD)") from exc

    db = get_db()
    entries = {entry["id"]: entry for entry in legacy_balance_entries(customer_id)}
    planned = []
    already_written = []
    # Validate the whole request first, so a bad row cannot leave a half-applied
    # writeoff behind.
    for raw_id, raw_expected in expected_rows.items():
        try:
            balance_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Unknown old order reference: {raw_id!r}") from exc
        entry = entries.get(balance_id)
        if entry is None:
            raise ValueError(f"Old order {balance_id} does not belong to this customer")
        try:
            expected = round(float(raw_expected), 2)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Expected outstanding for old order {balance_id} must be a number") from exc
        if expected <= 0:
            raise ValueError(f"Old order {balance_id} has nothing outstanding to write off")
        existing = db.execute(
            "SELECT id, amount FROM legacy_balance_writeoffs WHERE ticket_id = ? AND legacy_balance_id = ?",
            (ticket_id, balance_id),
        ).fetchone()
        if existing is not None:
            already_written.append({"legacy_balance_id": balance_id, "amount": round(float(existing["amount"] or 0), 2)})
            continue
        if entry["status"] != ACTIVE_STATUS:
            raise ValueError(f"Old order {balance_id} is not open ({entry['status']}); no writeoff is needed")
        if abs(entry["outstanding"] - expected) > 0.005:
            raise ValueError(
                f"Old order {balance_id} changed: expected {format_currency(expected)} outstanding, now {format_currency(entry['outstanding'])}. Refresh and try again."
            )
        planned.append((balance_id, expected))

    if not planned:
        return {
            "ticket_id": ticket_id,
            "amount": 0.0,
            "ticket_total": round(sum(row["amount"] for row in already_written), 2),
            "rows": [],
            "already_written": already_written,
            "already_applied": True,
        }

    stamp = now()
    for balance_id, amount in planned:
        db.execute(
            """INSERT INTO legacy_balance_writeoffs
            (legacy_balance_id, customer_id, amount, writeoff_date, reason, ticket_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (balance_id, customer_id, amount, writeoff_date, reason, ticket_id, stamp),
        )
        db.execute(
            "UPDATE legacy_customer_balances SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
            (WAIVED_STATUS, stamp, balance_id, ACTIVE_STATUS),
        )

    written = round(sum(amount for _, amount in planned), 2)
    return {
        "ticket_id": ticket_id,
        "amount": written,
        "ticket_total": round(written + sum(row["amount"] for row in already_written), 2),
        "rows": [{"legacy_balance_id": balance_id, "amount": amount} for balance_id, amount in planned],
        "already_written": already_written,
        "already_applied": False,
    }
