"""Ticket ABI-341953292: noncash write-offs of imported legacy debt on a statement.

An approved write-off forgives debt without money changing hands, so the
statement must do two things at once:

  * keep the ORIGINAL imported debt visible (never shrink the source row), and
  * print the write-off as its own credit row, dated by its approval date, with
    the ticket and reason a reader can audit.

The write-off is subtracted from the opening, period and lifetime balances
separately. It is never a payment: ``paid_total`` and ``payments`` keep counting
only money actually collected, and the shared statement summary is unchanged.
"""
import pymupdf
import pytest

from test_ticket_341953085_customer_statement import (
    app, client, login, pdf_source, seed_customer,
)
from app.db import get_db
from app.services.customers import customer_statement
from app.services.legacy_balances import upsert_legacy_balance, record_legacy_payment
from app.services.pdf_documents import (
    STATEMENT_TYPE_X, STATEMENT_DETAIL_COLUMN_X, customer_statement_pdf_bytes,
    _statement_summary_rows,
)

TICKET = "ABI-341953292"


def balance(db, cid, ref, amount, started="2026-08-01"):
    upsert_legacy_balance(cid, "old", ref, amount, source_total=9000,
                          source_paid=8000, source_started_at=started)
    row = db.execute(
        "SELECT id FROM legacy_customer_balances WHERE source_order_number = ?", (ref,)
    ).fetchone()
    assert row is not None
    return row["id"]


