"""Ticket ABI-341953034 - unprocessed deposits: returned orders only, visible to staff.

Requested edit:
1. make the "how many deposits are unprocessed" count and the total unprocessed
   deposit value visible to staff as well;
2. the unprocessed deposit count should only pick from returned orders only.

Both halves ride on ONE definition. ``_process_deposit_clause()`` in
``app/services/orders.py`` is the single predicate behind the rail's "Process
deposit" folder, its badge, the Orders metric cards and the CSV export, so
narrowing it to ``status = 'returned'`` narrows all of them together - the
invariant pinned by ticket ABI-341953032 (card == badge == folder). A deposit on a
merely picked-up (``started``) order is not actionable yet, and a canceled record
will never be refunded, so neither belongs in the folder any more; those orders stay
fully visible through the All / Status filters.

The staff half needs no permission change at all: the Orders route already passes
``counts`` to both profiles, so this is markup plus one CSS column rule.

These tests pin the exact numbers on a seeded mix (returned / started / canceled /
settled / method-stored / no-deposit / reserved / other branch), that started and
canceled deposits leave the count, the money and the badge, that they re-enter the
set the moment the order is returned, that the started order is still listed
normally, that the card equals the badge equals the folder rows, that staff get the
three cards branch-scoped with a working badge and folder of their own, and that the
staff bar's CSS is genuinely responsive.
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
    with client.application.app_context():
        if name is None:
            row = get_db().execute(
                "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
        else:
            row = get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
    return client.post('/login', data={'user_id': str(row['id']), 'password': password},
                       follow_redirects=True)


def add_staff(client, app, name, branch_id, password='staff123'):
    login(client)
    client.post('/settings/users/permissions', data={'module': list(MODULE_KEYS)},
                follow_redirects=True)
    client.post('/settings/users/add', data={
        'name': name, 'password': password, 'branch_id': str(branch_id),
    }, follow_redirects=True)
    with app.app_context():
        return get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()['id']


def _insert_order(db, number, status, *, deposit=0, applied=0, refund=0, method='',
                  processed='', due=0, branch_id=1):
    db.execute(
        """INSERT INTO orders (order_number, booking_type, collect_branch_id, return_branch_id,
        status, payment_status, start_at, end_at, subtotal, tax_total, deposit_total,
        deposit_applied_amount, deposit_refund_amount, deposit_process_method,
        deposit_processed_at, total, due_total, notes, created_at)
        VALUES (?, 'return', ?, ?, ?, 'payment_due', ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, '', ?)""",
        (number, branch_id, branch_id, status,
         f"{DAY}T09:00:00", f"{DAY}T17:00:00", 1000, deposit, applied, refund, method,
         processed, 1000 + deposit, due, f"{DAY}T09:00:00"),
    )
    return db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()['id']


def seed_orders(app):
    """A mix that exercises every branch of the narrowed predicate.

    IN  (returned)  : ORD-40001 spent 700 · ORD-40004 applied 400 refund 0 (remainder 0)
                      · ORD-40009 branch 2, 120
    OUT (status)    : ORD-40002 started 250 · ORD-40003 canceled 300
    OUT (settled)   : ORD-40005 refunded 600 + method + timestamp
    OUT (method)    : ORD-40006 returned 500 with a payout method already stored
    OUT (no deposit): ORD-40007 returned 0
    OUT (reserved)  : ORD-40008 reserved 150
    """
    with app.app_context():
        db = get_db()
        ids = {
            'returned_open': _insert_order(db, 'ORD-40001', 'returned', deposit=700),
            'started_open': _insert_order(db, 'ORD-40002', 'started', deposit=250, due=250),
            'canceled_open': _insert_order(db, 'ORD-40003', 'canceled', deposit=300),
            'applied_no_refund': _insert_order(db, 'ORD-40004', 'returned', deposit=900,
                                               applied=400),
            'settled': _insert_order(db, 'ORD-40005', 'returned', deposit=600, refund=600,
                                     method='cash', processed=f'{DAY}T16:00:00'),
            'method_only': _insert_order(db, 'ORD-40006', 'returned', deposit=500, method='eft'),
            'no_deposit': _insert_order(db, 'ORD-40007', 'returned'),
            'reserved': _insert_order(db, 'ORD-40008', 'reserved', deposit=150),
            'other_branch': _insert_order(db, 'ORD-40009', 'returned', deposit=120, due=90,
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


def badge_with_filters(app, **filters):
    ctx = _session_context(app)
    try:
        return order_filter_counts(**filters)['payment_status']['process_deposit']
    finally:
        ctx.pop()


def _sections(html):
    """(label, value, classes) for every metric card, in document order."""
    out = []
    for match in re.finditer(
            rb'<div class="([^"]*metric-card[^"]*)"><small>(.*?)</small><b>(.*?)</b></div>',
            html, re.S):
        out.append((match.group(2).decode().strip(), match.group(3).decode().strip(),
                    match.group(1).decode()))
    return out


def _metric_section(html, marker):
    match = re.search(rb'<section class="[^"]*"[^>]*' + marker + rb'[^>]*>(.*?)</section>',
                      html, re.S)
    assert match, f'the metric section for {marker!r} was not rendered'
    return match.group(0)


def _cards(html):
    section = _metric_section(html, b'orders-staff-metrics' if b'orders-staff-metrics' in html
                              else b'id="orders-metrics"')
    return {label: (value, classes) for label, value, classes in _sections(section)}


def _rows(html):
    """(order_number, status, due_cell, deposit_cell) for every rendered order row."""
    body = html.split(b'<tbody>')[1].split(b'</tbody>')[0]
    rows = []
    for row in body.split(b'<tr>')[1:]:
        cells = re.findall(rb'<td[^>]*>(.*?)</td>', row, re.S)
        number = re.search(rb'(ORD-\d+)', cells[0]).group(1).decode()
        status = re.search(rb'status-([a-z_]+)', cells[2]).group(1).decode()
        rows.append((number, status, cells[6].strip().decode(), cells[7].strip().decode()))
    return rows


def _money(text):
    match = re.search(r'R([0-9.]+)', text)
    return float(match.group(1)) if match else 0.0


def _badge(html):
    """The rail's own "Process deposit" count, read from the rendered page."""
    match = re.search(rb'<span>Process deposit</span>\s*<em>\((\d+)\)</em>', html)
    assert match, 'the Process deposit rail badge was not rendered'
    return int(match.group(1))


