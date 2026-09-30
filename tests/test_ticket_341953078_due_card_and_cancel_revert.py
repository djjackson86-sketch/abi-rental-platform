"""Ticket ABI-341953078: Due card exclusions and cancelled-order revert.

Requested:
* drafts never add to the Orders Due card, even when proforma/document rows exist;
* Sales/Repairs orders only add to the Due card after the quote is accepted;
* the main profile can revert a cancelled order to draft, without deleting related
  money/document/item rows.
"""
import os
import re
import tempfile

import pytest
from flask import session as flask_session

from app import create_app
from app.db import get_db
from app.services.access import create_additional_user
from app.services.orders import SALES_REPAIRS_STATUS, order_counts

DAY = "2026-09-30"


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    application = create_app({
        "TESTING": True,
        "DATABASE": path,
        "SECRET_KEY": "test",
        "ADMIN_EMAIL": "admin@abi.local",
        "ADMIN_PASSWORD": "admin123",
    })
    yield application
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client, *, user_id=None, password="admin123"):
    if user_id is None:
        with client.application.app_context():
            row = get_db().execute(
                "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
            ).fetchone()
            user_id = row["id"]
    return client.post(
        "/login", data={"user_id": str(user_id), "password": password}, follow_redirects=True
    )


def _counts(app, **filters):
    ctx = app.test_request_context("/orders")
    ctx.push()
    flask_session["user_id"] = 1
    flask_session["user_role"] = "owner"
    try:
        return order_counts(**filters)
    finally:
        ctx.pop()


def _insert_order(db, number, status, due, *, payment_status="payment_due"):
    db.execute(
        """INSERT INTO orders (order_number, booking_type, collect_branch_id, return_branch_id,
        status, payment_status, start_at, end_at, subtotal, tax_total, deposit_total, total,
        due_total, notes, created_at)
        VALUES (?, 'return', 1, 1, ?, ?, ?, ?, ?, 0, 0, ?, ?, '', ?)""",
        (
            number,
            status,
            payment_status,
            f"{DAY}T09:00:00",
            f"{DAY}T17:00:00",
            due,
            due,
            due,
            f"{DAY}T08:00:00",
        ),
    )
    return db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()["id"]


def _document(db, order_id, document_type, status, number):
    db.execute(
        """INSERT INTO documents (order_id, document_type, status, number, pdf_path,
        revision_of_id, revision_number, revised_at, created_at)
        VALUES (?, ?, ?, ?, '', NULL, 0, '', ?)""",
        (order_id, document_type, status, number, f"{DAY}T10:00:00"),
    )


def _payment(db, order_id, amount):
    db.execute(
        """INSERT INTO payments (order_id, amount, method, reference, status, payment_date,
        deleted_at, created_at) VALUES (?, ?, 'cash', 'seed', 'paid', ?, '', ?)""",
        (order_id, amount, DAY, f"{DAY}T11:00:00"),
    )


def _item(db, order_id):
    db.execute(
        """INSERT INTO order_items (order_id, product_id, custom_name, quantity, unit_price,
        line_subtotal, line_tax, line_total, billing_mode)
        VALUES (?, NULL, 'Seed line', 1, 100, 100, 0, 100, 'fixed')""",
        (order_id,),
    )


def _related_counts(db, order_id):
    return {
        "items": db.execute("SELECT COUNT(*) c FROM order_items WHERE order_id = ?", (order_id,)).fetchone()["c"],
        "payments": db.execute("SELECT COUNT(*) c FROM payments WHERE order_id = ?", (order_id,)).fetchone()["c"],
        "documents": db.execute("SELECT COUNT(*) c FROM documents WHERE order_id = ?", (order_id,)).fetchone()["c"],
    }


