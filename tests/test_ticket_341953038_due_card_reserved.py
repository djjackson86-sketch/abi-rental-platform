"""Reserved orders are off the Orders "Due" card (ticket ABI-341953038, item 2).

Client ask: "do not count reserved orders on the due card".

The Due card answers "what is still collectable", so a reserved order — a booking
held, nothing collected yet — no longer adds to it. Only the CARD changes:

* the per-row Due column on the Orders list still prints the order's own balance,
  so a reserved R500 order is still visibly outstanding on its row;
* the order detail's own Due figure is untouched;
* Reports "Amount due" (``summary_metrics``) deliberately still counts a reserved
  order — that figure is the booking's own balance, not the collectable card.

Started, returned and accepted-quote balances keep counting exactly as before.
"""
import os
import re
import tempfile

import pytest
from flask import session as flask_session

from app import create_app
from app.db import get_db
from app.services.orders import order_counts
from app.services.reports import summary_metrics

DAY = '2026-07-01'


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


def _insert_order(db, number, status, due, *, payment_status='payment_due', branch_id=1):
    db.execute(
        """INSERT INTO orders (order_number, booking_type, collect_branch_id, return_branch_id,
        status, payment_status, start_at, end_at, subtotal, tax_total, deposit_total, total,
        due_total, notes, created_at)
        VALUES (?, 'return', ?, ?, ?, ?, ?, ?, 1000, 0, 0, 1000, ?, '', ?)""",
        (number, branch_id, branch_id, status, payment_status,
         f'{DAY}T09:00:00', f'{DAY}T17:00:00', due, f'{DAY}T09:00:00'),
    )
    return db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()['id']


#: The Due card's value for the seed below, and the reason it is not the sum of
#: every row: started 400 + returned 300. The reserved 500 / 250 and the archived
#: 700 are excluded by this ticket and by the collectible basis's own rules, and
#: the Sales/Repairs 150 was ALREADY excluded before it (its status is not
#: reserved/started/returned and it carries no accepted quote) — which is what
#: makes this figure a surgical pin.
BASE_DUE = 400 + 300


def seed_orders(app):
    """reserved 500 · reserved/partially_paid 250 · started 400 · returned 300 ·
    archived 700 · sales/repairs draft 150."""
    with app.app_context():
        db = get_db()
        ids = {
            'reserved': _insert_order(db, 'ORD-50001', 'reserved', 500),
            'reserved_partial': _insert_order(db, 'ORD-50002', 'reserved', 250,
                                              payment_status='partially_paid'),
            'started': _insert_order(db, 'ORD-50003', 'started', 400),
            'returned': _insert_order(db, 'ORD-50004', 'returned', 300),
            'archived': _insert_order(db, 'ORD-50005', 'archived', 700),
            'sales_repairs': _insert_order(db, 'ORD-50006', 'sales_repairs', 150),
        }
        db.commit()
        return ids


def counters(app, **filters):
    ctx = app.test_request_context('/orders')
    ctx.push()
    flask_session['user_id'] = 1
    flask_session['user_role'] = 'owner'
    try:
        return order_counts(**filters)
    finally:
        ctx.pop()


def _metric_card(html, label):
    """The value of one metric card on the Orders page."""
    body = html if isinstance(html, bytes) else html.encode()
    match = re.search(rb'<small>' + label.encode() + rb'</small><b>(.*?)</b>', body, re.S)
    assert match, f'the {label!r} metric card was not rendered'
    return match.group(1).decode().strip()


# ------------------------------------------------------------------ the card

def test_a_reserved_order_no_longer_adds_to_the_due_card(app):
    seed_orders(app)
    # started 400 + returned 300: the reserved 500 and 250 and the archived 700
    # all stay out.
    assert counters(app)['due'] == BASE_DUE


def test_the_still_counted_statuses_are_untouched(app):
    """Only 'reserved' is excluded — nothing else about the card moved."""
    seed_orders(app)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE orders SET status = 'canceled' WHERE order_number = 'ORD-50001'")
        db.commit()
    # A canceled order was already excluded by the collectible basis above it.
    assert counters(app)['due'] == BASE_DUE


def test_the_reports_amount_due_basis_still_counts_a_reserved_order(app):
    """Deliberate boundary: Reports reports each order's own balance."""
    seed_orders(app)
    with app.app_context():
        assert summary_metrics(DAY, DAY)['due'] == 500 + 250 + 400 + 300


def test_the_card_agrees_with_the_page_and_the_row_still_shows_the_balance(client, app):
    seed_orders(app)
    login(client)
    html = client.get('/orders').get_data()

    assert _metric_card(html, 'Due') == 'R700.00'          # 400 + 300
    # The per-row Due column is untouched: the reserved order still reads R500.00.
    body = html.decode()
    assert '<small>Due</small><b>R700.00</b>' in body
    reserved_row = re.search(r'ORD-50001.*?</tr>', body, re.S)
    assert reserved_row, 'the reserved order is missing from the list'
    assert 'R500.00' in reserved_row.group(0)


def test_the_card_follows_the_page_filters(app):
    seed_orders(app)
    # Filtering to Reserved shows the rows, but they are worth nothing on the card.
    assert counters(app, status='reserved')['due'] == 0
    assert counters(app, status='returned')['due'] == 300
    assert counters(app, status='started')['due'] == 400


def test_a_reserved_order_starts_counting_once_it_is_picked_up(app):
    seed_orders(app)
    assert counters(app)['due'] == BASE_DUE
    with app.app_context():
        get_db().execute("UPDATE orders SET status = 'started' WHERE order_number = 'ORD-50001'")
        get_db().commit()
    # 500 reserved → started now counts, on top of the existing started 400.
    assert counters(app)['due'] == BASE_DUE + 500


def test_the_card_is_branch_scoped_exactly_as_before(app):
    seed_orders(app)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE orders SET collect_branch_id = 2, return_branch_id = 2 "
                   "WHERE order_number = 'ORD-50001'")
        db.execute("UPDATE orders SET collect_branch_id = 2, return_branch_id = 2 "
                   "WHERE order_number = 'ORD-50004'")
        db.commit()
    branch_one = counters(app, branch_id=1)
    branch_two = counters(app, branch_id=2)
    # Branch 2 holds the reserved 500 AND the returned 300, but only the returned
    # one counts — a reserved order stays out of the card on every branch.
    assert branch_two['due'] == 300
    assert branch_one['due'] == 400          # the started order
    assert counters(app)['due'] == branch_one['due'] + branch_two['due']


def test_staff_get_the_same_reserved_exclusion(client, app):
    seed_orders(app)
    login(client)
    client.post('/settings/users/permissions',
                data={'module': ['new_order', 'dashboard', 'calendar', 'orders', 'customers']},
                follow_redirects=True)
    client.post('/settings/users/add',
                data={'name': 'Depot Clerk', 'password': 'staff123', 'branch_id': '1'},
                follow_redirects=True)
    client.post('/logout')
    with app.app_context():
        staff_id = get_db().execute("SELECT id FROM users WHERE name = 'Depot Clerk'").fetchone()['id']
    client.post('/login', data={'user_id': str(staff_id), 'password': 'staff123'},
                follow_redirects=True)

    html = client.get('/orders').get_data()
    assert b'orders-staff-metrics' in html
    assert _metric_card(html, 'Due') == 'R700.00'
