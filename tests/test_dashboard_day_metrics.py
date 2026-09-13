"""The dashboard's "for the day" cards (ticket ABI-341952946, items 2-8, 10, 11).

The client asked for the dashboard to report the day's trading by payment
method, reservations, pick ups and trailer movement, and to rename two existing
cards. These tests pin the *definitions* — "created today", "picked up today",
"rental trailers only, excluding the Other Rental Products group" — because the
numbers are only trustworthy if the wording on the card matches the query behind
it.

All fixtures use a fixed business day so nothing depends on the wall clock.
"""
import os
import tempfile

import pytest
from flask import session

from app import create_app
from app.db import get_db
from app.services.orders import transition_order
from app.services.reports import dashboard_day_metrics

TODAY = '2026-09-13'
YESTERDAY = '2026-09-12'

HIRE_GROUP = 'Utility Trailer'
OTHER_GROUP = 'Other Rental Products'


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


def _seed_catalogue(db):
    """One hire group, the excluded group, and a service + sale product.

    The hire group carries a realistic yard (10 units on one row) and the
    excluded group a deliberately larger count, so the fleet assertion proves the
    "Other Rental Products" units are not counted as trailers.
    """
    def group(name):
        db.execute(
            "INSERT INTO product_groups (name, description, active, sort_order, created_at, updated_at)"
            " VALUES (?, '', 1, 0, ?, ?)", (name, TODAY, TODAY))
        return db.execute("SELECT id FROM product_groups WHERE name = ?", (name,)).fetchone()['id']

    def product(name, product_type, group_id=None, quantity=1):
        db.execute(
            """INSERT INTO products (name, product_type, description, sku, active, public_visible,
            price_amount, price_unit, security_deposit, hourly_extra_rate, product_group_id, quantity,
            tracking_method, branch_id, created_at)
            VALUES (?, ?, '', '', 1, 1, 200, 'day', 0, 0, ?, ?, 'bulk', NULL, ?)""",
            (name, product_type, group_id, quantity, TODAY))
        return db.execute("SELECT id FROM products WHERE name = ?", (name,)).fetchone()['id']

    hire_group = group(HIRE_GROUP)
    other_group = group(OTHER_GROUP)
    return {
        'hire': product('2.6m Utility Trailer - 1\u20442 ton', 'rental', hire_group, quantity=10),
        'extra': product('Ratchet + Strap Rental', 'rental', other_group, quantity=25),
        'service': product('Damage Waiver', 'service', None),
        'sale': product('Tow Ball', 'sale', hire_group, quantity=6),
    }


def _order(db, number, status, created_at, branch_id=1, picked_up_at=None, start_at=None,
           end_at=None):
    db.execute(
        """INSERT INTO orders (order_number, booking_type, collect_branch_id, return_branch_id,
        status, payment_status, start_at, end_at, subtotal, tax_total, deposit_total, total,
        due_total, notes, picked_up_at, created_at)
        VALUES (?, 'return', ?, ?, ?, 'paid', ?, ?, 0, 0, 0, 0, 0, '', ?, ?)""",
        (number, branch_id, branch_id, status, start_at, end_at, picked_up_at, created_at))
    return db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()['id']


def _item(db, order_id, product_id, quantity=1):
    db.execute(
        """INSERT INTO order_items (order_id, product_id, custom_name, quantity, unit_price,
        line_subtotal, line_tax, line_total, billing_mode)
        VALUES (?, ?, '', ?, 200, 200, 30, 230, 'catalog')""",
        (order_id, product_id, quantity))


def _payment(db, order_id, amount, method, payment_date, status='paid', deleted_at='',
             created_at=None):
    db.execute(
        """INSERT INTO payments (order_id, amount, method, reference, status, payment_date,
        deleted_at, created_at) VALUES (?, ?, ?, '', ?, ?, ?, ?)""",
        (order_id, amount, method, status, payment_date, deleted_at, created_at or payment_date))


