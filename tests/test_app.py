import os
import tempfile
from datetime import datetime

import pytest

from app import create_app
from app.services.orders import rental_days


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


def test_booqable_reference_navigation_pages_exist(client):
    login(client)
    for path, expected in [
        ('/coupons', b'Coupons'),
        ('/app-store', b'App store'),
        ('/ask-bo', b'Ask Bo'),
        ('/scan-barcode', b'Scan a barcode'),
        ('/help', b'Help'),
    ]:
        res = client.get(path)
        assert res.status_code == 200
        assert expected in res.data


def test_coupon_creation_and_order_application(client):
    login(client)
    created = client.post('/coupons', data={
        'code': 'TRAILER10',
        'description': '10 percent trailer promo',
        'discount_type': 'percent',
        'value': '10',
        'active': '1',
    }, follow_redirects=True)
    assert created.status_code == 200
    assert b'Coupon created' in created.data
    assert b'TRAILER10' in created.data
    assert b'10.0%' in created.data

    seed_customer_and_product(client)
    order = client.post('/orders/new', data={
        'customer_id': '1',
        'product_id': '1',
        'quantity': '2',
        'coupon_code': 'TRAILER10',
        'start_date': '2026-07-01',
        'start_time': '09:00',
        'end_date': '2026-07-03',
        'end_time': '15:00',
        'notes': 'Discounted order test',
    }, follow_redirects=True)
    assert b'Draft order created' in order.data
    assert b'TRAILER10' in order.data
    assert b'R120.00' in order.data  # 10% discount on R1200 rental subtotal.
    assert b'R2580.00' in order.data  # discounted rental total plus refundable security deposit.


def test_admin_can_edit_coupon_fields_and_validation(client, app):
    login(client)
    client.post('/coupons', data={
        'code': 'SAVE10',
        'description': 'Original promo',
        'discount_type': 'percent',
        'value': '10',
        'active': '1',
    }, follow_redirects=True)
    client.post('/coupons', data={
        'code': 'OTHER',
        'description': 'Other promo',
        'discount_type': 'fixed',
        'value': '5',
        'active': '1',
    }, follow_redirects=True)

    edit_page = client.get('/coupons/1/edit')
    assert edit_page.status_code == 200
    assert b'Edit coupon' in edit_page.data
    assert b'Save coupon changes' in edit_page.data
    assert b'SAVE10' in edit_page.data

    duplicate = client.post('/coupons/1/edit', data={
        'code': 'OTHER',
        'description': 'Duplicate code attempt',
        'discount_type': 'fixed',
        'value': '25',
        'active': '1',
    }, follow_redirects=True)
    assert b'Coupon code already exists' in duplicate.data

    for invalid_value, message in [
        ('0', b'Coupon discount must be greater than zero'),
        ('101', b'Percentage coupons cannot exceed 100%'),
    ]:
        response = client.post('/coupons/1/edit', data={
            'code': 'SAVE10',
            'description': 'Invalid promo',
            'discount_type': 'percent',
            'value': invalid_value,
            'active': '1',
        }, follow_redirects=True)
        assert message in response.data

    saved = client.post('/coupons/1/edit', data={
        'code': 'SAVE25',
        'description': 'Updated fixed discount',
        'discount_type': 'fixed',
        'value': '25',
    }, follow_redirects=True)
    assert saved.status_code == 200
    assert b'Coupon updated' in saved.data
    assert b'SAVE25' in saved.data
    assert b'Updated fixed discount' in saved.data
    assert b'R25.00' in saved.data
    assert b'Inactive' in saved.data

    with app.app_context():
        from app.db import get_db
        coupon = get_db().execute('SELECT code, description, discount_type, value, active FROM coupons WHERE id=1').fetchone()
        assert coupon is not None
        assert coupon['code'] == 'SAVE25'
        assert coupon['description'] == 'Updated fixed discount'
        assert coupon['discount_type'] == 'fixed'
        assert coupon['value'] == 25
        assert coupon['active'] == 0


def test_edited_coupon_values_affect_new_and_edited_orders(client, app):
    login(client)
    seed_customer_and_product(client)
    client.post('/coupons', data={
        'code': 'EDITME',
        'description': 'Starts as ten percent',
        'discount_type': 'percent',
        'value': '10',
        'active': '1',
    }, follow_redirects=True)

    first_order = client.post('/orders/new', data={
        'customer_id': '1',
        'product_id': '1',
        'quantity': '1',
        'coupon_code': 'EDITME',
        'start_date': '2026-07-01',
        'start_time': '09:00',
        'end_date': '2026-07-02',
        'end_time': '09:00',
        'deposit_option': 'no_deposit',
    }, follow_redirects=True)
    assert b'Draft order created' in first_order.data
    with app.app_context():
        from app.db import get_db
        original = get_db().execute('SELECT discount_total, total FROM orders WHERE id=1').fetchone()
        assert original is not None
        assert original['discount_total'] == 20
        assert original['total'] == 180

    client.post('/coupons/1/edit', data={
        'code': 'EDITME',
        'description': 'Now fixed seventy five',
        'discount_type': 'fixed',
        'value': '75',
        'active': '1',
    }, follow_redirects=True)

    new_order = client.post('/orders/new', data={
        'customer_id': '1',
        'product_id': '1',
        'quantity': '1',
        'coupon_code': 'EDITME',
        'start_date': '2026-07-03',
        'start_time': '09:00',
        'end_date': '2026-07-04',
        'end_time': '09:00',
        'deposit_option': 'no_deposit',
    }, follow_redirects=True)
    assert b'Draft order created' in new_order.data
    with app.app_context():
        from app.db import get_db
        created_after_edit = get_db().execute('SELECT discount_total, total FROM orders WHERE id=2').fetchone()
        assert created_after_edit is not None
        assert created_after_edit['discount_total'] == 75
        assert created_after_edit['total'] == 125

    edited_order = client.post('/orders/1/edit', data={
        'customer_id': '1',
        'product_id': ['1'],
        'custom_name': [''],
        'custom_unit_price': [''],
        'custom_billing_mode': ['fixed'],
        'quantity': ['1'],
        'coupon_code': 'EDITME',
        'start_date': '2026-07-05',
        'start_time': '09:00',
        'end_date': '2026-07-06',
        'end_time': '09:00',
        'deposit_option': 'no_deposit',
    }, follow_redirects=True)
    assert b'Order saved' in edited_order.data
    with app.app_context():
        from app.db import get_db
        edited = get_db().execute('SELECT discount_total, total FROM orders WHERE id=1').fetchone()
        assert edited is not None
        assert edited['discount_total'] == 75
        assert edited['total'] == 125