def test_draft_order_with_documents_never_adds_to_the_due_card(app):
    with app.app_context():
        db = get_db()
        started_id = _insert_order(db, "ORD-341953078-STARTED", "started", 100)
        draft_id = _insert_order(db, "ORD-341953078-DRAFT", "draft", 250)
        _document(db, draft_id, "quote", "accepted", "QUO-DRAFT")
        _document(db, draft_id, "invoice", "draft", "")  # proforma exists
        db.commit()

    assert _counts(app)["due"] == 100
    assert _counts(app, status="draft")["due"] == 0

    with app.app_context():
        # Boundary: only the Orders-card basis changed. The row/report basis still
        # considers the accepted quote collectable until the draft exclusion is applied.
        assert started_id
        due = get_db().execute(
            "SELECT due_total FROM orders WHERE order_number = 'ORD-341953078-DRAFT'"
        ).fetchone()["due_total"]
        assert due == 250


def test_sales_repairs_with_invoice_but_without_accepted_quote_stays_off_due_card(app):
    with app.app_context():
        db = get_db()
        started_id = _insert_order(db, "ORD-341953078-BASE", "started", 100)
        sales_id = _insert_order(
            db,
            "ORD-341953078-SALES-NO-QUOTE",
            SALES_REPAIRS_STATUS,
            400,
            payment_status="partially_paid",
        )
        _document(db, sales_id, "quote", "draft", "QUO-DRAFT")
        _document(db, sales_id, "invoice", "finalized", "INV-SALES")
        db.commit()

    assert started_id
    assert _counts(app)["due"] == 100
    assert _counts(app, status=SALES_REPAIRS_STATUS)["due"] == 0


def test_sales_repairs_with_accepted_or_finalized_quote_adds_to_due_card(app):
    with app.app_context():
        db = get_db()
        accepted_id = _insert_order(db, "ORD-341953078-SALES-ACCEPTED", SALES_REPAIRS_STATUS, 450)
        finalized_id = _insert_order(db, "ORD-341953078-SALES-FINAL", SALES_REPAIRS_STATUS, 550)
        _document(db, accepted_id, "quote", "accepted", "QUO-ACCEPTED")
        _document(db, finalized_id, "quote", "finalized", "QUO-FINAL")
        db.commit()

    assert _counts(app)["due"] == 1000
    assert _counts(app, status=SALES_REPAIRS_STATUS)["due"] == 1000


def test_normal_started_and_returned_due_logic_is_unchanged(app):
    with app.app_context():
        db = get_db()
        _insert_order(db, "ORD-341953078-STARTED", "started", 175)
        _insert_order(db, "ORD-341953078-RETURNED", "returned", 225)
        _insert_order(db, "ORD-341953078-RESERVED", "reserved", 325)
        db.commit()

    assert _counts(app)["due"] == 400
    assert _counts(app, status="started")["due"] == 175
    assert _counts(app, status="returned")["due"] == 225
    assert _counts(app, status="reserved")["due"] == 0


def test_main_user_can_revert_canceled_order_to_draft_without_deleting_related_rows(client, app):
    with app.app_context():
        db = get_db()
        order_id = _insert_order(db, "ORD-341953078-CANCEL", "canceled", 300)
        _item(db, order_id)
        _payment(db, order_id, 75)
        _document(db, order_id, "quote", "accepted", "QUO-CANCEL")
        _document(db, order_id, "invoice", "finalized", "INV-CANCEL")
        before = _related_counts(db, order_id)
        db.commit()

    login(client)
    detail = client.get(f"/orders/{order_id}").get_data(as_text=True)
    assert f'action="/orders/{order_id}/revert-draft"' in detail
    assert re.search(r">Revert to Draft</button>", detail)

    response = client.post(f"/orders/{order_id}/revert-draft", follow_redirects=True)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Order reverted to draft" in body

    with app.app_context():
        db = get_db()
        order = db.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
        assert order["status"] == "draft"
        assert _related_counts(db, order_id) == before


def test_non_main_user_cannot_post_canceled_order_revert(client, app):
    with app.app_context():
        db = get_db()
        order_id = _insert_order(db, "ORD-341953078-STAFF-BLOCK", "canceled", 300)
        _item(db, order_id)
        db.commit()
        staff_id, error = create_additional_user("Depot Staff", "staff123", branch_id=1)
        assert error is None

    login(client, user_id=staff_id, password="staff123")
    response = client.post(f"/orders/{order_id}/revert-draft")
    assert response.status_code == 403

    with app.app_context():
        row = get_db().execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
        assert row["status"] == "canceled"
