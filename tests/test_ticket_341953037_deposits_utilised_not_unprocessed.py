"""Ticket ABI-341953037 - a fully utilised deposit is not "unprocessed" any more.

Requested edit: "if deposit has been utilised/used it must not fall under
unprocessed because its no longer available".

``use_return_deposit()`` (``app/services/orders.py``) lets the office spend a
customer's security deposit against what the order still owes. It records the split
as ``deposit_applied_amount`` / ``deposit_refund_amount`` but deliberately leaves
``deposit_process_method`` and ``deposit_processed_at`` empty - those two fields mean
"the office paid the refund out", which is a different event. The shared
``_process_deposit_clause()`` predicate therefore could not see the difference and a
fully spent deposit kept sitting in the rail's "Process deposit" folder (and in the
count / money cards) while its own row already read "Done", i.e. nothing to process.

The predicate now reads: still in the set when nothing has been applied or refunded
yet, or when a **refund remainder** is still owed to the customer. ``applied > 0``
with ``refund = 0`` means the whole deposit went onto the order - fully used, nothing
left to hand back - so it drops out. A partially used deposit with a remainder
(``applied > 0 AND refund > 0``) stays in, because that money IS still owed; that is
the behaviour pinned by ``tests/test_app.py`` (the use-deposit remainder test).

These tests pin: a fully used deposit leaves the count, the money, the rail badge and
the folder rows; it does not disappear from the app (still in All, still under
``status=returned``, its detail page still shows the deposit as used); an untouched
deposit stays in; a partly used deposit with a remainder stays in and contributes
only its remainder; the settled/EFT path still leaves; the card == badge == folder
invariant survives; and ``deposit_to_process_amount()`` is untouched, so the per-row
column still reads "Done" (R0.00) for a fully used deposit.
"""
import os
import re
import tempfile

import pytest
from flask import session as flask_session

from app import create_app
from app.db import get_db
from app.services.orders import (_process_deposit_clause, deposit_to_process_amount,
                                 order_counts, order_filter_counts)

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


def _insert_order(db, number, status, *, deposit=0, applied=0, refund=0, method='',
                  processed='', due=0, branch_id=1):
    """One order with a deposit, inserted straight into the table (no app writes)."""
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


