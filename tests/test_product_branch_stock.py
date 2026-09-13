"""Per-branch stock counts for tracked products (ticket ABI-341952940).

A tracked product may hold one stock count per branch (``product_branch_stock``).
When it has rows it is branch-managed: a booking collected at a branch with no
row, or a 0 row, is not available from that branch, and ``products.quantity``
stays the sum of those rows so reports, the calendar and the order-form picker
keep working unchanged. With no rows the product keeps the legacy single shared
pool, bookable from every branch — which is what all live products do today.
"""
import os
import re
import tempfile
from datetime import datetime, timedelta

import pytest

from app import create_app
from app.db import get_db, init_db
from app.services.orders import calendar_group_availability
from app.services.products import product_branch_stock, set_product_branch_stock


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
    return client.post('/login', data={'user_id': str(user_id), 'password': password}, follow_redirects=True)


OWNER_PRODUCT = {
    'name': 'Branch Trailer',
    'sku': 'BR-TRL',
    'description': 'Branch stock trailer.',
    'product_type': 'rental',
    'tracking_method': 'bulk',
    'price_amount': '200',
    'price_unit': 'day',
    'security_deposit': '750',
    'active': '1',
    'public_visible': '1',
}


def create_product(client, **overrides):
    payload = dict(OWNER_PRODUCT)
    payload.update(overrides)
    res = client.post('/inventory/new', data=payload, follow_redirects=False)
    assert res.status_code == 302, res.data[:400]
    # redirects to /inventory/<id>/edit
    return int(res.headers['Location'].rstrip('/').split('/')[-2])


def edit_product(client, product_id, **overrides):
    payload = dict(OWNER_PRODUCT)
    payload.update(overrides)
    res = client.post(f'/inventory/{product_id}/edit', data=payload, follow_redirects=True)
    return res


def create_customer(client):
    client.post('/customers/new', data={
        'customer_type': 'individual',
        'name': 'Branch Stock Customer',
        'email': 'branch-stock@example.com',
        'phone': '+27000000000',
    }, follow_redirects=True)


def create_order(client, product_id, quantity='1', collect_branch_id='1',
                 start_date='2026-07-01', end_date='2026-07-02'):
    res = client.post('/orders/new', data={
        'customer_id': '1',
        'product_id': str(product_id),
        'quantity': quantity,
        'collect_branch_id': str(collect_branch_id),
        'return_branch_id': str(collect_branch_id),
        'start_date': start_date,
        'start_time': '09:00',
        'end_date': end_date,
        'end_time': '15:00',
    }, follow_redirects=False)
    assert res.status_code == 302, res.data[:400]
    return int(res.headers['Location'].rstrip('/').split('/')[-1])


def reserve(client, order_id):
    return client.post(f'/orders/{order_id}/reserve', follow_redirects=True)


def flash_messages(res):
    return re.findall(r'<div class="flash[^"]*">(.*?)</div>', res.data.decode(), re.S)


def stored(client, product_id):
    with client.application.app_context():
        return get_db().execute('SELECT quantity FROM products WHERE id = ?', (product_id,)).fetchone()['quantity']


def field_tag(html, marker):
    """The opening tag that carries ``marker`` (a data-* attr or an input name)."""
    match = re.search(rb'<[^>]*' + marker + rb'[^>]*>', html)
    return match.group(0) if match else b''


def test_inventory_form_offers_one_stock_box_per_branch(client):
    """A SALES ITEM keeps the per-branch grid (the only type that shows it)."""
    login(client)
    product_id = create_product(client, product_type='sale', sku='SALE-BR-1')
    body = client.get(f'/inventory/{product_id}/edit').data
    assert b'name="qty_branch_1"' in body
    assert b'name="qty_branch_2"' in body
    assert b'name="qty_branch_3"' in body
    assert b'Total in stock' in body
    assert b'Sales items only: leave every branch box empty' in body
    # The branch block is the visible one and its boxes are enabled...
    assert b'hidden' not in field_tag(body, b'data-stock-field')
    for branch in (1, 2, 3):
        assert b'disabled' not in field_tag(body, b'name="qty_branch_%d"' % branch)
    # ...while the single box is there only for a type switch, hidden and disabled.
    assert b'hidden' in field_tag(body, b'data-stock-quantity-field')
    assert b'disabled' in field_tag(body, b'name="quantity"')


