"""Ticket ABI-341953052 - dashboard POS Cashup.

Requested edit: add a POS Cashup section after Cash up, mirroring cash-up but
counting card/POS payments and showing the expected card total.
"""

import os
import tempfile

import pytest
from flask import session as flask_session

from app import create_app
from app.db import get_db, run_migrations
from app.services import cash
from app.services.access import create_additional_user

TODAY = cash.today_iso()
FUTURE = '2030-01-01'


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


def login(client, app, name=None, password='admin123'):
    with app.app_context():
        if name is None:
            row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
        else:
            row = get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
    return client.post('/login', data={'user_id': str(row['id']), 'password': password}, follow_redirects=True)


def add_staff(app, name, branch_id, password='staff123'):
    with app.app_context():
        user_id, error = create_additional_user(name, password, branch_id=branch_id)
        assert error is None, error
        return user_id


def seed_payment(app, amount, method, day=TODAY, branch_id=1, number='ORD-POS-1', status='paid', deleted_at=''):
    with app.app_context():
        db = get_db()
        db.execute(
            """INSERT INTO orders (order_number, booking_type, collect_branch_id, return_branch_id,
            status, payment_status, start_at, end_at, subtotal, tax_total, deposit_total, total,
            due_total, notes, created_at)
            VALUES (?, 'return', ?, ?, 'started', 'paid', ?, ?, 100, 15, 0, 115, 0, '', ?)""",
            (number, branch_id, branch_id, f'{day} 08:00', f'{day} 18:00', f'{day} 08:00'),
        )
        order_id = db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()['id']
        db.execute(
            """INSERT INTO payments (order_id, amount, method, reference, status, payment_date,
            deleted_at, created_at) VALUES (?, ?, ?, '', ?, ?, ?, ?)""",
            (order_id, amount, method, status, day, deleted_at, f'{day} 09:00'),
        )
        db.commit()
        return order_id


def summary(app, day=TODAY, branch_id=1, staff_user_id=None):
    with app.test_request_context('/dashboard'):
        flask_session['user_id'] = staff_user_id or 1
        flask_session['user_role'] = 'staff' if staff_user_id else 'owner'
        return cash.day_summary(day=day, branch_id=branch_id)


def cash_rows(app):
    with app.app_context():
        return [dict(row) for row in get_db().execute('SELECT branch_id, business_day, counted_card, counted_cash FROM cash_ups ORDER BY branch_id').fetchall()]


def test_card_received_counts_only_paid_non_deleted_card_payments_for_the_depot(client, app):
    seed_payment(app, 350, 'card', branch_id=1, number='ORD-POS-A')
    seed_payment(app, 50, 'CARD', branch_id=1, number='ORD-POS-B')
    seed_payment(app, 80, 'cash', branch_id=1, number='ORD-POS-C')
    seed_payment(app, 90, 'card', branch_id=2, number='ORD-POS-D')
    seed_payment(app, 70, 'card', branch_id=1, number='ORD-POS-E', deleted_at=f'{TODAY} 10:00')
    seed_payment(app, 60, 'card', branch_id=1, number='ORD-POS-F', status='draft')

    data = summary(app, branch_id=1)
    assert data['card_received'] == 400.0
    assert data['expected_card'] == 400.0
    assert data['cash_received'] == 80.0


def test_pos_cashup_saves_counted_card_and_variance_without_touching_cash_count(client, app):
    seed_payment(app, 400, 'card')
    login(client, app)
    body = client.post('/cash-up/pos', data={'day': TODAY, 'branch': '1', 'counted_card': '390.50'}, follow_redirects=True).get_data(as_text=True)

    assert 'POS cashup saved' in body
    data = summary(app)
    assert data['pos_cashed_up'] is True
    assert data['counted_card'] == 390.5
    assert data['expected_card'] == 400.0
    assert data['pos_variance'] == -9.5
    assert data['pos_variance_display'] == '-R9.50'
    assert data['pos_variance_label'] == 'Short by R9.50'
    assert data['cashed_up'] is False
    assert cash_rows(app)[0]['counted_cash'] is None


def test_dashboard_places_pos_cashup_after_cash_up_and_before_spare_wheel_count(client, app):
    seed_payment(app, 275, 'card')
    login(client, app)
    body = client.get('/dashboard?branch=1').get_data(as_text=True)

    cash_up = body.index('<h2>Cash up · Branch 1</h2>')
    pos = body.index('<h2>POS Cashup · Branch 1</h2>')
    spare = body.index('<h2>Spare wheel count</h2>')
    assert cash_up < pos < spare
    panel = body[pos:body.index('</section>', pos)]
    assert 'action="/cash-up/pos"' in panel
    assert 'name="counted_card"' in panel
    assert 'Expected card total' in panel
    assert 'R275.00' in panel
    assert 'Save POS cashup' in panel