def _staff_orders(client, app, name, branch_id, query=''):
    add_staff(client, app, name, branch_id)
    client.post('/logout')
    login(client, name, 'staff123')
    response = client.get('/orders' + query)
    assert response.status_code == 200
    return response.data


# ------------------------------------------- the narrowed set (returned orders only)


def test_only_returned_orders_are_counted_and_valued(app):
    seed_orders(app)
    counts = counters_with_filters(app)

    # returned 700 + returned applied-400/refund-0 remainder 0 (branch 1) + returned
    # 120 (branch 2). The started 250, the canceled 300, the settled 600 and the
    # method-stored 500 are all out.
    assert counts['deposits_unprocessed_total'] == 3
    assert counts['deposits_unprocessed_amount'] == 820.0


def test_the_badge_and_the_folder_predicate_agree_with_the_count(app):
    seed_orders(app)
    assert badge_with_filters(app) == 3
    folder = counters_with_filters(app, payment_status='process_deposit')
    assert folder['deposits_unprocessed_total'] == 3
    assert folder['deposits_unprocessed_amount'] == 820.0


def test_started_and_canceled_deposits_rejoin_the_set_once_returned(app):
    """The narrowing is the STATUS, not the deposit - nothing else is filtered out."""
    seed_orders(app)
    before = counters_with_filters(app)
    assert (before['deposits_unprocessed_total'], badge_with_filters(app)) == (3, 3)

    ctx = _session_context(app)
    try:
        db = get_db()
        db.execute("UPDATE orders SET status = 'returned' WHERE order_number = 'ORD-40002'")
        db.commit()
        with_started = order_counts()
    finally:
        ctx.pop()
    assert with_started['deposits_unprocessed_total'] == 4
    assert with_started['deposits_unprocessed_amount'] == 1070.0  # + the started 250

    ctx = _session_context(app)
    try:
        db = get_db()
        db.execute("UPDATE orders SET status = 'returned' WHERE order_number = 'ORD-40003'")
        db.commit()
        with_canceled = order_counts()
        badge = order_filter_counts()['payment_status']['process_deposit']
    finally:
        ctx.pop()
    assert with_canceled['deposits_unprocessed_total'] == 5
    assert with_canceled['deposits_unprocessed_amount'] == 1370.0  # + the canceled 300
    assert badge == 5


