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


def test_production_requires_non_default_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv('FLASK_ENV', 'production')
    monkeypatch.delenv('SECRET_KEY', raising=False)
    monkeypatch.delenv('ADMIN_PASSWORD', raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        create_app({'DATABASE': str(tmp_path / 'prod.db')})
    message = str(excinfo.value)
    assert 'SECRET_KEY must be set' in message
    assert 'ADMIN_PASSWORD must be set' in message


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


def test_online_store_settings_persist_and_drive_public_store(client):
    login(client)
    res = client.post('/online-store', data={
        'store_enabled': '1',
        'show_prices': '1',
        'show_availability': '1',
        'store_title': 'ABI Event Rentals',
        'store_intro': 'Choose equipment, dates and request a booking online.',
        'store_hero_text': 'Reliable trailers and event gear for your next job.',
        'checkout_instructions': 'Submit your request and our team will confirm availability before payment.',
        'store_contact_email': 'bookings@abi.test',
        'store_contact_phone': '+27 11 555 0100',
    }, follow_redirects=True)
    assert res.status_code == 200
    assert b'Online store settings saved' in res.data
    assert b'ABI Event Rentals' in res.data
    assert b'bookings@abi.test' in res.data

    public = client.get('/store')
    assert b'ABI Event Rentals' in public.data
    assert b'Choose equipment, dates and request a booking online.' in public.data
    assert b'Reliable trailers and event gear for your next job.' in public.data
    assert b'bookings@abi.test' in public.data


def test_online_store_can_be_disabled(client):
    login(client)
    res = client.post('/online-store', data={
        'store_title': 'Hidden Store',
        'store_intro': 'Should not be visible while disabled.',
        'store_hero_text': 'Temporarily unavailable.',
        'checkout_instructions': 'Do not show checkout.',
        'store_contact_email': 'closed@abi.test',
        'store_contact_phone': '+27 11 555 0200',
    }, follow_redirects=True)
    assert b'Online store settings saved' in res.data

    public = client.get('/store')
    assert public.status_code == 200
    assert b'Online booking is temporarily unavailable' in public.data
    assert b'Should not be visible while disabled.' not in public.data


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


def create_order_for_status(client, quantity='2', start_date='2026-07-01', end_date='2026-07-03'):
    res = client.post('/orders/new', data={
        'customer_id': '1',
        'product_id': '1',
        'quantity': quantity,
        'start_date': start_date,
        'start_time': '09:00',
        'end_date': end_date,
        'end_time': '15:00',
        'notes': 'Status workflow order',
    }, follow_redirects=False)
    assert res.status_code == 302
    return res.headers['Location'].rstrip('/').split('/')[-1]


def test_order_status_workflow_and_calendar(client):
    login(client)
    seed_customer_and_product(client)
    order_id = create_order_for_status(client)

    detail = client.get(f'/orders/{order_id}')
    assert b'Draft' in detail.data
    assert b'Reserve order' in detail.data

    reserve = client.post(f'/orders/{order_id}/reserve', follow_redirects=True)
    assert b'Order reserved' in reserve.data
    assert b'Reserved' in reserve.data
    assert b'Start order' in reserve.data

    dashboard = client.get('/dashboard')
    assert b'ORD-00001' in dashboard.data
    assert b'Going out' in dashboard.data
    assert b'Coming back' in dashboard.data

    calendar = client.get('/calendar')
    assert calendar.status_code == 200
    assert b'Reservation calendar' in calendar.data
    assert b'ORD-00001' in calendar.data
    assert b'Order Trailer' in calendar.data

    start = client.post(f'/orders/{order_id}/start', follow_redirects=True)
    assert b'Order started' in start.data
    assert b'Started' in start.data
    assert b'Return order' in start.data

    returned = client.post(f'/orders/{order_id}/return', follow_redirects=True)
    assert b'Order returned' in returned.data
    assert b'Returned' in returned.data
    assert b'Archive order' in returned.data


def test_reserve_prevents_overbooking(client):
    login(client)
    seed_customer_and_product(client)
    first_id = create_order_for_status(client, quantity='3')
    second_id = create_order_for_status(client, quantity='2')

    first = client.post(f'/orders/{first_id}/reserve', follow_redirects=True)
    assert b'Order reserved' in first.data

    second = client.post(f'/orders/{second_id}/reserve', follow_redirects=True)
    assert b'Only 1 available for Order Trailer' in second.data
    assert b'Draft' in second.data


def test_document_generation_list_and_printable_detail(client):
    login(client)
    seed_customer_and_product(client)
    order_id = create_order_for_status(client)

    detail = client.get(f'/orders/{order_id}')
    assert b'New quote' in detail.data
    assert b'New contract' in detail.data
    assert b'New invoice' in detail.data
    assert b'Packing slip' in detail.data

    created = client.post(f'/orders/{order_id}/documents', data={'document_type': 'quote'}, follow_redirects=True)
    assert b'Document created' in created.data
    assert b'Quote' in created.data
    assert b'QUO-00001' in created.data
    assert b'Order Customer' in created.data
    assert b'Order Trailer' in created.data
    assert b'R1200.00' in created.data

    order_detail = client.get(f'/orders/{order_id}')
    assert b'QUO-00001' in order_detail.data
    assert b'Quote' in order_detail.data

    documents = client.get('/documents')
    assert documents.status_code == 200
    assert b'Documents' in documents.data
    assert b'QUO-00001' in documents.data
    assert b'ORD-00001' in documents.data


def test_document_type_validation(client):
    login(client)
    seed_customer_and_product(client)
    order_id = create_order_for_status(client)

    bad = client.post(f'/orders/{order_id}/documents', data={'document_type': 'receipt'}, follow_redirects=True)
    assert b'Unsupported document type' in bad.data
    assert b'Receipt' not in bad.data


def test_public_store_checkout_creates_draft_order(client):
    login(client)
    seed_customer_and_product(client)

    store = client.get('/store')
    assert b'Order Trailer' in store.data
    assert b'Book now' in store.data

    product_page = client.get('/store/products/1')
    assert product_page.status_code == 200
    assert b'Book Order Trailer' in product_page.data
    assert b'Pickup date' in product_page.data

    confirmation = client.post('/store/products/1/book', data={
        'customer_name': 'Public Booker',
        'customer_email': 'public@example.test',
        'customer_phone': '+270****0003',
        'quantity': '1',
        'start_date': '2026-10-01',
        'start_time': '09:00',
        'end_date': '2026-10-03',
        'end_time': '15:00',
        'notes': 'Public booking request',
    }, follow_redirects=True)
    assert confirmation.status_code == 200
    assert b'Booking request received' in confirmation.data
    assert b'ORD-00001' in confirmation.data
    assert b'Order Trailer' in confirmation.data
    assert b'R600.00' in confirmation.data

    login(client)
    orders = client.get('/orders?query=Public+Booker')
    assert b'ORD-00001' in orders.data
    assert b'Public Booker' in orders.data


def test_public_checkout_validates_required_fields(client):
    login(client)
    seed_customer_and_product(client)

    response = client.post('/store/products/1/book', data={
        'customer_name': '',
        'customer_email': '',
        'quantity': '1',
        'start_date': '2026-10-01',
        'end_date': '2026-10-03',
    }, follow_redirects=True)
    assert b'Name and email are required' in response.data
    assert b'Book Order Trailer' in response.data


def test_order_manual_payments_update_payment_status_and_history(client):
    login(client)
    seed_customer_and_product(client)
    order_id = create_order_for_status(client)

    detail = client.get(f'/orders/{order_id}')
    assert b'Payment due' in detail.data
    assert b'Record payment' in detail.data
    assert b'R1200.00' in detail.data

    partial = client.post(f'/orders/{order_id}/payments', data={
        'amount': '500',
        'method': 'cash',
        'reference': 'CASH-001',
    }, follow_redirects=True)
    assert b'Payment recorded' in partial.data
    assert b'Partially paid' in partial.data
    assert b'R500.00' in partial.data
    assert b'R700.00' in partial.data
    assert b'CASH-001' in partial.data

    paid = client.post(f'/orders/{order_id}/payments', data={
        'amount': '700',
        'method': 'eft',
        'reference': 'EFT-001',
    }, follow_redirects=True)
    assert b'Paid' in paid.data
    assert b'R1200.00' in paid.data
    assert b'R0.00' in paid.data

    ledger = client.get('/payments')
    assert ledger.status_code == 200
    assert b'Payments' in ledger.data
    assert b'ORD-00001' in ledger.data
    assert b'CASH-001' in ledger.data
    assert b'EFT-001' in ledger.data


def test_payment_validation_rejects_non_positive_amount(client):
    login(client)
    seed_customer_and_product(client)
    order_id = create_order_for_status(client)

    response = client.post(f'/orders/{order_id}/payments', data={
        'amount': '0',
        'method': 'cash',
        'reference': 'BAD-001',
    }, follow_redirects=True)
    assert b'Payment amount must be greater than zero' in response.data
    assert b'BAD-001' not in response.data


def test_reports_dashboard_summarizes_orders_payments_products_and_customers(client):
    login(client)
    seed_customer_and_product(client)
    first_id = create_order_for_status(client, quantity='2')
    second_id = create_order_for_status(client, quantity='1', start_date='2026-07-10', end_date='2026-07-11')
    client.post(f'/orders/{first_id}/reserve', follow_redirects=True)
    client.post(f'/orders/{second_id}/payments', data={'amount': '200', 'method': 'cash', 'reference': 'REPORT-CASH'}, follow_redirects=True)

    report = client.get('/reports')
    assert report.status_code == 200
    assert b'Reports' in report.data
    assert b'Revenue summary' in report.data
    assert b'Orders by status' in report.data
    assert b'Product performance' in report.data
    assert b'Customer summary' in report.data
    assert b'Payment summary' in report.data
    assert b'R1600.00' in report.data
    assert b'R200.00' in report.data
    assert b'Reserved' in report.data
    assert b'Draft' in report.data
    assert b'Order Trailer' in report.data
    assert b'Order Customer' in report.data
    assert b'Download orders CSV' in report.data


def test_reports_orders_csv_export(client):
    login(client)
    seed_customer_and_product(client)
    create_order_for_status(client, quantity='1')

    response = client.get('/reports/orders.csv')
    assert response.status_code == 200
    assert response.headers['Content-Type'].startswith('text/csv')
    assert 'attachment; filename=orders.csv' in response.headers['Content-Disposition']
    body = response.data.decode()
    assert 'order_number,customer,status,payment_status,total,due_total' in body
    assert 'ORD-00001,Order Customer,draft,payment_due,600.00,600.00' in body
