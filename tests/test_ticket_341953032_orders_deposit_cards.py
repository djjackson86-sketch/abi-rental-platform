"""Ticket ABI-341953032 - unprocessed-deposit cards on the Orders page.

Requested edit:
1. a card with the total NUMBER of unprocessed deposits;
2. a card with the total AMOUNT of unprocessed deposits, in red;
3. let the "Due" card show for users other than the main profile (in red).

"Unprocessed deposits" is the set the Orders filter rail already calls
**Process deposit** (``order_counts`` / ``order_filter_counts`` ->
``_process_deposit_clause``): an order whose refundable deposit has not been
refunded/used yet. The count already existed as a rail badge; only the money total
was missing. The value mirrors ``deposit_to_process_amount()`` per order - once any
part of a deposit has been refunded or applied, only the refunded remainder is
still outstanding, otherwise the whole deposit is.

Ticket ABI-341953034 narrowed that single definition to **returned orders only**
(a deposit on a merely picked-up order is not actionable yet, and a canceled record
will never be refunded) and gave staff the two deposit cards beside their Due card.
The numbers below are re-pinned to the narrowed set; the narrowing itself is pinned
by ``tests/test_ticket_341953034_deposits_returned_only_staff_cards.py``.

These tests pin: the exact numbers on a seeded mix, that processed/settled and
non-returned deposits leave both cards, that the cards agree with the rail badge
and the list's own Due column, that they follow the page's filters and the branch
scope, that the red styling is real CSS, and that staff (non-main) accounts get the
same Due / unprocessed-deposit cards - branch-scoped - while the main-only totals
row and its Hide-metrics toggle stay main-only.
"""
import os
import re
import tempfile

import pytest
from flask import session as flask_session

from app import create_app
from app.db import get_db
from app.services.access import MODULE_KEYS
from app.services.orders import order_counts, order_filter_counts