def seed_days(app):
    """A day of trading plus enough history to prove the date windows."""
    with app.app_context():
        db = get_db()
        products = _seed_catalogue(db)

        # Today: started / draft / reserved / returned / archived.
        started = _order(db, 'ORD-1', 'started', f'{TODAY}T09:00:00', picked_up_at=f'{TODAY}T09:05:00')
        _item(db, started, products['hire'], 2)
        draft = _order(db, 'ORD-2', 'draft', f'{TODAY}T09:30:00')
        _item(db, draft, products['hire'], 1)
        _item(db, draft, products['extra'], 4)          # excluded group
        reserved = _order(db, 'ORD-3', 'reserved', f'{TODAY}T10:00:00')
        _item(db, reserved, products['hire'], 1)
        returned = _order(db, 'ORD-9', 'returned', f'{TODAY}T08:00:00', branch_id=2)
        _item(db, returned, products['hire'], 1)
        archived = _order(db, 'ORD-8', 'archived', f'{TODAY}T07:00:00')
        _item(db, archived, products['hire'], 5)

        # History: reserved earlier, collected today (the proxy + the real stamp).
        collected_today = _order(db, 'ORD-4', 'started', f'{YESTERDAY}T09:00:00',
                                 picked_up_at=f'{TODAY}T08:30:00')
        _item(db, collected_today, products['hire'], 3)
        # No stamp at all: falls back to its scheduled pickup (documented proxy).
        proxy = _order(db, 'ORD-7', 'started', f'{YESTERDAY}T09:00:00',
                       start_at=f'{TODAY}T11:00')
        _item(db, proxy, products['hire'], 1)
        # Collected on another day, and an untouched reservation from yesterday.
        collected_before = _order(db, 'ORD-5', 'started', f'{YESTERDAY}T09:00:00',
                                  picked_up_at=f'{YESTERDAY}T09:10:00')
        _item(db, collected_before, products['extra'], 2)
        _order(db, 'ORD-6', 'reserved', f'{YESTERDAY}T09:00:00')

        # Payments: one of each method today, plus three that must not count.
        _payment(db, started, 500.0, 'card', f'{TODAY}T09:10:00')
        _payment(db, started, 250.0, 'Cash', f'{TODAY}T09:20:00')      # case-insensitive
        _payment(db, draft, 1000.0, 'eft', f'{TODAY}T09:40:00', status='pending')
        _payment(db, collected_today, 300.0, 'card', f'{TODAY}T08:35:00', deleted_at=f'{TODAY}T08:40:00')
        _payment(db, collected_today, 700.0, 'eft', '', created_at=f'{TODAY}T08:36:00')
        _payment(db, collected_before, 99.0, 'card', f'{YESTERDAY}T08:00:00')

        # Customers created today vs earlier.
        for index in range(2):
            db.execute("INSERT INTO customers (name, created_at) VALUES (?, ?)",
                       (f'Today Customer {index}', f'{TODAY}T0{index + 8}:00:00'))
        db.execute("INSERT INTO customers (name, created_at) VALUES (?, ?)",
                   ('Yesterday Customer', f'{YESTERDAY}T08:00:00'))
        db.commit()
    return products


def metrics(app, **session_values):
    with app.test_request_context():
        session.clear()
        session.update(session_values)
        return dashboard_day_metrics(day=TODAY)


def test_the_day_cards_count_what_the_client_asked_for(app):
    seed_days(app)
    day = metrics(app, user_id=1, user_role='owner')

    assert day['day'] == TODAY
    # New orders = started today. Draft, reserved and archived are not orders yet.
    assert day['orders'] == 2
    assert day['customers'] == 2
    assert day['reservations'] == 1
    # Reserved earlier, collected today: the real stamp and the schedule proxy.
    assert day['reservation_pickups'] == 2
    # Payments by method, for the day, paid only and never archived.
    assert day['card_payments'] == 500.0
    assert day['cash_payments'] == 250.0
    assert day['eft_payments'] == 700.0


def test_trailer_cards_count_rental_items_and_skip_the_other_rental_group(app):
    seed_days(app)
    day = metrics(app, user_id=1, user_role='owner')

    # Out: started orders only (ORD-1 x2, ORD-4 x3, ORD-7 x1); the Other Rental
    # Products line, the draft, the reservation, the returned and the archived
    # order are all excluded.
    assert day['trailers_out'] == 6
    # The fleet is the hire group's 10 units — not the 25 ratchet/strap units in
    # the excluded group, and not the service or the sale item.
    assert day['fleet'] == 10
    assert day['on_hire'] == 6
    # In = the rest of the yard. A draft and a reservation are still standing in
    # it (nothing has been collected), and a returned trailer is back in it too.
    assert day['trailers_in'] == 4
    assert isinstance(day['fleet'], int) and isinstance(day['on_hire'], int)


def test_trailers_in_is_the_whole_yard_less_what_is_on_hire(app):
    """The card is a snapshot: whole fleet minus the trailers currently out."""
    with app.app_context():
        db = get_db()
        products = _seed_catalogue(db)
        order_id = _order(db, 'ORD-40', 'reserved', f'{TODAY}T09:00:00',
                          start_at='2030-01-02T09:00', end_at='2030-01-03T09:00')
        _item(db, order_id, products['hire'], 3)
        db.commit()
    day = metrics(app, user_id=1, user_role='owner')
    # Reserved but not collected: nothing has left the yard yet.
    assert day['trailers_out'] == 0
    assert day['trailers_in'] == 10


def test_trailers_in_never_goes_negative(app):
    """More on hire than on the books shows 0, never a negative count."""
    with app.app_context():
        db = get_db()
        products = _seed_catalogue(db)
        db.execute("UPDATE products SET quantity = 1 WHERE id = ?", (products['hire'],))
        order_id = _order(db, 'ORD-41', 'started', f'{TODAY}T09:00:00')
        _item(db, order_id, products['hire'], 5)
        db.commit()
    day = metrics(app, user_id=1, user_role='owner')
    assert day['fleet'] == 1
    assert day['on_hire'] == 5
    assert day['trailers_in'] == 0


