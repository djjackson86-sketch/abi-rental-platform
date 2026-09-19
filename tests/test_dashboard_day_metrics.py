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
import re
import tempfile

import pytest
from flask import session

from app import create_app
from app.db import get_db
from app.services.orders import transition_order
from app.services.reports import dashboard_day_metrics
from app.services.timezone import local_now_iso

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

        # Customers created today vs earlier, each filed under the branch that
        # added it (ticket ABI-341952962: "New customers for the day = customers
        # added by that particular branch"). The yesterday row carries no branch:
        # legacy/imported rows belong to no depot.
        for index in range(2):
            db.execute("INSERT INTO customers (name, branch_id, created_at) VALUES (?, 1, ?)",
                       (f'Today Customer {index}', f'{TODAY}T0{index + 8}:00:00'))
        db.execute("INSERT INTO customers (name, branch_id, created_at) VALUES ('Today Depot Two Customer', 2, ?)",
                   (f'{TODAY}T09:30:00',))
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
    # Customers ADDED today: 2 at depot 1 + 1 at depot 2 (ticket ABI-341952962).
    assert day['customers'] == 3
    assert day['reservations'] == 1
    # Reserved earlier, collected today: the real stamp and the schedule proxy.
    assert day['reservation_pickups'] == 2
    # Payments by method, for the day, paid only and never archived.
    assert day['card_payments'] == 500.0
    assert day['cash_payments'] == 250.0
    assert day['eft_payments'] == 700.0
    # Revenue for the day is money RECEIVED today, not the value of the orders
    # raised today: 500 card + 250 cash + 700 EFT. The pending payment on the
    # draft, the archived payment and yesterday's card payment are all out.
    assert day['revenue'] == 1450.0
    assert day['revenue'] == day['card_payments'] + day['cash_payments'] + day['eft_payments']


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
    # A depot's figures are its own: depot 2 has no takings and only the one
    # customer it added today.
    assert scoped['revenue'] == 0.0
    assert scoped['customers'] == 1
    # ...and the active branch narrows it further without hiding its own depot.
    other = metrics(app, user_id=2, user_role='staff', can_view_all_branches=False,
                    branch_id=2, branch_ids=[2, 1], active_branch_id=1)
    assert other['orders'] == 1          # ORD-1 (started today, branch 1)
    assert other['reservations'] == 1
    assert other['card_payments'] == 500.0
    assert other['trailers_out'] == 6
    assert other['revenue'] == 1450.0
    assert other['customers'] == 2


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
    # The quick ranges belong to that same main-profile row.
    assert b'Quick ranges' not in page
    assert b'report-preset' not in page
    assert b'name="range"' not in page


# --- the branch a customer was created at (ticket ABI-341952962) -------------

def day_card(app, day, **session_values):
    with app.test_request_context():
        session.clear()
        session.update(session_values)
        return dashboard_day_metrics(day=day)


def test_a_created_customer_is_filed_under_the_branch_that_created_it(client, app):
    """The day card counts customers ADDED by the branch, from real sign-ins.

    A depot account's customer lands on its depot; head office (every branch, but
    attached to branch 1 as the order form's default) lands on branch 1. A legacy
    or imported row with no branch belongs to nobody's depot figure.
    """
    login(client)
    client.post('/settings/users/permissions', data={'module': ['dashboard', 'customers']},
                follow_redirects=True)
    client.post('/settings/users/add',
                data={'name': 'Depot Two Clerk', 'password': 'staff123', 'branch_id': '2'},
                follow_redirects=True)

    # A depot account creating a customer files it under ITS depot...
    client.post('/logout')
    login(client, 'Depot Two Clerk', 'staff123')
    client.post('/customers/new',
                data={'name': 'Depot Two Walk In', 'customer_type': 'individual'},
                follow_redirects=True)
    # ...while head office, which may see every branch but is attached to branch 1
    # (the same default its new orders are filed under), files it there.
    client.post('/logout')
    login(client)
    client.post('/customers/new',
                data={'name': 'Head Office Walk In', 'customer_type': 'individual'},
                follow_redirects=True)

    with app.app_context():
        rows = {row['name']: row['branch_id'] for row in get_db().execute(
            "SELECT name, branch_id FROM customers WHERE name LIKE '% Walk In'").fetchall()}
    assert rows == {'Depot Two Walk In': 2, 'Head Office Walk In': 1}

    today = local_now_iso(timespec='seconds')[:10]
    depot_two = day_card(app, today, user_id=2, user_role='staff',
                         can_view_all_branches=False, branch_id=2, branch_ids=[2])
    everything = day_card(app, today, user_id=1, user_role='owner')
    # Only the depot's own customer counts for it; an unrestricted view counts both.
    assert depot_two['customers'] == 1
    assert everything['customers'] == 2


