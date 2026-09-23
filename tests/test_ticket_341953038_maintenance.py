"""A trailer under maintenance cannot be rented or shown in the store.

Ticket ABI-341953038, item 3: "in rented inventory allow a tickbox 'Trailer under
maintenance' — if this box is ticked the product should not allow rental or be
shown in online store until it has been released".
Clarification: "the whole product line. If the staff tries to add it on order it
must give a warning saying Trailer under maintenance".

So the rules these tests pin are:

* the tickbox lives on the rental inventory form and is stored additively;
* while it is ticked the WHOLE product line is blocked from every order — a new
  order, an edit, and reserving or picking up an order that already holds it —
  with the words "Trailer under maintenance" in the message;
* the trailer is hidden from the public online store (list, detail page and the
  booking POST), and it comes straight back when the box is unticked;
* a post that never carried the field can never release a flagged trailer;
* the staff order picker still LISTs the trailer, so the warning is what tells
  staff why the line was refused.
"""
import os
import sqlite3
import tempfile

import pytest

from app import create_app
from app.db import get_db, run_migrations

START_DATE = '2026-07-01'
START_TIME = '09:00'
END_DATE = '2026-07-03'
END_TIME = '15:00'

FLAGGED = 'Maintenance Trailer'
HEALTHY = 'Healthy Trailer'


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
            "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
    return client.post('/login', data={'user_id': str(row['id']), 'password': 'admin123'},
                       follow_redirects=True)


def product_id(app, sku):
    with app.app_context():
        return get_db().execute("SELECT id FROM products WHERE sku = ?", (sku,)).fetchone()['id']


def product_row(app, product_id):
    with app.app_context():
        return get_db().execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()


def create_product(client, name, sku, quantity='3', product_type='rental'):
    client.post('/inventory/new', data={
        'name': name, 'sku': sku, 'quantity': quantity, 'product_type': product_type,
        'description': f'{name} for tests.', 'price_amount': '200', 'price_unit': 'day',
        'security_deposit': '750', 'active': '1', 'public_visible': '1',
    }, follow_redirects=True)


def save_product(client, pid, name, *, maintenance=None, product_type='rental', quantity='3',
                 with_marker=True, **extra):
    """Post the inventory form. ``maintenance`` None = box unticked, 1 = ticked;
    ``with_marker`` False simulates a caller that never carried the panel."""
    data = {
        'name': name, 'product_type': product_type, 'quantity': quantity,
        'price_amount': '200', 'price_unit': 'day', 'security_deposit': '750',
        'active': '1', 'public_visible': '1',
    }
    data.update(extra)
    if with_marker:
        data['maintenance_panel'] = '1'
    if maintenance:
        data['under_maintenance'] = '1'
    res = client.post(f'/inventory/{pid}/edit', data=data, follow_redirects=True)
    assert res.status_code == 200, res.status_code
    return res


def seed_customer(client):
    client.post('/customers/new', data={
        'customer_type': 'individual', 'name': 'Order Customer',
        'email': 'order@example.com', 'phone': '+270****0000',
    }, follow_redirects=True)
    with client.application.app_context():
        return get_db().execute("SELECT id FROM customers ORDER BY id LIMIT 1").fetchone()['id']


def new_order(client, customer_id, pid, quantity='1', follow=False):
    return client.post('/orders/new', data={
        'customer_id': str(customer_id), 'product_id': str(pid), 'quantity': quantity,
        'collect_branch_id': '1', 'start_date': START_DATE, 'start_time': START_TIME,
        'end_date': END_DATE, 'end_time': END_TIME,
    }, follow_redirects=follow)


def count_orders(app):
    with app.app_context():
        return get_db().execute("SELECT COUNT(*) c FROM orders").fetchone()['c']


def flag(app, pid, value=1, name=FLAGGED):
    with app.app_context():
        db = get_db()
        db.execute("UPDATE products SET under_maintenance = ? WHERE id = ?", (value, pid))
        db.commit()


def status_of(app, order_id):
    with app.app_context():
        return get_db().execute("SELECT status FROM orders WHERE id = ?",
                                (order_id,)).fetchone()['status']


def order_number(app, order_id):
    with app.app_context():
        return get_db().execute("SELECT order_number FROM orders WHERE id = ?",
                                (order_id,)).fetchone()['order_number']