def test_barcode_lookup(client):
    login(client)
    # GET the page
    res = client.get('/scan-barcode')
    assert res.status_code == 200
    assert b'Scan a barcode' in res.data
    assert b'Barcode (SKU)' in res.data

    # Create a product to test with
    product_data = {
        'name': 'Barcode Test Product',
        'sku': 'BARCODE-TEST',
        'quantity': '1',
        'description': 'Product for barcode test',
        'product_type': 'rental',
        'price_amount': '100',
        'price_unit': 'day',
        'security_deposit': '0',
        'tax_profile_id': '1',
        'active': '1',
        'public_visible': '1',
    }
    # Create the product via the inventory route
    resp = client.post('/inventory/new', data=product_data, follow_redirects=False)
    assert resp.status_code == 302  # redirect to edit page

    # Test valid barcode
    res = client.post('/scan-barcode', data={'barcode': 'BARCODE-TEST'}, follow_redirects=False)
    assert res.status_code == 302
    assert '/inventory/' in res.location
    assert '/edit' in res.location

    # Test blank barcode
    res = client.post('/scan-barcode', data={'barcode': ''}, follow_redirects=True)
    assert res.status_code == 200
    assert b'Barcode is required' in res.data

    # Test invalid barcode
    res = client.post('/scan-barcode', data={'barcode': 'NON-EXISTENT'}, follow_redirects=True)
    assert res.status_code == 200
    assert b'No active product found with barcode' in res.data


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
        'tracking_method': 'bulk',
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
    assert b'Track quantities' in res.data

    res = client.get('/inventory')
    assert b'Box Trailer' in res.data
    assert b'TRL-BOX-001' in res.data
    assert b'Visible' in res.data

    res = client.get('/store')
    assert b'Box Trailer' in res.data
    assert b'R450.00 / day' in res.data



def test_inventory_product_groups_assign_products_and_show_branch(client, app):
    login(client)
    group = client.post('/inventory/groups/new', data={
        'name': 'Trailer rentals',
        'description': 'Rental product folder',
        'sort_order': '1',
        'active': '1',
    }, follow_redirects=True)
    assert group.status_code == 200
    assert b'Product group created' in group.data
    assert b'Trailer rentals' in group.data

    client.post('/branches', data={'branch_id': '2', 'name': 'North Depot', 'code': 'NORTH', 'active': '1'}, follow_redirects=True)
    rental = client.post('/inventory/new', data={
        'name': 'Grouped Rental Trailer',
        'sku': 'GRP-RENT',
        'quantity': '1',
        'tracking_method': 'individual',
        'description': 'Individual rental stock under a category.',
        'product_type': 'rental',
        'product_group_id': '1',
        'branch_id': '2',
        'price_amount': '500',
        'price_unit': 'day',
        'security_deposit': '1000',
        'tax_profile_id': '1',
        'active': '1',
        'public_visible': '1',
    }, follow_redirects=True)
    assert b'Product created' in rental.data
    assert b'Trailer rentals' in rental.data

    sale = client.post('/inventory/new', data={
        'name': 'Grouped Sales Strap',
        'sku': 'GRP-SALE',
        'quantity': '8',
        'tracking_method': 'bulk',
        'description': 'Sales stock under the same category.',
        'product_type': 'sale',
        'product_group_id': '1',
        'branch_id': '2',
        'price_amount': '75',
        'price_unit': 'fixed',
        'security_deposit': '0',
        'tax_profile_id': '1',
        'active': '1',
        'public_visible': '1',
    }, follow_redirects=True)
    assert b'Product created' in sale.data

    inventory = client.get('/inventory')
    assert b'\xe2\x96\xbe Trailer rentals' in inventory.data
    assert b'Grouped Rental Trailer' in inventory.data
    assert b'Grouped Sales Strap' in inventory.data
    assert b'North Depot' in inventory.data
    assert b'Track individually' in inventory.data

    filtered = client.get('/inventory?product_group_id=1')
    assert b'Grouped Rental Trailer' in filtered.data
    assert b'Grouped Sales Strap' in filtered.data
    assert b'North Depot' in filtered.data

    export = client.get('/inventory/export.csv?product_group_id=1')
    body = export.data.decode()
    assert 'group,name,sku,product_type,branch' in body
    assert 'Trailer rentals,Grouped Rental Trailer,GRP-RENT,rental,North Depot' in body

    with app.app_context():
        from app.db import get_db
        rows = get_db().execute('SELECT product_group_id, branch_id FROM products WHERE sku IN (?, ?) ORDER BY sku', ('GRP-RENT', 'GRP-SALE')).fetchall()
        assert [row['product_group_id'] for row in rows] == [1, 1]
        assert [row['branch_id'] for row in rows] == [2, 2]


