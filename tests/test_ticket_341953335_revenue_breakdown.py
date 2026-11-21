"""The "What's the Revenue for the day made of?" breakdown (ABI-341953335).

The client asked what the Midrand revenue figure for a day is made of. These
tests pin the answer to the same engine that produces the card: every line's
recognised amount sums to the Revenue card, a held security deposit is received
money but not revenue, a deposit refund reduces the method card without touching
revenue, and one depot's receipts never appear in another depot's breakdown.
"""
import os
import tempfile

import pytest

from app import create_app
from app.db import get_db
from app.services.reports import daily_revenue_breakdown, dashboard_day_metrics
from app.services.timezone import local_now_iso

DAY = '2026-11-21'
OTHER_DAY = '2026-11-20'


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
    # A fresh test database ships "Branch 1/2/3"; name them like the live depots.
    with application.app_context():
        db = get_db()
        db.execute("UPDATE branches SET name = 'Midrand' WHERE id = 1")
        db.execute("UPDATE branches SET name = 'Pretoria' WHERE id = 2")
        db.commit()
    yield application
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


def _branch_ids(db):
    rows = db.execute("SELECT id, name FROM branches ORDER BY id").fetchall()
    return {row['name']: row['id'] for row in rows}


def _customer(db, name, branch_id=1):
    db.execute("INSERT INTO customers (name, branch_id, created_at) VALUES (?, ?, ?)",
               (name, branch_id, f'{DAY}T07:00:00'))
    return db.execute("SELECT id FROM customers WHERE name = ?", (name,)).fetchone()['id']


def _order(db, number, customer_id, branch_id=1, status='started', total=0.0,
           deposit_option='no_deposit', deposit_total=0.0, created_at=None):
    db.execute(
        """INSERT INTO orders (order_number, customer_id, booking_type, collect_branch_id,
        return_branch_id, status, payment_status, subtotal, tax_total, deposit_option,
        deposit_total, total, due_total, notes, created_at)
        VALUES (?, ?, 'return', ?, ?, ?, 'paid', ?, 0, ?, ?, ?, 0, '', ?)""",
        (number, customer_id, branch_id, branch_id, status, total, deposit_option,
         deposit_total, total, created_at or f'{DAY}T08:00:00'),
    )
    return db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()['id']


def _payment(db, order_id, amount, method, day=DAY, created_at=None):
    db.execute(
        """INSERT INTO payments (order_id, amount, method, reference, status, payment_date,
        deleted_at, created_at) VALUES (?, ?, ?, '', 'paid', ?, '', ?)""",
        (order_id, amount, method, f'{day}T10:00:00', created_at or f'{day}T10:00:00'),
    )


def seed(app):
    """Midrand trading for the day, a Pretoria receipt, and a deposit refund."""
    with app.app_context():
        db = get_db()
        branches = _branch_ids(db)
        midrand = branches['Midrand']
        pretoria = branches['Pretoria']

        ns = _customer(db, 'NS Urban Grown Projects', midrand)
        sello = _customer(db, 'Sello Cosnet Sekhwela', midrand)
        tyron = _customer(db, 'Tyron Bila', midrand)
        alton = _customer(db, 'Alton Mudau', midrand)
        pta = _customer(db, 'Pretoria Customer', pretoria)

        plain = _order(db, 'ORD-1001', ns, midrand, total=935.0)
        _payment(db, plain, 935.0, 'card')
        # A refundable security deposit: R995 received, R500 of it still held.
        deposit = _order(db, 'ORD-1002', sello, midrand, total=995.0,
                         deposit_option='security_deposit', deposit_total=500.0)
        _payment(db, deposit, 995.0, 'card')
        cash = _order(db, 'ORD-1003', tyron, midrand, total=300.0)
        _payment(db, cash, 300.0, 'cash')
        # Yesterday's money must not leak into today.
        _payment(db, plain, 50.0, 'card', day=OTHER_DAY)
        # Another depot's receipt.
        other = _order(db, 'ORD-2001', pta, pretoria, total=700.0)
        _payment(db, other, 700.0, 'card')

        # A deposit paid back today, on the card machine.
        returned = _order(db, 'ORD-1004', alton, midrand, status='returned', total=1790.0,
                          deposit_option='security_deposit', deposit_total=600.0)
        db.execute(
            """UPDATE orders SET deposit_refund_amount = 600, deposit_processed_at = ?,
            deposit_process_method = 'card' WHERE id = ?""",
            (f'{DAY}T09:00:00', returned),
        )
        db.commit()
        return {'midrand': midrand, 'pretoria': pretoria}


def metrics(app, branch_id):
    with app.test_request_context():
        return dashboard_day_metrics(day=DAY, branch_id=branch_id)


def breakdown(app, branch_id):
    with app.test_request_context():
        return daily_revenue_breakdown(day=DAY, branch_id=branch_id)


