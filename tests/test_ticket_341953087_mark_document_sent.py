import os
import tempfile

import pytest
from flask import session as flask_session

from app import create_app
from app.db import get_db
from app.services.access import create_additional_user, save_user_modules
from app.services.orders import order_counts


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
        row = get_db().execute(
            "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
        ).fetchone()
    assert row is not None
    return client.post(
        '/login',
        data={'user_id': str(row['id']), 'password': 'admin123'},
        follow_redirects=True,
    )


def login_staff(client, app, name='Orders Staff'):
    with app.app_context():
        row = get_db().execute(
            "SELECT id FROM users WHERE name = ?", (name,)
        ).fetchone()
    assert row is not None
    return client.post(
        '/login',
        data={'user_id': str(row['id']), 'password': 'staff123'},
        follow_redirects=True,
    )


def add_staff(app, name='Orders Staff', branch_id=1, modules=None):
    with app.app_context():
        user_id, error = create_additional_user(name, 'staff123', branch_id=branch_id)
        assert error is None
        if modules is not None:
            ok, result = save_user_modules(user_id, modules)
            assert ok, result
        return user_id


def _seed_order(db, order_number='ORD-SENT-1'):
    db.execute(
        "INSERT INTO customers (name, email, phone, created_at) VALUES (?, ?, '', ?)",
        ('Sent Customer', 'sent-customer@example.test', '2026-10-02T09:00:00'),
    )
    customer_id = db.execute("SELECT id FROM customers WHERE name = 'Sent Customer'").fetchone()['id']
    db.execute(
        """INSERT INTO orders (order_number, customer_id, booking_type, collect_branch_id,
        return_branch_id, status, payment_status, start_at, end_at, subtotal, tax_total,
        total, due_total, notes, created_at)
        VALUES (?, ?, 'return', 1, 1, 'started', 'payment_due', ?, ?, 100, 15, 115, 115, '', ?)""",
        (order_number, customer_id, '2026-10-02T09:00:00', '2026-10-02T17:00:00', '2026-10-02T08:00:00'),
    )
    return db.execute("SELECT id FROM orders WHERE order_number = ?", (order_number,)).fetchone()['id']


def _seed_order_for_branch(db, order_number, branch_id):
    db.execute(
        "INSERT INTO customers (name, email, phone, created_at) VALUES (?, ?, '', ?)",
        (f'Sent Customer {branch_id}', f'sent-customer-{branch_id}@example.test', '2026-10-02T09:00:00'),
    )
    customer_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()['id']
    db.execute(
        """INSERT INTO orders (order_number, customer_id, booking_type, collect_branch_id,
        return_branch_id, status, payment_status, start_at, end_at, subtotal, tax_total,
        total, due_total, notes, created_at)
        VALUES (?, ?, 'return', ?, ?, 'started', 'payment_due', ?, ?, 100, 15, 115, 115, '', ?)""",
        (order_number, customer_id, branch_id, branch_id, '2026-10-02T09:00:00', '2026-10-02T17:00:00', '2026-10-02T08:00:00'),
    )
    return db.execute("SELECT id FROM orders WHERE order_number = ?", (order_number,)).fetchone()['id']


def _seed_document(db, order_id, document_type='invoice', status='draft', email_status='not_sent', sent_at='', sent_to='', email_error='old error'):
    prefix = {
        'quote': 'QUO',
        'invoice': 'INV',
        'contract': 'CON',
        'packing_slip': 'PCK',
    }[document_type]
    db.execute(
        """INSERT INTO documents (order_id, document_type, status, number, pdf_path,
        sent_at, sent_to, email_status, email_error, revision_number, revised_at, created_at)
        VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, 0, '', ?)""",
        (order_id, document_type, status, f'{prefix}-SENT-1', sent_at, sent_to, email_status, email_error, '2026-10-02T10:00:00'),
    )
    return db.execute("SELECT last_insert_rowid() AS id").fetchone()['id']


def _counts(app):
    ctx = app.test_request_context('/orders')
    ctx.push()
    flask_session['user_id'] = 1
    flask_session['user_role'] = 'owner'
    try:
        return order_counts()
    finally:
        ctx.pop()


@pytest.mark.parametrize('document_type', ['quote', 'invoice', 'contract', 'packing_slip'])
def test_every_created_document_detail_can_be_marked_sent(client, app, document_type):
    with app.app_context():
        db = get_db()
        order_id = _seed_order(db, f'ORD-SENT-{document_type}')
        document_id = _seed_document(db, order_id, document_type=document_type)
        db.commit()

    login(client)
    detail = client.get(f'/documents/{document_id}')
    assert detail.status_code == 200
    assert b'Mark as Sent' in detail.data
    assert f'/documents/{document_id}/mark-sent'.encode() in detail.data

    response = client.post(f'/documents/{document_id}/mark-sent', follow_redirects=True)
    assert response.status_code == 200
    assert b'Document marked as sent' in response.data
    assert b'Mark as Sent' not in response.data

    with app.app_context():
        row = get_db().execute(
            'SELECT document_type, status, email_status, sent_at, sent_to, email_error FROM documents WHERE id = ?',
            (document_id,),
        ).fetchone()
    assert row is not None
    assert row['document_type'] == document_type
    assert row['status'] == 'draft'
    assert row['email_status'] == 'sent'
    assert row['sent_at']
    assert row['sent_to'] == 'sent-customer@example.test'
    assert row['email_error'] == ''