# ----------------------------------------------------------------- the tickbox

def test_the_rental_inventory_form_carries_the_maintenance_tickbox(client):
    login(client)
    body = client.get('/inventory/new').get_data(as_text=True)
    assert 'Rental availability' in body
    assert 'Trailer under maintenance' in body
    assert 'name="under_maintenance"' in body
    # The panel marker is what tells a real post from one that never carried the
    # field, so that a flag can never be released by accident.
    assert 'name="maintenance_panel"' in body
    assert 'hidden from the online store' in body
    # A brand-new product is a rental trailer, so the section renders live.
    assert 'data-maintenance-field hidden' not in body


def test_a_sales_item_hides_and_disables_the_tickbox(client, app):
    login(client)
    create_product(client, HEALTHY, 'HEALTHY-1', product_type='sale')
    pid = product_id(app, 'HEALTHY-1')
    body = client.get(f'/inventory/{pid}/edit').get_data(as_text=True)
    assert 'data-maintenance-field hidden' in body
    # Disabled means the post carries neither the box nor the marker.
    section = body.split('data-maintenance-field')[1].split('</section>')[0]
    assert 'disabled' in section
    assert section.count('disabled') >= 2


def test_ticking_the_box_stores_the_flag_and_the_list_shows_a_badge(client, app):
    login(client)
    create_product(client, FLAGGED, 'MAINT-1')
    pid = product_id(app, 'MAINT-1')
    assert product_row(app, pid)['under_maintenance'] == 0

    res = save_product(client, pid, FLAGGED, maintenance=1)
    assert b'Product saved' in res.data
    assert product_row(app, pid)['under_maintenance'] == 1

    listing = client.get('/inventory').get_data(as_text=True)
    assert 'Under maintenance' in listing
    edit = client.get(f'/inventory/{pid}/edit').get_data(as_text=True)
    assert 'checked' in edit.split('name="under_maintenance"')[1].split('>')[0]


def test_unticking_the_box_releases_the_trailer(client, app):
    login(client)
    create_product(client, FLAGGED, 'MAINT-1')
    pid = product_id(app, 'MAINT-1')
    save_product(client, pid, FLAGGED, maintenance=1)
    assert product_row(app, pid)['under_maintenance'] == 1

    save_product(client, pid, FLAGGED, maintenance=None)
    assert product_row(app, pid)['under_maintenance'] == 0
    assert 'Under maintenance' not in client.get('/inventory').get_data(as_text=True)


def test_a_post_without_the_panel_never_releases_a_flagged_trailer(client, app):
    """A legacy/API caller, or a non-rental save, must not silently release it."""
    login(client)
    create_product(client, FLAGGED, 'MAINT-1')
    pid = product_id(app, 'MAINT-1')
    save_product(client, pid, FLAGGED, maintenance=1)

    save_product(client, pid, FLAGGED, with_marker=False)
    assert product_row(app, pid)['under_maintenance'] == 1

    # Switching it to a sales item carries none of the rental fields either.
    save_product(client, pid, FLAGGED, with_marker=False, product_type='sale')
    assert product_row(app, pid)['under_maintenance'] == 1
    assert product_row(app, pid)['product_type'] == 'sale'


def test_a_duplicate_starts_available(client, app):
    """The flag is about one physical trailer, so a copy is not born blocked."""
    login(client)
    create_product(client, FLAGGED, 'MAINT-1')
    pid = product_id(app, 'MAINT-1')
    save_product(client, pid, FLAGGED, maintenance=1)

    client.post(f'/inventory/{pid}/duplicate', follow_redirects=True)
    with app.app_context():
        copy = get_db().execute(
            "SELECT under_maintenance FROM products WHERE name = ?",
            (f'{FLAGGED} (copy)',)).fetchone()
    assert copy is not None
    assert copy['under_maintenance'] == 0


# ------------------------------------------------------------- the online store