def test_breakdown_lines_reconcile_to_the_revenue_card(app):
    ids = seed(app)
    day = metrics(app, ids['midrand'])
    data = breakdown(app, ids['midrand'])

    # Received today: 935 + 995 card, 300 cash. The R500 deposit is held.
    assert data['received_total'] == 2230.0
    assert data['held_total'] == 500.0
    assert data['total'] == 1730.0
    assert day['revenue'] == data['total']
    assert data['reconciles'] is True
    # Every line's recognised amount adds up to the card, and to the panel's own
    # received-less-held arithmetic.
    assert round(sum(line['recognised'] for line in data['lines']), 2) == data['total']
    assert data['reconciles_total'] == data['total']
    # The category split is the same money again.
    assert round(sum(category['amount'] for category in data['categories']), 2) == data['total']


def test_a_held_deposit_is_received_but_not_revenue(app):
    ids = seed(app)
    data = breakdown(app, ids['midrand'])

    deposit_line = next(line for line in data['lines'] if line['order_number'] == 'ORD-1002')
    assert deposit_line['received'] == 995.0
    assert deposit_line['recognised'] == 495.0
    assert deposit_line['held'] == 500.0
    assert deposit_line['category'] == 'trailer_rentals'


def test_a_deposit_refund_moves_the_method_card_but_not_revenue(app):
    ids = seed(app)
    day = metrics(app, ids['midrand'])
    data = breakdown(app, ids['midrand'])

    # 935 + 995 card received, less the R600 card deposit refunded today.
    assert day['card_payments'] == 1330.0
    assert data['refund_total'] == 600.0
    assert [(refund['key'], refund['amount']) for refund in data['refunds']] == [('card', 600.0)]
    # The refund is a liability moving back out, so revenue is unchanged by it.
    assert data['total'] == 1730.0


def test_each_depot_sees_only_its_own_receipts(app):
    ids = seed(app)
    midrand = breakdown(app, ids['midrand'])
    pretoria = breakdown(app, ids['pretoria'])

    midrand_orders = {line['order_number'] for line in midrand['lines']}
    pretoria_orders = {line['order_number'] for line in pretoria['lines']}
    assert 'ORD-2001' not in midrand_orders
    assert 'ORD-1001' not in pretoria_orders
    assert midrand['total'] == 1730.0
    assert pretoria['total'] == 700.0


def test_the_day_boundary_is_respected(app):
    ids = seed(app)
    data = breakdown(app, ids['midrand'])
    assert all(line['payment_date'].startswith(DAY) for line in data['lines'])
    # Yesterday's R50 card payment is not in today's receipts.
    assert data['received_total'] == 2230.0


def test_a_day_with_no_money_returns_zeros_not_an_error(app):
    ids = seed(app)
    with app.test_request_context():
        data = daily_revenue_breakdown(day='2026-01-01', branch_id=ids['midrand'])
    assert data['total'] == 0.0
    assert data['lines'] == []
    assert data['received_total'] == 0.0
    assert data['held_total'] == 0.0
    assert data['reconciles'] is True
    assert [category['amount'] for category in data['categories']] == [0.0, 0.0, 0.0]


def test_legacy_receipts_land_in_other_unclassified(app):
    ids = seed(app)
    with app.app_context():
        db = get_db()
        customer = db.execute("SELECT id FROM customers WHERE name = 'NS Urban Grown Projects'").fetchone()['id']
        db.execute(
            """INSERT INTO legacy_balance_payments (customer_id, branch_id, amount, method,
            reference, status, payment_date, created_at)
            VALUES (?, ?, 120, 'eft', 'LEG-1', 'active', ?, ?)""",
            (customer, ids['midrand'], f'{DAY}T12:00:00', f'{DAY}T12:00:00'),
        )
        db.commit()
    data = breakdown(app, ids['midrand'])
    legacy = next(line for line in data['lines'] if line['source'] == 'legacy')
    assert legacy['recognised'] == 120.0
    assert legacy['category'] == 'unattributed'
    assert data['total'] == 1850.0
    unattributed = next(c for c in data['categories'] if c['key'] == 'unattributed')
    assert unattributed['amount'] == 120.0


def _login_owner(client, app):
    with app.app_context():
        owner = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
    with client.session_transaction() as sess:
        sess['user_id'] = owner['id']
        sess['user_role'] = 'owner'


def test_the_partial_renders_the_receipts_for_a_main_session(client, app):
    ids = seed(app)
    _login_owner(client, app)
    response = client.get(f'/dashboard/partials/day/revenue?branch={ids["midrand"]}&day={DAY}')
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'ORD-1001' in page
    assert 'ORD-1002' in page
    assert 'ORD-2001' not in page          # Pretoria never leaks in
    assert 'held' in page.lower()
    assert 'Revenue for the day' in page


def test_a_staff_session_cannot_ask_for_a_past_day(client, app):
    seed(app)
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['user_role'] = 'staff'
        sess['staff_modules'] = ['dashboard']   # may see the dashboard, not history
    response = client.get('/dashboard/partials/day/revenue?day=2020-01-01')
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert local_now_iso(timespec='seconds')[:10] in page
    assert '2020-01-01' not in page
