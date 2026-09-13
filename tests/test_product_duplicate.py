"""Duplicate a product (ticket ABI-341952945, part 2).

Ticket ask: "allow an option to duplicate a product in inventory".

Rules these tests pin:
- the copy is a brand-new row: the source product is never modified;
- the copy is named "<name> (copy)" and its SKU is BLANK (a SKU identifies a real
  item, so two rows sharing one would be ambiguous in the fleet list);
- the Booqable source keys are not cloned either (they carry a partial unique
  index), so a duplicate can never collide with an imported record;
- per-branch stock rows are copied within the session's branch scope, so a
  branch-limited user cannot create another depot's stock row;
- duplication is additive only, so it is allowed even for a product that has been
  used on an order (only permanent DELETE is blocked for those);
- the endpoint is gated by the Inventory module like the rest of the blueprint.
"""
import os
import re
import tempfile

import pytest

from app import create_app
from app.db import get_db
from app.services.products import product_branch_stock


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


def login(client, name=None, password='admin123'):
    with client.application.app_context():
        if name is None:
            row = get_db().execute(
                "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
            ).fetchone()
        else:
            row = get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
        user_id = row['id'] if row else None
    return client.post('/login', data={'user_id': str(user_id), 'password': password},
                       follow_redirects=True)


BASE_PRODUCT = {
    'name': 'Duplicate Me Trailer',
    'sku': 'DUP-001',
    'description': 'A trailer worth copying.',
    'product_type': 'rental',
    'tracking_method': 'bulk',
    'price_amount': '345',
    'price_unit': 'day',
    'security_deposit': '750',
    'hourly_extra_rate': '95',
    'active': '1',
    'public_visible': '1',
}


def create_product(client, **overrides):
    payload = dict(BASE_PRODUCT)
    payload.update(overrides)
    res = client.post('/inventory/new', data=payload, follow_redirects=False)
    assert res.status_code == 302, res.data[:400]
    return int(res.headers['Location'].rstrip('/').split('/')[-2])


def duplicate(client, product_id, follow_redirects=False):
    return client.post(f'/inventory/{product_id}/duplicate', follow_redirects=follow_redirects)


def duplicate_id(client, product_id):
    """POST the duplicate route and return the id of the new product."""
    res = duplicate(client, product_id)
    assert res.status_code == 302, res.data[:400]
    return int(res.headers['Location'].rstrip('/').split('/')[-2])


def row_of(client, product_id):
    with client.application.app_context():
        return get_db().execute('SELECT * FROM products WHERE id = ?', (product_id,)).fetchone()


def product_count(client):
    with client.application.app_context():
        return get_db().execute('SELECT COUNT(*) AS c FROM products').fetchone()['c']


def add_staff(client, name, branch_id=None, modules=None):
    if modules is not None:
        client.post('/settings/users/permissions', data={'module': modules}, follow_redirects=True)
    data = {'name': name, 'password': 'staff123'}
    if branch_id:
        data['branch_id'] = branch_id
    client.post('/settings/users/add', data=data, follow_redirects=True)


def create_customer(client):
    client.post('/customers/new', data={
        'customer_type': 'individual',
        'name': 'Duplicate Customer',
        'email': 'duplicate@example.com',
        'phone': '+270****0001',
    }, follow_redirects=True)


def create_order(client, product_id, quantity='1'):
    res = client.post('/orders/new', data={
        'customer_id': '1',
        'product_id': str(product_id),
        'quantity': quantity,
        'collect_branch_id': '1',
        'return_branch_id': '1',
        'start_date': '2026-07-01',
        'start_time': '09:00',
        'end_date': '2026-07-02',
        'end_time': '15:00',
    }, follow_redirects=False)
    assert res.status_code == 302, res.data[:400]
    return int(res.headers['Location'].rstrip('/').split('/')[-1])


def flash_messages(res):
    return re.findall(r'<div class="flash[^"]*">(.*?)</div>', res.data.decode(), re.S)


# --- copying ------------------------------------------------------------------

