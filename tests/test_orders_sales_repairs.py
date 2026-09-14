"""The Orders "Sales/Repairs" status (tickets ABI-341952960 + ABI-341952962).

Original ask (ABI-341952960): "order page: introduce a button order status
SALES/REPAIRS, this would be applicable to orders that don't have rental items but
have sales, service or custom."

Follow-up (ABI-341952962) made it a REAL stored status: "after Sales/Repairs is
selected, change the status from draft to Sales/Repairs... the draft status must
still be applicable until the sales/repairs button is selected, 'Save as draft'
must still remain even if it's sales/repairs."

So the folder is no longer derived from the order lines — selecting the button
writes ``orders.status = 'sales_repairs'``, an unmarked sale/service/custom order
is still a plain draft, and a Sales/Repairs order can be saved back to draft.

These tests pin: the folder/rail/metric agree on the stored status, draft stays
valid, the detail action marks and un-marks, the form's "Save as Sales/Repairs"
button stores the status, a rental order can never be marked (even by a crafted
POST), the badge only shows for a marked order, and the two long-standing rules
on the Orders page — the branch filter narrows, a branch-limited account cannot
widen — still hold.
"""
import os
import re
import tempfile

import pytest
from werkzeug.datastructures import MultiDict

from app import create_app
from app.db import get_db
from app.services.access import create_additional_user
from app.services.orders import (
    SALES_REPAIRS_STATUS,
    list_orders,
    order_filter_counts,
)

CREATED_AT = '2026-09-14T08:00:00'
START_AT = '2026-09-14T08:00:00'
END_AT = '2026-09-20T08:00:00'


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