def writeoff(db, legacy_balance_id, amount, day="2026-09-10",
             ticket=TICKET, reason="Approved noncash write-off", waive=True):
    """Insert the audited write-off row the schema (sibling ticket) provides.

    A FULL write-off also marks the imported row waived, exactly as the approval
    path does; the original amount_due/settled_amount must stay untouched.
    """
    row = db.execute(
        "SELECT customer_id FROM legacy_customer_balances WHERE id = ?", (legacy_balance_id,)
    ).fetchone()
    db.execute(
        """INSERT INTO legacy_balance_writeoffs
           (legacy_balance_id, customer_id, amount, writeoff_date, reason, ticket_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (legacy_balance_id, row["customer_id"], amount, day, reason, ticket, f"{day}T09:00:00"),
    )
    if waive:
        db.execute(
            "UPDATE legacy_customer_balances SET status = 'waived' WHERE id = ?",
            (legacy_balance_id,),
        )


def spans(data):
    doc = pymupdf.open(stream=data, filetype="pdf")
    found = []
    for page_index, page in enumerate(doc):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    found.append((page_index, span["text"], span["bbox"]))
    return found


def test_full_writeoff_retains_the_debt_and_prints_a_credit(app):
    with app.app_context():
        db = get_db()
        cid = seed_customer(db)
        legacy_id = balance(db, cid, "8355", 1000.0)
        writeoff(db, legacy_id, 1000.0, day="2026-09-10",
                 reason="Client liquidated, debt forgiven")
        db.commit()
        view = customer_statement(cid)

    # The source row keeps its full imported debt; only status says waived.
    assert view["legacy_total"] == 1000.0
    assert view["legacy_balance_count"] == 1
    assert "unpaid remainder waived" not in view["legacy_balances"][0]["detail"]

    # Two rows: the debt, then the explicit noncash credit.
    assert [row["type"] for row in view["activity"]] == ["Legacy balance", "Legacy write-off"]
    debt, credit = view["activity"]
    assert debt["amount_display"] == "R1000.00"
    assert credit["date"] == "2026-09-10"
    assert credit["amount"] == 1000.0
    assert credit["amount_display"] == "-R1000.00"
    assert credit["is_credit"] is True
    assert TICKET in credit["detail"]
    assert "Client liquidated, debt forgiven" in credit["detail"]
    # The running balance closes on zero, and the row leaves zero behind.
    assert credit["balance"] == view["closing_balance"] == 0.0
    assert credit["balance_display"] == "R0.00"

    # No money moved: nothing reaches the cash/payment side of the statement.
    assert view["paid_total"] == 0.0
    assert view["payments"] == [] and view["payment_count"] == 0
    assert view["writeoff_total"] == 1000.0
    assert view["writeoff_total_display"] == "R1000.00"
    assert view["outstanding_balance"] == view["closing_balance"] == 0.0
    # The summary block is untouched by this ticket.
    assert [label for label, _, _ in _statement_summary_rows(view)] == ["Balance outstanding"]


def test_writeoff_after_a_partial_receipt_keeps_cash_and_forgiveness_apart(app):
    with app.app_context():
        db = get_db()
        cid = seed_customer(db)
        legacy_id = balance(db, cid, "WO-PART", 1000.0)
        db.commit()
        record_legacy_payment(cid, {"amount": "400", "payment_date": "2026-08-20",
                                    "reference": "RECEIPT-400"})
        writeoff(db, legacy_id, 600.0, day="2026-09-10")
        db.commit()
        row = db.execute(
            "SELECT status, amount_due, settled_amount FROM legacy_customer_balances WHERE id = ?",
            (legacy_id,),
        ).fetchone()
        view = customer_statement(cid)

    # Full writeoff marks the row waived but preserves the original figures.
    assert row["status"] == "waived"
    assert float(row["amount_due"]) == 1000.0 and float(row["settled_amount"]) == 400.0

    assert view["legacy_total"] == 1000.0
    # Only the real receipt is a payment; the write-off never joins it.
    assert view["paid_total"] == 400.0
    assert [item["amount"] for item in view["payments"]] == [400.0]
    assert view["writeoff_total"] == 600.0
    assert view["closing_balance"] == view["outstanding_balance"] == 0.0
    assert [r["type"] for r in view["activity"]] == [
        "Legacy balance", "Legacy payment", "Legacy write-off",
    ]
    assert [r["balance_display"] for r in view["activity"]] == ["R1000.00", "R600.00", "R0.00"]
    assert view["activity"][-1]["amount_display"] == "-R600.00"


def test_writeoff_is_subtracted_in_opening_and_period_balances(app):
    with app.app_context():
        db = get_db()
        cid = seed_customer(db)
        legacy_id = balance(db, cid, "WO-RANGE", 1000.0, started="2026-08-01")
        db.commit()
        writeoff(db, legacy_id, 1000.0, day="2026-09-10")
        db.commit()

        # The period that contains the write-off: the debt is opening, the
        # write-off is the only row, and the balance closes on zero.
        in_range = customer_statement(cid, "2026-09-01", "2026-09-30")
        assert in_range["opening_balance"] == 1000.0
        assert in_range["legacy_total"] == 0.0 and in_range["legacy_balances"] == []
        assert in_range["writeoff_total"] == 1000.0
        assert [r["type"] for r in in_range["activity"]] == ["Legacy write-off"]
        assert in_range["activity"][0]["balance_display"] == "R0.00"
        assert in_range["closing_balance"] == 0.0

        # A later period starts from the already-written-off opening balance.
        after = customer_statement(cid, "2026-10-01")
        assert after["opening_balance"] == 0.0
        assert after["writeoff_total"] == 0.0 and after["activity"] == []
        assert after["closing_balance"] == 0.0

        # Lifetime view still reconciles, and a write-off after date_to is not
        # pulled forward into an earlier period.
        before = customer_statement(cid, "", "2026-09-09")
        assert before["opening_balance"] == 0.0
        assert before["legacy_total"] == 1000.0 and before["writeoff_total"] == 0.0
        assert before["closing_balance"] == 1000.0

        full = customer_statement(cid)
        assert full["closing_balance"] == full["outstanding_balance"] == 0.0


def test_other_customers_and_plain_waivers_are_untouched(app):
    """A write-off is per customer; the older bare waiver still reads as before."""
    with app.app_context():
        db = get_db()
        cid = seed_customer(db)
        other = seed_customer(db, email="other-writeoff@example.com")
        legacy_id = balance(db, cid, "WO-ONLY", 500.0)
        balance(db, other, "WO-OTHER", 250.0)
        plain = seed_customer(db, email="plain-waiver@example.com")
        plain_id = balance(db, plain, "WO-PLAIN", 300.0)
        db.commit()
        record_legacy_payment(plain, {"amount": "100", "payment_date": "2026-08-20",
                                      "reference": "PLAIN-100"})
        writeoff(db, legacy_id, 500.0)
        db.execute(
            "UPDATE legacy_customer_balances SET status = 'waived' WHERE id = ?",
            (plain_id,),
        )
        db.commit()

        assert customer_statement(cid)["closing_balance"] == 0.0
        assert customer_statement(cid)["writeoff_total"] == 500.0
        assert customer_statement(other)["outstanding_balance"] == 250.0
        assert customer_statement(other)["writeoff_total"] == 0.0
        # A waiver without a write-off record keeps the ABI-341953218 reading.
        waived = customer_statement(plain)
        assert waived["legacy_total"] == 100.0
        assert "unpaid remainder waived" in waived["legacy_balances"][0]["detail"]
        assert waived["closing_balance"] == 0.0


def test_statement_pdf_prints_the_writeoff_row_and_ticket(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        cid = seed_customer(db)
        legacy_id = balance(db, cid, "WO-1", 750.0)
        writeoff(db, legacy_id, 750.0, day="2026-09-10",
                 reason="Approved write-off")
        db.commit()
        view = customer_statement(cid)
        data = customer_statement_pdf_bytes(view)

    found = spans(data)
    type_boxes = [bbox for _, text, bbox in found if text == "Legacy write-off"]
    assert len(type_boxes) == 1
    # The TYPE cell stays inside its column, clear of DETAIL.
    assert type_boxes[0][0] >= STATEMENT_TYPE_X - 0.5
    assert type_boxes[0][2] < STATEMENT_DETAIL_COLUMN_X
    assert [bbox for _, text, bbox in found if text == "-R750.00"]
    source = pdf_source(data)
    assert TICKET in source and "Approved write-off" in source
    assert "Legacy write-off" in source
    # A written-off debt still prints its original amount, not R0.00.
    assert "R750.00" in source