def seed(app):
    """The ticket's own case plus every neighbouring boundary.

    IN  (untouched)      : ORD-50001 returned, deposit 700, nothing applied/refunded
    IN  (remainder owed) : ORD-50002 returned, deposit 1000, applied 300, refund 200
    OUT (fully utilised) : ORD-50003 returned, deposit 600, applied 600, refund 0
                           <- the ticket: the deposit was spent, nothing is left
    OUT (settled / EFT)  : ORD-50004 returned, deposit 450, applied 450, refund 300,
                           method 'eft' + timestamp (the paid-out path)
    OUT (nothing to do)  : ORD-50005 returned, deposit 0
    """
    with app.app_context():
        db = get_db()
        ids = {
            'untouched': _insert_order(db, 'ORD-50001', 'returned', deposit=700),
            'remainder_owed': _insert_order(db, 'ORD-50002', 'returned', deposit=1000,
                                            applied=300, refund=200),
            'fully_used': _insert_order(db, 'ORD-50003', 'returned', deposit=600,
                                        applied=600),
            'settled': _insert_order(db, 'ORD-50004', 'returned', deposit=450,
                                     applied=450, refund=300, method='eft',
                                     processed=f'{DAY}T16:00:00'),
            'no_deposit': _insert_order(db, 'ORD-50005', 'returned'),
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


def _card(html, label):
    """(value, classes) of one metric card, main totals row or staff bar."""
    section = re.search(rb'<section class="[^"]*"[^>]*id="orders-(?:staff-)?metrics"[^>]*>(.*?)</section>',
                        html, re.S)
    assert section, 'the Orders metric section was not rendered'
    found = re.search(rb'<div class="([^"]*metric-card[^"]*)"><small>' + label +
                      rb'</small><b>(.*?)</b></div>', section.group(1), re.S)
    assert found, f'the {label!r} card was not rendered'
    return found.group(2).decode().strip(), found.group(1).decode()


def _folder_rows(client):
    html = client.get('/orders?payment_status=process_deposit').data
    return _rows(html)


# -------------------------------------------------- the ticket: a used-up deposit


def test_a_fully_utilised_deposit_leaves_the_count_the_value_and_the_badge(app):
    seed(app)
    counts = counters_with_filters(app)

    # ORD-50001 (700, untouched) + ORD-50002 (remainder 200 owed). The fully used
    # 600 and the settled one are out.
    assert counts['deposits_unprocessed_total'] == 2
    assert counts['deposits_unprocessed_amount'] == 900.0
    assert badge_with_filters(app) == 2


def test_a_fully_utilised_deposit_leaves_the_folder_of_orders_to_process(client, app):
    seed(app)
    login(client)
    rows = _folder_rows(client)

    listed = {number for number, _, _, _ in rows}
    assert listed == {'ORD-50001', 'ORD-50002'}
    assert 'ORD-50003' not in listed, \
        'a deposit that has been utilised is no longer available to process'
    assert all(status == 'returned' for _, status, _, _ in rows)


def test_the_used_up_order_does_not_vanish_from_the_app(client, app):
    """Leaving the folder must not hide the order - it is one filter away."""
    ids = seed(app)
    login(client)
    order_id = ids['fully_used']

    listing = client.get('/orders').data
    assert 'ORD-50003' in {row[0] for row in _rows(listing)}
    assert 'ORD-50003' in client.get('/orders?status=returned').data.decode()

    detail = client.get(f'/orders/{order_id}')
    assert detail.status_code == 200
    assert b'ORD-50003' in detail.data
    # The bookkeeping is preserved: the deposit was spent, not wiped.
    with app.app_context():
        order = get_db().execute(
            "SELECT deposit_total, deposit_applied_amount, deposit_refund_amount "
            "FROM orders WHERE id = ?", (order_id,)).fetchone()
    assert order['deposit_total'] == 600
    assert order['deposit_applied_amount'] == 600
    assert order['deposit_refund_amount'] == 0


def test_a_fully_utilised_deposit_rejoins_the_set_when_it_becomes_actionable_again(app):
    """The rule is the money, not a status - clearing the split brings it back."""
    seed(app)
    assert counters_with_filters(app)['deposits_unprocessed_total'] == 2
    assert badge_with_filters(app) == 2

    ctx = _session_context(app)
    try:
        db = get_db()
        db.execute("UPDATE orders SET deposit_applied_amount = 0, deposit_refund_amount = 0 "
                   "WHERE order_number = 'ORD-50003'")
        db.commit()
        after = order_counts()
        badge = order_filter_counts()['payment_status']['process_deposit']
    finally:
        ctx.pop()
    assert after['deposits_unprocessed_total'] == 3
    assert after['deposits_unprocessed_amount'] == 1500.0  # + the whole 600 again
    assert badge == 3


# ------------------------------------------------------------- the boundaries


def test_an_untouched_deposit_stays_in_the_set(client, app):
    seed(app)
    login(client)
    rows = _folder_rows(client)
    by_number = {number: (due, deposit) for number, _, due, deposit in rows}

    assert 'ORD-50001' in by_number
    assert _money(by_number['ORD-50001'][1]) == 700.0
    assert by_number['ORD-50001'][1] == '<strong>R700.00</strong>'


def test_a_partly_used_deposit_with_a_remainder_stays_in_owwing_only_its_remainder(app):
    """Pinned today by tests/test_app.py (use-deposit then settle-return): money
    still owed to the customer must keep the deposit actionable."""
    seed(app)
    counts = counters_with_filters(app)
    assert counts['deposits_unprocessed_total'] == 2
    # 700 untouched + only the 200 refund remainder of the partly used one.
    assert counts['deposits_unprocessed_amount'] == 900.0


def test_a_partly_used_deposit_with_nothing_left_over_leaves(app):
    seed(app)
    assert counters_with_filters(app)['deposits_unprocessed_total'] == 2
    ctx = _session_context(app)
    try:
        db = get_db()
        # Same order, but the whole deposit is now applied: nothing left to pay out.
        db.execute("UPDATE orders SET deposit_applied_amount = 1000, deposit_refund_amount = 0 "
                   "WHERE order_number = 'ORD-50002'")
        db.commit()
        after = order_counts()
        badge = order_filter_counts()['payment_status']['process_deposit']
    finally:
        ctx.pop()
    assert after['deposits_unprocessed_total'] == 1
    assert after['deposits_unprocessed_amount'] == 700.0
    assert badge == 1


def test_the_settled_eft_path_still_leaves_the_set(app):
    seed(app)
    ctx = _session_context(app)
    try:
        db = get_db()
        order = db.execute("SELECT deposit_process_method, deposit_processed_at "
                           "FROM orders WHERE order_number = 'ORD-50004'").fetchone()
    finally:
        ctx.pop()
    assert order['deposit_process_method'] == 'eft'
    assert order['deposit_processed_at'] == f'{DAY}T16:00:00'
    assert counters_with_filters(app)['deposits_unprocessed_total'] == 2


def test_an_order_with_no_deposit_was_never_in_the_set(app):
    seed(app)
    assert counters_with_filters(app)['deposits_unprocessed_total'] == 2
    ctx = _session_context(app)
    try:
        db = get_db()
        row = db.execute("SELECT deposit_total FROM orders WHERE order_number = 'ORD-50005'"
                         ).fetchone()
    finally:
        ctx.pop()
    assert not row['deposit_total']


# ------------------------------------------------- card == badge == folder


def test_the_card_the_badge_and_the_folder_still_all_agree(client, app):
    """The ABI-341953032 invariant survives the narrowing: one number everywhere."""
    seed(app)
    login(client)
    listing = client.get('/orders').data
    rows = _folder_rows(client)

    assert _badge(listing) == 2
    count_card, _ = _card(listing, b'Unprocessed deposits')
    money_card, money_classes = _card(listing, b'Unprocessed deposit value')
    assert count_card == str(len(rows)) == '2'
    assert money_card == 'R900.00'
    assert 'is-alert' in money_classes
    # The folder's own "Deposit to process" column sums to the money card exactly.
    assert round(sum(_money(row[3]) for row in rows), 2) == float(money_card.lstrip('R'))


def test_the_per_row_deposit_column_is_untouched_for_a_fully_used_deposit(client, app):
    """deposit_to_process_amount() is deliberately unchanged - R0.00 / "Done"."""
    seed(app)
    login(client)
    listing = client.get('/orders').data
    rows = {number: deposit for number, _, _, deposit in _rows(listing)}

    assert rows['ORD-50003'] == '<small>Done</small>'
    assert _money(rows['ORD-50003']) == 0.0
    assert rows['ORD-50002'] == '<strong>R200.00</strong>'  # the remainder only
    with app.app_context():
        by_number = {row['order_number']: row for row in
                     get_db().execute("SELECT * FROM orders").fetchall()}
    assert deposit_to_process_amount(by_number['ORD-50003']) == 0
    assert deposit_to_process_amount(by_number['ORD-50001']) == 700.0
    assert deposit_to_process_amount(by_number['ORD-50002']) == 200.0


def test_a_partly_used_deposit_on_a_started_order_is_still_out_of_the_set(app):
    """Ticket ABI-341953034's rule is untouched by this ticket."""
    seed(app)
    ctx = _session_context(app)
    try:
        db = get_db()
        db.execute("UPDATE orders SET status = 'started', deposit_applied_amount = 400, "
                   "deposit_refund_amount = 0 WHERE order_number = 'ORD-50001'")
        db.commit()
        after = order_counts()
    finally:
        ctx.pop()
    assert after['deposits_unprocessed_total'] == 1  # only ORD-50002's remainder
    assert after['deposits_unprocessed_amount'] == 200.0


def test_the_clause_is_still_a_single_definition_used_everywhere():
    """Source guard: the rail, its badge and the cards all read one predicate."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'app', 'services', 'orders.py'), encoding='utf-8') as handle:
        source = handle.read()
    # Exactly one definition (list_orders, the folder-where builder, the badge and
    # the money card are the four call sites) - no surface may hand-roll its own
    # applied/refund test.
    assert source.count('def _process_deposit_clause(') == 1
    assert source.count('_process_deposit_clause(') == 5


def test_the_predicate_itself_reads_exactly_this_rule():
    """The whole ticket in one string: fully used (`applied > 0`, `refund = 0`) is OUT."""
    flat = ' '.join(_process_deposit_clause('o').split())
    assert flat == (
        "o.status = 'returned' "
        "AND COALESCE(o.deposit_total, 0) > 0 "
        "AND COALESCE(o.deposit_processed_at, '') = '' "
        "AND COALESCE(o.deposit_process_method, '') = '' "
        "AND ((COALESCE(o.deposit_applied_amount, 0) = 0 "
        "AND COALESCE(o.deposit_refund_amount, 0) = 0) "
        "OR (COALESCE(o.deposit_applied_amount, 0) > 0 "
        "AND COALESCE(o.deposit_refund_amount, 0) > 0))"
    )
    # The applied/refund group must be an OR of two whole conditions - an
    # `... OR applied > 0` tail was exactly the bug (it kept applied>0/refund=0 in).
    assert 'refund_amount, 0) = 0 OR' not in flat
    # No alias -> no prefix (the unaliased folder-where path).
    bare = _process_deposit_clause('')
    assert 'status = ' in bare and 'o.' not in bare