DAY = '2026-09-20'


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
    """Sign in the way the UI does: pick a name, then the password."""
    with client.application.app_context():
        if name is None:
            row = get_db().execute(
                "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
        else:
            row = get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
    return client.post('/login', data={'user_id': str(row['id']), 'password': password},
                       follow_redirects=True)


def add_staff(client, app, name, branch_id, password='staff123'):
    """Owner creates an additional (non-main) account tied to one depot."""
    login(client)
    client.post('/settings/users/permissions', data={'module': list(MODULE_KEYS)},
                follow_redirects=True)
    client.post('/settings/users/add', data={
        'name': name, 'password': password, 'branch_id': str(branch_id),
    }, follow_redirects=True)
    with app.app_context():
        return get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()['id']


def _insert_order(db, number, status, *, deposit=0, applied=0, refund=0, method='',
                  processed='', due=0, payment_status='payment_due', branch_id=1):
    """One order with a deposit, inserted straight into the table (no app writes)."""
    db.execute(
        """INSERT INTO orders (order_number, booking_type, collect_branch_id, return_branch_id,
        status, payment_status, start_at, end_at, subtotal, tax_total, deposit_total,
        deposit_applied_amount, deposit_refund_amount, deposit_process_method,
        deposit_processed_at, total, due_total, notes, created_at)
        VALUES (?, 'return', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, '', ?)""",
        (number, branch_id, branch_id, status, payment_status,
         f"{DAY}T09:00:00", f"{DAY}T17:00:00", 1000, deposit, applied, refund, method,
         processed, 1000 + deposit, due, f"{DAY}T09:00:00"),
    )
    return db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()['id']


def seed_deposits(app):
    """The documented mix. Returns the order numbers by nickname.

    in the set    : returned 750 · returned 1000/applied 300/refund 200 (remainder
                    200) · returned 100 on branch 2
    processed     : returned 500 refunded+timestamped · returned 600 with a method stored
    out of set    : returned with no deposit · reserved 300 · started 200
                    (ticket ABI-341953034) · canceled 400 (ticket ABI-341953034)
    """
    with app.app_context():
        db = get_db()
        ids = {
            'returned_open': _insert_order(db, 'ORD-30001', 'returned', deposit=750),
            'started_open': _insert_order(db, 'ORD-30002', 'started', deposit=200, due=250),
            'partly_refunded': _insert_order(db, 'ORD-30003', 'returned', deposit=1000,
                                             applied=300, refund=200),
            'canceled_open': _insert_order(db, 'ORD-30004', 'canceled', deposit=400),
            'settled': _insert_order(db, 'ORD-30005', 'returned', deposit=500, refund=500,
                                     method='cash', processed=f'{DAY}T16:00:00'),
            'method_only': _insert_order(db, 'ORD-30006', 'returned', deposit=600, method='eft'),
            'no_deposit': _insert_order(db, 'ORD-30007', 'returned'),
            'reserved': _insert_order(db, 'ORD-30008', 'reserved', deposit=300),
            'other_branch': _insert_order(db, 'ORD-30009', 'returned', deposit=100, due=100,
                                          branch_id=2),
        }
        db.commit()
        return ids


def _session_context(app, user_id=1, role='owner'):
    ctx = app.test_request_context('/orders')
    ctx.push()
    flask_session['user_id'] = user_id
    flask_session['user_role'] = role
    return ctx


def counters_with_filters(app, **filters):
    ctx = _session_context(app)
    try:
        return order_counts(**filters)
    finally:
        ctx.pop()


def _cards(html):
    """(label, value, classes) for every metric card on the page, in order."""
    out = []
    for match in re.finditer(
            rb'<div class="([^"]*metric-card[^"]*)"><small>(.*?)</small><b>(.*?)</b></div>',
            html, re.S):
        out.append((match.group(2).decode().strip(), match.group(3).decode().strip(),
                    match.group(1).decode()))
    return out


def _by_label(html):
    return {label: (value, classes) for label, value, classes in _cards(html)}


def _metric_section(html, marker):
    """The metric <section> carrying ``marker`` (an id or a class)."""
    match = re.search(rb'<section class="[^"]*"[^>]*' + marker + rb'[^>]*>(.*?)</section>',
                      html, re.S)
    assert match, f'the metric section for {marker!r} was not rendered'
    return match.group(0)


def _staff_orders(client, app, name, branch_id):
    add_staff(client, app, name, branch_id)
    client.post('/logout')
    login(client, name, 'staff123')
    response = client.get('/orders')
    assert response.status_code == 200
    return response.data


# --------------------------------------------------------------- service numbers


def test_the_cards_count_and_sum_the_unprocessed_deposits(app):
    seed_deposits(app)
    counts = counters_with_filters(app)

    # returned 750 + partly-refunded remainder 200 (branch 1) + returned 100
    # (branch 2). The started 200 and the canceled 400 are no longer in the set
    # (ticket ABI-341953034).
    assert counts['deposits_unprocessed_total'] == 3
    assert counts['deposits_unprocessed_amount'] == 1050.0


def test_processed_and_settled_deposits_leave_both_cards(app):
    """A refunded/timestamped deposit or a stored payout method means "done"."""
    seed_deposits(app)

    ctx = _session_context(app)
    try:
        db = get_db()
        settled = db.execute(
            "SELECT deposit_total FROM orders WHERE order_number IN ('ORD-30005', 'ORD-30006')"
        ).fetchall()
        assert sum(row['deposit_total'] for row in settled) == 1100  # both would inflate the card
        counts = order_counts()
    finally:
        ctx.pop()

    # Neither 500 (refunded with a timestamp) nor 600 (payout method already stored)
    # may appear in the count or the money.
    assert counts['deposits_unprocessed_total'] == 3
    assert counts['deposits_unprocessed_amount'] == 1050.0


def test_a_partly_refunded_deposit_only_counts_its_remainder(app):
    """Mirrors deposit_to_process_amount(): applied/refunded -> the remainder only."""
    seed_deposits(app)
    ctx = _session_context(app)
    try:
        db = get_db()
        only_part = order_counts()
        db.execute("UPDATE orders SET deposit_applied_amount = 0, deposit_refund_amount = 0 "
                   "WHERE order_number = 'ORD-30003'")
        db.commit()
        whole = order_counts()
    finally:
        ctx.pop()

    # Applied-but-unrefunded deposits are excluded by the clause entirely...
    assert only_part['deposits_unprocessed_total'] == 3
    assert only_part['deposits_unprocessed_amount'] == 1050.0
    # ...while a genuinely untouched 1000 counts whole once the partial refund is gone.
    assert whole['deposits_unprocessed_total'] == 3
    assert whole['deposits_unprocessed_amount'] == 1850.0


def test_the_count_card_agrees_with_the_rail_badge(app):
    """One definition of "unprocessed": the same set the Process-deposit folder uses."""
    seed_deposits(app)
    ctx = _session_context(app)
    try:
        counts = order_counts()
        badge = order_filter_counts()['payment_status']['process_deposit']
    finally:
        ctx.pop()

    assert badge == counts['deposits_unprocessed_total'] == 3


def test_the_cards_follow_the_page_filters(app):
    seed_deposits(app)
    # The started (picked-up) deposit left the set entirely (ticket ABI-341953034)...
    started = counters_with_filters(app, status='started')
    assert started['deposits_unprocessed_total'] == 0
    assert started['deposits_unprocessed_amount'] == 0.0
    # ...so filtering to Returned shows the whole set.
    returned = counters_with_filters(app, status='returned')
    assert returned['deposits_unprocessed_total'] == 3
    assert returned['deposits_unprocessed_amount'] == 1050.0
    # Filtering to the Process-deposit folder itself shows the very same figures.
    folder = counters_with_filters(app, payment_status='process_deposit')
    assert folder['deposits_unprocessed_total'] == 3
    assert folder['deposits_unprocessed_amount'] == 1050.0


def test_the_cards_and_the_due_card_are_branch_scoped(app):
    seed_deposits(app)
    branch_one = counters_with_filters(app, branch_id=1)
    branch_two = counters_with_filters(app, branch_id=2)
    everything = counters_with_filters(app)

    assert branch_two['deposits_unprocessed_total'] == 1
    assert branch_two['deposits_unprocessed_amount'] == 100.0
    assert branch_one['deposits_unprocessed_total'] == 2
    assert branch_one['deposits_unprocessed_amount'] == 950.0
    assert everything['deposits_unprocessed_amount'] == 1050.0

    # The Due figure is scoped the same way: depot 2's own charge exists only there.
    assert branch_two['due'] == 100.0
    assert everything['due'] == branch_one['due'] + branch_two['due']


# -------------------------------------------------------------------- the markup


def test_the_main_profile_sees_every_card_and_the_red_ones(client, app):
    seed_deposits(app)
    login(client)
    html = _metric_section(client.get('/orders').data, b'id="orders-metrics"')
    cards = _cards(html)

    assert [label for label, _, _ in cards] == [
        'Orders', 'Items ordered', 'Revenue received', 'Due',
        'Unprocessed deposits', 'Unprocessed deposit value',
    ]
    values = {label: value for label, value, _ in cards}
    assert values['Unprocessed deposits'] == '3'
    assert values['Unprocessed deposit value'] == 'R1050.00'
    alert = [label for label, _, classes in cards if 'is-alert' in classes]
    assert alert == ['Due', 'Unprocessed deposit value']


def test_staff_get_the_same_three_cards_branch_scoped(client, app):
    """Ticket ABI-341953034: the deposit count and value are no longer main-only."""
    seed_deposits(app)
    html = _staff_orders(client, app, 'Depot Two Staff', 2)
    staff_bar = _metric_section(html, b'orders-staff-metrics')
    cards = _cards(staff_bar)

    assert [label for label, _, _ in cards] == [
        'Due', 'Unprocessed deposits', 'Unprocessed deposit value',
    ]
    values = {label: value for label, value, _ in cards}
    # Branch 2 only: its own returned order is the only money owed there, and the
    # only unprocessed deposit there is the same R100.
    assert values['Due'] == 'R100.00'
    assert values['Unprocessed deposits'] == '1'
    assert values['Unprocessed deposit value'] == 'R100.00'
    classes = {label: cls for label, _, cls in cards}
    assert 'is-alert' in classes['Due']
    assert 'is-alert' not in classes['Unprocessed deposits']
    assert 'is-alert' in classes['Unprocessed deposit value']

    # The main-only totals row and its Hide-metrics toggle stay out of reach; the
    # deposit cards themselves are now deliberately shared.
    assert b'id="orders-metrics"' not in html
    assert b'metrics-toggle' not in html
    assert b'orders.metrics.hidden' not in html


def _due_cells(html):
    """The Due cell of every rendered order row (column 7 of the orders table)."""
    body = html.split(b'<tbody>')[1].split(b'</tbody>')[0]
    cells = []
    for row in body.split(b'<tr>')[1:]:
        row_cells = re.findall(rb'<td[^>]*>(.*?)</td>', row, re.S)
        cells.append(row_cells[6].strip())
    return cells


def test_the_staff_due_card_matches_the_due_column_of_the_list(client, app):
    """DB-to-UI parity: the staff card equals the Due cells they are looking at."""
    seed_deposits(app)
    html = _staff_orders(client, app, 'Depot Two Staff', 2)
    rendered = client.get('/orders?status=returned').data

    due_cells = _due_cells(rendered)
    assert len(due_cells) == 1, due_cells
    cell_total = sum(float(re.sub(rb'[^0-9\.]', b'', cell)) for cell in due_cells)
    assert cell_total == 100.0, due_cells
    assert _by_label(_metric_section(html, b'orders-staff-metrics'))['Due'][0] == \
        'R%.2f' % cell_total


def test_the_staff_deposit_card_matches_their_rail_badge_and_folder(client, app):
    """The staff card == the "Process deposit" badge == the rows in that folder."""
    seed_deposits(app)
    html = _staff_orders(client, app, 'Depot Two Staff', 2)
    value = _by_label(_metric_section(html, b'orders-staff-metrics'))['Unprocessed deposits'][0]

    folder = client.get('/orders?payment_status=process_deposit').data
    assert value == '1'
    # The rail badge prints the same count, and the folder lists exactly that row.
    badge = re.search(rb'<span>Process deposit</span>\s*<em>\((\d+)\)</em>', html)
    assert badge and badge.group(1) == b'1'
    assert b'ORD-30009' in folder
    assert b'ORD-30002' not in folder  # the started deposit is not actionable yet


def test_the_main_profile_still_gets_the_hide_metrics_toggle(client, app):
    login(client)
    html = client.get('/orders').data
    assert b'id="orders-metrics"' in html
    assert b'metrics-toggle' in html


def test_the_main_profile_only_rule_is_the_only_thing_that_split(client, app):
    """Only the totals row and the toggle stay main-only; the deposit cards are shared."""
    seed_deposits(app)
    login(client)
    main_html = client.get('/orders').data
    staff_html = _staff_orders(client, app, 'Depot Two Staff', 2)

    for marker in (b'id="orders-metrics"', b'metrics-toggle'):
        assert marker in main_html
        assert marker not in staff_html
    for marker in (b'Unprocessed deposits', b'Unprocessed deposit value', b'Due'):
        assert marker in main_html
        assert marker in staff_html
    # Both profiles still get the tab row and the list itself.
    for html in (main_html, staff_html):
        assert b'aria-label="Order views"' in html
        assert b'Deposit to process' in html


# ----------------------------------------------------------------------- the css


def _css():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'static', 'css', 'app.css'), encoding='utf-8') as handle:
        return handle.read()


def test_the_red_cards_use_the_danger_token():
    css = _css()
    rule = re.search(r'\.metric-card\.is-alert b\{([^}]*)\}', css)
    assert rule, '.metric-card.is-alert b has no rule in app.css'
    assert 'var(--danger)' in rule.group(1)
    # The token itself is a red, and the staff bar lays its cards out responsively.
    assert '--danger:#d92d20' in css
    staff = re.search(r'\.orders-metrics-staff\{([^}]*)\}', css)
    assert staff, '.orders-metrics-staff has no rule in app.css'
    assert 'auto-fit' in staff.group(1)
    assert 'minmax(0,1fr)' not in staff.group(1)
