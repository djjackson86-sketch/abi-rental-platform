"""Ticket ABI-341953265: the running BALANCE column and negative payment values.

The customer statement printed only the movement per row, so a reader had to add
the column up by hand. This ticket adds a BALANCE column after AMOUNT (the balance
left behind by that row) and prints a payment as a credit (-R300.00). The summary
block keeps its positive "Payments" total, and the last row must close on the
statement's own closing balance — that is the arithmetic these tests pin.
"""
import pymupdf
import pytest

from test_ticket_341953085_customer_statement import (
    app, client, login, pdf_source, seed_customer, seed_invoice, seed_order,
    seed_payment, seed_ticket_ledger,
)
from app.db import get_db, now
from app.services.account_statements import account_statement
from app.services.customers import customer_statement
from app.services.legacy_balances import record_legacy_payment, upsert_legacy_balance
from app.services.pdf_documents import (
    STATEMENT_AMOUNT_RIGHT, STATEMENT_DETAIL_COLUMN_X, STATEMENT_DETAIL_COLUMN_WIDTH,
    STATEMENT_RIGHT, _pdf_exact_text_width, customer_statement_pdf_bytes,
)


def spans(data):
    """Every text span on every page, as (page_index, text, bbox)."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    found = []
    for page_index, page in enumerate(doc):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    found.append((page_index, span["text"], span["bbox"]))
    return found


def by_text(found, text):
    return [bbox for _, value, bbox in found if value == text]


# --- the service: a running balance on every row --------------------------------

def test_invoice_payment_legacy_sequence_carries_the_running_balance(app):
    with app.app_context():
        db = get_db()
        cid = seed_customer(db)
        order = seed_order(db, cid, "ORD-BAL-1", 1000.0)
        seed_invoice(db, order, "INV-BAL-1", created_at="2026-08-01T09:00:00")
        seed_payment(db, order, 300.0, "2026-08-02T10:00:00", method="eft", reference="EFT-300")
        upsert_legacy_balance(cid, "old", "OLD-BAL-1", 500.0, source_total=9000,
                              source_paid=8000, source_started_at="2026-08-03")
        db.commit()
        record_legacy_payment(cid, {"amount": "200", "payment_date": "2026-08-04",
                                    "reference": "RECEIPT-200"})
        view = customer_statement(cid)

    rows = view["activity"]
    assert [row["type"] for row in rows] == [
        "Invoice", "Payment", "Legacy balance", "Legacy payment",
    ]
    # 0 -> +1000 -> -300 -> +500 -> -200
    assert [row["balance"] for row in rows] == [1000.0, 700.0, 1200.0, 1000.0]
    assert [row["balance_display"] for row in rows] == [
        "R1000.00", "R700.00", "R1200.00", "R1000.00",
    ]
    # The ticket's example: payments (current and imported) print as a credit.
    assert [row["amount_display"] for row in rows] == [
        "R1000.00", "-R300.00", "R500.00", "-R200.00",
    ]
    # The running balance closes on the statement's own closing figure, and the
    # summary totals keep the money positive (only the row's sign is a credit).
    assert rows[-1]["balance"] == view["closing_balance"] == 1000.0
    assert view["paid_total"] == 500.0 and view["paid_total_display"] == "R500.00"
    assert view["invoiced_total"] == 1000.0 and view["legacy_total"] == 500.0
    # ``payments`` (the list the summary is built from) is untouched.
    assert all(item["amount_display"].startswith("R") for item in view["payments"])


def test_refund_prints_positive_because_it_is_a_credit_back(app):
    with app.app_context():
        db = get_db()
        cid = seed_customer(db)
        order = seed_order(db, cid, "ORD-REF-1", 400.0)
        seed_invoice(db, order, "INV-REF-1", created_at="2026-08-01T09:00:00")
        seed_payment(db, order, -50.0, "2026-08-02T10:00:00", method="eft", reference="REFUND-50")
        db.commit()
        view = customer_statement(cid)

    payment = view["activity"][1]
    assert payment["type"] == "Payment"
    # Stored negative (money out of the account), printed as a positive credit.
    assert payment["amount"] == -50.0
    assert payment["amount_display"] == "R50.00"
    assert [row["balance_display"] for row in view["activity"]] == ["R400.00", "R450.00"]
    assert view["activity"][-1]["balance"] == view["closing_balance"] == 450.0


def test_ranged_statement_starts_the_balance_at_the_opening_figure(app):
    with app.app_context():
        db = get_db()
        cid = seed_customer(db)
        first = seed_order(db, cid, "ORD-RANGE-1", 1000.0)
        second = seed_order(db, cid, "ORD-RANGE-2", 500.0)
        seed_invoice(db, first, "INV-RANGE-1", created_at="2026-07-01T09:00:00")
        seed_invoice(db, second, "INV-RANGE-2", created_at="2026-08-01T09:00:00")
        seed_payment(db, second, 200.0, "2026-08-02T10:00:00", method="eft")
        db.commit()
        view = customer_statement(cid, "2026-08-01", "2026-08-31")

    assert view["opening_balance"] == 1000.0
    # Rows run from the opening balance, not from zero.
    assert [row["balance_display"] for row in view["activity"]] == ["R1500.00", "R1300.00"]
    assert view["activity"][-1]["balance"] == view["closing_balance"] == 1300.0


def seed_credit_customer(db, limit=150.0, total=115.0):
    """A customer drawing on the shared facility, with one drawn-down invoice."""
    cid = seed_customer(db)
    db.execute("UPDATE customers SET credit_allowed = 1, credit_limit = ? WHERE id = ?", (limit, cid))
    order = seed_order(db, cid, "ORD-FAC-1", total)
    return cid, order


def seed_account_payment(db, order_id, amount, day):
    """A historical 'account' allocation.

    New ones are refused by the retired-option trigger (ABI-341953145), so the
    row is inserted the way that ticket's own helper does it: with the trigger
    dropped, because the facility statement still has to print old history.
    """
    db.execute("DROP TRIGGER IF EXISTS account_payment_retired_insert")
    db.execute(
        "INSERT INTO payments (order_id, amount, method, status, payment_date, created_at) "
        "VALUES (?, ?, 'account', 'paid', ?, ?)",
        (order_id, amount, day, now()),
    )


def test_account_statement_rows_carry_balances_and_negative_repayments(app):
    with app.app_context():
        db = get_db()
        cid, order = seed_credit_customer(db)
        seed_account_payment(db, order, 115.0, "2026-10-07T10:00:00")
        seed_payment(db, order, 30.0, "2026-10-08T10:00:00", method="eft")
        db.commit()
        view = account_statement(cid)

    rows = view["activity"]
    assert [row["type"] for row in rows] == ["Account", "Repayment"]
    assert [row["amount_display"] for row in rows] == ["R115.00", "-R30.00"]
    assert [row["balance_display"] for row in rows] == ["R115.00", "R85.00"]
    assert rows[-1]["balance"] == 85.0
    # The facility summary keeps repaying positive: only the row is a credit.
    assert ("Repayments", "R30.00", False) in view["summary_rows"]


# --- the PDF: the column exists, is right-aligned, and nothing overlaps ---------

def test_statement_pdf_has_a_balance_column_after_the_amount(client, app):
    login(client)
    with app.app_context():
        customer_id = seed_ticket_ledger(get_db())
        get_db().commit()
        data = customer_statement_pdf_bytes(customer_statement(customer_id))

    found = spans(data)
    # The band carries five headings, BALANCE last and to the right of AMOUNT.
    amount_header = by_text(found, "AMOUNT")
    balance_header = by_text(found, "BALANCE")
    assert len(amount_header) == 1 and len(balance_header) == 1
    assert abs(amount_header[0][2] - STATEMENT_AMOUNT_RIGHT) < 1
    assert abs(balance_header[0][2] - STATEMENT_RIGHT) < 1
    assert balance_header[0][0] > amount_header[0][2] + 10

    # A payment row prints the negative value in AMOUNT and its balance next to it.
    paid = by_text(found, "-R300.00")
    assert len(paid) == 1
    assert abs(paid[0][2] - STATEMENT_AMOUNT_RIGHT) < 1
    closing_rows = by_text(found, "R275.00")
    assert closing_rows, "the closing balance row must print its balance"
    assert any(bbox[0] >= STATEMENT_AMOUNT_RIGHT for bbox in closing_rows)

    # No two spans on the same line may overlap: the two money columns and the
    # DETAIL text have to stay in their own space at every width.
    for page_index in sorted({page for page, _, _ in found}):
        lines = {}
        for page, text, bbox in found:
            if page == page_index:
                lines.setdefault(round(bbox[1], 1), []).append((bbox[0], bbox[2], text))
        for entries in lines.values():
            entries.sort()
            for left, right in zip(entries, entries[1:]):
                assert left[1] <= right[0] + 0.6, f"overlap: {left} / {right}"

    # DETAIL may be clipped, but only with the visible ellipsis the layout uses.
    detail_limit = STATEMENT_DETAIL_COLUMN_X + STATEMENT_DETAIL_COLUMN_WIDTH
    for _, text, bbox in found:
        if text.startswith("INV-") or text.startswith("Cash"):
            assert bbox[0] < detail_limit
            assert bbox[2] <= amount_header[0][0], f"detail runs into AMOUNT: {text}"


def test_account_statement_pdf_prints_negative_repayment(app):
    with app.app_context():
        db = get_db()
        cid, order = seed_credit_customer(db)
        seed_account_payment(db, order, 115.0, "2026-10-07T10:00:00")
        seed_payment(db, order, 30.0, "2026-10-08T10:00:00", method="eft")
        db.commit()
        view = account_statement(cid)
        source = pdf_source(customer_statement_pdf_bytes(view))
    assert "(BALANCE)" in source
    assert "(-R30.00)" in source and "(R-30.00)" not in source


def test_the_two_money_columns_hold_the_widest_realistic_figures():
    """The layout has room for a seven-figure statement, not just the fixtures."""
    widest_amount = _pdf_exact_text_width("-R1 234 567.89", 8)
    widest_balance = _pdf_exact_text_width("R1 234 567.89", 8)
    assert STATEMENT_DETAIL_COLUMN_X + STATEMENT_DETAIL_COLUMN_WIDTH < STATEMENT_AMOUNT_RIGHT - widest_amount
    assert STATEMENT_AMOUNT_RIGHT < STATEMENT_RIGHT - widest_balance
    # ...and the headings themselves never touch.
    assert STATEMENT_RIGHT - _pdf_exact_text_width("BALANCE", 7.5, True) > STATEMENT_AMOUNT_RIGHT