def test_the_fleet_follows_the_branch_scope(app):
    """A depot-scoped account counts its own yard (plus unassigned stock)."""
    with app.app_context():
        db = get_db()
        _seed_catalogue(db)
        group_id = db.execute("SELECT id FROM product_groups WHERE name = ?",
                              (HIRE_GROUP,)).fetchone()['id']
        db.execute(
            """INSERT INTO products (name, product_type, description, sku, active, public_visible,
            price_amount, price_unit, security_deposit, hourly_extra_rate, product_group_id, quantity,
            tracking_method, branch_id, created_at)
            VALUES ('Depot Two Trailer', 'rental', '', '', 1, 1, 200, 'day', 0, 0, ?, 3, 'bulk', 2, ?)""",
            (group_id, TODAY))
        db.commit()
    all_branches = metrics(app, user_id=1, user_role='owner')
    depot_two = metrics(app, user_id=2, user_role='staff', can_view_all_branches=False,
                        branch_id=2, branch_ids=[2])
    depot_one = metrics(app, user_id=3, user_role='staff', can_view_all_branches=False,
                        branch_id=1, branch_ids=[1])
    assert all_branches['fleet'] == 13
    assert depot_two['fleet'] == 13      # its own 3 units + the 10 unassigned
    assert depot_one['fleet'] == 10      # never the other depot's 3 units


def test_a_service_or_sale_line_is_never_a_trailer(app):
    with app.app_context():
        db = get_db()
        products = _seed_catalogue(db)
        order_id = _order(db, 'ORD-20', 'started', f'{TODAY}T09:00:00')
        _item(db, order_id, products['service'], 3)
        _item(db, order_id, products['sale'], 3)
        db.commit()
    day = metrics(app, user_id=1, user_role='owner')
    assert day['trailers_out'] == 0


def test_the_day_cards_follow_the_branch_scope(app):
    seed_days(app)
    # Branch 2 only holds ORD-9, so a depot-scoped account must not see the rest.
    scoped = metrics(app, user_id=2, user_role='staff', can_view_all_branches=False,
                     branch_id=2, branch_ids=[2])
    assert scoped['orders'] == 1
    assert scoped['reservations'] == 0
    assert scoped['card_payments'] == 0.0
    assert scoped['trailers_out'] == 0
    # ...and the active branch narrows it further without hiding its own depot.
    other = metrics(app, user_id=2, user_role='staff', can_view_all_branches=False,
                    branch_id=2, branch_ids=[2, 1], active_branch_id=1)
    assert other['orders'] == 1          # ORD-1 (started today, branch 1)
    assert other['reservations'] == 1
    assert other['card_payments'] == 500.0
    assert other['trailers_out'] == 6


def test_starting_an_order_records_when_it_was_collected(app):
    with app.app_context():
        db = get_db()
        products = _seed_catalogue(db)
        db.execute("INSERT INTO customers (name, created_at) VALUES ('Collect Customer', ?)", (TODAY,))
        customer_id = db.execute("SELECT id FROM customers ORDER BY id DESC LIMIT 1").fetchone()['id']
        order_id = _order(db, 'ORD-30', 'reserved', f'{TODAY}T09:00:00',
                          start_at='2030-01-02T09:00', end_at='2030-01-03T09:00')
        db.execute("UPDATE orders SET customer_id = ? WHERE id = ?", (customer_id, order_id))
        _item(db, order_id, products['hire'], 1)
        db.commit()
        assert db.execute("SELECT picked_up_at FROM orders WHERE id = ?",
                          (order_id,)).fetchone()['picked_up_at'] is None
        transition_order(order_id, 'start')
        stamped = db.execute("SELECT picked_up_at FROM orders WHERE id = ?",
                             (order_id,)).fetchone()['picked_up_at']
    assert stamped and stamped[:10]


def test_the_dashboard_renders_the_renamed_and_new_cards(client, app):
    seed_days(app)
    login(client)
    page = client.get('/dashboard').data
    for label in (b'New orders for the day', b'New customers for the day', b'Revenue for the day',
                  b'Total card payments', b'Total cash payments', b'Total EFT payments',
                  b'Reservations for the day', b'Reservation pick ups for the day',
                  b'Total no. of trailers out', b'Total no. of trailers in'):
        assert label in page, label
    # The two cards that were renamed are gone under their old names.
    assert b'Customer base for the day' not in page
    assert b'Order for the day' not in page


def test_restricted_staff_see_the_same_day_cards(client, app):
    seed_days(app)
    login(client)
    client.post('/settings/users/permissions', data={'module': ['dashboard']}, follow_redirects=True)
    client.post('/settings/users/add', data={'name': 'Dashboard Staff', 'password': 'staff123'},
                follow_redirects=True)
    client.post('/logout')
    login(client, 'Dashboard Staff', 'staff123')
    page = client.get('/dashboard').data
    assert b'Total card payments' in page
    assert b'Total no. of trailers out' in page
    assert b'Total orders' not in page  # the totals row stays main-profile only