def test_duplicate_copies_the_details_appends_copy_and_blanks_the_sku(client):
    login(client)
    source_id = create_product(client, product_group_id='')
    source = row_of(client, source_id)

    new_id = duplicate_id(client, source_id)
    copy = row_of(client, new_id)
    assert new_id != source_id

    assert copy['name'] == 'Duplicate Me Trailer (copy)'
    assert copy['sku'] == ''                      # an identifier is never cloned
    assert copy['description'] == source['description']
    assert copy['product_type'] == source['product_type'] == 'rental'
    assert copy['tracking_method'] == source['tracking_method'] == 'bulk'
    assert copy['price_amount'] == source['price_amount'] == 345.0
    assert copy['price_unit'] == source['price_unit'] == 'day'
    assert copy['security_deposit'] == source['security_deposit'] == 750.0
    assert copy['hourly_extra_rate'] == source['hourly_extra_rate'] == 95.0
    assert copy['quantity'] == source['quantity']
    assert copy['active'] == source['active'] == 1
    assert copy['public_visible'] == source['public_visible'] == 1
    assert copy['tax_profile_id'] == source['tax_profile_id']

    # The source row is untouched, and the copy is not an imported record.
    assert row_of(client, source_id)['sku'] == 'DUP-001'
    assert row_of(client, source_id)['name'] == 'Duplicate Me Trailer'
    assert (copy['source_system'] or '') == '' and (copy['source_id'] or '') == ''


def test_duplicate_lands_on_the_copy_edit_page_with_a_flash(client):
    login(client)
    source_id = create_product(client)
    res = duplicate(client, source_id, follow_redirects=True)
    assert b'Product duplicated' in res.data
    assert b'Duplicate Me Trailer (copy)' in res.data
    assert b'Edit product' in res.data


def test_duplicate_copies_branch_rows_and_keeps_the_total_equal_to_the_sum(client):
    login(client)
    source_id = create_product(client, product_type='sale', sku='SALE-DUP',
                               qty_branch_1='3', qty_branch_2='2')
    new_id = duplicate_id(client, source_id)

    with client.application.app_context():
        assert product_branch_stock(new_id) == {1: 3, 2: 2}
        assert product_branch_stock(source_id) == {1: 3, 2: 2}
    assert row_of(client, new_id)['quantity'] == 5
    assert row_of(client, source_id)['quantity'] == 5


def test_duplicate_is_allowed_for_a_product_used_on_an_order(client):
    """Duplicating is additive, so unlike permanent delete it stays available."""
    login(client)
    create_customer(client)
    source_id = create_product(client)
    create_order(client, source_id)

    new_id = duplicate_id(client, source_id)
    assert new_id != source_id
    assert row_of(client, new_id)['name'] == 'Duplicate Me Trailer (copy)'
    # The order still points at the original, and the copy carries no history.
    with client.application.app_context():
        db = get_db()
        linked = db.execute('SELECT COUNT(*) AS c FROM order_items WHERE product_id = ?',
                            (new_id,)).fetchone()['c']
        assert linked == 0


def test_duplicate_of_a_missing_product_flashes_and_does_not_create_anything(client):
    login(client)
    before = product_count(client)
    res = duplicate(client, 999999, follow_redirects=True)
    assert b'Product not found' in res.data
    assert product_count(client) == before


# --- scope + gating -----------------------------------------------------------

def test_branch_limited_staff_duplicate_only_writes_their_own_branch_count(client):
    login(client)
    source_id = create_product(client, product_type='sale', sku='SALE-DUP-SCOPE',
                               qty_branch_1='3', qty_branch_2='2')
    add_staff(client, 'Duplicate Depot Staff', branch_id='2',
              modules=['new_order', 'dashboard', 'orders', 'customers', 'inventory'])
    client.post('/logout')
    login(client, 'Duplicate Depot Staff', 'staff123')

    new_id = duplicate_id(client, source_id)
    with client.application.app_context():
        assert product_branch_stock(new_id) == {2: 2}      # branch 1 is not theirs
        assert product_branch_stock(source_id) == {1: 3, 2: 2}
    assert row_of(client, new_id)['quantity'] == 2


def test_duplicate_without_the_inventory_module_is_refused(client):
    login(client)
    source_id = create_product(client)
    add_staff(client, 'No Inventory Staff', modules=['dashboard', 'orders'])
    client.post('/logout')
    login(client, 'No Inventory Staff', 'staff123')

    before = product_count(client)
    assert duplicate(client, source_id).status_code == 403
    assert product_count(client) == before


def test_duplicate_requires_a_signed_in_user(client):
    login(client)
    source_id = create_product(client)
    client.post('/logout')
    res = duplicate(client, source_id)
    assert res.status_code == 302
    assert '/login' in res.headers['Location']
    assert product_count(client) == 1


def test_the_duplicate_button_is_on_the_product_form(client):
    login(client)
    product_id = create_product(client)
    body = client.get(f'/inventory/{product_id}/edit').data
    assert b'Duplicate product' in body
    assert f'/inventory/{product_id}/duplicate'.encode() in body
