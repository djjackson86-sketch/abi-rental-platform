import os
import tempfile

import pytest

from app import create_app
from app.db import get_db


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    app = create_app({
        'TESTING': True,
        'DATABASE': path,
        'SECRET_KEY': 'test',
        'ADMIN_EMAIL': 'admin@abi.local',
        'ADMIN_PASSWORD': 'admin123',
    })
    yield app
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
    return client.post('/login', data={'user_id': str(row['id']), 'password': 'admin123'}, follow_redirects=True)


def seed_customer_product_order(client):
    client.post('/customers/new', data={
        'customer_type': 'individual',
        'name': 'Indicator Customer',
        'email': 'indicator@example.com',
        'phone': '+270****3419',
    }, follow_redirects=True)
    client.post('/inventory/new', data={
        'name': 'Indicator Trailer',
        'sku': 'IND-TRL',
        'quantity': '4',
        'description': 'Indicator test trailer.',
        'product_type': 'rental',
        'price_amount': '200',
        'price_unit': 'day',
        'security_deposit': '750',
        'tax_profile_id': '1',
        'active': '1',
        'public_visible': '1',
    }, follow_redirects=True)
    res = client.post('/orders/new', data={
        'customer_id': '1',
        'product_id': '1',
        'quantity': '1',
        'start_date': '2026-07-01',
        'start_time': '09:00',
        'end_date': '2026-07-03',
        'end_time': '15:00',
        'notes': 'Document email indicator order',
    }, follow_redirects=False)
    assert res.status_code == 302
    return res.headers['Location'].rstrip('/').split('/')[-1]


def create_document(client, order_id, document_type):
    res = client.post(f'/orders/{order_id}/documents', data={'document_type': document_type}, follow_redirects=False)
    assert res.status_code == 302
    return res.headers['Location'].rstrip('/').split('/')[-1]


def indicator_count(html, state):
    return html.count(f'data-document-email-indicator="{state}"'.encode())


def test_order_document_rows_show_unsent_until_that_document_generates_email(client, app):
    login(client)
    order_id = seed_customer_product_order(client)
    quote_id = create_document(client, order_id, 'quote')
    invoice_id = create_document(client, order_id, 'invoice')

    initial = client.get(f'/orders/{order_id}')
    assert initial.status_code == 200
    assert indicator_count(initial.data, 'unsent') == 2
    assert indicator_count(initial.data, 'sent') == 0
    assert b'<small>Status</small><b class="document-status-line">Draft <span class="document-email-indicator document-email-indicator-unsent"' in initial.data

    draft = client.post(f'/documents/{quote_id}/send-email', data={'to_email': 'indicator@example.com'}, follow_redirects=False)
    assert draft.status_code == 200
    assert draft.mimetype == 'message/rfc822'

    after_quote = client.get(f'/orders/{order_id}')
    assert after_quote.status_code == 200
    assert indicator_count(after_quote.data, 'sent') == 1
    assert indicator_count(after_quote.data, 'unsent') == 1
    assert b'document-email-indicator-sent' in after_quote.data
    assert b'document-email-indicator-unsent' in after_quote.data

    with app.app_context():
        rows = get_db().execute(
            'SELECT id, email_status FROM documents WHERE id IN (?, ?) ORDER BY id',
            (quote_id, invoice_id),
        ).fetchall()
    statuses = {row['id']: row['email_status'] for row in rows}
    assert statuses[int(quote_id)] == 'prepared'
    assert statuses[int(invoice_id)] == 'not_sent'