def test_rental_form_offers_one_shared_quantity_box_and_no_branch_boxes(client):
    """Rentals keep ONE shared count: the branch grid is hidden and disabled, so a
    rental submit posts no ``qty_branch_*`` key at all (ABI-341952945)."""
    login(client)
    product_id = create_product(client, quantity='4')
    body = client.get(f'/inventory/{product_id}/edit').data
    assert b'name="quantity"' in body
    assert b'hidden' not in field_tag(body, b'data-stock-quantity-field')
    assert b'disabled' not in field_tag(body, b'name="quantity"')
    assert b'hidden' in field_tag(body, b'data-stock-field')
    for branch in (1, 2, 3):
        assert b'disabled' in field_tag(body, b'name="qty_branch_%d"' % branch)
    assert b'Per-branch stock counts are only offered on sales items' in body


def test_new_product_form_defaults_to_the_shared_quantity_box(client):
    login(client)
    body = client.get('/inventory/new').data
    assert b'hidden' in field_tag(body, b'data-stock-field')
    assert b'hidden' not in field_tag(body, b'data-stock-quantity-field')
    assert b'disabled' not in field_tag(body, b'name="quantity"')


def test_saving_a_rental_stores_one_shared_count_and_no_branch_rows(client):
    """Exactly what a rental now posts: the single quantity box, no branch boxes."""
    login(client)
    product_id = create_product(client, quantity='4')
    assert stored(client, product_id) == 4
    with client.application.app_context():
        assert product_branch_stock(product_id) == {}

    res = edit_product(client, product_id, quantity='9')
    assert b'Product saved' in res.data
    assert stored(client, product_id) == 9
    with client.application.app_context():
        assert product_branch_stock(product_id) == {}


def test_saving_a_rental_never_clears_or_zeroes_existing_branch_rows(client):
    """A hidden block must never wipe stock.

    A product can still carry per-branch rows (a sales item changed to a rental),
    and the rental form posts no branch boxes. Saving must leave those rows alone
    and keep the total equal to the sum of them — never zero it.
    """
    login(client)
    product_id = create_product(client, quantity='5')
    with client.application.app_context():
        set_product_branch_stock(product_id, {1: 3, 2: 2})
    assert stored(client, product_id) == 5

    res = edit_product(client, product_id, quantity='1')
    assert b'Product saved' in res.data
    with client.application.app_context():
        assert product_branch_stock(product_id) == {1: 3, 2: 2}
    assert stored(client, product_id) == 5


def test_inventory_list_only_breaks_down_sales_item_stock(client):
    """The per-branch breakdown text belongs beside a sales item, not a rental."""
    login(client)
    create_product(client, name='Branch Split Sale', sku='SALE-BR-2',
                   product_type='sale', qty_branch_1='3', qty_branch_2='2')
    sale = client.get('/inventory').data.decode()
    assert 'Branch 1 3' in sale and 'Branch 2 2' in sale

    # A rental that somehow carries rows must not sprout a branch breakdown.
    rental_id = create_product(client, name='Branch Split Rental', sku='RENT-BR-1')
    with client.application.app_context():
        set_product_branch_stock(rental_id, {1: 3, 2: 2})
    listing = client.get('/inventory').data.decode()
    assert 'Branch Split Rental' in listing
    rented = listing.split('Branch Split Rental', 1)[1].split('</tr>', 1)[0]
    assert 'Branch 1 3' not in rented
    assert 'Multiple branches' not in rented


