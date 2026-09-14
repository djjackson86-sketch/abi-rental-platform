"""The Orders "Sales/Repairs" view (ticket ABI-341952960).

Ticket ask: "order page: introduce a button order status SALES/REPAIRS, this would
be applicable to orders that don't have rental items but have sales, service or
custom."

"Sales/Repairs" is therefore a *derived grouping* of the orders list, not a stored
order status: an order with at least one line, where no line hires anything out.
The line-level test mirrors ``app.services.orders.order_has_rental_items`` exactly
(catalogue rental product, or a custom line billed by rental day), so the folder
label, the list and the count badges can never disagree about what a rental item is.

These tests pin the cases the ticket names — sale-only, service-only and
custom-fixed orders are in; rental-only, mixed, empty and rental-day-custom orders
are out — plus the two rules the Orders page already guarantees: the branch filter
still narrows, and a branch-limited staff account cannot widen its scope.
"""
import os
import re
import tempfile

import pytest

from app import create_app
from app.db import get_db
from app.services.access import create_additional_user
from app.services.orders import (
    SALES_REPAIRS_STATUS,
    list_orders,
    order_filter_counts,
)

CREATED_AT = '2026-09-14T08:00:00'


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


def _product(db, name, product_type):
    db.execute(
        """INSERT INTO products (name, product_type, description, sku, active, public_visible,
        price_amount, price_unit, security_deposit, hourly_extra_rate, product_group_id, quantity,
        tracking_method, branch_id, created_at)
        VALUES (?, ?, '', '', 1, 1, 200, 'day', 0, 0, NULL, 5, 'bulk', NULL, ?)""",
        (name, product_type, CREATED_AT),
    )
    return db.execute("SELECT id FROM products WHERE name = ?", (name,)).fetchone()['id']


def _order(db, number, status='started', branch_id=1):
    db.execute(
        """INSERT INTO orders (order_number, booking_type, collect_branch_id, return_branch_id,
        status, payment_status, start_at, end_at, subtotal, tax_total, deposit_total, total,
        due_total, notes, created_at)
        VALUES (?, 'return', ?, ?, ?, 'payment_due', ?, ?, 0, 0, 0, 0, 0, '', ?)""",
        (number, branch_id, branch_id, status, CREATED_AT, '2026-09-20T08:00:00', CREATED_AT),
    )
    return db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()['id']


def _item(db, order_id, product_id, billing_mode='catalog', custom_name=''):
    db.execute(
        """INSERT INTO order_items (order_id, product_id, custom_name, quantity, unit_price,
        line_subtotal, line_tax, line_total, billing_mode)
        VALUES (?, ?, ?, 1, 200, 200, 30, 230, ?)""",
        (order_id, product_id, custom_name, billing_mode),
    )


def seed_orders(app):
    """One order per shape the ticket names, plus the shapes that must stay out."""
    with app.app_context():
        db = get_db()
        rental = _product(db, 'Utility Trailer', 'rental')
        sale = _product(db, 'Tow Ball', 'sale')
        service = _product(db, 'Brake Repair', 'service')

        sale_only = _order(db, 'ORD-9001')                      # sale line, branch 1
        _item(db, sale_only, sale)

        service_only = _order(db, 'ORD-9002', branch_id=2)      # service line, branch 2
        _item(db, service_only, service)

        custom_fixed = _order(db, 'ORD-9003')                   # custom one-off, no product
        _item(db, custom_fixed, None, billing_mode='fixed', custom_name='Workshop labour')

        rental_only = _order(db, 'ORD-9004')
        _item(db, rental_only, rental)

        mixed = _order(db, 'ORD-9005')
        _item(db, mixed, rental)
        _item(db, mixed, sale)

        _order(db, 'ORD-9006')                                  # no lines at all

        custom_rental_day = _order(db, 'ORD-9007')
        _item(db, custom_rental_day, None, billing_mode='rental_day', custom_name='Hire day rate')

        _order(db, 'ORD-9008', status='draft')                  # empty draft
        db.commit()


def rail_count(body, value):
    """The count badge next to a filter-rail radio.

    Anchored on ``type="radio"`` because the search form carries a hidden
    ``name="status"`` input with the same value when a status is selected.
    """
    match = re.search(r'type="radio" name="status" value="' + value + r'"[\s\S]*?<em>\((\d+)\)</em>', body)
    assert match, f'no filter-rail count rendered for {value}'
    return int(match.group(1))


def listed_orders(body):
    return re.findall(r'<strong>(ORD-\d+)</strong>', body)


def metric_orders(body):
    match = re.search(r'class="metric-card"><small>Orders</small><b>(\d+)</b>', body)
    assert match, 'the Orders metric card was not rendered'
    return int(match.group(1))


def href_of(body, text):
    match = re.search(r'href="([^"]+)"[^>]*>' + re.escape(text) + r'</a>', body)
    assert match, f'no link labelled {text!r}'
    return match.group(1)


# --- what the folder contains ------------------------------------------------

