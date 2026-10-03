import os
import tempfile

import pytest

from app import create_app
from app.db import get_db
from app.services.orders import missing_pickup_critical_customer_fields


START_DATE = '2026-07-01'
START_TIME = '09:00'
END_DATE = '2026-07-03'
END_TIME = '15:00'


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


def login(client, password='admin123'):
    with client.application.app_context():
        row = get_db().execute(
            "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
        ).fetchone()
        user_id = row['id'] if row else None
    return client.post('/login', data={'user_id': str(user_id), 'password': password},
                       follow_redirects=True)


def seed_product(client):
    client.post('/inventory/new', data={
        'name': 'Critical Pickup Trailer',
        'sku': 'CRIT-PICKUP',
        'quantity': '4',
        'description': 'Pickup warning test trailer.',
        'product_type': 'rental',
        'price_amount': '200',
        'price_unit': 'day',
        'security_deposit': '750',
        'tax_profile_id': '1',
        'active': '1',
        'public_visible': '1',
    }, follow_redirects=True)
    with client.application.app_context():
        return get_db().execute("SELECT id FROM products WHERE sku = 'CRIT-PICKUP'").fetchone()['id']


def create_customer(client, **overrides):
    data = {
        'customer_type': 'individual',
        'name': 'Pickup Customer',
        'email': 'pickup@example.com',
        'phone': '+27123456789',
        'address_line1': '1 Trailer Street',
        'client_verified': '1',
        'vehicle_make': 'Toyota',
        'vehicle_color': 'White',
        'vehicle_reg_no': 'CA123456',
        'alternative_contact_name': 'Backup Person',
        'alternative_contact_number': '+27987654321',
    }
    data.update(overrides)
    client.post('/customers/new', data=data, follow_redirects=True)
    with client.application.app_context():
        return get_db().execute('SELECT id FROM customers ORDER BY id DESC LIMIT 1').fetchone()['id']


def create_draft_order(client, customer_id, product_id):
    res = client.post('/orders/new', data={
        'customer_id': str(customer_id),
        'product_id': str(product_id),
        'quantity': '1',
        'start_date': START_DATE,
        'start_time': START_TIME,
        'end_date': END_DATE,
        'end_time': END_TIME,
    }, follow_redirects=False)
    assert res.status_code == 302
    return int(res.headers['Location'].rstrip('/').split('/')[-1])


def stored_order(app, order_id):
    with app.app_context():
        row = get_db().execute(
            'SELECT status, picked_up_at FROM orders WHERE id = ?',
            (order_id,),
        ).fetchone()
        return dict(row)


def missing_fields_for(app, order_id):
    with app.app_context():
        from app.services.orders import get_order
        return missing_pickup_critical_customer_fields(get_order(order_id))


def test_complete_customer_can_be_picked_up_without_warning_confirmation(client, app):
    login(client)
    product_id = seed_product(client)
    customer_id = create_customer(client)
    order_id = create_draft_order(client, customer_id, product_id)

    assert missing_fields_for(app, order_id) == []
    response = client.post(f'/orders/{order_id}/start', follow_redirects=True)

    assert b'Order started' in response.data
    stored = stored_order(app, order_id)
    assert stored['status'] == 'started'
    assert stored['picked_up_at']


def test_missing_critical_customer_details_require_explicit_pickup_confirmation(client, app):
    login(client)
    product_id = seed_product(client)
    customer_id = create_customer(
        client,
        phone='',
        address_line1='',
        client_verified='0',
        vehicle_make='',
        vehicle_color='',
        vehicle_reg_no='',
        alternative_contact_name='',
        alternative_contact_number='',
    )
    order_id = create_draft_order(client, customer_id, product_id)

    missing = missing_fields_for(app, order_id)
    assert missing == [
        'Customer phone',
        'Client verified',
        'Address line 1',
        'Vehicle Make',
        'Vehicle Colour',
        'Vehicle Reg No',
        'Alternative Contact Name',
        'Alternative Contact Number',
    ]

    blocked = client.post(f'/orders/{order_id}/start', follow_redirects=True)
    assert b'Critical customer details are missing for pickup' in blocked.data
    assert b'Confirm pickup to continue without them' in blocked.data
    assert stored_order(app, order_id)['status'] == 'draft'

    confirmed = client.post(
        f'/orders/{order_id}/start',
        data={'pickup_critical_confirm': '1'},
        follow_redirects=True,
    )
    assert b'Order started' in confirmed.data
    stored = stored_order(app, order_id)
    assert stored['status'] == 'started'
    assert stored['picked_up_at']


def test_pickup_page_renders_mobile_confirmation_hook_for_missing_details(client, app):
    login(client)
    product_id = seed_product(client)
    customer_id = create_customer(client, phone='', client_verified='')
    order_id = create_draft_order(client, customer_id, product_id)

    page = client.get(f'/orders/{order_id}')
    body = page.data.decode('utf-8')

    assert 'data-pickup-critical-missing=' in body
    assert 'confirmCriticalPickup(this)' in body
    assert 'Customer phone' in body
    assert 'Client verified' in body
    assert 'pickup_critical_confirm' in body


def test_existing_no_customer_hard_check_still_runs_before_pickup_warning(client, app):
    login(client)
    product_id = seed_product(client)
    order_id = create_draft_order(client, '', product_id)

    response = client.post(f'/orders/{order_id}/start', follow_redirects=True)

    assert b'Add customer details before reserving or pickup' in response.data
    assert b'Critical customer details are missing for pickup' not in response.data
    assert stored_order(app, order_id)['status'] == 'draft'
