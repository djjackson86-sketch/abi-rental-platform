"""ABI-341953292: noncash write-off of imported legacy debt.

The write-off must forgive debt without creating a receipt, without editing the
original import or money already collected, and without touching any other
customer. Every selected row is validated before the first write, the same
ticket is idempotent, and a re-import can never reopen waived debt.
"""
import pytest

from test_legacy_customer_balances import app, client, login, seed_customer

from app.db import get_db
from app.routes.orders import _customer_summary_by_id, _order_customer_summary
from app.services.legacy_balances import (
    legacy_balance_summary,
    record_legacy_payment,
    record_legacy_writeoff,
    upsert_legacy_balance,
)

DAY = "2026-11-04"
TICKET = "ABI-341953292"


def _balance_id(db, order_number):
    return db.execute(
        "SELECT id FROM legacy_customer_balances WHERE source_order_number = ?",
        (order_number,),
    ).fetchone()["id"]


def _row(db, balance_id):
    return db.execute("SELECT * FROM legacy_customer_balances WHERE id = ?", (balance_id,)).fetchone()


def _seed_two_rows(app, *, payment=0):
    """One customer with two imported orders, optionally part-paid."""
    with app.app_context():
        db = get_db()
        cid = seed_customer(db, f"Writeoff Customer {payment}")
        db.execute("UPDATE customers SET source_id = ? WHERE id = ?", (f"writeoff-customer-{payment}", cid))
        other = seed_customer(db, f"Untouched Customer {payment}")
        upsert_legacy_balance(cid, "old-cust-1", f"WO-A-{payment}", 1000, note="Original import note A")
        upsert_legacy_balance(cid, "old-cust-1", f"WO-B-{payment}", 500, note="Original import note B")
        upsert_legacy_balance(other, "old-cust-1", f"WO-OTHER-{payment}", 250, note="Other customer")
        db.commit()
        if payment:
            # Oldest-first: the first row gets the whole R300.
            record_legacy_payment(cid, {"amount": str(payment), "method": "eft", "payment_date": DAY})
        return {
            "customer_id": cid,
            "other_id": other,
            "first": _balance_id(db, f"WO-A-{payment}"),
            "second": _balance_id(db, f"WO-B-{payment}"),
            "other": _balance_id(db, f"WO-OTHER-{payment}"),
        }