def test_the_cards_follow_the_page_filters(app):
    seed_orders(app)
    # A started-only page can no longer hold a single unprocessed deposit.
    started = counters_with_filters(app, status='started')
    assert started['deposits_unprocessed_total'] == 0
    assert started['deposits_unprocessed_amount'] == 0.0
    canceled = counters_with_filters(app, status='canceled')
    assert canceled['deposits_unprocessed_total'] == 0
    returned = counters_with_filters(app, status='returned')
    assert returned['deposits_unprocessed_total'] == 3
    assert returned['deposits_unprocessed_amount'] == 820.0


def test_the_cards_and_the_badge_stay_branch_scoped(app):
    seed_orders(app)
    branch_one = counters_with_filters(app, branch_id=1)
    branch_two = counters_with_filters(app, branch_id=2)
    everything = counters_with_filters(app)

    assert branch_one['deposits_unprocessed_total'] == 2
    assert branch_one['deposits_unprocessed_amount'] == 700.0
    assert branch_two['deposits_unprocessed_total'] == 1
    assert branch_two['deposits_unprocessed_amount'] == 120.0
    assert everything['deposits_unprocessed_amount'] == 820.0
    assert badge_with_filters(app, branch_id=2) == 1
    assert badge_with_filters(app) == 3


# ------------------------------------------------------- the folder itself (HTML)


def test_the_process_deposit_folder_lists_returned_rows_only(client, app):
    seed_orders(app)
    login(client)
    html = client.get('/orders?payment_status=process_deposit').data
    rows = _rows(html)

    assert {status for _, status, _, _ in rows} == {'returned'}
    listed = {number for number, _, _, _ in rows}
    assert listed == {'ORD-40001', 'ORD-40004', 'ORD-40009'}
    for gone in ('ORD-40002', 'ORD-40003'):
        assert gone not in listed, f'{gone} is not an actionable deposit'


def test_the_card_the_badge_and_the_folder_all_agree(client, app):
    """The ABI-341953032 invariant: one definition, four surfaces, one number."""
    seed_orders(app)
    login(client)
    listing = client.get('/orders').data
    folder = client.get('/orders?payment_status=process_deposit').data

    cards = _cards(listing)
    rows = _rows(folder)

    assert _badge(listing) == 3
    assert cards['Unprocessed deposits'][0] == str(len(rows)) == '3'
    # The per-row "Deposit to process" column sums to the money card exactly - the
    # applied-400/refund-0 row sits in the count and contributes R0.00 to the value
    # on BOTH surfaces (it shows "Done" in the column).
    by_number = {number: deposit for number, _, _, deposit in rows}
    assert round(sum(_money(row[3]) for row in rows), 2) == \
        float(cards['Unprocessed deposit value'][0].lstrip('R'))
    assert cards['Unprocessed deposit value'][0] == 'R820.00'
    assert by_number['ORD-40004'] == '<small>Done</small>'
    assert _money(by_number['ORD-40004']) == 0.0


def test_the_started_and_canceled_orders_are_still_reachable_in_the_plain_list(client, app):
    """Narrowing the folder must not hide the orders themselves."""
    seed_orders(app)
    login(client)

    listing = client.get('/orders').data
    listed = {row[0] for row in _rows(listing)}
    assert {'ORD-40002', 'ORD-40003'} <= listed
    # ...and each is still one status filter away, with its own status badge.
    assert 'ORD-40002' in client.get('/orders?status=started').data.decode()
    assert 'ORD-40003' in client.get('/orders?status=canceled').data.decode()


def test_the_main_totals_row_still_shows_six_cards_with_two_red(client, app):
    seed_orders(app)
    login(client)
    section = _metric_section(client.get('/orders').data, b'id="orders-metrics"')
    cards = _sections(section)

    assert [label for label, _, _ in cards] == [
        'Orders', 'Items ordered', 'Revenue received', 'Due',
        'Unprocessed deposits', 'Unprocessed deposit value',
    ]
    values = {label: value for label, value, _ in cards}
    assert values['Unprocessed deposits'] == '3'
    assert values['Unprocessed deposit value'] == 'R820.00'
    assert [label for label, _, cls in cards if 'is-alert' in cls] == \
        ['Due', 'Unprocessed deposit value']