def test_all_branches_pos_cashup_is_read_only_aggregate(client, app):
    seed_payment(app, 100, 'card', branch_id=1, number='ORD-POS-1')
    seed_payment(app, 225, 'card', branch_id=2, number='ORD-POS-2')
    with app.app_context():
        cash.save_pos_cash_up(TODAY, '90', branch_id=1)
    login(client, app)
    body = client.get('/dashboard').get_data(as_text=True)

    assert '<h2>POS Cashup · All branches</h2>' in body
    panel = body[body.index('<h2>POS Cashup · All branches</h2>'):body.index('</section>', body.index('<h2>POS Cashup · All branches</h2>'))]
    assert 'R325.00' in panel
    assert 'R90.00' in panel
    assert 'Partial: 1 of 3 depots POS cashed up' in panel
    assert 'action="/cash-up/pos"' not in panel


def test_crafted_all_branches_pos_post_is_refused(client, app):
    login(client, app)
    body = client.post('/cash-up/pos', data={'day': TODAY, 'branch': 'all', 'counted_card': '10'}, follow_redirects=True).get_data(as_text=True)
    assert 'Select a branch to save POS cashup' in body
    assert cash_rows(app) == []


def test_pos_cashup_rejects_junk_negative_and_future_day(client, app):
    login(client, app)
    assert 'Enter the POS card total counted for the day' in client.post('/cash-up/pos', data={'day': TODAY, 'branch': '1', 'counted_card': 'x'}, follow_redirects=True).get_data(as_text=True)
    assert 'Counted POS card total cannot be negative' in client.post('/cash-up/pos', data={'day': TODAY, 'branch': '1', 'counted_card': '-5'}, follow_redirects=True).get_data(as_text=True)
    assert 'Cash up today or a past day only' in client.post('/cash-up/pos', data={'day': FUTURE, 'branch': '1', 'counted_card': '5'}, follow_redirects=True).get_data(as_text=True)
    assert cash_rows(app) == []


def test_staff_cannot_widen_pos_cashup_to_another_depot(client, app):
    add_staff(app, 'Depot Two Clerk', 2)
    seed_payment(app, 100, 'card', branch_id=1, number='ORD-POS-1')
    seed_payment(app, 220, 'card', branch_id=2, number='ORD-POS-2')
    login(client, app, 'Depot Two Clerk', 'staff123')
    client.post('/cash-up/pos', data={'day': TODAY, 'branch': '1', 'counted_card': '210'}, follow_redirects=True)

    rows = cash_rows(app)
    assert rows == [{'branch_id': 2, 'business_day': TODAY, 'counted_card': 210.0, 'counted_cash': None}]
    assert summary(app, branch_id=2)['expected_card'] == 220.0


def test_counted_card_column_is_in_schema_and_migration(app):
    with app.app_context():
        db = get_db()
        columns = {row['name'] for row in db.execute('PRAGMA table_info(cash_ups)').fetchall()}
        assert 'counted_card' in columns
        db.execute('ALTER TABLE cash_ups DROP COLUMN counted_card')
        run_migrations(db)
        columns = {row['name'] for row in db.execute('PRAGMA table_info(cash_ups)').fetchall()}
        assert 'counted_card' in columns


def test_pos_cashup_reaches_day_report_csv_and_pdf(client, app):
    seed_payment(app, 500, 'card')
    with app.app_context():
        cash.save_pos_cash_up(TODAY, '505', branch_id=1)
    login(client, app)

    csv_body = client.get(f'/cash-up/export.csv?branch=1&day={TODAY}').get_data(as_text=True)
    assert 'POS Cashup,Expected card total,R500.00' in csv_body
    assert 'POS Cashup,POS/card counted,R505.00' in csv_body
    assert 'POS Cashup,Variance (counted - expected),+R5.00 (Over by R5.00)' in csv_body

    pdf_body = client.get(f'/cash-up/report.pdf?branch=1&day={TODAY}').data.decode('latin-1')
    assert 'POS CASHUP' in pdf_body
    assert 'Expected card total' in pdf_body
    assert 'R500.00' in pdf_body