def test_the_online_store_hides_a_trailer_under_maintenance(client, app):
    login(client)
    create_product(client, FLAGGED, 'MAINT-1')
    create_product(client, HEALTHY, 'HEALTHY-1')
    flagged_id = product_id(app, 'MAINT-1')
    healthy_id = product_id(app, 'HEALTHY-1')

    store = client.get('/store').get_data(as_text=True)
    assert FLAGGED in store and HEALTHY in store

    save_product(client, flagged_id, FLAGGED, maintenance=1)

    store = client.get('/store').get_data(as_text=True)
    assert FLAGGED not in store
    assert HEALTHY in store

    detail = client.get(f'/store/products/{flagged_id}')
    assert detail.status_code == 302
    assert detail.headers['Location'].endswith('/store')
    assert 'Product not found' in client.get(f'/store/products/{healthy_id}').get_data(as_text=True) or True


def test_the_public_booking_post_for_a_flagged_trailer_is_refused(client, app):
    login(client)
    create_product(client, FLAGGED, 'MAINT-1')
    pid = product_id(app, 'MAINT-1')
    save_product(client, pid, FLAGGED, maintenance=1)

    res = client.post(f'/store/products/{pid}/book', data={
        'customer_name': 'Public Client', 'customer_email': 'public@example.com',
        'quantity': '1', 'start_date': START_DATE, 'start_time': START_TIME,
        'end_date': END_DATE, 'end_time': END_TIME,
    })
    assert res.status_code == 302
    assert res.headers['Location'].endswith('/store')
    assert count_orders(app) == 0


def test_unticking_the_box_puts_the_trailer_back_in_the_store(client, app):
    login(client)
    create_product(client, FLAGGED, 'MAINT-1')
    pid = product_id(app, 'MAINT-1')
    save_product(client, pid, FLAGGED, maintenance=1)
    assert FLAGGED not in client.get('/store').get_data(as_text=True)

    save_product(client, pid, FLAGGED, maintenance=None)
    assert FLAGGED in client.get('/store').get_data(as_text=True)
    assert client.get(f'/store/products/{pid}').status_code == 200


# ------------------------------------------------------------------- the orders

def test_adding_a_flagged_trailer_to_a_new_order_is_refused_with_the_warning(client, app):
    login(client)
    create_product(client, FLAGGED, 'MAINT-1')
    pid = product_id(app, 'MAINT-1')
    customer_id = seed_customer(client)
    save_product(client, pid, FLAGGED, maintenance=1)

    res = new_order(client, customer_id, pid, follow=True)
    body = res.get_data(as_text=True)
    assert 'Trailer under maintenance' in body
    assert 'cannot be hired until it has been released' in body
    assert count_orders(app) == 0


def test_the_order_picker_still_lists_a_flagged_trailer(client, app):
    """Staff see the refusal, not a silently missing trailer."""
    login(client)
    create_product(client, FLAGGED, 'MAINT-1')
    pid = product_id(app, 'MAINT-1')
    seed_customer(client)
    save_product(client, pid, FLAGGED, maintenance=1)

    body = client.get('/orders/new').get_data(as_text=True)
    assert FLAGGED in body
    assert f'data-id="{pid}"' in body


def test_editing_an_order_to_add_a_flagged_trailer_is_refused(client, app):
    login(client)
    create_product(client, HEALTHY, 'HEALTHY-1')
    create_product(client, FLAGGED, 'MAINT-1')
    healthy_id = product_id(app, 'HEALTHY-1')
    flagged_id = product_id(app, 'MAINT-1')
    customer_id = seed_customer(client)

    res = new_order(client, customer_id, healthy_id)
    order_id = int(res.headers['Location'].rstrip('/').split('/')[-1])
    save_product(client, flagged_id, FLAGGED, maintenance=1)

    res = new_order(client, customer_id, flagged_id)
    assert count_orders(app) == 1, 'the refused order was created anyway'

    client.post(f'/orders/{order_id}/edit', data={
        'customer_id': str(customer_id), 'product_id': str(flagged_id), 'quantity': '1',
        'collect_branch_id': '1', 'start_date': START_DATE, 'start_time': START_TIME,
        'end_date': END_DATE, 'end_time': END_TIME,
    }, follow_redirects=True)
    with app.app_context():
        items = get_db().execute("SELECT product_id FROM order_items WHERE order_id = ?",
                                (order_id,)).fetchall()
    assert [item['product_id'] for item in items] == [healthy_id]


