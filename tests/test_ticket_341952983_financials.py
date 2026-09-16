import os
import tempfile

import pytest
from flask import session as flask_session

from app import create_app
from app.db import get_db
from app.services import cash
from app.services.documents import accept_quote
from app.services.orders import order_counts
from app.services.reports import summary_metrics

DAY = "2026-09-16"


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


def login(client):
    with client.application.app_context():
        user_id = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()["id"]
    return client.post('/login', data={'user_id': str(user_id), 'password': 'admin123'}, follow_redirects=True)


def _session_context(app):
    ctx = app.test_request_context('/dashboard')
    ctx.push()
    flask_session['user_id'] = 1
    flask_session['user_role'] = 'owner'
    return ctx


def _insert_order(db, number, status, payment_status, total, due, *, deposit=0, applied=0, branch_id=1):
    db.execute(
        """INSERT INTO orders (order_number, booking_type, collect_branch_id, return_branch_id,
        status, payment_status, start_at, end_at, subtotal, tax_total, deposit_total,
        deposit_applied_amount, total, due_total, notes, created_at)
        VALUES (?, 'return', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, '', ?)""",
        (number, branch_id, branch_id, status, payment_status, f"{DAY}T09:00:00", f"{DAY}T17:00:00", total - deposit, deposit, applied, total, due, f"{DAY}T09:00:00"),
    )
    return db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()["id"]


def test_revenue_excludes_refundable_deposit_but_includes_applied_deposit(app):
    with app.app_context():
        db = get_db()
        _insert_order(db, 'PAID-DEPOSIT-REFUNDABLE', 'returned', 'paid', 950, 0, deposit=750, applied=0)
        _insert_order(db, 'PAID-DEPOSIT-APPLIED', 'returned', 'paid', 950, 0, deposit=750, applied=300)
        db.commit()

        # First order recognizes only the rental/extras (200). Second recognizes
        # rental/extras plus the R300 of deposit retained for settlement costs.
        assert summary_metrics(DAY, DAY)['revenue'] == 700
        assert order_counts()['revenue'] == 700


def test_cash_deposit_refunds_reduce_expected_drawer_cash(app):
    with app.app_context():
        db = get_db()
        order_id = _insert_order(db, 'CASH-REFUND', 'returned', 'paid', 950, 0, deposit=750)
        db.execute(
            """INSERT INTO payments (order_id, amount, method, reference, status, payment_date, deleted_at, created_at)
            VALUES (?, 950, 'cash', '', 'paid', ?, '', ?)""",
            (order_id, DAY, f"{DAY}T10:00:00"),
        )
        db.execute(
            """UPDATE orders SET deposit_refund_amount = 750, deposit_process_method = 'cash',
            deposit_processed_at = ? WHERE id = ?""",
            (f"{DAY}T16:00:00", order_id),
        )
        db.commit()

        ctx = _session_context(app)
        try:
            summary = cash.day_summary(day=DAY, branch_id=1)
        finally:
            ctx.pop()
        assert summary['cash_received'] == 950
        assert summary['deposit_refund_total'] == 750
        assert summary['expected'] == 200


def test_due_metrics_exclude_unaccepted_drafts_and_include_accepted_quotes(app):
    with app.app_context():
        db = get_db()
        draft_id = _insert_order(db, 'DRAFT-QUOTE', 'draft', 'payment_due', 1000, 1000)
        accepted_id = _insert_order(db, 'ACCEPTED-QUOTE', 'draft', 'payment_due', 1200, 1200)
        _insert_order(db, 'PARTIAL-RES', 'reserved', 'partially_paid', 1500, 500)
        _insert_order(db, 'PICKED-UP', 'started', 'payment_due', 800, 800)
        _insert_order(db, 'RETURNED-DUE', 'returned', 'payment_due', 600, 600)
        sales_id = _insert_order(db, 'SALES-DRAFT-QUOTE', 'sales_repairs', 'payment_due', 400, 400)
        for order_id, status in [(draft_id, 'draft'), (accepted_id, 'accepted'), (sales_id, 'draft')]:
            db.execute(
                """INSERT INTO documents (order_id, document_type, status, number, pdf_path,
                revision_of_id, revision_number, revised_at, created_at)
                VALUES (?, 'quote', ?, ?, '', NULL, 0, '', ?)""",
                (order_id, status, f'QUO-{order_id:05d}', f"{DAY}T09:00:00"),
            )
        db.commit()

        assert order_counts()['due'] == 1200 + 500 + 800 + 600
        assert summary_metrics(DAY, DAY)['due'] == 1200 + 500 + 800 + 600


def test_quote_accepted_button_marks_quote_accepted(client, app):
    with app.app_context():
        db = get_db()
        order_id = _insert_order(db, 'QUOTE-BUTTON', 'draft', 'payment_due', 1000, 1000)
        db.execute(
            """INSERT INTO documents (order_id, document_type, status, number, pdf_path,
            revision_of_id, revision_number, revised_at, created_at)
            VALUES (?, 'quote', 'draft', 'QUO-00001', '', NULL, 0, '', ?)""",
            (order_id, f"{DAY}T09:00:00"),
        )
        document_id = db.execute("SELECT id FROM documents WHERE order_id = ?", (order_id,)).fetchone()["id"]
        db.commit()

    login(client)
    detail = client.get(f'/documents/{document_id}').get_data(as_text=True)
    assert 'Quote Accepted' in detail
    response = client.post(f'/documents/{document_id}/accept-quote', follow_redirects=True)
    assert response.status_code == 200
    assert 'Quote accepted' in response.get_data(as_text=True)
    with app.app_context():
        status = get_db().execute("SELECT status FROM documents WHERE id = ?", (document_id,)).fetchone()["status"]
        assert status == 'accepted'
        assert accept_quote(document_id) == document_id