def test_saving_branch_counts_stores_rows_and_the_total(client):
    login(client)
    product_id = create_product(client, product_type='sale', qty_branch_1='3', qty_branch_2='2')
    with client.application.app_context():
        assert product_branch_stock(product_id) == {1: 3, 2: 2}
    assert stored(client, product_id) == 5

    form = client.get(f'/inventory/{product_id}/edit').data
    assert b'name="qty_branch_1"' in form and b'name="qty_branch_2"' in form
    assert re.search(rb'name="qty_branch_1"[^>]*value="3"', form)
    assert re.search(rb'name="qty_branch_2"[^>]*value="2"', form)
    assert re.search(rb'name="qty_branch_3"[^>]*value=""', form)

    listing = client.get('/inventory').data
    assert b'Branch 1 3' in listing and b'Branch 2 2' in listing


def test_blank_boxes_keep_the_shared_pool_untouched(client):
    """The 146 live products hold no per-branch counts: saving the form without
    entering a count must not silently zero their stock."""
    login(client)
    product_id = create_product(client, quantity='4')
    with client.application.app_context():
        assert product_branch_stock(product_id) == {}
    assert stored(client, product_id) == 4

    # Exactly what the browser posts when every branch box is left blank.
    res = edit_product(client, product_id, qty_branch_1='', qty_branch_2='', qty_branch_3='')
    assert b'Product saved' in res.data
    assert stored(client, product_id) == 4
    with client.application.app_context():
        assert product_branch_stock(product_id) == {}


def test_clearing_every_box_returns_a_split_product_to_the_shared_pool(client):
    login(client)
    product_id = create_product(client, qty_branch_1='3', qty_branch_2='2')
    edit_product(client, product_id, qty_branch_1='', qty_branch_2='', qty_branch_3='')
    with client.application.app_context():
        assert product_branch_stock(product_id) == {}
    # The split total becomes the shared pool rather than dropping to zero.
    assert stored(client, product_id) == 5


def test_negative_and_fractional_counts_are_refused(client):
    login(client)
    product_id = create_product(client, qty_branch_1='3', qty_branch_2='2')
    negative = edit_product(client, product_id, qty_branch_1='-4', qty_branch_2='2')
    assert any('cannot be negative' in message for message in flash_messages(negative))
    assert stored(client, product_id) == 5
    fractional = edit_product(client, product_id, qty_branch_1='1.5', qty_branch_2='2')
    assert any('whole numbers' in message for message in flash_messages(fractional))
    assert stored(client, product_id) == 5
    with client.application.app_context():
        assert product_branch_stock(product_id) == {1: 3, 2: 2}


# --- availability -------------------------------------------------------------

def test_zero_stock_branch_blocks_only_that_branch(client):
    login(client)
    create_customer(client)
    product_id = create_product(client, qty_branch_1='1', qty_branch_2='0')

    stocked = create_order(client, product_id, quantity='1', collect_branch_id='1')
    assert b'Order reserved' in reserve(client, stocked).data

    blocked = create_order(client, product_id, quantity='1', collect_branch_id='2')
    response = reserve(client, blocked)
    assert b'Order reserved' not in response.data
    assert any('at this collection branch' in message for message in flash_messages(response))


def test_per_branch_counts_are_consumed_per_branch(client):
    login(client)
    create_customer(client)
    product_id = create_product(client, qty_branch_1='1', qty_branch_2='3')

    first = create_order(client, product_id, quantity='1', collect_branch_id='1')
    assert b'Order reserved' in reserve(client, first).data

    # Midrand's single unit is gone, but Wonderboom's three are untouched.
    second = create_order(client, product_id, quantity='1', collect_branch_id='1')
    assert b'Order reserved' not in reserve(client, second).data
    other_branch = create_order(client, product_id, quantity='1', collect_branch_id='2')
    assert b'Order reserved' in reserve(client, other_branch).data


