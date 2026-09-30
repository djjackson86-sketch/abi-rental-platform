"""Ticket ABI-341953076: invoices need at least one real payment before finalising.

Invoices start life as unnumbered proformas. The business rule is that the
proforma may only become a numbered/finalised invoice once the linked order has
positive active paid money recorded against it. Partial, full and over payments
all count; archived/deleted/refund-only/zero net rows do not.
"""
import os
import tempfile

import pytest

from app import create_app
from app.db import get_db, now
from app.services.documents import create_document, finalize_document


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    application = create_app({
        'TESTING': True,
        'DATABASE': path,
        'SECRET_KEY': 'test',
        'ADMIN_EMAIL': 'admin@abi.local',
        'ADMIN_PASSWORD': 'admin123',
    })
    yield application
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client):
    with client.application.app_context():
        row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
    assert row is not None
    return client.post('/login', data={'user_id': str(row['id']), 'password': 'admin123'}, follow_redirects=True)


def seed_order(db, number, total=500):
    db.execute(
        """INSERT INTO orders (order_number, booking_type, collect_branch_id, return_branch_id,
        status, payment_status, start_at, end_at, subtotal, tax_total, deposit_total,
        total, due_total, notes, created_at)
        VALUES (?, 'return', 1, 1, 'reserved', 'payment_due', '2026-09-30T09:00:00',
        '2026-10-01T15:00:00', ?, 0, 0, ?, ?, '', ?)""",
        (number, total, total, total, now()),
    )
    row = db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()
    assert row is not None
    return row['id']


def seed_payment(db, order_id, amount, *, status='paid', deleted_at=''):
    db.execute(
        """INSERT INTO payments (order_id, amount, method, reference, status, payment_date,
        deleted_at, created_at) VALUES (?, ?, 'cash', 'ticket-341953076', ?, '2026-09-30T10:00:00', ?, ?)""",
        (order_id, amount, status, deleted_at, now()),
    )


def document_row(db, document_id):
    row = db.execute("SELECT status, number FROM documents WHERE id = ?", (document_id,)).fetchone()
    assert row is not None
    return row


def test_draft_invoice_without_payment_cannot_be_finalised_and_stays_unnumbered(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        order_id = seed_order(db, 'ORD-341953076-NOPAY')
        invoice_id = create_document(order_id, 'invoice')
        db.commit()

    response = client.post(f'/documents/{invoice_id}/finalize', follow_redirects=True)

    assert response.status_code == 200
    assert b'Record at least one payment before finalising an invoice' in response.data
    assert b'Proforma Invoice' in response.data
    with app.app_context():
        row = document_row(get_db(), invoice_id)
        assert row['status'] == 'draft'
        assert row['number'] == ''


@pytest.mark.parametrize('amount,total', [
    (100, 500),  # partial payment is enough
    (500, 500),  # full payment is enough
    (600, 500),  # overpayment is still positive active paid money
])
def test_invoice_with_positive_payment_can_be_finalised(amount, total, app):
    with app.app_context():
        db = get_db()
        order_id = seed_order(db, f'ORD-341953076-PAID-{amount}', total=total)
        invoice_id = create_document(order_id, 'invoice')
        seed_payment(db, order_id, amount)
        db.commit()

        finalize_document(invoice_id)

        row = document_row(db, invoice_id)
        assert row['status'] == 'finalized'
        assert row['number'].startswith('INV-')


def test_quote_finalisation_is_unchanged_without_payment(app):
    with app.app_context():
        db = get_db()
        order_id = seed_order(db, 'ORD-341953076-QUOTE')
        quote_id = create_document(order_id, 'quote')
        before = document_row(db, quote_id)
        assert before['number'].startswith('QUO-')
        db.commit()

        finalize_document(quote_id)

        after = document_row(db, quote_id)
        assert after['status'] == 'finalized'
        assert after['number'] == before['number']


@pytest.mark.parametrize('payments', [
    [{'amount': 0}],
    [{'amount': -50}],
    [{'amount': 100, 'status': 'archived'}],
    [{'amount': 100, 'deleted_at': '2026-09-30T11:00:00'}],
    [{'amount': 100}, {'amount': -100}],
])
def test_archived_deleted_refund_only_or_zero_net_payments_do_not_satisfy_invoice_rule(payments, app):
    with app.app_context():
        db = get_db()
        order_id = seed_order(db, f'ORD-341953076-BLOCK-{abs(hash(str(payments))) % 100000}')
        invoice_id = create_document(order_id, 'invoice')
        for payment in payments:
            seed_payment(db, order_id, payment['amount'], status=payment.get('status', 'paid'), deleted_at=payment.get('deleted_at', ''))
        db.commit()

        with pytest.raises(ValueError, match='Record at least one payment before finalising an invoice'):
            finalize_document(invoice_id)

        row = document_row(db, invoice_id)
        assert row['status'] == 'draft'
        assert row['number'] == ''