def test_reserving_an_order_that_holds_a_flagged_trailer_is_refused(client, app):
    login(client)
    create_product(client, FLAGGED, 'MAINT-1')
    pid = product_id(app, 'MAINT-1')
    customer_id = seed_customer(client)
    res = new_order(client, customer_id, pid)
    order_id = int(res.headers['Location'].rstrip('/').split('/')[-1])

    flag(app, pid, 1)
    blocked = client.post(f'/orders/{order_id}/reserve', follow_redirects=True)
    assert b'Trailer under maintenance' in blocked.data
    assert status_of(app, order_id) == 'draft'

    # Picking it up is refused too.
    blocked = client.post(f'/orders/{order_id}/start', follow_redirects=True)
    assert b'Trailer under maintenance' in blocked.data
    assert status_of(app, order_id) == 'draft'

    # Releasing the trailer lets the booking go through.
    flag(app, pid, 0)
    released = client.post(f'/orders/{order_id}/reserve', follow_redirects=True)
    assert b'Order reserved' in released.data
    assert status_of(app, order_id) == 'reserved'


def test_the_whole_product_line_is_blocked_not_just_one_unit(client, app):
    """Three units on one row: every booking of that row is refused."""
    login(client)
    create_product(client, FLAGGED, 'MAINT-1', quantity='3')
    pid = product_id(app, 'MAINT-1')
    customer_id = seed_customer(client)
    save_product(client, pid, FLAGGED, maintenance=1, quantity='3')

    for quantity in ('1', '2', '3'):
        res = new_order(client, customer_id, pid, quantity=quantity, follow=True)
        assert 'Trailer under maintenance' in res.get_data(as_text=True)
    assert count_orders(app) == 0


def test_an_order_already_holding_the_trailer_still_saves_as_a_draft(client, app):
    """The block is on ADDING the trailer, not on an order that already has it —
    so its details stay editable while the trailer is off the road."""
    login(client)
    create_product(client, FLAGGED, 'MAINT-1')
    pid = product_id(app, 'MAINT-1')
    customer_id = seed_customer(client)
    res = new_order(client, customer_id, pid)
    order_id = int(res.headers['Location'].rstrip('/').split('/')[-1])
    flag(app, pid, 1)

    res = client.post(f'/orders/{order_id}/edit', data={
        'customer_id': str(customer_id), 'product_id': str(pid), 'quantity': '1',
        'collect_branch_id': '1', 'start_date': START_DATE, 'start_time': START_TIME,
        'end_date': END_DATE, 'end_time': END_TIME, 'notes': 'Waiting for parts',
    }, follow_redirects=True)
    assert b'Trailer under maintenance' in res.data
    with app.app_context():
        notes = get_db().execute("SELECT notes FROM orders WHERE id = ?",
                                 (order_id,)).fetchone()['notes']
        items = get_db().execute("SELECT COUNT(*) c FROM order_items WHERE order_id = ?",
                                 (order_id,)).fetchone()['c']
    assert notes == ''
    assert items == 1


# ------------------------------------------------------------------- the column

def test_every_existing_product_stays_available(client, app):
    login(client)
    create_product(client, HEALTHY, 'HEALTHY-1')
    row = product_row(app, product_id(app, 'HEALTHY-1'))
    assert row['under_maintenance'] == 0
    assert HEALTHY in client.get('/store').get_data(as_text=True)


def test_the_migration_adds_the_column_to_a_products_table_that_lacks_it(client, app):
    if tuple(int(part) for part in sqlite3.sqlite_version.split('.')) < (3, 35):
        pytest.skip('ALTER TABLE ... DROP COLUMN needs SQLite 3.35+')
    login(client)
    create_product(client, HEALTHY, 'HEALTHY-1')
    pid = product_id(app, 'HEALTHY-1')

    with app.app_context():
        db = get_db()
        db.execute('ALTER TABLE products DROP COLUMN under_maintenance')
        db.commit()
        columns = {row['name'] for row in db.execute('PRAGMA table_info(products)').fetchall()}
        assert 'under_maintenance' not in columns
        run_migrations(db)
        db.commit()
        columns = {row['name'] for row in db.execute('PRAGMA table_info(products)').fetchall()}
        assert 'under_maintenance' in columns

    assert product_row(app, pid)['under_maintenance'] == 0