def test_a_branch_with_no_row_is_not_bookable_once_counts_exist(client):
    login(client)
    create_customer(client)
    product_id = create_product(client, qty_branch_1='5')
    other = create_order(client, product_id, quantity='1', collect_branch_id='3')
    blocked = reserve(client, other)
    assert b'Order reserved' not in blocked.data
    assert any('Only 0 available' in message for message in flash_messages(blocked))


def test_product_without_counts_keeps_the_shared_pool_across_branches(client):
    """Regression guard for the imported catalogue: no branch rows means the old
    behaviour — one shared pool, bookable from every branch."""
    login(client)
    create_customer(client)
    product_id = create_product(client, quantity='3')
    for branch in ('1', '2', '3'):
        order_id = create_order(client, product_id, quantity='1', collect_branch_id=branch)
        assert b'Order reserved' in reserve(client, order_id).data
    # The shared pool is the limit no matter which branch collects the booking.
    overflow = create_order(client, product_id, quantity='1', collect_branch_id='1')
    assert b'Order reserved' not in reserve(client, overflow).data


def test_untracked_products_are_never_blocked_and_clear_their_rows(client):
    login(client)
    create_customer(client)
    product_id = create_product(client, qty_branch_1='2', qty_branch_2='1')
    # Never used on an order, so the tracking method may still change.
    edit_product(client, product_id, tracking_method='none',
                 qty_branch_1='2', qty_branch_2='1')
    with client.application.app_context():
        assert product_branch_stock(product_id) == {}
    assert stored(client, product_id) == 0

    order_id = create_order(client, product_id, quantity='3', collect_branch_id='1')
    assert b'Order reserved' in reserve(client, order_id).data

    service_id = create_product(client, name='Branch Setup Service', sku='BR-SVC',
                                product_type='service', qty_branch_1='')
    with client.application.app_context():
        assert product_branch_stock(service_id) == {}
    service_order = create_order(client, service_id, quantity='1', collect_branch_id='2')
    assert b'Order reserved' in reserve(client, service_order).data


# --- staff scope --------------------------------------------------------------

def test_branch_limited_staff_can_only_write_their_own_branch_count(client, app):
    login(client)
    product_id = create_product(client, qty_branch_1='3', qty_branch_2='1')
    client.post('/settings/users/permissions', data={
        'module': ['new_order', 'dashboard', 'calendar', 'orders', 'customers', 'inventory'],
    }, follow_redirects=True)
    client.post('/settings/users/add', data={
        'name': 'Depot Staff',
        'password': 'staff123',
        'branch_id': '2',
    }, follow_redirects=True)
    client.post('/logout')
    login(client, 'Depot Staff', 'staff123')

    form = client.get(f'/inventory/{product_id}/edit').data
    assert b'name="qty_branch_2"' in form
    assert b'name="qty_branch_1"' not in form      # another depot's count is not offered

    edit_product(client, product_id, qty_branch_1='99', qty_branch_2='5')
    with client.application.app_context():
        assert product_branch_stock(product_id) == {1: 3, 2: 5}
    assert stored(client, product_id) == 8


# --- branch delete, CSV and calendar -----------------------------------------

def test_deleting_a_branch_removes_its_stock_rows_and_recomputes_the_total(client, app):
    login(client)
    product_id = create_product(client, qty_branch_1='2', qty_branch_2='3')
    assert stored(client, product_id) == 5
    deleted = client.post('/branches/2/delete', follow_redirects=True)
    assert b'Branch deleted' in deleted.data
    with client.application.app_context():
        assert product_branch_stock(product_id) == {1: 2}
    assert stored(client, product_id) == 2


def test_csv_export_carries_the_per_branch_breakdown(client):
    login(client)
    create_product(client, qty_branch_1='3', qty_branch_2='2')
    export = client.get('/inventory/export.csv')
    assert export.status_code == 200
    text = export.data.decode()
    assert 'stock_by_branch' in text.splitlines()[0]
    assert 'Branch 1 3' in text and 'Branch 2 2' in text