def test_writeoff_after_partial_payment_forgives_debt_without_a_receipt(app, client):
    ids = _seed_two_rows(app, payment=300)
    login(client)
    with app.app_context():
        db = get_db()
        before_first = dict(_row(db, ids["first"]))
        before_second = dict(_row(db, ids["second"]))
        before_payments = [tuple(row) for row in db.execute("SELECT * FROM payments")]
        before_receipts = [tuple(row) for row in db.execute("SELECT * FROM legacy_balance_payments")]
        assert legacy_balance_summary(ids["customer_id"])["total"] == 1200.0
        assert _customer_summary_by_id(ids["customer_id"])["legacy_balance"] == 1200.0

        result = record_legacy_writeoff(
            ids["customer_id"],
            {ids["first"]: 700, ids["second"]: 500},
            TICKET,
            "Approved noncash writeoff of uncollectable old debt",
            DAY,
        )
        db.commit()

        assert result["amount"] == 1200.0 and result["already_applied"] is False
        assert len(result["rows"]) == 2

        writeoffs = db.execute(
            "SELECT * FROM legacy_balance_writeoffs WHERE customer_id = ? ORDER BY amount DESC",
            (ids["customer_id"],),
        ).fetchall()
        assert [row["amount"] for row in writeoffs] == [700.0, 500.0]
        assert {row["ticket_id"] for row in writeoffs} == {TICKET}
        assert {row["writeoff_date"] for row in writeoffs} == {DAY}
        assert all(row["reason"] == "Approved noncash writeoff of uncollectable old debt" for row in writeoffs)

        after_first = _row(db, ids["first"])
        after_second = _row(db, ids["second"])
        # The import and money actually collected are unchanged; only the status
        # moved to waived.
        assert after_first["amount_due"] == before_first["amount_due"] == 1000.0
        assert after_second["amount_due"] == before_second["amount_due"] == 500.0
        assert after_first["settled_amount"] == before_first["settled_amount"] == 300.0
        assert after_second["settled_amount"] == before_second["settled_amount"] == 0.0
        assert after_first["note"] == before_first["note"] == "Original import note A"
        assert after_second["note"] == before_second["note"] == "Original import note B"
        assert after_first["status"] == after_second["status"] == "waived"

        summary = legacy_balance_summary(ids["customer_id"])
        assert summary["total"] == summary["active_count"] == 0
        assert summary["imported_total"] == 1500.0
        assert summary["settled_total"] == 300.0     # money really received stays reported
        assert summary["writeoff_total"] == 1200.0  # separate from cash
        assert summary["writeoff_count"] == 2
        assert [entry["outstanding"] for entry in summary["entries"]] == [0.0, 0.0]
        assert [entry["writeoff_amount"] for entry in summary["entries"]] == [700.0, 500.0]
        assert summary["entries"][0]["writeoff_ticket_id"] == TICKET

        # A write-off is not money: no order payment and no legacy receipt appears.
        assert [tuple(row) for row in db.execute("SELECT * FROM payments")] == before_payments
        assert [tuple(row) for row in db.execute("SELECT * FROM legacy_balance_payments")] == before_receipts
        assert summary["payments"] == legacy_balance_summary(ids["customer_id"])["payments"]

        # Both order-form balance paths stop counting the written-off debt.
        assert _customer_summary_by_id(ids["customer_id"])["legacy_balance"] == 0.0
        picked = _order_customer_summary(ids["customer_id"])
        assert picked["legacy_balance"] == 0.0

        # Another customer's debt is completely untouched.
        other = legacy_balance_summary(ids["other_id"])
        assert other["total"] == 250.0 and other["writeoff_total"] == 0.0
        assert _customer_summary_by_id(ids["other_id"])["legacy_balance"] == 250.0

        # The customer page says this was a noncash write-off, not a payment.
        page = client.get(f"/customers/{ids['customer_id']}")
        assert page.status_code == 200
        html = page.get_data(as_text=True)
        panel = html.split('id="legacy-balance"', 1)[1].split("</article>", 1)[0]
        assert "Noncash writeoff" in panel
        assert "written off — noncash, no money received" in panel
        assert TICKET in panel
        assert "Record payment" not in panel


def test_replaying_the_same_ticket_is_idempotent(app):
    ids = _seed_two_rows(app)
    with app.app_context():
        db = get_db()
        first = record_legacy_writeoff(ids["customer_id"], {ids["first"]: 1000, ids["second"]: 500}, TICKET, "writeoff", DAY)
        db.commit()
        second = record_legacy_writeoff(ids["customer_id"], {ids["first"]: 1000, ids["second"]: 500}, TICKET, "writeoff", DAY)
        db.commit()

        assert first["amount"] == 1500.0 and first["already_applied"] is False
        assert second["amount"] == 0.0 and second["already_applied"] is True
        assert second["ticket_total"] == 1500.0
        count = db.execute(
            "SELECT COUNT(*) AS c FROM legacy_balance_writeoffs WHERE ticket_id = ?", (TICKET,)
        ).fetchone()["c"]
        assert count == 2
        assert legacy_balance_summary(ids["customer_id"])["total"] == 0.0