# --- the dashboard's own branch filter + the branded greeting ---------------
# (ticket ABI-341952954: a modern dashboard with the client's logo, the signed-in
# name, and a branch filter on the main profile.)

def day_cards(app, filter_branch=None, **session_values):
    """``dashboard_day_metrics`` with the branch filter the dashboard passes.

    ``filter_branch`` is the UI filter (``?branch=``); a session's own
    ``branch_id`` travels in ``session_values`` as usual.
    """
    with app.test_request_context():
        session.clear()
        session.update(session_values)
        return dashboard_day_metrics(day=TODAY, branch_id=filter_branch)


def today_depot_orders(app, number_a='ORD-90001', number_b='ORD-90002'):
    """One reservation per depot, dated the REAL business day.

    ``dashboard_day_metrics`` compares against the live business day, so a
    page-level day-card assertion cannot use the fixed fixture date.
    """
    today = local_now_iso()[:10]
    with app.app_context():
        db = get_db()
        one = _order(db, number_a, 'reserved', f'{today}T08:00:00', branch_id=1,
                     start_at=f'{today}T09:00', end_at=f'{today}T17:00')
        _payment(db, one, 500.0, 'card', f'{today}T09:10:00')
        _order(db, number_b, 'reserved', f'{today}T08:30:00', branch_id=2,
               start_at=f'{today}T10:00', end_at=f'{today}T18:00')
        db.commit()
    return number_a, number_b


def branch_product(app, name, branch_id, quantity):
    with app.app_context():
        db = get_db()
        group_id = db.execute("SELECT id FROM product_groups WHERE name = ?",
                              (HIRE_GROUP,)).fetchone()['id']
        db.execute(
            """INSERT INTO products (name, product_type, description, sku, active, public_visible,
            price_amount, price_unit, security_deposit, hourly_extra_rate, product_group_id, quantity,
            tracking_method, branch_id, created_at)
            VALUES (?, 'rental', '', '', 1, 1, 200, 'day', 0, 0, ?, ?, 'bulk', ?, ?)""",
            (name, group_id, quantity, branch_id, TODAY))
        db.commit()


def owner_session(**extra):
    values = {'user_id': 1, 'user_role': 'owner'}
    values.update(extra)
    return values


def filter_form(page):
    """The dashboard's own branch filter form, not the cash-up write forms."""
    match = re.search(r'<form class="dashboard-branch-filter".*?</form>', page, re.S)
    assert match, 'the dashboard branch filter form is missing'
    return match.group(0)


