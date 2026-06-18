import os
import tempfile

import pytest

from app import create_app


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
    return client.post('/login', data={'email': 'admin@abi.local', 'password': 'admin123'}, follow_redirects=True)


def test_health(client):
    res = client.get('/health')
    assert res.status_code == 200
    assert res.get_json()['ok'] is True


def test_login_and_setup(client):
    res = login(client)
    assert res.status_code == 200
    assert b'Welcome, follow these steps' in res.data
    assert b'Create a tax profile' in res.data


def test_protected_pages_redirect(client):
    res = client.get('/dashboard')
    assert res.status_code == 302
    assert '/login' in res.headers['Location']


def test_settings_persist(client, app):
    login(client)
    res = client.post('/settings/general', data={
        'company_name': 'ABI Solutions Test',
        'email': 'hello@example.com',
        'phone': '123',
        'website': 'https://example.com',
        'country': 'South Africa',
        'city': 'Cape Town',
        'province': 'Western Cape',
        'postcode': '8000',
        'address_line1': '1 Road',
        'address_line2': '',
        'timezone': 'Africa/Johannesburg',
        'date_format': 'dd-mm-yyyy',
        'units': 'metric',
        'first_day_of_week': 'Sunday',
        'currency': 'ZAR',
        'currency_symbol': 'R',
        'currency_position': 'before',
        'tax_mode': 'exclusive',
        'default_pickup_time': '09:00',
        'default_return_time': '15:00',
        'time_increment_minutes': '60',
        'deposit_mode': 'product_specific',
        'deposit_value': '0',
        'pricing_enabled': '1',
        'enable_time_selection': '1',
    }, follow_redirects=True)
    assert res.status_code == 200
    assert b'ABI Solutions Test' in res.data


def test_tax_profile_creation(client):
    login(client)
    res = client.post('/settings/taxes', data={'name': 'VAT', 'rate': '15', 'is_default': 'on'}, follow_redirects=True)
    assert res.status_code == 200
    assert b'VAT' in res.data
    assert b'15.0%' in res.data


def test_store_empty_state(client):
    res = client.get('/store')
    assert res.status_code == 200
    assert b'No products' in res.data
    assert b'Select a rental period' in res.data


def test_inventory_product_crud_and_public_store(client):
    login(client)
    res = client.get('/inventory')
    assert res.status_code == 200
    assert b'Add your first product' in res.data

    product_data = {
        'name': 'Box Trailer',
        'sku': 'TRL-BOX-001',
        'quantity': '3',
        'description': 'Reliable enclosed rental trailer.',
        'product_type': 'rental',
        'price_amount': '450',
        'price_unit': 'day',
        'security_deposit': '1000',
        'tax_profile_id': '1',
        'active': '1',
        'public_visible': '1',
    }
    res = client.post('/inventory/new', data=product_data, follow_redirects=True)
    assert res.status_code == 200
    assert b'Product created' in res.data
    assert b'Box Trailer' in res.data

    res = client.get('/inventory')
    assert b'Box Trailer' in res.data
    assert b'TRL-BOX-001' in res.data
    assert b'Visible' in res.data

    res = client.get('/store')
    assert b'Box Trailer' in res.data
    assert b'R450.00 / day' in res.data


def test_archived_product_hidden_from_store(client):
    login(client)
    res = client.post('/inventory/new', data={
        'name': 'Hidden Trailer',
        'sku': 'HIDE-001',
        'quantity': '1',
        'description': 'Should disappear from store.',
        'product_type': 'rental',
        'price_amount': '100',
        'price_unit': 'day',
        'security_deposit': '0',
        'tax_profile_id': '1',
        'active': '1',
        'public_visible': '1',
    }, follow_redirects=False)
    product_id = res.headers['Location'].rstrip('/').split('/')[-2]
    client.post(f'/inventory/{product_id}/archive', follow_redirects=True)
    store = client.get('/store')
    assert b'Hidden Trailer' not in store.data


def test_customer_crud_search_and_detail(client):
    login(client)
    res = client.get('/customers')
    assert res.status_code == 200
    assert b'Add your first customer' in res.data

    res = client.post('/customers/new', data={
        'customer_type': 'company',
        'name': 'Acme Rentals',
        'email': 'bookings@acme.test',
        'phone': '+27123456789',
        'marketing_opt_in': '1',
    }, follow_redirects=True)
    assert res.status_code == 200
    assert b'Customer created' in res.data
    assert b'Acme Rentals' in res.data
    assert b'Subscribed' in res.data

    res = client.get('/customers?query=acme&customer_type=company&marketing=subscribed')
    assert b'Acme Rentals' in res.data
    assert b'bookings@acme.test' in res.data
    assert b'Company' in res.data

    detail_url = '/customers/1/edit'
    res = client.post(detail_url, data={
        'customer_type': 'individual',
        'name': 'Don Customer',
        'email': 'don@example.com',
        'phone': '+27999999999',
    }, follow_redirects=True)
    assert b'Customer saved' in res.data
    assert b'Don Customer' in res.data
    assert b'Not subscribed' in res.data


def test_setup_marks_customer_complete(client):
    login(client)
    before = client.get('/setup')
    assert b'Create an order' in before.data
    client.post('/customers/new', data={'customer_type': 'individual', 'name': 'Setup Customer', 'email': '', 'phone': ''}, follow_redirects=True)
    after = client.get('/setup')
    assert b'Setup Customer' not in after.data
    # Setup completion count should now include seeded tax profile and this customer.
    assert b'2/14 completed' in after.data


def seed_customer_and_product(client):
    client.post('/customers/new', data={
        'customer_type': 'individual',
        'name': 'Order Customer',
        'email': 'order@example.com',
        'phone': '+27000000000',
    }, follow_redirects=True)
    client.post('/inventory/new', data={
        'name': 'Order Trailer',
        'sku': 'ORD-TRL',
        'quantity': '4',
        'description': 'Order test trailer.',
        'product_type': 'rental',
        'price_amount': '200',
        'price_unit': 'day',
        'security_deposit': '750',
        'tax_profile_id': '1',
        'active': '1',
        'public_visible': '1',
    }, follow_redirects=True)


def test_order_draft_creation_and_totals(client):
    login(client)
    seed_customer_and_product(client)

    res = client.get('/orders/new')
    assert res.status_code == 200
    assert b'Order Customer' in res.data
    assert b'Order Trailer' in res.data

    res = client.post('/orders/new', data={
        'customer_id': '1',
        'product_id': '1',
        'quantity': '2',
        'start_date': '2026-07-01',
        'start_time': '09:00',
        'end_date': '2026-07-03',
        'end_time': '15:00',
        'notes': 'Draft order test',
    }, follow_redirects=True)
    assert res.status_code == 200
    assert b'Draft order created' in res.data
    assert b'ORD-00001' in res.data
    assert b'Order Customer' in res.data
    assert b'Order Trailer' in res.data
    # 2 qty * R200 * 3 rounded rental days, no tax profile VAT in seed.
    assert b'R1200.00' in res.data
    assert b'R1500.00' in res.data  # security deposit 2 * R750

    list_res = client.get('/orders')
    assert b'ORD-00001' in list_res.data
    assert b'Order Customer' in list_res.data
    assert b'built-in method items' not in list_res.data


def test_order_requires_customer_and_product(client):
    login(client)
    res = client.post('/orders/new', data={
        'customer_id': '',
        'product_id': '',
        'start_date': '2026-07-01',
        'end_date': '2026-07-02',
    }, follow_redirects=True)
    assert b'Customer is required' in res.data
