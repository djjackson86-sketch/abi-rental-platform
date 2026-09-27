"""Ticket ABI-341953059: multi-select order filters, customer paid-order count, Reports link."""
import os
import re
import tempfile

import pytest

from app import create_app
from app.db import get_db
from app.services.orders import list_orders, order_counts


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
    return client.post('/login', data={'user_id': str(row['id']), 'password': 'admin123'}, follow_redirects=True)


def body(response):
    return response.data.decode('utf-8')


def seed_customer(app):
    with app.app_context():
        db = get_db()
        db.execute("""INSERT INTO customers (name, email, phone, created_at)
                   VALUES ('ABI Filter Customer', 'filter@example.test', '+27000000000', '2026-09-20T08:00:00')""")
        row = db.execute("SELECT id FROM customers WHERE email = 'filter@example.test'").fetchone()
        assert row is not None
        customer_id = row['id']
        db.commit()
        return customer_id


def insert_order(db, number, customer_id, *, status='draft', payment_status='payment_due', deposit=0, total=100, due=100):
    db.execute(
        """INSERT INTO orders (order_number, customer_id, booking_type, collect_branch_id, return_branch_id,
        status, payment_status, start_at, end_at, subtotal, tax_total, deposit_total, total, due_total, notes, created_at)
        VALUES (?, ?, 'return', 1, 1, ?, ?, '2026-09-20T09:00:00', '2026-09-21T09:00:00', ?, 0, ?, ?, ?, '', '2026-09-20T08:00:00')""",
        (number, customer_id, status, payment_status, total - deposit, deposit, total, due),
    )
    row = db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()
    assert row is not None
    return row['id']


def seed_filter_orders(app):
    customer_id = seed_customer(app)
    with app.app_context():
        db = get_db()
        ids = {
            'returned_paid': insert_order(db, 'ORD-59001', customer_id, status='returned', payment_status='paid', total=100, due=0),
            'started_paid': insert_order(db, 'ORD-59002', customer_id, status='started', payment_status='paid', total=120, due=0),
            'reserved_due': insert_order(db, 'ORD-59003', customer_id, status='reserved', payment_status='payment_due', total=130, due=130),
            'returned_process_deposit': insert_order(db, 'ORD-59004', customer_id, status='returned', payment_status='payment_due', deposit=200, total=300, due=200),
        }
        db.commit()
    return ids


def test_orders_page_accepts_multiple_status_and_payment_filters(client, app):
    login(client)
    seed_filter_orders(app)

    response = client.get('/orders?status=returned&status=started&payment_status=paid&payment_status=process_deposit')
    assert response.status_code == 200
    html = body(response)
    assert 'ORD-59001' in html
    assert 'ORD-59002' in html
    assert 'ORD-59004' in html
    assert 'ORD-59003' not in html
    assert 'type="checkbox" name="status" value="returned" checked' in html
    assert 'type="checkbox" name="status" value="started" checked' in html
    assert 'type="checkbox" name="payment_status" value="paid" checked' in html
    assert 'type="checkbox" name="payment_status" value="process_deposit" checked' in html
    assert 'type="radio" name="status"' not in html
    assert 'type="radio" name="payment_status"' not in html


def test_order_services_treat_multi_filters_as_union_sets(app):
    seed_filter_orders(app)
    with app.app_context():
        rows = list_orders(status=['returned', 'started'], payment_status=['paid', 'process_deposit'])
        numbers = {row['order_number'] for row in rows}
        assert numbers == {'ORD-59001', 'ORD-59002', 'ORD-59004'}
        counts = order_counts(status=['returned', 'started'], payment_status=['paid', 'process_deposit'])
        assert counts['total'] == 3


def test_new_order_customer_summary_shows_paid_orders_to_date(client, app):
    login(client)
    customer_id = seed_customer(app)
    with app.app_context():
        db = get_db()
        insert_order(db, 'ORD-59010', customer_id, status='returned', payment_status='paid', total=100, due=0)
        insert_order(db, 'ORD-59011', customer_id, status='returned', payment_status='overpaid', total=100, due=-5)
        insert_order(db, 'ORD-59012', customer_id, status='returned', payment_status='payment_due', total=100, due=100)
        insert_order(db, 'ORD-59013', customer_id, status='canceled', payment_status='paid', total=100, due=0)
        insert_order(db, 'ORD-59014', customer_id, status='archived', payment_status='paid', total=100, due=0)
        db.commit()

    response = client.get(f'/orders/new?customer_id={customer_id}')
    assert response.status_code == 200
    html = body(response)
    assert 'Orders to Date' in html
    assert 'id="orders-to-date-value">2</strong>' in html
    assert re.search(r'data-summary=\'.*"orders_to_date":\s*2', html)


def test_reports_link_is_available_next_to_settings_in_mobile_nav(client):
    login(client)
    response = client.get('/dashboard')
    assert response.status_code == 200
    html = body(response)
    match = re.search(r'<nav class="mobile-nav">(.*?)</nav>', html, re.S)
    assert match is not None
    mobile_nav = match.group(1)
    assert 'href="/reports"' in mobile_nav
    assert mobile_nav.index('href="/reports"') < mobile_nav.index('href="/settings/general"')