def _order(db, number, status='draft', branch_id=1):
    db.execute(
        """INSERT INTO orders (order_number, booking_type, collect_branch_id, return_branch_id,
        status, payment_status, start_at, end_at, subtotal, tax_total, deposit_total, total,
        due_total, notes, created_at)
        VALUES (?, 'return', ?, ?, ?, 'payment_due', ?, ?, 0, 0, 0, 0, 0, '', ?)""",
        (number, branch_id, branch_id, status, START_AT, END_AT, CREATED_AT),
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
    """Marked Sales/Repairs orders, unmarked sale drafts, and the shapes that
    must never be sales/repairs (rental, mixed, custom-by-rental-day, empty)."""
    with app.app_context():
        db = get_db()
        rental = _product(db, 'Utility Trailer', 'rental')
        sale = _product(db, 'Tow Ball', 'sale')
        service = _product(db, 'Brake Repair', 'service')

        marked_sale = _order(db, 'ORD-9001', status=SALES_REPAIRS_STATUS)
        _item(db, marked_sale, sale)

        marked_service = _order(db, 'ORD-9002', status=SALES_REPAIRS_STATUS, branch_id=2)
        _item(db, marked_service, service)

        marked_custom = _order(db, 'ORD-9003', status=SALES_REPAIRS_STATUS)
        _item(db, marked_custom, None, billing_mode='fixed', custom_name='Workshop labour')

        rental_only = _order(db, 'ORD-9004', status='started')
        _item(db, rental_only, rental)

        mixed = _order(db, 'ORD-9005', status='started')
        _item(db, mixed, rental)
        _item(db, mixed, sale)

        _order(db, 'ORD-9006')                                  # no lines at all

        custom_rental_day = _order(db, 'ORD-9007')
        _item(db, custom_rental_day, None, billing_mode='rental_day', custom_name='Hire day rate')

        _order(db, 'ORD-9008')                                  # empty draft

        unmarked_sale = _order(db, 'ORD-9009')                  # sale line, still a draft
        _item(db, unmarked_sale, sale)

        rental_draft = _order(db, 'ORD-9010')                   # rental line, still a draft
        _item(db, rental_draft, rental)

        db.commit()
    return {'rental': rental, 'sale': sale, 'service': service,
            'marked_sale': marked_sale, 'marked_service': marked_service,
            'marked_custom': marked_custom, 'unmarked_sale': unmarked_sale,
            'rental_draft': rental_draft}


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


def order_id_for(app, order_number):
    with app.app_context():
        row = get_db().execute(
            "SELECT id FROM orders WHERE order_number = ?", (order_number,)
        ).fetchone()
        assert row, f'order {order_number} was not seeded'
        return row['id']


def stored_status(app, order_number):
    with app.app_context():
        return get_db().execute(
            "SELECT status FROM orders WHERE order_number = ?", (order_number,)
        ).fetchone()['status']


SALES_REPAIRS_BADGE = (
    '<a class="btn ghost" href="/orders?status=sales_repairs" '
    'title="View all sales and repairs orders">Sales/Repairs</a>'
)

SALES_REPAIRS_ACTION = (
    '<form method="post" action="/orders/{order_id}/sales_repairs" class="inline-form">\n'
    '          <button class="btn ghost" type="submit">Sales/Repairs</button>'
)


def detail_body(client, app, order_number):
    response = client.get(f'/orders/{order_id_for(app, order_number)}')
    assert response.status_code == 200
    return response.get_data(as_text=True)


def edit_payload(app, order_number, product_id, order_action=''):
    """A valid Edit-order POST payload with one catalogue line."""
    with app.app_context():
        order = get_db().execute("SELECT * FROM orders WHERE order_number = ?",
                                 (order_number,)).fetchone()
        start_at, end_at = order['start_at'], order['end_at']
    return MultiDict([
        ('customer_id', ''),
        ('booking_type', 'return'),
        ('collect_branch_id', '1'),
        ('return_branch_id', '1'),
        ('start_date', start_at[:10]),
        ('start_time', start_at[11:16]),
        ('end_date', end_at[:10]),
        ('end_time', end_at[11:16]),
        ('product_id', str(product_id)),
        ('quantity', '1'),
        ('custom_name', ''),
        ('custom_unit_price', ''),
        ('custom_billing_mode', 'fixed'),
        ('deposit_option', 'no_deposit'),
        ('notes', ''),
        ('order_action', order_action),
    ])


# --- what the folder contains ------------------------------------------------

def test_sales_repairs_view_lists_only_marked_orders(client, app):
    seed_orders(app)
    login(client)
    body = client.get('/orders?status=sales_repairs').get_data(as_text=True)

    assert listed_orders(body) == ['ORD-9003', 'ORD-9002', 'ORD-9001']
    # An UNMARKED sale draft stays a draft, and rental / mixed / rental-day-custom
    # / empty orders are never sales/repairs.
    for number in ('ORD-9009', 'ORD-9004', 'ORD-9005', 'ORD-9006', 'ORD-9007', 'ORD-9008'):
        assert number not in body, f'{number} is not a sales/repairs order'
    # The list total and the rail badge agree with the rows actually returned.
    assert metric_orders(body) == 3
    assert rail_count(body, SALES_REPAIRS_STATUS) == 3


def test_the_stored_status_is_what_the_folder_and_the_badges_read(app):
    seed_orders(app)
    with app.app_context():
        marked = {row['order_number'] for row in list_orders(status=SALES_REPAIRS_STATUS)}
        assert marked == {'ORD-9001', 'ORD-9002', 'ORD-9003'}
        assert order_filter_counts()['status'][SALES_REPAIRS_STATUS] == 3
        # The badge is 0 for a status nobody has selected yet.
        assert order_filter_counts()['status'].get('reserved', 0) == 0


def test_draft_stays_apply_until_sales_repairs_is_selected(client, app):
    """The clarification, end to end: unmarked sale orders are still drafts."""
    seed_orders(app)
    login(client)

    drafts = listed_orders(client.get('/orders?status=draft').get_data(as_text=True))
    assert 'ORD-9009' in drafts, 'an unmarked sale order must still be a draft'
    assert 'ORD-9001' not in drafts, 'a marked order must leave the draft folder'
    # ...and a real status never picks up the marked rows.
    assert rail_count(client.get('/orders?status=draft').get_data(as_text=True), 'draft') == 5


# --- the status actions -----------------------------------------------------

def test_marking_a_draft_stores_the_sales_repairs_status(client, app):
    ids = seed_orders(app)
    login(client)
    order_id = ids['unmarked_sale']

    response = client.post(f'/orders/{order_id}/sales_repairs', follow_redirects=True)
    assert response.status_code == 200
    assert stored_status(app, 'ORD-9009') == SALES_REPAIRS_STATUS
    body = client.get('/orders?status=sales_repairs').get_data(as_text=True)
    assert listed_orders(body) == ['ORD-9009', 'ORD-9003', 'ORD-9002', 'ORD-9001']


def test_save_as_draft_remains_on_a_sales_repairs_order(client, app):
    """'Save as draft must still remain even if its sales/repairs'."""
    ids = seed_orders(app)
    login(client)
    order_id = ids['marked_sale']

    body = client.get(f'/orders/{order_id}').get_data(as_text=True)
    assert f'<form method="post" action="/orders/{order_id}/draft" class="inline-form">' in body
    assert 'Save as draft</button>' in body

    client.post(f'/orders/{order_id}/draft', follow_redirects=True)
    assert stored_status(app, 'ORD-9001') == 'draft'
    assert 'ORD-9001' in listed_orders(client.get('/orders?status=draft').get_data(as_text=True))


def test_a_sales_repairs_order_can_be_archived_once_the_sale_is_done(client, app):
    ids = seed_orders(app)
    login(client)
    client.post(f"/orders/{ids['marked_custom']}/archive", follow_redirects=True)
    assert stored_status(app, 'ORD-9003') == 'archived'


def test_a_rental_order_is_never_offered_or_given_the_status(client, app):
    """The button is hidden for a hire order AND a crafted POST is refused."""
    ids = seed_orders(app)
    login(client)

    body = client.get(f"/orders/{ids['rental_draft']}/edit").get_data(as_text=True)
    assert 'Save as Sales/Repairs' not in body

    detail = client.get(f"/orders/{ids['rental_draft']}").get_data(as_text=True)
    assert f"/orders/{ids['rental_draft']}/sales_repairs" not in detail

    response = client.post(f"/orders/{ids['rental_draft']}/sales_repairs", follow_redirects=True)
    assert response.status_code == 200
    assert stored_status(app, 'ORD-9010') == 'draft'
    assert b'Sales/Repairs applies to orders without rental items' in response.data


def test_the_marked_status_is_what_shows_the_detail_badge(client, app):
    ids = seed_orders(app)
    login(client)

    marked = client.get(f"/orders/{ids['marked_service']}").get_data(as_text=True)
    assert SALES_REPAIRS_BADGE in marked
    assert 'Sales/Repairs</b>' in marked      # the Status row shows the stored status

    unmarked = client.get(f"/orders/{ids['unmarked_sale']}").get_data(as_text=True)
    assert SALES_REPAIRS_BADGE not in unmarked
    assert 'Sales/Repairs</button>' in unmarked, 'the action is offered instead'


def test_the_detail_action_renders_on_an_unmarked_sale_order_for_main_and_staff(client, app):
    ids = seed_orders(app)
    with app.app_context():
        create_additional_user('Depot Two Clerk', 'staff123', branch_id=2)

    login(client)
    assert SALES_REPAIRS_ACTION.format(order_id=ids['unmarked_sale']) in detail_body(client, app, 'ORD-9009')

    login(client, name='Depot Two Clerk', password='staff123')
    body = detail_body(client, app, 'ORD-9002')
    assert SALES_REPAIRS_BADGE in body, 'their own depot\'s marked order'


def test_branch_limited_staff_order_detail_guard_still_blocks_other_depots_orders(client, app):
    seed_orders(app)
    with app.app_context():
        create_additional_user('Depot Two Clerk', 'staff123', branch_id=2)
    login(client, name='Depot Two Clerk', password='staff123')

    own_response = client.get(f'/orders/{order_id_for(app, "ORD-9002")}')
    other_depot_response = client.get(f'/orders/{order_id_for(app, "ORD-9001")}')

    assert own_response.status_code == 200
    assert other_depot_response.status_code == 404


# --- the order form's "Save as Sales/Repairs" button -------------------------

def test_the_form_offers_save_as_sales_repairs_and_stores_it(client, app):
    ids = seed_orders(app)
    login(client)

    form = client.get(f"/orders/{ids['unmarked_sale']}/edit").get_data(as_text=True)
    assert 'Save as draft' not in form               # edit form keeps "Save order changes"
    assert 'Save as Sales/Repairs' in form

    response = client.post(
        f"/orders/{ids['unmarked_sale']}/edit",
        data=edit_payload(app, 'ORD-9009', ids['sale'], order_action=SALES_REPAIRS_STATUS),
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert stored_status(app, 'ORD-9009') == SALES_REPAIRS_STATUS
    assert b'Order saved as Sales/Repairs' in response.data


def test_the_form_without_the_button_saves_a_plain_draft(client, app):
    ids = seed_orders(app)
    login(client)
    response = client.post(
        f"/orders/{ids['unmarked_sale']}/edit",
        data=edit_payload(app, 'ORD-9009', ids['sale']),
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert stored_status(app, 'ORD-9009') == 'draft'
    assert b'Order saved' in response.data


def test_the_new_order_form_offers_both_save_buttons(client, app):
    seed_orders(app)
    login(client)
    body = client.get('/orders/new').get_data(as_text=True)
    assert 'Save as draft' in body
    assert 'Save as Sales/Repairs' in body


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
    # Branch 1's marked orders are invisible, with or without the filter.
    assert 'ORD-9001' not in body
    assert 'ORD-9001' not in client.get('/orders').get_data(as_text=True)
    # And the count badge is scoped the same way as the rows.
    assert rail_count(body, SALES_REPAIRS_STATUS) == 1