def test_the_branch_filter_narrows_every_day_card(app):
    """?branch= narrows the day cards and adds back up to the unfiltered view."""
    seed_days(app)
    everything = day_cards(app, **owner_session())
    midrand = day_cards(app, filter_branch=1, **owner_session())
    pretoria = day_cards(app, filter_branch=2, **owner_session())

    # Only ORD-9 was raised at depot 2; ORD-1 (started, today) is depot 1's.
    assert everything['orders'] == 2
    assert (midrand['orders'], pretoria['orders']) == (1, 1)
    assert (midrand['reservations'], pretoria['reservations']) == (1, 0)
    # Money: card/cash/EFT today all sit on depot 1's orders.
    assert (midrand['card_payments'], midrand['cash_payments'],
            midrand['eft_payments']) == (500.0, 250.0, 700.0)
    assert (pretoria['card_payments'], pretoria['cash_payments'],
            pretoria['eft_payments']) == (0.0, 0.0, 0.0)
    assert (midrand['reservation_pickups'], pretoria['reservation_pickups']) == (2, 0)
    assert (midrand['trailers_out'], pretoria['trailers_out']) == (6, 0)

    # Every narrowed figure is the two depots added back together.
    for key in ('orders', 'reservations', 'reservation_pickups', 'trailers_out',
                'card_payments', 'cash_payments', 'eft_payments', 'revenue', 'customers'):
        assert midrand[key] + pretoria[key] == everything[key], key
    # Revenue is money received (500 card + 250 cash + 700 EFT), all of it at
    # depot 1 today, and the new-customer count is the branch that added them.
    assert everything['revenue'] == 1450.0
    assert (midrand['revenue'], pretoria['revenue']) == (1450.0, 0.0)
    assert (midrand['customers'], pretoria['customers']) == (2, 1)
    assert everything['customers'] == 3


def test_the_fleet_card_counts_the_chosen_depots_yard(app):
    """A chosen branch means that branch's stock — unassigned units do not ride along."""
    seed_days(app)
    branch_product(app, 'Depot Two Trailer', 2, 4)
    everything = day_cards(app, **owner_session())
    depot_two = day_cards(app, filter_branch=2, **owner_session())
    depot_one = day_cards(app, filter_branch=1, **owner_session())

    # The catalogue products are unassigned, so only the filtered depot's own
    # units are its yard (the staff scope view keeps letting unassigned ride).
    assert everything['fleet'] == 14
    assert depot_two['fleet'] == 4
    assert depot_one['fleet'] == 0


def test_the_day_cards_session_scope_still_beats_the_branch_filter(app):
    """A crafted ?branch= can only ever narrow a depot-scoped account."""
    seed_days(app)
    scoped = {'user_id': 2, 'user_role': 'staff', 'can_view_all_branches': False,
              'branch_id': 2, 'branch_ids': [2]}
    own = day_cards(app, **scoped)
    assert own['orders'] == 1 and own['trailers_out'] == 0
    # Asking for depot 1 — which this account may not see — changes nothing.
    crafted = day_cards(app, filter_branch=1, **scoped)
    assert crafted['orders'] == own['orders']
    assert crafted['reservations'] == own['reservations']
    assert crafted['card_payments'] == own['card_payments']
    # ...and its own depot can still be selected explicitly.
    assert day_cards(app, filter_branch=2, **scoped)['orders'] == 1


def test_the_dashboard_branch_filter_narrows_the_page_and_submits_branch_once(client, app):
    one, two = today_depot_orders(app)
    login(client)

    everything = client.get('/dashboard').get_data(as_text=True)
    assert 'id="dashboard-branch-filter"' in everything
    assert filter_form(everything).count('name="branch"') == 1, \
        'a hidden duplicate would win over the picker'
    assert re.search(r'Reservations for the day</small><b>2</b>', everything)
    assert re.search(r'Total card payments</small><b>R500\.00</b>', everything)
    assert f'>{one}<' in everything and f'>{two}<' in everything

    narrowed = client.get('/dashboard?branch=2').get_data(as_text=True)
    # The picker keeps the chosen depot, and the day cards follow it.
    assert '<option value="2" selected>' in narrowed
    assert filter_form(narrowed).count('name="branch"') == 1
    assert re.search(r'Reservations for the day</small><b>1</b>', narrowed)
    assert re.search(r'Total card payments</small><b>R0\.00</b>', narrowed)
    # Only depot 2's booking is left in the movement lists (a reserved order
    # shows in both halves: going out and coming back).
    assert f'>{one}<' not in narrowed and f'>{two}<' in narrowed
    assert 'No reserved pickups scheduled yet.' not in narrowed

    for crafted in ('', '1', '2', '3', '999', 'abc', '2 OR 1=1'):
        page = client.get('/dashboard?branch=' + crafted)
        assert page.status_code == 200, crafted


