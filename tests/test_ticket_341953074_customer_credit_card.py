"""Ticket ABI-341953074: order form shows "Customer Credit" beside Previous Orders Balance.

The client asked for a "Customer Credit" label next to Previous Orders Balance that
returns the credit amount (in green) when the customer has credit. Before this ticket
the credit hijacked the Previous Orders Balance card (that card showed the credit and
turned green), so the true outstanding balance disappeared. Now:
  * Previous Orders Balance always shows the customer's real previous_orders_balance;
  * a sibling green card `#customer-credit-card` shows the credit, and is only rendered
    when the customer actually has credit ("if available");
  * the customer picker's datalist still carries both figures, and the page JS updates
    both cards when the selected customer changes (no reload).
"""
import json
import os
import re
import tempfile

import pytest

from app import create_app
from app.db import get_db

DAY = '2026-09-28'

CREDIT_CARD_RE = re.compile(
    r'<div class="stat-card customer-credit-card" id="customer-credit-card"[^>]*>'
    r'<span>Customer Credit</span>\s*'
    r'<strong id="customer-credit-value" class="balance-credit">R([0-9.]+)</strong>'
)
BALANCE_VALUE_RE = re.compile(
    r'<strong id="previous-balance-value" class="([^"]*)">R([0-9.]+)</strong>'
)


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


def seed_customer(db, name='Credit Card Customer'):
    db.execute("""INSERT INTO customers (customer_type, name, email, phone, marketing_opt_in,
        address_line1, address_line2, suburb, city, province, postal_code, country,
        custom_fields_json, balance_due, standard_discount_percent, client_verified,
        is_blocked, blocked_reason, created_by_user_id, branch_id, created_at)
        VALUES ('individual', ?, '', '', 0, '', '', '', '', '', '', 'South Africa', '{}', 0, 0, NULL, 0, '', 1, 1, ?)""",
        (name, f'{DAY}T08:00:00'))
    return db.execute("SELECT id FROM customers WHERE name = ?", (name,)).fetchone()['id']


def seed_order(db, customer_id, number, *, total=400, deposit=0, status='returned', due=None):
    due = total if due is None else due
    db.execute("""INSERT INTO orders (order_number, customer_id, booking_type, collect_branch_id,
        return_branch_id, status, payment_status, start_at, end_at, subtotal, tax_total,
        deposit_total, total, due_total, notes, created_at)
        VALUES (?, ?, 'return', 1, 1, ?, ?, ?, ?, ?, 0, ?, ?, ?, '', ?)""",
        (number, customer_id, status, 'payment_due' if due > 0 else 'paid',
         f'{DAY}T09:00:00', f'{DAY}T17:00:00', total - deposit, deposit, total, due, f'{DAY}T09:00:00'))
    return db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()['id']


def seed_credit(db, customer_id, amount, note='kept as customer credit'):
    db.execute("""INSERT INTO customer_credits (customer_id, source_order_id, amount, source_type,
        note, status, created_at) VALUES (?, NULL, ?, 'order_refund', ?, 'active', ?)""",
        (customer_id, amount, note, f'{DAY}T12:00:00'))


def datalist_summary(body, customer_id):
    """The customer picker payload the page JS uses to update the stat cards live."""
    match = re.search(r'data-id="%d" data-summary=\'(.*?)\'' % customer_id, body)
    assert match, f'no datalist option found for customer {customer_id}'
    return json.loads(match.group(1))


def test_customer_with_credit_shows_green_customer_credit_card_beside_balance(client, app):
    """A credited customer gets its own green card; the balance card keeps the real balance."""
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db)
        seed_order(db, customer_id, 'ORD-CREDITCARD-1', total=400, due=400)
        seed_credit(db, customer_id, 250)
        db.commit()

    body = client.get(f'/orders/new?customer_id={customer_id}').get_data(as_text=True)

    # 1. The new card exists, carries the green class and the real credit amount.
    credit_match = CREDIT_CARD_RE.search(body)
    assert credit_match, 'the Customer Credit card is missing from the order form'
    assert credit_match.group(1) == '250.00'

    # 2. It sits next to (immediately after) Previous Orders Balance, before Orders to Date.
    order_seen = (
        body.index('<div class="stat-card previous-balance-card"'),
        body.index('<div class="stat-card customer-credit-card"'),
        body.index('<div class="stat-card orders-to-date-card"'),
    )
    assert order_seen[0] < order_seen[1] < order_seen[2], 'the credit card is not next to Previous Orders Balance'

    # 3. Previous Orders Balance shows the true outstanding balance, not the credit.
    balance_match = BALANCE_VALUE_RE.search(body)
    assert balance_match, 'the Previous Orders Balance value is missing'
    assert balance_match.group(2) == '400.00'
    assert balance_match.group(1) == 'balance-owing', 'the balance card must not be painted as credit'

    # 4. The credit amount is green, like the other credit figures in the app.
    assert '.customer-credit-card{display:flex;align-items:baseline;gap:8px' in body
    assert '.customer-credit-card strong.balance-credit{color:#047857}' in body

    # 5. The old hijack (credit swapped into the balance card) is gone: the template no
    #    longer reads previous_orders_balance_display (the picker payload may still carry it).
    assert 'summary_obj.previous_orders_balance_display' not in body