def test_editing_product_group_assignment_keeps_order_creation_working(client, app):
    login(client)
    client.post('/inventory/groups/new', data={'name': 'Rental folders', 'sort_order': '1', 'active': '1'}, follow_redirects=True)
    seed_customer_and_product(client)

    edited = client.post('/inventory/1/edit', data={
        'name': 'Order Trailer',
        'sku': 'ORD-TRL',
        'quantity': '4',
        'tracking_method': 'bulk',
        'description': 'Order test trailer.',
        'product_type': 'rental',
        'product_group_id': '1',
        'branch_id': '1',
        'price_amount': '200',
        'price_unit': 'day',
        'security_deposit': '750',
        'tax_profile_id': '1',
        'active': '1',
        'public_visible': '1',
    }, follow_redirects=True)
    assert b'Product saved' in edited.data
    assert b'value="1" selected' in edited.data

    order = client.post('/orders/new', data={
        'customer_id': '1',
        'product_id': '1',
        'quantity': '2',
        'start_date': '2026-07-01',
        'start_time': '09:00',
        'end_date': '2026-07-03',
        'end_time': '15:00',
        'notes': 'Grouped product order test',
    }, follow_redirects=True)
    assert order.status_code == 200
    assert b'Draft order created' in order.data
    assert b'Order Trailer' in order.data
    assert b'R1200.00' in order.data

    inventory = client.get('/inventory?product_group_id=1')
    assert b'Order Trailer' in inventory.data
    assert b'Branch 1' in inventory.data

    with app.app_context():
        from app.db import get_db
        product = get_db().execute('SELECT product_group_id FROM products WHERE id=1').fetchone()
        assert product['product_group_id'] == 1

def test_product_type_and_tracking_method_are_immutable_after_create(client, app):
    login(client)
    res = client.post('/inventory/new', data={
        'name': 'Immutable Trailer',
        'sku': 'IMM-TRL',
        'quantity': '2',
        'tracking_method': 'bulk',
        'description': 'Booqable-style immutable type test.',
        'product_type': 'rental',
        'price_amount': '300',
        'price_unit': 'day',
        'security_deposit': '500',
        'tax_profile_id': '1',
        'active': '1',
        'public_visible': '1',
    }, follow_redirects=False)
    product_id = res.headers['Location'].rstrip('/').split('/')[-2]

    edited = client.post(f'/inventory/{product_id}/edit', data={
        'name': 'Immutable Trailer Updated',
        'sku': 'IMM-TRL',
        'quantity': '4',
        'tracking_method': 'individual',
        'description': 'Attempted to change type and tracking.',
        'product_type': 'sale',
        'price_amount': '350',
        'price_unit': 'fixed',
        'security_deposit': '0',
        'tax_profile_id': '1',
        'active': '1',
        'public_visible': '1',
    }, follow_redirects=True)
    assert b'Product saved' in edited.data
    assert b'Product type and tracking method cannot be changed after saving' in edited.data

    with app.app_context():
        from app.db import get_db
        product = get_db().execute('SELECT product_type, tracking_method, quantity FROM products WHERE id = ?', (product_id,)).fetchone()
        assert product['product_type'] == 'rental'
        assert product['tracking_method'] == 'bulk'
        assert product['quantity'] == 4


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
        'phone': '+271****6789',
        'marketing_opt_in': '1',
        'vat_number': '4123456789',
        'company_reg_no': '2024/123456/07',
        'vehicle_details': 'Bakkie CA 123',
    }, follow_redirects=True)
    assert res.status_code == 200
    assert b'Customer created' in res.data
    assert b'Acme Rentals' in res.data
    assert b'Subscribed' in res.data
    assert b'VAT No' in res.data
    assert b'4123456789' in res.data
    assert b'Company Reg No' in res.data
    assert b'2024/123456/07' in res.data
    assert b'Bakkie CA 123' in res.data

    edit_page = client.get('/customers/1/edit')
    assert edit_page.status_code == 200
    assert b'data-company-details' in edit_page.data
    assert b'name="vat_number"' in edit_page.data
    assert b'value="4123456789"' in edit_page.data
    assert b'name="company_reg_no"' in edit_page.data
    assert b'value="2024/123456/07"' in edit_page.data

    res = client.get('/customers?query=acme&customer_type=company&marketing=subscribed')
    assert b'Acme Rentals' in res.data
    assert b'bookings@acme.test' in res.data
    assert b'Company' in res.data

    detail_url = '/customers/1/edit'
    res = client.post(detail_url, data={
        'customer_type': 'individual',
        'name': 'Don Customer',
        'email': 'don@example.com',
        'phone': '+279****9999',
    }, follow_redirects=True)
    assert b'Customer saved' in res.data
    assert b'Don Customer' in res.data
    assert b'Not subscribed' in res.data
    assert b'VAT No' not in res.data
    assert b'4123456789' not in res.data
    assert b'Company Reg No' not in res.data
    assert b'2024/123456/07' not in res.data
    assert b'Bakkie CA 123' not in res.data


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
        'phone': '+270****0000',
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
    assert b'id="rental-days-card"' in res.data
    assert b'Rental days' in res.data
    assert b'id="rental-days-count"' in res.data
    assert b'id="order-estimate-panel"' in res.data
    assert b'Order estimate' in res.data
    assert b'Product total' in res.data
    assert b'class="line-total-preview"' in res.data
    assert b'data-deposit="750.00"' in res.data
    assert b'data-tax-rate=' in res.data

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
    assert b'R2700.00' in res.data  # order total includes refundable security deposit.
    assert b'Total incl. taxes and refundable deposit' in res.data

    list_res = client.get('/orders')
    assert b'ORD-00001' in list_res.data
    assert b'Order Customer' in list_res.data
    assert b'Total incl. deposit' in list_res.data
    assert b'built-in method items' not in list_res.data