def test_a_depot_scoped_account_gets_a_fixed_branch_label(client, app):
    one, two = today_depot_orders(app)
    login(client)
    client.post('/settings/users/permissions', data={'module': ['dashboard']},
                follow_redirects=True)
    client.post('/settings/users/add',
                data={'name': 'Depot Clerk', 'password': 'staff123', 'branch_id': '2'},
                follow_redirects=True)
    client.post('/logout')
    login(client, 'Depot Clerk', 'staff123')

    # Their own depot renders even when the query string asks for another one.
    body = client.get('/dashboard?branch=1').get_data(as_text=True)
    # The control is a fixed label for them, never a chooser to tamper with.
    assert 'class="filter-fixed"' in filter_form(body)
    assert 'name="branch"' not in filter_form(body)
    assert 'name="cash_branch"' not in body
    assert 'Total no. of trailers out' in body
    # Their own depot's day figures, never depot 1's (whose booking carries the
    # R500 card payment).
    assert re.search(r'Reservations for the day</small><b>1</b>', body)
    assert re.search(r'Total card payments</small><b>R0\.00</b>', body)
    # A staff dashboard keeps its historic shape: day cards and cash up only.
    assert 'Total orders' not in body and 'Going out' not in body


def test_the_dashboard_greets_the_signed_in_user_with_the_branded_hero(client, app):
    login(client)
    with app.app_context():
        name = get_db().execute(
            "SELECT name FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()['name']
    page = client.get('/dashboard').get_data(as_text=True)
    assert f'Welcome back, {name}' in page
    assert '· all branches —' in page, 'the hero note names the scope of the figures'
    assert 'class="dashboard-hero"' in page
    # The client asked for their logo OFF the dashboard hero (ticket
    # ABI-341952957). The sidebar wordmark is the SAME file, so the old
    # filename assertion passed even when the hero image was still there —
    # assert on the hero class itself.
    assert 'dashboard-hero-logo' not in page, 'the logo came off the dashboard hero'
    assert 'class="logo"' in page and 'sano-trailers-logo-trimmed.png' in page, \
        'the sidebar wordmark stays'
    # The day cards and the totals row are still the same cards.
    assert 'Total no. of trailers out' in page
    assert 'Total orders' in page


# --- quick ranges on the four headline cards (ticket ABI-341952957) ---------
# The client asked for the reports page's quick ranges on the main profile's
# four KPI cards, opening on This month, with their logo removed from the
# dashboard (it must stay on the PDF documents, which this file never touches).

def _ranged_fixtures(app):
    """Orders/products/customers inside and outside the CURRENT month.

    The pills resolve against the LIVE business day, so the fixed ``TODAY``
    fixture cannot be used for a range assertion — it is "last month" as soon
    as the suite runs in a later month. Returns ``(today, first_of_month, old)``.
    """
    today = local_now_iso()[:10]
    first = today[:8] + '01'
    old = '2021-03-04'
    with app.app_context():
        db = get_db()
        for number, created, total, branch in (
            ('ORD-R1', f'{today}T09:00:00', 100.0, 1),
            ('ORD-R2', f'{first}T09:00:00', 250.0, 1),
            ('ORD-R3', f'{old}T09:00:00', 5.0, 1),
            ('ORD-R4', f'{today}T10:00:00', 50.0, 2),
        ):
            db.execute(
                """INSERT INTO orders (order_number, booking_type, collect_branch_id, return_branch_id,
                status, payment_status, subtotal, tax_total, deposit_total, total, due_total, notes,
                created_at) VALUES (?, 'return', ?, ?, 'started', 'paid', 0, 0, 0, ?, 0, '', ?)""",
                (number, branch, branch, total, created))
            # Ticket ABI-341952993: the Gross revenue card is money RECEIVED, so
            # each order needs a real payment to carry its value — a total alone
            # is a booked figure and would leave every window reading R0.00.
            order_id = db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()['id']
            db.execute(
                """INSERT INTO payments (order_id, amount, method, reference, status, payment_date,
                deleted_at, created_at) VALUES (?, ?, 'cash', '', 'paid', ?, '', ?)""",
                (order_id, total, created[:10], created))
        for name, created in (('Range Today A', f'{today}T08:00:00'),
                              ('Range Today B', f'{today}T08:30:00'),
                              ('Range Old', f'{old}T08:00:00')):
            db.execute("INSERT INTO products (name, product_type, created_at) VALUES (?, 'sale', ?)",
                       (name, created))
        for name, created in (('Range Customer Today', f'{today}T08:00:00'),
                              ('Range Customer Old', f'{old}T08:00:00')):
            db.execute("INSERT INTO customers (name, created_at) VALUES (?, ?)", (name, created))
        db.commit()
    return today, first, old


def _window_counts(app, start, end, branch_id=None):
    """The four card numbers read straight from the fixtures.

    Orders / products / customers are the *legacy* queries (the ones the
    dashboard ran before the ranges existed); ``start``/``end`` of ``''`` is the
    all-time baseline. Revenue is money RECEIVED in the window (ticket
    ABI-341952993) — the same query as the "Revenue for the day" card, windowed.
    """
    order_where, order_params = '', []
    for column, value, operator in (('o.created_at', start, '>='), ('o.created_at', end, '<=')):
        if value:
            order_where += f' AND DATE({column}) {operator} ?'
            order_params.append(value)
    if branch_id is not None:
        order_where += ' AND (o.collect_branch_id = ? OR o.return_branch_id = ?)'
        order_params += [branch_id, branch_id]

    payment_where, payment_params = '', []
    for _prefix, value, operator in (('', start, '>='), ('', end, '<=')):
        if value:
            payment_where += (" AND substr(COALESCE(NULLIF(pay.payment_date, ''), pay.created_at), 1, 10) "
                              f"{operator} ?")
            payment_params.append(value)
    if branch_id is not None:
        payment_where += ' AND (o.collect_branch_id = ? OR o.return_branch_id = ?)'
        payment_params += [branch_id, branch_id]

    product_where, product_params = '', []
    if start:
        product_where += ' AND DATE(p.created_at) >= ?'
        product_params.append(start)
    if end:
        product_where += ' AND DATE(p.created_at) <= ?'
        product_params.append(end)
    if branch_id is not None:
        product_where += ' AND p.branch_id = ?'
        product_params.append(branch_id)

    customer_where, customer_params = '', []
    if start:
        customer_where += ' AND DATE(created_at) >= ?'
        customer_params.append(start)
    if end:
        customer_where += ' AND DATE(created_at) <= ?'
        customer_params.append(end)

    with app.app_context():
        db = get_db()
        row = db.execute(
            f"SELECT COUNT(*) AS c FROM orders o WHERE 1=1{order_where}",
            order_params).fetchone()
        counts = {'orders': row['c']}
        counts['revenue'] = round(float(db.execute(
            f"""SELECT COALESCE(SUM(pay.amount), 0) AS s FROM payments pay
            JOIN orders o ON o.id = pay.order_id
            WHERE pay.status = 'paid' AND COALESCE(pay.deleted_at, '') = ''
            {payment_where}""", payment_params).fetchone()['s'] or 0), 2)
        counts['products'] = db.execute(
            f"SELECT COUNT(*) AS c FROM products p WHERE 1=1{product_where}",
            product_params).fetchone()['c']
        counts['customers'] = db.execute(
            f"SELECT COUNT(*) AS c FROM customers WHERE 1=1{customer_where}",
            customer_params).fetchone()['c']
    return counts


CARD_LABELS = ('Total orders', 'Catalog size', 'Customer base', 'Gross revenue')


def card_values(page):
    """The four headline card values, read from the rendered page."""
    values = {}
    for label in CARD_LABELS:
        match = re.search(re.escape(label) + r'</small><b>(.*?)</b>', page)
        assert match, f'the {label} card is missing'
        values[label] = match.group(1)
    return values


def expected_cards(counts):
    return {
        'Total orders': str(counts['orders']),
        'Catalog size': str(counts['products']),
        'Customer base': str(counts['customers']),
        'Gross revenue': 'R%.2f' % counts['revenue'],
    }


def active_pill(page):
    """The single quick range the page marks as active."""
    matches = re.findall(r'<a class="report-preset is-active"[^>]*>([^<]+)</a>', page)
    assert len(matches) == 1, f'expected exactly one active pill, got {matches}'
    return matches[0]


def pill_href(page, label):
    match = re.search(r'<a class="report-preset[^"]*" href="([^"]+)">' + re.escape(label) + r'</a>', page)
    assert match, f'the {label} pill is missing'
    return match.group(1).replace('&amp;', '&')


def test_the_four_cards_open_on_this_month(client, app):
    today, first, _old = _ranged_fixtures(app)
    login(client)

    page = client.get('/dashboard').get_data(as_text=True)
    assert card_values(page) == expected_cards(_window_counts(app, first, today))
    assert active_pill(page) == 'This month'
    # The sub-caption names the window, so a windowed catalogue count cannot be
    # mistaken for the whole catalogue.
    assert 'Created · This month' in page
    assert 'Added · This month' in page
    # Ticket ABI-341952993: the Gross revenue card now says what it measures.
    assert 'Received · This month' in page


def test_all_time_is_the_pre_ticket_baseline(client, app):
    """The three count cards are unchanged all-time; only revenue moved basis
    (ticket ABI-341952993 — money received instead of booked value)."""
    today, first, _old = _ranged_fixtures(app)
    login(client)

    all_time = _window_counts(app, '', '')
    this_month = _window_counts(app, first, today)
    page = client.get('/dashboard?range=all_time').get_data(as_text=True)
    assert card_values(page) == expected_cards(all_time)
    assert active_pill(page) == 'All time'
    assert 'Created · All time' in page
    # The fixtures reach back beyond the month, so the two windows really differ.
    assert all_time['orders'] > this_month['orders']
    assert all_time['products'] > this_month['products']
    assert all_time['customers'] > this_month['customers']


def test_the_today_pill_narrows_to_the_day(client, app):
    today, _first, _old = _ranged_fixtures(app)
    login(client)

    page = client.get('/dashboard?range=today').get_data(as_text=True)
    assert card_values(page) == expected_cards(_window_counts(app, today, today))
    assert active_pill(page) == 'Today'
    assert 'Created · Today' in page


def test_an_unknown_range_falls_back_to_the_default(client, app):
    _ranged_fixtures(app)
    login(client)

    default = client.get('/dashboard').get_data(as_text=True)
    for crafted in ('abc', '', 'THIS MONTH', '999', 'all time', 'last year', '2 OR 1=1'):
        page = client.get('/dashboard?range=' + crafted)
        assert page.status_code == 200, crafted
        body = page.get_data(as_text=True)
        assert card_values(body) == card_values(default), crafted
        assert active_pill(body) == 'This month', crafted


def test_the_branch_and_the_range_survive_each_other(client, app):
    _ranged_fixtures(app)
    login(client)

    page = client.get('/dashboard?range=all_time&branch=2').get_data(as_text=True)
    assert card_values(page) == expected_cards(_window_counts(app, '', '', branch_id=2))
    assert active_pill(page) == 'All time'

    form = filter_form(page)
    assert form.count('name="branch"') == 1, 'a hidden duplicate would win over the picker'
    assert form.count('name="range"') == 1, 'the picker submits the range it is showing'
    assert 'value="all_time"' in form

    # Each pill links back with the chosen branch intact.
    today_pill = pill_href(page, 'Today')
    assert 'range=today' in today_pill and 'branch=2' in today_pill
    month_pill = pill_href(page, 'This month')
    assert 'range=this_month' in month_pill and 'branch=2' in month_pill
    # ...and clearing the branch keeps the range.
    clear_href = re.search(r'<a class="filter-clear" href="([^"]+)"', page)
    assert clear_href, 'the Clear branch link is missing'
    assert 'range=all_time' in clear_href.group(1).replace('&amp;', '&')
