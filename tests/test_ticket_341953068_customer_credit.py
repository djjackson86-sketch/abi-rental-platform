"""Ticket ABI-341953068: refund/deposit refund credit can be kept on customer."""
import os
import tempfile

import pytest
from flask import session as flask_session

from app import create_app
from app.db import get_db
from app.services import cash
from app.services.customer_credits import customer_credit_balance
from app.services.payments import payment_summary

DAY = '2026-09-28'


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
        row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
    return client.post('/login', data={'user_id': str(row['id']), 'password': 'admin123'}, follow_redirects=True)


def seed_customer(db, name='Credit Customer'):
    db.execute("""INSERT INTO customers (customer_type, name, email, phone, marketing_opt_in,
        address_line1, address_line2, suburb, city, province, postal_code, country,
        custom_fields_json, balance_due, standard_discount_percent, client_verified,
        is_blocked, blocked_reason, created_by_user_id, branch_id, created_at)
        VALUES ('individual', ?, '', '', 0, '', '', '', '', '', '', 'South Africa', '{}', 0, 0, NULL, 0, '', 1, 1, ?)""",
        (name, f'{DAY}T08:00:00'))
    return db.execute("SELECT id FROM customers WHERE name = ?", (name,)).fetchone()['id']


def seed_order(db, customer_id, number, *, total=1000, deposit=0, status='returned', due=None):
    due = total if due is None else due
    db.execute("""INSERT INTO orders (order_number, customer_id, booking_type, collect_branch_id,
        return_branch_id, status, payment_status, start_at, end_at, subtotal, tax_total,
        deposit_total, total, due_total, notes, created_at)
        VALUES (?, ?, 'return', 1, 1, ?, ?, ?, ?, ?, 0, ?, ?, ?, '', ?)""",
        (number, customer_id, status, 'payment_due' if due > 0 else 'paid',
         f'{DAY}T09:00:00', f'{DAY}T17:00:00', total - deposit, deposit, total, due, f'{DAY}T09:00:00'))
    return db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()['id']


def seed_paid(db, order_id, amount, method='cash'):
    db.execute("""INSERT INTO payments (order_id, amount, method, reference, status, payment_date, created_at)
        VALUES (?, ?, ?, 'seed', 'paid', ?, ?)""", (order_id, amount, method, DAY, f'{DAY}T10:00:00'))


def as_owner(app):
    ctx = app.test_request_context('/dashboard')
    ctx.push()
    flask_session['user_id'] = 1
    flask_session['user_role'] = 'owner'
    return ctx


def test_order_refund_can_credit_customer_then_credit_can_pay_a_new_order(app, client):
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db)
        refund_order = seed_order(db, customer_id, 'ORD-CREDIT-1', total=1000, due=-250)
        seed_paid(db, refund_order, 1250)
        next_order = seed_order(db, customer_id, 'ORD-CREDIT-2', total=400, due=400)
        db.commit()

    response = client.post(f'/orders/{refund_order}/refund', data={
        'refund_amount': '250.00',
        'refund_method': 'customer_credit',
        'refund_date': f'{DAY}T12:00',
        'reference': 'Keep as customer credit',
    }, follow_redirects=True)
    assert b'Refund recorded' in response.data
    with app.app_context():
        assert customer_credit_balance(customer_id) == 250.0
        row = get_db().execute("SELECT amount, source_type FROM customer_credits WHERE customer_id = ?", (customer_id,)).fetchone()
        assert row['amount'] == 250.0 and row['source_type'] == 'order_refund'

    customer_page = client.get(f'/customers/{customer_id}').data.decode()
    assert 'Customer credit' in customer_page and 'R250.00' in customer_page

    order_form = client.get(f'/orders/new?customer_id={customer_id}').data.decode()
    assert 'Previous Orders Balance' in order_form
    assert 'balance-credit' in order_form and 'R250.00' in order_form

    detail = client.get(f'/orders/{next_order}').data.decode()
    assert 'value="customer_credit"' in detail and 'Use Customer Credit (R250.00)' in detail

    paid = client.post(f'/orders/{next_order}/payments', data={
        'amount': '200.00',
        'method': 'customer_credit',
        'payment_date': f'{DAY}T13:00',
        'reference': 'Use customer credit',
    }, follow_redirects=True)
    assert b'Payment recorded' in paid.data
    with app.app_context():
        assert customer_credit_balance(customer_id) == 50.0
        summary = payment_summary(next_order)
        assert summary['paid_total'] == 200.0
        assert summary['due_total'] == 200.0
        method = get_db().execute("SELECT method FROM payments WHERE order_id = ? ORDER BY id DESC LIMIT 1", (next_order,)).fetchone()['method']
        assert method == 'customer_credit'


def test_deposit_refund_credit_updates_customer_credit_and_stays_out_of_cash_up(app, client):
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db, 'Deposit Credit Customer')
        order_id = seed_order(db, customer_id, 'ORD-CREDIT-DEP', total=2000, deposit=1000, due=2000)
        seed_paid(db, order_id, 1500)
        db.commit()

    response = client.post(f'/orders/{order_id}/settle-return', data={
        'deposit_process_method': 'customer_credit',
        'deposit_processed_at': f'{DAY}T14:00',
        'deposit_note': 'Credit remaining deposit',
    }, follow_redirects=True)
    assert b'Deposit settled' in response.data
    with app.app_context():
        assert customer_credit_balance(customer_id) == 500.0
        order = get_db().execute("SELECT deposit_process_method, deposit_refund_amount FROM orders WHERE id = ?", (order_id,)).fetchone()
        assert order['deposit_process_method'] == 'customer_credit'
        assert order['deposit_refund_amount'] == 500.0
    ctx = as_owner(app)
    try:
        assert cash.cash_deposit_refunds(DAY, 1) == 0.0
    finally:
        ctx.pop()

    edit = client.get(f'/orders/{order_id}/deposit-refund/edit').data.decode()
    assert 'Credit to Customer' in edit and 'value="customer_credit" selected' in edit

    client.post(f'/orders/{order_id}/deposit-refund/edit', data={
        'refund_amount': '300.00',
        'deposit_process_method': 'eft',
        'deposit_processed_at': f'{DAY}T15:00',
        'deposit_note': 'Switched to EFT',
    }, follow_redirects=True)
    with app.app_context():
        assert customer_credit_balance(customer_id) == 0.0
        statuses = [row['status'] for row in get_db().execute(
            "SELECT status FROM customer_credits WHERE customer_id = ?", (customer_id,)).fetchall()]
        assert statuses == ['reversed']
