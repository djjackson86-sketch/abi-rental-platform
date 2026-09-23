"""Ticket ABI-341953028 - block a customer and stop orders being raised for them.

Requested edit: a "Block Customer" checkbox on the customer record with a space
for the reason, a "Are you sure you want to block this customer?" Yes/No confirm
before the block sticks, and a "Customer Blocked" pop-up carrying the reason when
someone tries to raise an order for that customer.

The browser confirm is cosmetic, so most of these tests drive the server directly:
a crafted POST for a blocked customer must be refused with the reason, and the
customer form's block state must survive every other way a customer is edited.
"""
import os

from app.services import portal_intake
import re
import sqlite3
import tempfile

import pytest
from werkzeug.datastructures import MultiDict

from app import create_app
from app.db import get_db, now


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
    return client.post('/login', data={'user_id': str(row['id']), 'password': 'admin123'}, follow_redirects=True)


def customer_row(app, customer_id):
    with app.app_context():
        return get_db().execute('SELECT * FROM customers WHERE id = ?', (customer_id,)).fetchone()


def order_row(app, order_id):
    with app.app_context():
        return get_db().execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()


def order_count(app):
    with app.app_context():
        return get_db().execute('SELECT COUNT(*) AS c FROM orders').fetchone()['c']


def create_customer(client, name='Blocking Test Client', email='blocking@example.test'):
    response = client.post('/customers/new', data={
        'customer_type': 'individual',
        'name': name,
        'email': email,
        'phone': '+27110000000',
    }, follow_redirects=True)
    assert b'Customer created' in response.data
    return int(response.request.path.rstrip('/').split('/')[-1])


def save_customer_form(client, app, customer_id, **overrides):
    """POST the customer form the way the template renders it.

    Values are taken from the stored record so only the fields under test change;
    ``blocking_panel`` is added by callers that exercise the Block Customer panel.
    """
    row = customer_row(app, customer_id)
    data = {
        'customer_type': row['customer_type'] or 'individual',
        'name': row['name'],
        'email': row['email'] or '',
        'phone': row['phone'] or '',
        'standard_discount_percent': row['standard_discount_percent'] or 0,
        'blocked_reason': row['blocked_reason'] or '',
    }
    if row['marketing_opt_in']:
        data['marketing_opt_in'] = '1'
    data.update(overrides)
    return client.post(f'/customers/{customer_id}/edit', data=data, follow_redirects=True)


def block_customer(client, app, customer_id, reason='Owes R4 500 from ORD-10100'):
    return save_customer_form(client, app, customer_id, blocking_panel='1', is_blocked='1', blocked_reason=reason)


def order_payload(customer_id, quantity='1'):
    return MultiDict([
        ('customer_id', str(customer_id) if customer_id else ''),
        ('booking_type', 'return'),
        ('collect_branch_id', '1'),
        ('return_branch_id', '1'),
        ('deposit_option', 'security_deposit'),
        ('start_date', '2026-07-01'),
        ('start_time', '09:00'),
        ('end_date', '2026-07-03'),
        ('end_time', '15:00'),
        ('product_id', ''),
        ('custom_name', 'Single axle trailer hire'),
        ('custom_unit_price', '100'),
        ('custom_billing_mode', 'fixed'),
        ('quantity', quantity),
    ])


def create_order(client, customer_id, quantity='1'):
    response = client.post('/orders/new', data=order_payload(customer_id, quantity), follow_redirects=False)
    assert response.status_code == 302, response.status_code
    return int(response.headers['Location'].rstrip('/').split('/')[-1])


def banner_html(html):
    match = re.search(r'<div class="blocked-customer-banner".*?</div>', html, re.S)
    return match.group(0) if match else ''


# --------------------------------------------------------------------------- DB

def test_the_block_columns_exist_and_every_existing_customer_stays_unblocked(client, app):
    with app.app_context():
        columns = {row['name']: row for row in get_db().execute('PRAGMA table_info(customers)').fetchall()}
    assert 'is_blocked' in columns and 'blocked_reason' in columns
    assert str(columns['is_blocked']['dflt_value']).strip() in ('0', "'0'")
    assert str(columns['blocked_reason']['dflt_value']).strip() in ("''", '""')

    login(client)
    customer_id = create_customer(client)
    row = customer_row(app, customer_id)
    assert row['is_blocked'] == 0
    assert (row['blocked_reason'] or '') == ''