def test_security_deposit_is_in_order_total_and_due_but_stored_separately(client, app):
    login(client)
    seed_customer_and_product(client)

    res = client.post('/orders/new', data={
        'customer_id': '1',
        'product_id': '1',
        'quantity': '1',
        'start_date': '2026-07-01',
        'start_time': '09:00',
        'end_date': '2026-07-02',
        'end_time': '09:00',
        'deposit_option': 'security_deposit',
    }, follow_redirects=True)

    assert res.status_code == 200
    assert b'R950.00' in res.data  # R200 rental + R750 refundable security deposit.
    assert b'Security deposit' in res.data
    with app.app_context():
        from app.db import get_db
        db = get_db()
        order = db.execute('SELECT total, due_total, deposit_total, deposit_option, payment_status FROM orders WHERE id=1').fetchone()
        assert order is not None
        assert order['total'] == 950
        assert order['due_total'] == 950
        assert order['deposit_total'] == 750
        assert order['deposit_option'] == 'security_deposit'
        assert order['payment_status'] == 'payment_due'


@pytest.mark.parametrize('deposit_option,waiver,total,due,deposit_total', [
    ('damage_waiver', '125', 325, 325, 0),
    ('no_deposit', '', 200, 200, 0),
])
def test_non_security_deposit_modes_do_not_add_security_deposit_to_total(client, app, deposit_option, waiver, total, due, deposit_total):
    login(client)
    seed_customer_and_product(client)

    res = client.post('/orders/new', data={
        'customer_id': '1',
        'product_id': '1',
        'quantity': '1',
        'start_date': '2026-07-01',
        'start_time': '09:00',
        'end_date': '2026-07-02',
        'end_time': '09:00',
        'deposit_option': deposit_option,
        'damage_waiver_amount': waiver,
    }, follow_redirects=True)

    assert res.status_code == 200
    with app.app_context():
        from app.db import get_db
        db = get_db()
        order = db.execute('SELECT total, due_total, deposit_total, deposit_option, damage_waiver_amount FROM orders WHERE id=1').fetchone()
        assert order is not None
        assert order['total'] == total
        assert order['due_total'] == due
        assert order['deposit_total'] == deposit_total
        assert order['deposit_option'] == deposit_option
        assert order['damage_waiver_amount'] == (float(waiver) if waiver else 0)


def test_rental_days_matches_partial_day_rounding_rule():
    assert rental_days(datetime(2026, 7, 1, 9, 0), datetime(2026, 7, 1, 11, 0)) == 1
    assert rental_days(datetime(2026, 7, 1, 9, 0), datetime(2026, 7, 2, 9, 0)) == 1
    assert rental_days(datetime(2026, 7, 1, 9, 0), datetime(2026, 7, 2, 9, 1)) == 2
    assert rental_days(datetime(2026, 7, 3, 9, 0), datetime(2026, 7, 2, 9, 0)) == 1




def edit_order_payload(quantity='3', custom_price='50', start_date='2026-07-02', start_time='10:15', end_date='2026-07-04', end_time='11:00'):
    return {
        'customer_id': '1',
        'product_id': ['1', ''],
        'custom_name': ['', 'Admin edit fee'],
        'custom_unit_price': ['', custom_price],
        'custom_billing_mode': ['fixed', 'fixed'],
        'quantity': [quantity, '1'],
        'start_date': start_date,
        'start_time': start_time,
        'end_date': end_date,
        'end_time': end_time,
        'deposit_option': 'damage_waiver',
        'damage_waiver_amount': '125',
        'notes': 'Edited order note',
    }