def test_sales_repairs_view_lists_orders_without_rental_items(client, app):
    seed_orders(app)
    login(client)
    body = client.get('/orders?status=sales_repairs').get_data(as_text=True)

    assert listed_orders(body) == ['ORD-9003', 'ORD-9002', 'ORD-9001']
    # Rental-only, mixed rental+sale, custom-billed-by-rental-day and empty orders stay out.
    for number in ('ORD-9004', 'ORD-9005', 'ORD-9006', 'ORD-9007', 'ORD-9008'):
        assert number not in body, f'{number} is not a sales/repairs order'
    # The list total and the rail badge agree with the rows actually returned.
    assert metric_orders(body) == 3
    assert rail_count(body, SALES_REPAIRS_STATUS) == 3


def test_sales_repairs_clause_matches_the_line_level_helper(app):
    """The SQL predicate and order_has_rental_items() must agree on every shape."""
    from app.services.orders import order_has_rental_items, order_items

    seed_orders(app)
    with app.app_context():
        derived = {row['order_number'] for row in list_orders(status=SALES_REPAIRS_STATUS)}
        assert derived == {'ORD-9001', 'ORD-9002', 'ORD-9003'}
        assert order_filter_counts()['status'][SALES_REPAIRS_STATUS] == 3
        for row in get_db().execute('SELECT id, order_number FROM orders').fetchall():
            lines = order_items(row['id'])
            should_be_in = bool(lines) and not order_has_rental_items(lines)
            assert (row['order_number'] in derived) is should_be_in, (
                f'{row["order_number"]}: SQL and Python disagree '
                f'(has_rental={order_has_rental_items(lines)}, lines={len(lines)})'
            )
        # The metric cards are rebuilt from the same where-clause; they must agree too.
        from app.services.orders import order_counts
        assert order_counts(status=SALES_REPAIRS_STATUS)['total'] == 3


# --- the stored-status path is untouched ------------------------------------

def test_a_stored_status_filter_still_uses_the_stored_status(client, app):
    seed_orders(app)
    login(client)
    body = client.get('/orders?status=draft').get_data(as_text=True)
    # An empty draft carries no lines, so it can only appear via the real status filter.
    assert 'ORD-9008' in body
    # ...and a real status never picks up the derived grouping's rows.
    assert 'ORD-9001' not in body
    assert rail_count(body, 'draft') == 1


# --- the button itself ------------------------------------------------------

def test_sales_repairs_button_renders_for_main_and_staff(client, app):
    seed_orders(app)
    with app.app_context():
        create_additional_user('Depot Two Clerk', 'staff123', branch_id=2)

    login(client)
    main_body = client.get('/orders').get_data(as_text=True)
    assert '<a class="" href="/orders?status=sales_repairs">Sales/Repairs</a>' in main_body
    assert 'value="sales_repairs"' in main_body

    login(client, name='Depot Two Clerk', password='staff123')
    staff_body = client.get('/orders').get_data(as_text=True)
    assert '<a class="" href="/orders?status=sales_repairs">Sales/Repairs</a>' in staff_body
    assert 'value="sales_repairs"' in staff_body


def test_the_sales_repairs_button_shows_active_and_clears_back_to_all(client, app):
    seed_orders(app)
    login(client)
    body = client.get('/orders?status=sales_repairs').get_data(as_text=True)
    assert '<a class="active" href="/orders?status=sales_repairs">Sales/Repairs</a>' in body
    assert '<a class="active" href="/orders">All</a>' not in body
    assert 'Showing filtered orders' in body

    cleared = client.get(href_of(body, 'Clear status')).get_data(as_text=True)
    assert '<a class="active" href="/orders">All</a>' in cleared
    assert 'Showing filtered orders' not in cleared
    assert listed_orders(cleared) == ['ORD-9008', 'ORD-9007', 'ORD-9006', 'ORD-9005',
                                      'ORD-9004', 'ORD-9003', 'ORD-9002', 'ORD-9001']


# --- the branch rules still hold --------------------------------------------

def test_sales_repairs_respects_the_branch_filter_and_ignores_junk(client, app):
    seed_orders(app)
    login(client)
    branch_two = client.get('/orders?status=sales_repairs&branch=2').get_data(as_text=True)
    assert listed_orders(branch_two) == ['ORD-9002']
    assert rail_count(branch_two, SALES_REPAIRS_STATUS) == 1
    assert metric_orders(branch_two) == 1

    for junk in ('999', 'abc', '-1'):
        body = client.get(f'/orders?status=sales_repairs&branch={junk}').get_data(as_text=True)
        assert listed_orders(body) == ['ORD-9003', 'ORD-9002', 'ORD-9001'], junk


def test_branch_limited_staff_cannot_see_another_depots_sales_repairs(client, app):
    seed_orders(app)
    with app.app_context():
        create_additional_user('Depot Two Clerk', 'staff123', branch_id=2)
    login(client, name='Depot Two Clerk', password='staff123')

    body = client.get('/orders?status=sales_repairs').get_data(as_text=True)
    assert listed_orders(body) == ['ORD-9002']
    # Branch 1's sale-only order is invisible, with or without the filter.
    assert 'ORD-9001' not in body
    assert 'ORD-9001' not in client.get('/orders').get_data(as_text=True)
    # And the count badge is scoped the same way as the rows.
    assert rail_count(body, SALES_REPAIRS_STATUS) == 1