def test_calendar_availability_follows_the_branch_filter(client, app):
    login(client)
    create_customer(client)
    product_id = create_product(client, qty_branch_1='1', qty_branch_2='2')
    order_id = create_order(client, product_id, quantity='1', collect_branch_id='1')
    assert b'Order reserved' in reserve(client, order_id).data

    with app.app_context():
        def row(branch_filter):
            availability = calendar_group_availability(
                start_date='2026-07-01', end_date='2026-07-01', branch_id=branch_filter
            )
            for group in availability['groups']:
                for product in group['products']:
                    if product['id'] == product_id:
                        return product
            raise AssertionError('product missing from the calendar availability')

        central = row(1)
        assert (central['total_quantity'], central['booked_quantity'], central['available_quantity']) == (1, 1, 0)
        north = row(2)
        assert (north['total_quantity'], north['booked_quantity'], north['available_quantity']) == (2, 0, 2)
        everything = row(None)
        assert (everything['total_quantity'], everything['booked_quantity']) == (3, 1)

    page = client.get('/calendar?start_date=2026-07-01&end_date=2026-07-01&branch=2')
    assert page.status_code == 200
    assert b'Branch Trailer' in page.data


def test_legacy_post_cannot_drift_the_total_away_from_the_rows(client):
    """A caller that posts no per-branch boxes must not overwrite a split product's
    computed total (products.quantity always equals the sum of the rows)."""
    login(client)
    product_id = create_product(client, qty_branch_1='3', qty_branch_2='2')
    edit_product(client, product_id, quantity='99')
    assert stored(client, product_id) == 5
    with client.application.app_context():
        assert product_branch_stock(product_id) == {1: 3, 2: 2}


def test_migration_creates_the_branch_stock_table_on_an_existing_database(client, app):
    """run_migrations() is idempotent and must add the table to a live-shaped DB."""
    with app.app_context():
        db = get_db()
        db.execute('DROP TABLE IF EXISTS product_branch_stock')
        db.commit()
        init_db()
        columns = [row[1] for row in db.execute('PRAGMA table_info(product_branch_stock)').fetchall()]
    assert columns == ['id', 'product_id', 'branch_id', 'quantity', 'updated_at']
    with app.app_context():
        init_db()  # second run must not raise


def test_calendar_availability_batches_its_queries(client, app):
    """The calendar must not run a query per rental product.

    It used to run two per product, so a live page view (an Oregon app instance
    talking to an Ireland database) cost ~180 Turso round trips and ~30s even
    though production held three orders - the cost was round trips, not data.
    The statement count has to stay flat as the catalogue grows; a per-product
    query would push this well past the bound with a dozen products.
    """
    login(client)
    for index in range(12):
        create_product(client, name=f'Batch Trailer {index:02d}', sku=f'BATCH-{index:02d}')

    import app.services.orders as orders_mod

    counter = {'n': 0}
    real_get_db = orders_mod.get_db

    class CountingConnection:
        """Counts statements while behaving exactly like the real connection."""

        def __init__(self, conn):
            self._conn = conn

        def execute(self, *args, **kwargs):
            counter['n'] += 1
            return self._conn.execute(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    orders_mod.get_db = lambda: CountingConnection(real_get_db())
    try:
        with app.app_context():
            availability = calendar_group_availability(start_date='2026-07-01', end_date='2026-07-01')
    finally:
        orders_mod.get_db = real_get_db

    listed = sum(len(group['products']) for group in availability['groups'])
    assert listed >= 12, listed
    # 1 products query + 3 per chunk (stock rows, booked totals, reservation rows).
    # The lower bound keeps this from passing vacuously if the counter ever stops
    # wrapping the connection.
    assert 2 <= counter['n'] <= 5, f"{counter['n']} statements for {listed} products - batching regressed"