def test_draft_order_can_be_edited_without_creating_new_order(client, app):
    login(client)
    seed_customer_and_product(client)
    order_id = create_order_for_status(client, quantity='1')

    edit_page = client.get(f'/orders/{order_id}/edit')
    assert edit_page.status_code == 200
    assert b'Edit order' in edit_page.data
    assert b'Save order changes' in edit_page.data
    assert b'id="order-estimate-panel"' in edit_page.data
    assert b'class="line-total-preview"' in edit_page.data
    assert b'data-deposit="750.00"' in edit_page.data
    assert b'Edit order' in client.get(f'/orders/{order_id}').data

    saved = client.post(f'/orders/{order_id}/edit', data=edit_order_payload(), follow_redirects=True)

    assert saved.status_code == 200
    assert b'Order saved' in saved.data
    assert b'ORD-00001' in saved.data
    assert b'Admin edit fee' in saved.data
    assert b'Edited order note' not in saved.data  # internal notes are saved but not shown on detail yet.
    assert b'R1975.00' in saved.data  # 3 * R200 * 3 days + R50 + R125 damage waiver.
    assert b'R0.00' in saved.data  # security deposit removed by damage waiver option.

    with app.app_context():
        from app.db import get_db
        db = get_db()
        orders_count = db.execute('SELECT COUNT(*) AS count FROM orders').fetchone()['count']
        order = db.execute('SELECT status, total, due_total, deposit_total, damage_waiver_amount, notes FROM orders WHERE id=?', (order_id,)).fetchone()
        item_count = db.execute('SELECT COUNT(*) AS count FROM order_items WHERE order_id=?', (order_id,)).fetchone()['count']
        assert orders_count == 1
        assert order['status'] == 'draft'
        assert order['total'] == 1975
        assert order['due_total'] == 1975
        assert order['deposit_total'] == 0
        assert order['damage_waiver_amount'] == 125
        assert order['notes'] == 'Edited order note'
        assert item_count == 2


def test_non_draft_order_can_be_edited_and_keeps_status_with_payment_recalculation(client, app):
    login(client)
    seed_customer_and_product(client)
    order_id = create_order_for_status(client)
    client.post(f'/orders/{order_id}/reserve', follow_redirects=True)

    edit_page = client.get(f'/orders/{order_id}/edit')
    assert edit_page.status_code == 200
    assert b'Edit order' in edit_page.data
    assert b'Save order changes' in edit_page.data
    assert b'Edit order' in client.get('/orders').data
    assert b'Edit order' in client.get(f'/orders/{order_id}').data

    with app.app_context():
        from app.db import get_db, now
        db = get_db()
        db.execute("INSERT INTO payments (order_id, amount, method, reference, status, created_at) VALUES (?, 150, 'cash', 'TEST-PARTIAL', 'paid', ?)", (order_id, now()))
        db.commit()

    saved = client.post(
        f'/orders/{order_id}/edit',
        data=edit_order_payload(quantity='1', custom_price='75', start_date='2026-07-02', start_time='10:00', end_date='2026-07-03', end_time='10:00'),
        follow_redirects=True,
    )
    assert saved.status_code == 200
    assert b'Order saved' in saved.data
    assert b'Reserved' in saved.data
    assert b'R400.00' in saved.data  # 1 * R200 * 1 day + R75 custom + R125 damage waiver.

    with app.app_context():
        from app.db import get_db
        db = get_db()
        order = db.execute('SELECT status, total, due_total, payment_status, start_at, end_at FROM orders WHERE id=?', (order_id,)).fetchone()
        items = db.execute('SELECT product_id, custom_name, quantity, unit_price FROM order_items WHERE order_id=? ORDER BY id', (order_id,)).fetchall()
        assert order['status'] == 'reserved'
        assert order['total'] == 400
        assert order['due_total'] == 250
        assert order['payment_status'] == 'partially_paid'
        assert order['start_at'] == '2026-07-02T10:00'
        assert order['end_at'] == '2026-07-03T10:00'
        assert len(items) == 2
        assert items[0]['product_id'] == 1
        assert items[0]['quantity'] == 1
        assert items[1]['custom_name'] == 'Admin edit fee'
        assert items[1]['unit_price'] == 75


def test_edit_recalculates_security_deposit_inclusive_total_and_due(client, app):
    login(client)
    seed_customer_and_product(client)
    order_id = create_order_for_status(client, quantity='1')
    with app.app_context():
        from app.db import get_db, now
        db = get_db()
        db.execute("INSERT INTO payments (order_id, amount, method, reference, status, created_at) VALUES (?, 200, 'cash', 'TEST-RECALC', 'paid', ?)", (order_id, now()))
        db.commit()

    saved = client.post(f'/orders/{order_id}/edit', data={
        'customer_id': '1',
        'product_id': ['1'],
        'custom_name': [''],
        'custom_unit_price': [''],
        'custom_billing_mode': ['fixed'],
        'quantity': ['2'],
        'start_date': '2026-07-01',
        'start_time': '09:00',
        'end_date': '2026-07-02',
        'end_time': '09:00',
        'deposit_option': 'security_deposit',
        'damage_waiver_amount': '',
    }, follow_redirects=True)

    assert b'Order saved' in saved.data
    assert b'R1900.00' in saved.data
    with app.app_context():
        from app.db import get_db
        db = get_db()
        order = db.execute('SELECT total, due_total, deposit_total, damage_waiver_amount, payment_status FROM orders WHERE id=?', (order_id,)).fetchone()
        assert order is not None
        assert order['total'] == 1900
        assert order['due_total'] == 1700
        assert order['deposit_total'] == 1500
        assert order['damage_waiver_amount'] == 0
        assert order['payment_status'] == 'partially_paid'