def test_the_migration_adds_the_columns_to_a_customers_table_that_lacks_them(client, app):
    """The additive migration path, not just the fresh CREATE TABLE path."""
    if tuple(int(part) for part in sqlite3.sqlite_version.split('.')) < (3, 35):
        pytest.skip('ALTER TABLE ... DROP COLUMN needs SQLite 3.35+')
    login(client)
    customer_id = create_customer(client, name='Pre-existing Client', email='pre@example.test')

    with app.app_context():
        db = get_db()
        db.execute('ALTER TABLE customers DROP COLUMN is_blocked')
        db.execute('ALTER TABLE customers DROP COLUMN blocked_reason')
        db.commit()
        columns = {row['name'] for row in db.execute('PRAGMA table_info(customers)').fetchall()}
        assert 'is_blocked' not in columns and 'blocked_reason' not in columns

        from app.db import run_migrations
        run_migrations(db)
        db.commit()

    row = customer_row(app, customer_id)
    assert row['is_blocked'] == 0
    assert (row['blocked_reason'] or '') == ''


# ----------------------------------------------------------------- customer form

def test_blocking_a_customer_stores_the_flag_and_reason_and_shows_it(client, app):
    login(client)
    customer_id = create_customer(client)
    blocked = block_customer(client, app, customer_id, reason='Owes money on two orders')
    assert b'Customer saved' in blocked.data

    row = customer_row(app, customer_id)
    assert row['is_blocked'] == 1
    assert row['blocked_reason'] == 'Owes money on two orders'

    detail = client.get(f'/customers/{customer_id}').get_data(as_text=True)
    assert 'badge blocked' in detail
    assert 'Blocked reason' in detail
    assert 'Owes money on two orders' in detail

    listing = client.get('/customers').get_data(as_text=True)
    assert 'badge blocked' in listing


def test_unblocking_clears_the_flag_and_keeps_the_reason_on_record(client, app):
    login(client)
    customer_id = create_customer(client)
    block_customer(client, app, customer_id, reason='Payment dispute')

    unblocked = save_customer_form(client, app, customer_id, blocking_panel='1', blocked_reason='Payment dispute')
    assert b'Customer saved' in unblocked.data

    row = customer_row(app, customer_id)
    assert row['is_blocked'] == 0
    assert row['blocked_reason'] == 'Payment dispute'

    detail = client.get(f'/customers/{customer_id}').get_data(as_text=True)
    assert 'badge blocked' not in detail


def test_the_customer_form_carries_the_blocking_panel_and_the_confirm_dialog(client, app):
    login(client)
    customer_id = create_customer(client)
    page = client.get(f'/customers/{customer_id}/edit').get_data(as_text=True)
    assert 'name="blocking_panel" value="1"' in page
    assert 'name="is_blocked"' in page
    assert 'Block Customer' in page
    assert 'name="blocked_reason"' in page
    assert "Are you sure you want to block this customer?" in page

    # Once the box is ticked the reason field is no longer rendered hidden.
    block_customer(client, app, customer_id, reason='Reason must show')
    page = client.get(f'/customers/{customer_id}/edit').get_data(as_text=True)
    reason_field = re.search(r'<div class="form-grid" id="blocked-reason-field"[^>]*>', page)
    assert reason_field and 'hidden' not in reason_field.group(0)
    assert 'value="Reason must show"' in page


# -------------------------------------------------------------- order enforcement

def test_a_blocked_customer_cannot_have_a_new_order_and_the_reason_is_flashed(client, app):
    login(client)
    customer_id = create_customer(client)
    block_customer(client, app, customer_id, reason='Account in dispute')

    response = client.post('/orders/new', data=order_payload(customer_id), follow_redirects=True)
    assert response.status_code == 200
    assert 'Customer blocked — Account in dispute' in response.get_data(as_text=True)
    assert order_count(app) == 0


def test_a_block_without_a_reason_still_refuses_the_order(client, app):
    login(client)
    customer_id = create_customer(client)
    save_customer_form(client, app, customer_id, blocking_panel='1', is_blocked='1', blocked_reason='')

    response = client.post('/orders/new', data=order_payload(customer_id), follow_redirects=True)
    assert 'Customer blocked — no reason recorded' in response.get_data(as_text=True)
    assert order_count(app) == 0


def test_an_unblocked_customer_can_still_take_a_new_order(client, app):
    login(client)
    customer_id = create_customer(client)
    order_id = create_order(client, customer_id)
    row = order_row(app, order_id)
    assert row['customer_id'] == customer_id

    # ... and the same customer creates a second order after being unblocked.
    block_customer(client, app, customer_id, reason='Temporary hold')
    assert order_count(app) == 1
    save_customer_form(client, app, customer_id, blocking_panel='1', blocked_reason='Temporary hold')
    create_order(client, customer_id)
    assert order_count(app) == 2


def test_the_order_form_names_the_blocked_customer_and_the_reason(client, app):
    login(client)
    blocked_id = create_customer(client, name='Blocked Client', email='blocked@example.test')
    open_id = create_customer(client, name='Open Client', email='open@example.test')
    block_customer(client, app, blocked_id, reason='Non-payment since June')

    page = client.get(f'/orders/new?customer_id={blocked_id}').get_data(as_text=True)
    banner = banner_html(page)
    assert 'Customer Blocked' in banner
    assert 'Non-payment since June' in banner
    assert 'hidden' not in banner
    # The picker's own payload carries the block state for the JS refresh.
    assert '"is_blocked": true' in page

    page = client.get(f'/orders/new?customer_id={open_id}').get_data(as_text=True)
    banner = banner_html(page)
    assert 'Customer Blocked' in banner
    assert 'hidden' in banner