def test_every_row_is_validated_before_anything_is_written(app):
    ids = _seed_two_rows(app)
    with app.app_context():
        db = get_db()
        with pytest.raises(ValueError):
            # The second amount is stale: the first must not be written either.
            record_legacy_writeoff(ids["customer_id"], {ids["first"]: 1000, ids["second"]: 499}, TICKET, "writeoff", DAY)
        db.commit()
        assert db.execute("SELECT COUNT(*) AS c FROM legacy_balance_writeoffs").fetchone()["c"] == 0
        assert _row(db, ids["first"])["status"] == "active"
        assert _row(db, ids["second"])["status"] == "active"

        with pytest.raises(ValueError):
            # A row belonging to another customer can never be written off here.
            record_legacy_writeoff(ids["customer_id"], {ids["other"]: 250}, TICKET, "writeoff", DAY)
        with pytest.raises(ValueError):
            record_legacy_writeoff(ids["customer_id"], {ids["first"]: 0}, TICKET, "writeoff", DAY)
        with pytest.raises(ValueError):
            record_legacy_writeoff(ids["customer_id"], {}, TICKET, "writeoff", DAY)
        with pytest.raises(ValueError):
            record_legacy_writeoff(ids["customer_id"], {ids["first"]: 1000}, "", "writeoff", DAY)
        with pytest.raises(ValueError):
            record_legacy_writeoff(ids["customer_id"], {ids["first"]: 1000}, TICKET, "writeoff", "not-a-date")
        assert db.execute("SELECT COUNT(*) AS c FROM legacy_balance_writeoffs").fetchone()["c"] == 0


def test_writeoff_does_not_commit_its_own_transaction(app):
    ids = _seed_two_rows(app)
    with app.app_context():
        db = get_db()
        record_legacy_writeoff(ids["customer_id"], {ids["first"]: 1000}, TICKET, "writeoff", DAY)
        db.rollback()  # the caller owns the transaction; nothing may survive this

        assert db.execute("SELECT COUNT(*) AS c FROM legacy_balance_writeoffs").fetchone()["c"] == 0
        assert _row(db, ids["first"])["status"] == "active"
        assert legacy_balance_summary(ids["customer_id"])["total"] == 1500.0


def test_zero_balance_cannot_be_written_off(app):
    with app.app_context():
        db = get_db()
        cid = seed_customer(db, "Already Settled Customer")
        upsert_legacy_balance(cid, "old-cust-1", "WO-PAID", 500)
        db.commit()
        record_legacy_payment(cid, {"amount": "500", "method": "cash", "payment_date": DAY})
        balance_id = _balance_id(db, "WO-PAID")
        assert legacy_balance_summary(cid)["total"] == 0.0
        with pytest.raises(ValueError):
            record_legacy_writeoff(cid, {balance_id: 0}, TICKET, "writeoff", DAY)
        assert db.execute("SELECT COUNT(*) AS c FROM legacy_balance_writeoffs").fetchone()["c"] == 0


def test_reimport_preserves_waived_status_and_note(app):
    ids = _seed_two_rows(app, payment=300)
    with app.app_context():
        db = get_db()
        record_legacy_writeoff(ids["customer_id"], {ids["first"]: 700}, TICKET, "writeoff", DAY)
        db.commit()
        upsert_legacy_balance(
            ids["other_id"], "changed-source", "WO-A-300", 2000,
            source_payment_status="payment_due", note="Re-imported from Sano",
        )
        db.commit()
        row = _row(db, ids["first"])
        assert row["status"] == "waived"
        assert row["note"] == "Original import note A"
        summary = legacy_balance_summary(ids["customer_id"])
        assert summary["total"] == 500.0  # only the untouched second row remains
        assert summary["writeoff_total"] == 700.0
        assert _customer_summary_by_id(ids["customer_id"])["legacy_balance"] == 500.0


def test_a_recorded_writeoff_reduces_a_still_active_row(app):
    """The decorated outstanding subtracts recorded write-offs, not just status."""
    with app.app_context():
        db = get_db()
        cid = seed_customer(db, "Partly Written Off Customer")
        upsert_legacy_balance(cid, "old-cust-1", "WO-PART", 500)
        db.commit()
        balance_id = _balance_id(db, "WO-PART")
        db.execute(
            """INSERT INTO legacy_balance_writeoffs
            (legacy_balance_id, customer_id, amount, writeoff_date, reason, ticket_id, created_at)
            VALUES (?, ?, 100, ?, 'partial goodwill', 'ABI-PARTIAL', ?)""",
            (balance_id, cid, DAY, f"{DAY}T10:00:00"),
        )
        db.commit()
        summary = legacy_balance_summary(cid)
        assert summary["total"] == 400.0
        assert summary["writeoff_total"] == 100.0
        assert summary["entries"][0]["outstanding"] == 400.0
        assert _customer_summary_by_id(cid)["legacy_balance"] == 400.0