def test_canceled_and_archived_order_edit_is_rejected(client):
    login(client)
    seed_customer_and_product(client)

    canceled_id = create_order_for_status(client)
    client.post(f'/orders/{canceled_id}/cancel', follow_redirects=True)
    for method in ('get', 'post'):
        response = getattr(client, method)(f'/orders/{canceled_id}/edit', data=edit_order_payload(), follow_redirects=True)
        assert b'Canceled and archived orders cannot be edited' in response.data
        assert b'Canceled' in response.data
    assert b'Edit order' not in client.get(f'/orders/{canceled_id}').data

    archived_id = create_order_for_status(client, start_date='2026-08-01', end_date='2026-08-03')
    client.post(f'/orders/{archived_id}/reserve', follow_redirects=True)
    client.post(f'/orders/{archived_id}/start', follow_redirects=True)
    client.post(f'/orders/{archived_id}/return', follow_redirects=True)
    client.post(f'/orders/{archived_id}/archive', follow_redirects=True)
    for method in ('get', 'post'):
        response = getattr(client, method)(f'/orders/{archived_id}/edit', data=edit_order_payload(start_date='2026-08-02', end_date='2026-08-03'), follow_redirects=True)
        assert b'Canceled and archived orders cannot be edited' in response.data
        assert b'Archived' in response.data
    assert b'Edit order' not in client.get(f'/orders/{archived_id}').data