def test_pointing_an_existing_order_at_a_blocked_customer_is_refused(client, app):
    login(client)
    open_id = create_customer(client, name='Open Client', email='open@example.test')
    blocked_id = create_customer(client, name='Blocked Client', email='blocked@example.test')
    order_id = create_order(client, open_id)
    block_customer(client, app, blocked_id, reason='Blocked for new business')

    payload = order_payload(blocked_id)
    response = client.post(f'/orders/{order_id}/edit', data=payload, follow_redirects=True)
    assert 'Customer blocked — Blocked for new business' in response.get_data(as_text=True)
    assert order_row(app, order_id)['customer_id'] == open_id


def test_an_order_already_attached_to_a_blocked_customer_still_saves(client, app):
    """Blocking a customer must not brick the order it is already on."""
    login(client)
    customer_id = create_customer(client)
    order_id = create_order(client, customer_id)
    block_customer(client, app, customer_id, reason='Blocked after this order')

    response = client.post(f'/orders/{order_id}/edit', data=order_payload(customer_id, quantity='2'), follow_redirects=True)
    assert 'Order saved' in response.get_data(as_text=True)

    with app.app_context():
        item = get_db().execute('SELECT quantity FROM order_items WHERE order_id = ?', (order_id,)).fetchone()
    assert item['quantity'] == 2
    assert customer_row(app, customer_id)['is_blocked'] == 1


def test_editing_a_customer_without_the_blocking_panel_never_clears_the_block(client, app):
    """The order form's attached customer card posts through the shared _clean().

    Only the customer form's own blocking panel may change the block, so the same
    service call without the panel must leave is_blocked and its reason alone while
    still saving the fields it does own.
    """
    from app.services.customers import update_customer

    login(client)
    customer_id = create_customer(client)
    block_customer(client, app, customer_id, reason='Do not unblock by accident')

    with app.app_context():
        order_form_style = MultiDict([
            ('customer_type', 'individual'),
            ('name', 'Blocking Test Client'),
            ('email', 'blocking@example.test'),
            ('phone', '+27119999999'),
            ('standard_discount_percent', '0'),
        ])
        update_customer(customer_id, order_form_style)

    row = customer_row(app, customer_id)
    assert row['is_blocked'] == 1
    assert row['blocked_reason'] == 'Do not unblock by accident'
    assert row['phone'] == '+27119999999'
    # Nothing about the blocking panel leaked into the shared payload.
    with app.app_context():
        from app.services.customers import _clean
        assert 'is_blocked' not in _clean(order_form_style)


def test_the_order_form_customer_card_cannot_unblock_a_blocked_customer(client, app):
    login(client)
    customer_id = create_customer(client)
    block_customer(client, app, customer_id, reason='Blocked for new business')

    payload = order_payload(customer_id)
    payload['customer_edit_active'] = '1'
    payload['phone'] = '+27119999999'
    response = client.post('/orders/new', data=payload, follow_redirects=True)
    assert 'Customer blocked — Blocked for new business' in response.get_data(as_text=True)

    row = customer_row(app, customer_id)
    assert row['is_blocked'] == 1
    assert row['blocked_reason'] == 'Blocked for new business'
    assert order_count(app) == 0


def test_the_public_storefront_still_takes_booking_requests(client, app, monkeypatch):
    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO products (name, product_type, active, public_visible, price_amount, price_unit, quantity, created_at) VALUES (?, 'rental', 1, 1, 200, 'day', 3, ?)",
            ('Public Booking Trailer', now()),
        )
        db.commit()
        product_id = db.execute("SELECT id FROM products ORDER BY id DESC LIMIT 1").fetchone()['id']

    monkeypatch.setattr(portal_intake, "registration_is_open", lambda: True)
    response =     client.post('/store/book', data={
        'name': 'Walk In Client',
        'email': 'walkin@example.test',
        'phone': '+27000000003',
        'popia_consent': '1',
        'start_date': '2026-07-01',
        'start_time': '09:00',
        'end_date': '2026-07-03',
        'end_time': '15:00',
        'product_id': [str(product_id)],
        'quantity': ['1'],
    }, follow_redirects=False)
    assert response.status_code == 302, response.status_code
    assert response.headers['Location'].endswith('/store/booking/1') or '/store/booking/' in response.headers['Location']
    assert order_count(app) == 1
    with app.app_context():
        row = get_db().execute('SELECT is_blocked FROM customers ORDER BY id DESC LIMIT 1').fetchone()
    assert row['is_blocked'] == 0