# --------------------------------------------------------------- staff visibility


def test_staff_see_the_unprocessed_deposit_count_and_value(client, app):
    seed_orders(app)
    html = _staff_orders(client, app, 'Depot Two Staff', 2)
    cards = _cards(html)

    assert [label for label, _, _ in _sections(_metric_section(html, b'orders-staff-metrics'))] == [
        'Due', 'Unprocessed deposits', 'Unprocessed deposit value',
    ]
    # Branch 2 only: one returned order, R120 of deposit still to process.
    assert cards['Unprocessed deposits'][0] == '1'
    assert cards['Unprocessed deposit value'][0] == 'R120.00'
    assert cards['Due'][0] == 'R90.00'
    # Plain count, red money - exactly like the main row.
    assert 'is-alert' not in cards['Unprocessed deposits'][1]
    assert 'is-alert' in cards['Unprocessed deposit value'][1]
    # The main-only totals row (Orders / Items / Revenue) is still withheld.
    assert 'Orders' not in cards and 'Revenue received' not in cards
    assert b'metrics-toggle' not in html


def test_staff_deposit_cards_equal_their_own_badge_and_folder(client, app):
    """DB-to-UI parity for the staff view: card == badge == folder rows."""
    seed_orders(app)
    html = _staff_orders(client, app, 'Depot Two Staff', 2)
    cards = _cards(html)

    assert _badge(html) == 1  # the rail's own "Process deposit" badge
    folder = client.get('/orders?payment_status=process_deposit').data
    rows = _rows(folder)
    assert [number for number, _, _, _ in rows] == ['ORD-40009']
    assert cards['Unprocessed deposits'][0] == str(len(rows)) == '1'
    assert cards['Unprocessed deposit value'][0] == 'R%.2f' % sum(_money(row[3]) for row in rows)
    assert 'ORD-40002' not in folder.decode()


def test_staff_deposit_cards_follow_the_page_filters(client, app):
    seed_orders(app)
    _staff_orders(client, app, 'Depot Two Staff', 2)

    started = client.get('/orders?status=started').data
    started_cards = _cards(started)
    assert started_cards['Unprocessed deposits'][0] == '0'
    assert started_cards['Unprocessed deposit value'][0] == 'R0.00'

    returned = client.get('/orders?status=returned').data
    returned_cards = _cards(returned)
    assert returned_cards['Unprocessed deposits'][0] == '1'
    assert returned_cards['Unprocessed deposit value'][0] == 'R120.00'


def test_a_depot_with_no_unprocessed_deposits_shows_a_zero_card(client, app):
    seed_orders(app)
    ctx = _session_context(app)
    try:
        get_db().execute("UPDATE orders SET status = 'started' WHERE order_number = 'ORD-40009'")
        get_db().commit()
    finally:
        ctx.pop()

    html = _staff_orders(client, app, 'Depot Two Staff', 2)
    cards = _cards(html)
    assert cards['Unprocessed deposits'][0] == '0'
    assert cards['Unprocessed deposit value'][0] == 'R0.00'


# ----------------------------------------------------------------------- the css


def _css():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'static', 'css', 'app.css'), encoding='utf-8') as handle:
        return handle.read()


def test_the_staff_bar_lays_three_cards_out_responsively():
    css = _css()
    staff = re.search(r'\.orders-metrics-staff\{([^}]*)\}', css)
    assert staff, '.orders-metrics-staff has no rule in app.css'
    rule = staff.group(1)
    assert 'repeat(auto-fit,minmax(' in rule.replace(' ', '')
    minimum = re.search(r'minmax\((\d+)px', rule)
    assert minimum, rule
    # A real card width, not 0: the rule is declared after the .orders-metrics phone
    # breakpoints, so it is the one that decides the staff bar's column count.
    assert int(minimum.group(1)) >= 160, minimum.group(1)
    assert re.search(r'\.metrics\{[^}]*grid-template-columns:repeat\(4,minmax\(0,1fr\)\)', css)
    assert re.search(r'\.orders-metrics\{grid-template-columns:repeat\(2,minmax\(0,1fr\)\)', css)
    # The red money still uses the danger token.
    assert re.search(r'\.metric-card\.is-alert b\{[^}]*var\(--danger\)', css)
    assert '--danger:#d92d20' in css