def test_customer_without_credit_has_no_credit_card(client, app):
    """No credit means no card at all ("if available"), and the balance stays honest."""
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db, name='No Credit Customer')
        seed_order(db, customer_id, 'ORD-CREDITCARD-2', total=400, due=400)
        db.commit()

    body = client.get(f'/orders/new?customer_id={customer_id}').get_data(as_text=True)

    # No rendered card at all ("if available"). The inline JS still contains the label
    # text because it can build the card when a credited customer is picked, so assert on
    # the rendered markup (the element, not the script that can create it).
    assert not re.search(r'<div class="stat-card customer-credit-card"', body)
    assert not re.search(r'<strong id="customer-credit-value"', body)
    assert not re.search(r'<span>Customer Credit</span>', body)
    balance_match = BALANCE_VALUE_RE.search(body)
    assert balance_match and balance_match.group(2) == '400.00'
    assert balance_match.group(1) == 'balance-owing'


def test_partly_used_credit_shows_only_what_is_left(client, app):
    """Credit already spent on an order is not offered again (active rows only)."""
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db, name='Part Used Credit')
        seed_order(db, customer_id, 'ORD-CREDITCARD-3', total=400, due=400)
        seed_credit(db, customer_id, 250)
        # R200 of the R250 was used on another order.
        db.execute("""INSERT INTO customer_credits (customer_id, source_order_id, amount, source_type,
            note, status, created_at) VALUES (?, NULL, -200, 'order_payment', 'used', 'active', ?)""",
            (customer_id, f'{DAY}T13:00:00'))
        db.commit()

    body = client.get(f'/orders/new?customer_id={customer_id}').get_data(as_text=True)
    credit_match = CREDIT_CARD_RE.search(body)
    assert credit_match, 'the Customer Credit card is missing'
    assert credit_match.group(1) == '50.00'


def test_customer_picker_payload_carries_balance_and_credit_for_live_switching(client, app):
    """The datalist summary the JS reads must carry both figures, so switching works with no reload."""
    login(client)
    with app.app_context():
        db = get_db()
        credited = seed_customer(db, name='Picker Credit')
        seed_order(db, credited, 'ORD-CREDITCARD-4', total=400, due=400)
        seed_credit(db, credited, 250)
        plain = seed_customer(db, name='Picker Plain')
        seed_order(db, plain, 'ORD-CREDITCARD-5', total=900, due=900)
        db.commit()

    body = client.get('/orders/new').get_data(as_text=True)
    credited_summary = datalist_summary(body, credited)
    assert credited_summary['previous_orders_balance'] == 400.0
    assert credited_summary['customer_credit_balance'] == 250.0

    plain_summary = datalist_summary(body, plain)
    assert plain_summary['previous_orders_balance'] == 900.0
    assert plain_summary['customer_credit_balance'] == 0


def test_form_js_updates_balance_and_credit_cards_separately(client, app):
    """The page JS must fill the balance card from the balance key, and toggle the credit card."""
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db, name='JS Credit')
        seed_credit(db, customer_id, 250)
        db.commit()

    body = client.get(f'/orders/new?customer_id={customer_id}').get_data(as_text=True)
    assert "const value=summary?amount(summary.previous_orders_balance):0" in body
    assert "ensureCreditCard()" in body
    assert "creditCard.hidden=!(credit>0)" in body


def test_edit_order_form_still_renders_the_balance_card(client, app):
    """The same template serves Edit order; the cards must still render there."""
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db, name='Edit Form Customer')
        order_id = seed_order(db, customer_id, 'ORD-CREDITCARD-6', total=400, due=400,
                              status='draft')
        db.commit()

    response = client.get(f'/orders/{order_id}/edit')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '<div class="stat-card previous-balance-card"' in body
    assert '<span>Previous Orders Balance</span>' in body
    assert '.customer-credit-card{display:flex;align-items:baseline;gap:8px' in body