def test_order_supports_mixed_rental_sales_and_service_lines(client):
    login(client)
    seed_customer_and_product(client)
    client.post('/inventory/new', data={
        'name': 'LED Trailer Light Kit',
        'sku': 'CTW-PART-LIGHTKIT',
        'quantity': '20',
        'description': 'Sale part for mixed order.',
        'product_type': 'sale',
        'price_amount': '475',
        'price_unit': 'fixed',
        'security_deposit': '0',
        'tax_profile_id': '1',
        'active': '1',
        'public_visible': '1',
    }, follow_redirects=True)
    client.post('/inventory/new', data={
        'name': 'Trailer Safety Inspection',
        'sku': 'CTW-SVC-SAFE',
        'quantity': '999',
        'description': 'Workshop inspection service.',
        'product_type': 'service',
        'price_amount': '550',
        'price_unit': 'fixed',
        'security_deposit': '0',
        'tax_profile_id': '1',
        'active': '1',
        'public_visible': '1',
    }, follow_redirects=True)

    res = client.post('/orders/new', data={
        'customer_id': '1',
        'product_id': ['1', '2', '3'],
        'quantity': ['1', '2', '1'],
        'start_date': '2026-07-10',
        'start_time': '09:00',
        'end_date': '2026-07-12',
        'end_time': '15:00',
        'notes': 'Mixed line order',
    }, follow_redirects=True)
    assert res.status_code == 200
    assert b'Draft order created' in res.data
    assert b'Order Trailer' in res.data
    assert b'LED Trailer Light Kit' in res.data
    assert b'Trailer Safety Inspection' in res.data
    # Rental: 1 * R200 * 3 days; sales: 2 * R475 once; service: 1 * R550 once.
    assert b'R2100.00' in res.data
    assert b'R750.00' in res.data


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
        'notes': 'Status workload order',
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

    export = client.get('/documents/export.csv')
    assert export.status_code == 200
    assert export.mimetype == 'text/csv'
    assert b'number,document_type,order_number,customer_name,status,total,created_at' in export.data
    assert b'QUO-00001' in export.data
    assert b'ORD-00001' in export.data


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
    assert b'R2200.00' in partial.data
    assert b'CASH-001' in partial.data

    paid = client.post(f'/orders/{order_id}/payments', data={
        'amount': '2200',
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
    assert 'ORD-00001,Order Customer,draft,payment_due,1350.00,1350.00' in body


def test_app_store_functionality(client):
    login(client)
    # GET the app store page
    resp = client.get('/app-store')
    assert resp.status_code == 200
    assert b'App store' in resp.data
    # Check that we have at least one item (from seeding)
    assert b'ShipStation' in resp.data or b'Mailchimp' in resp.data  # one of the seeded items
    # We'll get the item id from the database for a known item.
    with client.application.app_context():
        from app.db import get_db
        db = get_db()
        item = db.execute('SELECT id FROM app_store_items WHERE name = ?', ('ShipStation',)).fetchone()
        # If ShipStation is not found (maybe the order is different), try the first item.
        if item is None:
            item = db.execute('SELECT id FROM app_store_items LIMIT 1').fetchone()
        assert item is not None, 'No app store items found'
        item_id = item['id']
    # Now test toggling the item's active status.
    # First, deactivate it: we do not send the 'is_active' key (unchecked checkbox).
    resp = client.post('/app-store', data={'item_id': str(item_id)}, follow_redirects=True)
    assert resp.status_code == 200
    assert b'App store item updated' in resp.data
    # Check that the item is now inactive in the database.
    with client.application.app_context():
        from app.db import get_db
        db = get_db()
        item = db.execute('SELECT is_active FROM app_store_items WHERE id = ?', (item_id,)).fetchone()
        assert item is not None
        assert item['is_active'] == 0
    # Now activate it again: we send 'is_active': 'on' (checked checkbox).
    resp = client.post('/app-store', data={'item_id': str(item_id), 'is_active': 'on'}, follow_redirects=True)
    assert resp.status_code == 200
    assert b'App store item updated' in resp.data
    with client.application.app_context():
        from app.db import get_db
        db = get_db()
        item = db.execute('SELECT is_active FROM app_store_items WHERE id = ?', (item_id,)).fetchone()
        assert item is not None
        assert item['is_active'] == 1


def test_internal_telegram_requires_secret(client):
    res = client.post('/api/internal/telegram/daily-summary')
    assert res.status_code == 401


def test_internal_telegram_daily_summary_skips_when_disabled(tmp_path):
    app = create_app({
        'TESTING': True,
        'DATABASE': str(tmp_path / 'telegram.db'),
        'SECRET_KEY': 'test',
        'ADMIN_EMAIL': 'admin@abi.local',
        'ADMIN_PASSWORD': 'admin123',
        'TELEGRAM_CRON_SECRET': 'secret',
        'TELEGRAM_NOTIFICATIONS_ENABLED': '',
        'PUBLIC_BASE_URL': 'https://abi-rental-platform.onrender.com',
    })
    test_client = app.test_client()
    res = test_client.post('/api/internal/telegram/daily-summary?date=2026-07-02', headers={'x-cron-secret': 'secret'})
    assert res.status_code == 200
    data = res.get_json()
    assert data['ok'] is True
    assert data['sent'] is False
    assert data['skipped'] == 'disabled'
    assert data['date'] == '2026-07-02'


def test_telegram_customer_formatter_escapes_html(client):
    from app.services.telegram import format_customer_message

    message = format_customer_message({
        'name': '<ACME & Co>',
        'customer_type': 'company',
        'email': 'boss@example.com',
        'phone': '<123>',
        'marketing_opt_in': 1,
    })
    assert '&lt;ACME &amp; Co&gt;' in message
    assert '<ACME' not in message

def test_customer_address_add_customer_from_order_and_custom_rental_line(client):
    login(client)
    client.post('/inventory/new', data={
        'name': 'Order Trailer', 'sku': 'ORD-TRL', 'quantity': '4', 'description': 'Order test trailer.',
        'product_type': 'rental', 'price_amount': '200', 'price_unit': 'day', 'security_deposit': '750',
        'tax_profile_id': '1', 'active': '1', 'public_visible': '1',
    }, follow_redirects=True)
    created = client.post('/orders/new', data={
        'order_action': 'create_customer_continue',
        'customer_type': 'company', 'name': 'Address Customer', 'email': 'address@example.test', 'phone': '+2711',
        'address_line1': '12 Test Street', 'city': 'Cape Town',
    }, follow_redirects=True)
    assert b'Customer created' in created.data
    assert b'Address Customer' in created.data
    order = client.post('/orders/new', data={
        'customer_id': '1',
        'product_id': ['', ''],
        'custom_name': ['Custom Rental Addon'],
        'custom_unit_price': ['50'],
        'custom_billing_mode': ['rental_day'],
        'quantity': ['2'],
        'start_date': '2026-07-01', 'start_time': '09:00',
        'end_date': '2026-07-03', 'end_time': '15:00',
    }, follow_redirects=True)
    assert b'Draft order created' in order.data
    assert b'Custom Rental Addon' in order.data
    assert b'R300.00' in order.data  # 2 qty * R50 * 3 rental days.
    assert b'12 Test Street' in order.data


def test_branch_one_way_return_moves_product_to_return_branch(client, app):
    login(client)
    seed_customer_and_product(client)
    branches_page = client.get('/branches')
    assert b'Branch 1' in branches_page.data
    assert b'Branch 2' in branches_page.data
    assert b'Branch 3' in branches_page.data
    saved = client.post('/branches', data={'branch_id': '2', 'name': 'North Depot', 'code': 'NORTH', 'active': '1'}, follow_redirects=True)
    assert b'Branch saved' in saved.data
    assert b'North Depot' in saved.data
    order_id = create_order_for_status(client, quantity='1')
    with app.app_context():
        db = __import__('app.db', fromlist=['get_db']).get_db()
        db.execute("UPDATE orders SET booking_type='oneway', collect_branch_id=1, return_branch_id=2 WHERE id=?", (order_id,))
        db.commit()
    assert b'Order reserved' in client.post(f'/orders/{order_id}/reserve', follow_redirects=True).data
    assert b'Order started' in client.post(f'/orders/{order_id}/start', follow_redirects=True).data
    returned = client.post(f'/orders/{order_id}/return', follow_redirects=True)
    assert b'Order returned' in returned.data
    inventory = client.get('/inventory')
    assert b'North Depot' in inventory.data


@pytest.mark.parametrize('method,label', [('eft', b'EFT'), ('card', b'CARD'), ('cash', b'CASH')])
def test_return_deposit_settlement_records_method_and_removes_from_due_filter(client, app, method, label):
    login(client)
    seed_customer_and_product(client)
    order_id = create_order_for_status(client, quantity='1')
    assert b'Order reserved' in client.post(f'/orders/{order_id}/reserve', follow_redirects=True).data
    assert b'Order started' in client.post(f'/orders/{order_id}/start', follow_redirects=True).data
    assert b'Order returned' in client.post(f'/orders/{order_id}/return', follow_redirects=True).data

    due_before = client.get('/orders?payment_status=process_deposit')
    assert b'ORD-00001' in due_before.data

    settled = client.post(f'/orders/{order_id}/settle-return', data={
        'extra_hours': '0',
        'extra_hourly_rate': '0',
        'deposit_process_method': method,
        'deposit_processed_at': '',
        'deposit_note': 'Processed deposit at counter',
    }, follow_redirects=True)
    assert b'Deposit refund marked' in settled.data
    assert label in settled.data

    due_after = client.get('/orders?payment_status=process_deposit')
    assert b'ORD-00001' not in due_after.data
    returned = client.get('/orders?status=returned')
    assert b'ORD-00001' in returned.data

    with app.app_context():
        db = __import__('app.db', fromlist=['get_db']).get_db()
        order = db.execute('SELECT deposit_process_method, deposit_processed_at FROM orders WHERE id=?', (order_id,)).fetchone()
        assert order['deposit_process_method'] == method
        assert order['deposit_processed_at']
        refunded_on = order['deposit_processed_at'][:16].replace('T', ' ')

    assert b'Deposit refund date' in settled.data
    assert refunded_on.encode() in settled.data


def test_return_deposit_settlement_records_manual_refund_date(client, app):
    login(client)
    seed_customer_and_product(client)
    order_id = create_order_for_status(client, quantity='1')
    client.post(f'/orders/{order_id}/reserve', follow_redirects=True)
    client.post(f'/orders/{order_id}/start', follow_redirects=True)
    client.post(f'/orders/{order_id}/return', follow_redirects=True)

    settled = client.post(f'/orders/{order_id}/settle-return', data={
        'extra_hours': '0',
        'extra_hourly_rate': '0',
        'deposit_process_method': 'eft',
        'deposit_processed_at': '2026-08-20T14:45',
        'deposit_note': 'Backdated deposit refund',
    }, follow_redirects=True)

    assert b'Deposit refund marked' in settled.data
    assert b'2026-08-20 14:45 UTC' in settled.data
    assert b'value="2026-08-20T14:45"' in settled.data
    with app.app_context():
        db = __import__('app.db', fromlist=['get_db']).get_db()
        order = db.execute('SELECT deposit_processed_at FROM orders WHERE id=?', (order_id,)).fetchone()
        assert order['deposit_processed_at'] == '2026-08-20T14:45:00'


def test_return_deposit_settlement_rejects_invalid_refund_date_without_overwrite(client, app):
    login(client)
    seed_customer_and_product(client)
    order_id = create_order_for_status(client, quantity='1')
    client.post(f'/orders/{order_id}/reserve', follow_redirects=True)
    client.post(f'/orders/{order_id}/start', follow_redirects=True)
    client.post(f'/orders/{order_id}/return', follow_redirects=True)
    with app.app_context():
        db = __import__('app.db', fromlist=['get_db']).get_db()
        db.execute("""UPDATE orders SET deposit_refund_amount=1000, deposit_process_method='eft',
            deposit_processed_at='2026-08-20T14:45:00' WHERE id=?""", (order_id,))
        db.commit()

    response = client.post(f'/orders/{order_id}/settle-return', data={
        'extra_hours': '0',
        'extra_hourly_rate': '0',
        'deposit_process_method': 'cash',
        'deposit_processed_at': 'not-a-date',
        'deposit_note': 'Should not save',
    }, follow_redirects=True)

    assert b'Deposit refund date must be a valid date and time' in response.data
    with app.app_context():
        db = __import__('app.db', fromlist=['get_db']).get_db()
        order = db.execute('SELECT deposit_process_method, deposit_processed_at, deposit_note FROM orders WHERE id=?', (order_id,)).fetchone()
        assert order['deposit_process_method'] == 'eft'
        assert order['deposit_processed_at'] == '2026-08-20T14:45:00'
        assert order['deposit_note'] != 'Should not save'


def test_historical_refunded_deposit_is_not_in_process_deposit_filter(client, app):
    login(client)
    seed_customer_and_product(client)
    order_id = create_order_for_status(client, quantity='1')
    client.post(f'/orders/{order_id}/reserve', follow_redirects=True)
    client.post(f'/orders/{order_id}/start', follow_redirects=True)
    client.post(f'/orders/{order_id}/return', follow_redirects=True)
    with app.app_context():
        db = __import__('app.db', fromlist=['get_db']).get_db()
        db.execute('UPDATE orders SET deposit_refund_amount=1000, deposit_process_method="", deposit_processed_at="" WHERE id=?', (order_id,))
        db.commit()

    detail = client.get(f'/orders/{order_id}')
    assert b'Deposit refund date' in detail.data
    assert b'processed before date tracking' in detail.data
    assert b'Not recorded' in detail.data

    process_deposit = client.get('/orders?payment_status=process_deposit')
    assert b'ORD-00001' not in process_deposit.data


def test_return_deposit_settlement_rejects_invalid_method(client):
    login(client)
    seed_customer_and_product(client)
    order_id = create_order_for_status(client, quantity='1')
    response = client.post(f'/orders/{order_id}/settle-return', data={
        'extra_hours': '0',
        'extra_hourly_rate': '0',
        'deposit_process_method': 'cheque',
    }, follow_redirects=True)
    assert b'Deposit process method must be EFT, Card, or Cash' in response.data


def test_document_email_requires_provider_but_generates_pdf_status(client):
    login(client)
    seed_customer_and_product(client)
    order_id = create_order_for_status(client)
    client.post(f'/orders/{order_id}/documents', data={'document_type': 'invoice'}, follow_redirects=True)
    detail = client.get('/documents/1')
    assert b'Send PDF email' in detail.data
    response = client.post('/documents/1/send-email', data={'to_email': 'order@example.com'}, follow_redirects=True)
    assert b'Email provider not configured' in response.data
    assert b'Failed' in response.data