def test_marking_an_already_sent_document_refreshes_cleanly_without_changing_document_status(client, app):
    with app.app_context():
        db = get_db()
        order_id = _seed_order(db, 'ORD-ALREADY-SENT')
        document_id = _seed_document(
            db,
            order_id,
            document_type='invoice',
            status='finalized',
            email_status='sent',
            sent_at='2026-10-01T10:00:00',
            sent_to='kept@example.test',
            email_error='',
        )
        db.commit()

    login(client)
    detail = client.get(f'/documents/{document_id}')
    assert detail.status_code == 200
    assert b'Mark as Sent' not in detail.data

    response = client.post(f'/documents/{document_id}/mark-sent', follow_redirects=True)
    assert response.status_code == 200
    with app.app_context():
        row = get_db().execute(
            'SELECT status, email_status, sent_at, sent_to, email_error FROM documents WHERE id = ?',
            (document_id,),
        ).fetchone()
    assert row is not None
    assert row['status'] == 'finalized'
    assert row['email_status'] == 'sent'
    assert row['sent_at']
    assert row['sent_to'] == 'kept@example.test'
    assert row['email_error'] == ''


def test_mark_as_sent_drops_the_unsent_invoice_count_and_order_badge(client, app):
    with app.app_context():
        db = get_db()
        order_id = _seed_order(db, 'ORD-COUNT-SENT')
        document_id = _seed_document(db, order_id, document_type='invoice', status='finalized')
        db.commit()
    assert _counts(app)['unsent_invoices'] == 1

    login(client)
    order_page = client.get(f'/orders/{order_id}')
    assert b'data-document-email-indicator="unsent"' in order_page.data

    response = client.post(f'/documents/{document_id}/mark-sent', follow_redirects=True)
    assert response.status_code == 200
    assert b'Last email status: Sent' in response.data
    assert _counts(app)['unsent_invoices'] == 0

    order_page = client.get(f'/orders/{order_id}')
    assert b'data-document-email-indicator="sent"' in order_page.data
    assert b'data-document-email-indicator="unsent"' not in order_page.data


def test_staff_with_order_access_can_mark_an_openable_document_sent(client, app):
    add_staff(app, branch_id=1, modules=['orders'])
    with app.app_context():
        db = get_db()
        order_id = _seed_order_for_branch(db, 'ORD-STAFF-SENT', branch_id=1)
        document_id = _seed_document(db, order_id, document_type='invoice')
        db.commit()

    login_staff(client, app)
    detail = client.get(f'/documents/{document_id}')
    assert detail.status_code == 200
    assert b'Mark as Sent' in detail.data
    assert f'/documents/{document_id}/mark-sent'.encode() in detail.data

    response = client.post(f'/documents/{document_id}/mark-sent', follow_redirects=True)
    assert response.status_code == 200
    assert b'Document marked as sent' in response.data
    with app.app_context():
        row = get_db().execute(
            'SELECT status, email_status, sent_to, email_error FROM documents WHERE id = ?',
            (document_id,),
        ).fetchone()
    assert row is not None
    assert row['status'] == 'draft'
    assert row['email_status'] == 'sent'
    assert row['sent_to'] == 'sent-customer-1@example.test'
    assert row['email_error'] == ''


def test_staff_without_order_or_branch_access_cannot_open_or_mark_sent(client, app):
    add_staff(app, name='No Orders Staff', branch_id=1, modules=['customers'])
    add_staff(app, name='Other Branch Staff', branch_id=2, modules=['orders'])
    with app.app_context():
        db = get_db()
        order_id = _seed_order_for_branch(db, 'ORD-BLOCKED-SENT', branch_id=1)
        document_id = _seed_document(db, order_id, document_type='invoice')
        db.commit()

    login_staff(client, app, name='No Orders Staff')
    assert client.get(f'/documents/{document_id}').status_code == 403
    assert client.post(f'/documents/{document_id}/mark-sent').status_code == 403

    client.post('/logout')
    login_staff(client, app, name='Other Branch Staff')
    assert client.get(f'/documents/{document_id}').status_code == 404
    assert client.post(f'/documents/{document_id}/mark-sent').status_code == 404

    with app.app_context():
        row = get_db().execute(
            'SELECT email_status, sent_at, sent_to, email_error FROM documents WHERE id = ?',
            (document_id,),
        ).fetchone()
    assert row is not None
    assert row['email_status'] == 'not_sent'
    assert not row['sent_at']
    assert row['sent_to'] == ''
    assert row['email_error'] == 'old error'
