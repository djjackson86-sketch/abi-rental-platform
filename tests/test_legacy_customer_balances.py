import json
import os
import re
import tempfile

import pytest

from app import create_app
from app.db import get_db
from app.services.legacy_balances import legacy_balance_summary, upsert_legacy_balance

DAY = '2026-10-04'


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


def seed_customer(db, name='Legacy Balance Customer'):
    db.execute("""INSERT INTO customers (customer_type, name, email, phone, marketing_opt_in,
        address_line1, address_line2, suburb, city, province, postal_code, country,
        custom_fields_json, balance_due, standard_discount_percent, client_verified,
        is_blocked, blocked_reason, created_by_user_id, branch_id, source_system, source_id, created_at)
        VALUES ('individual', ?, 'legacy@example.test', '0710000000', 0, '', '', '', '', '', '', 'South Africa', '{}', 0, 0, NULL, 0, '', 1, 1, 'booqable', 'old-cust-1', ?)""",
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


def test_legacy_balance_summary_totals_active_rows_only(app):
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db)
        upsert_legacy_balance(customer_id, 'old-cust-1', '3904', 30311, 'partially_paid', 'Stopped', 32386, 2075, 2, '2024-05-05', '2024-05-05')
        upsert_legacy_balance(customer_id, 'old-cust-1', '3905', 50, 'payment_due', 'Draft', 50, 0, 0, '', '2024-05-06')
        db.execute("UPDATE legacy_customer_balances SET status='collected' WHERE source_order_number='3905'")
        db.commit()

        summary = legacy_balance_summary(customer_id)
        assert summary['total'] == 30311.0
        assert summary['active_count'] == 1
        assert len(summary['entries']) == 2


def test_customer_page_shows_legacy_balance_panel(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db)
        upsert_legacy_balance(customer_id, 'old-cust-1', '3904', 30311, 'partially_paid', 'Stopped', 32386, 2075, 2, '2024-05-05', '2024-05-05')
        db.commit()

    body = client.get(f'/customers/{customer_id}').get_data(as_text=True)
    assert 'Legacy balance from old system' in body
    assert 'Still outstanding' in body
    assert 'R30311.00' in body
    assert 'Old order #3904' in body
    assert 'Total R32386.00 · Paid then R2075.00 · 2 invoices' in body


def test_customer_without_legacy_balance_hides_panel(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db, 'No Legacy Balance')
        db.commit()

    body = client.get(f'/customers/{customer_id}').get_data(as_text=True)
    assert 'Legacy balance from old system' not in body
    assert 'Legacy balance</small><strong class="balance-owing">R0.00' in body


# --- Settling the legacy balance from the customer page ----------------------

def seed_two_legacy_rows(db, customer_id):
    """Two open old orders: #3001 older, #3002 newer."""
    upsert_legacy_balance(customer_id, 'old-cust-1', '3001', 400, 'partially_paid', 'Stopped',
                          1000, 600, 1, '2022-01-10', '2022-01-10')
    upsert_legacy_balance(customer_id, 'old-cust-1', '3002', 250, 'payment_due', 'Draft',
                          250, 0, 0, '2023-05-20', '2023-05-20')


def test_recording_a_legacy_payment_reduces_the_outstanding_balance(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db, 'Pays Legacy')
        upsert_legacy_balance(customer_id, 'old-cust-1', '3904', 500, 'partially_paid', 'Stopped',
                              900, 400, 2, '2024-05-05', '2024-05-05')
        db.commit()

    response = client.post(f'/customers/{customer_id}/legacy-payments',
                           data={'amount': '200', 'method': 'cash', 'reference': 'RCPT-1'},
                           follow_redirects=True)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'R200.00 recorded against previous orders' in body
    assert 'R300.00 still outstanding' in body

    with app.app_context():
        db = get_db()
        summary = legacy_balance_summary(customer_id)
        assert summary['total'] == 300.0
        assert summary['settled_total'] == 200.0
        assert summary['imported_total'] == 500.0
        # The imported figure is never edited.
        row = db.execute("SELECT amount_due, settled_amount, status FROM legacy_customer_balances WHERE source_order_number='3904'").fetchone()
        assert float(row['amount_due']) == 500.0
        assert float(row['settled_amount']) == 200.0
        assert row['status'] == 'active'
        receipt = db.execute("SELECT amount, method, reference FROM legacy_balance_payments WHERE customer_id=?", (customer_id,)).fetchone()
        assert float(receipt['amount']) == 200.0
        assert receipt['method'] == 'cash'
        assert receipt['reference'] == 'RCPT-1'


def test_legacy_payment_settles_the_oldest_order_first(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db, 'Oldest First')
        seed_two_legacy_rows(db, customer_id)
        db.commit()

    # R400 exactly clears the older #3001, and nothing of #3002.
    client.post(f'/customers/{customer_id}/legacy-payments',
                data={'amount': '400', 'method': 'eft'}, follow_redirects=True)

    with app.app_context():
        db = get_db()
        rows = {r['source_order_number']: r for r in db.execute(
            "SELECT source_order_number, settled_amount, status FROM legacy_customer_balances WHERE customer_id=?",
            (customer_id,)).fetchall()}
        assert float(rows['3001']['settled_amount']) == 400.0
        assert rows['3001']['status'] == 'collected'
        assert float(rows['3002']['settled_amount']) == 0.0
        assert rows['3002']['status'] == 'active'
        summary = legacy_balance_summary(customer_id)
        assert summary['total'] == 250.0
        assert summary['active_count'] == 1


def test_legacy_payment_cannot_exceed_the_outstanding_balance(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db, 'Overpay Legacy')
        upsert_legacy_balance(customer_id, 'old-cust-1', '3904', 100, 'partially_paid', 'Stopped')
        db.commit()

    response = client.post(f'/customers/{customer_id}/legacy-payments',
                           data={'amount': '150', 'method': 'cash'}, follow_redirects=True)
    assert 'Payment cannot be more than the outstanding legacy balance R100.00' in response.get_data(as_text=True)
    with app.app_context():
        assert legacy_balance_summary(customer_id)['total'] == 100.0
        assert get_db().execute("SELECT COUNT(*) AS c FROM legacy_balance_payments").fetchone()['c'] == 0


def test_legacy_payment_refused_when_nothing_is_outstanding(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db, 'Nothing Owing')
        db.commit()

    response = client.post(f'/customers/{customer_id}/legacy-payments',
                           data={'amount': '50', 'method': 'cash'}, follow_redirects=True)
    assert 'no outstanding legacy balance to settle' in response.get_data(as_text=True)


def test_legacy_payment_refuses_customer_credit(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db, 'Credit Refused')
        upsert_legacy_balance(customer_id, 'old-cust-1', '3904', 100, 'partially_paid', 'Stopped')
        db.commit()

    response = client.post(f'/customers/{customer_id}/legacy-payments',
                           data={'amount': '50', 'method': 'customer_credit'}, follow_redirects=True)
    assert 'Customer credit cannot be used on an imported legacy balance' in response.get_data(as_text=True)


def test_settled_legacy_balance_keeps_the_panel_and_shows_settled(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db, 'Fully Settled')
        upsert_legacy_balance(customer_id, 'old-cust-1', '3904', 100, 'partially_paid', 'Stopped')
        db.commit()

    client.post(f'/customers/{customer_id}/legacy-payments',
                data={'amount': '100', 'method': 'cash'}, follow_redirects=True)

    body = client.get(f'/customers/{customer_id}').get_data(as_text=True)
    assert 'Legacy balance from old system' in body          # history stays visible
    assert '<span class="badge status-paid">Settled</span>' in body
    assert 'R100.00 of R100.00 settled' in body
    assert 'Settle previous order/s' not in body             # nothing left to settle
    assert 'Payments received against old orders' in body


def datalist_summary(body, customer_id):
    match = re.search(r'data-id="%d" data-summary=\'(.*?)\'' % customer_id, body)
    assert match, f'no datalist option found for customer {customer_id}'
    return json.loads(match.group(1))


def test_order_form_shows_legacy_balance_and_settle_link(client, app):
    """The imported balance must be visible on the new-order form, with a way to settle it."""
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db, 'Order Form Legacy')
        upsert_legacy_balance(customer_id, 'old-cust-1', '3904', 500, 'partially_paid', 'Stopped')
        db.commit()

    body = client.get(f'/orders/new?customer_id={customer_id}').get_data(as_text=True)
    assert 'Previous Orders Balance (Old System)' in body
    assert 'id="legacy-balance-value"' in body
    assert 'R500.00' in body
    assert 'Settle Previous Order/s' in body
    assert f'href="/customers/{customer_id}#legacy-balance"' in body
    # The card must not be hidden when the customer has an old balance.
    card = re.search(r'<div class="stat-card legacy-balance-stat-card" id="legacy-balance-card"[^>]*>', body)
    assert card, 'the legacy balance stat card is missing from the order form'
    assert 'hidden' not in card.group(0)

    summary = datalist_summary(body, customer_id)
    assert summary['legacy_balance'] == 500.0
    assert summary['legacy_balance_url'] == f'/customers/{customer_id}#legacy-balance'


def test_order_form_hides_legacy_card_when_nothing_is_owed(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db, 'Nothing Owed Form')
        db.commit()

    body = client.get(f'/orders/new?customer_id={customer_id}').get_data(as_text=True)
    card = re.search(r'<div class="stat-card legacy-balance-stat-card" id="legacy-balance-card"[^>]*>', body)
    assert card, 'the legacy balance stat card is missing from the order form'
    assert 'hidden' in card.group(0)
    assert datalist_summary(body, customer_id)['legacy_balance'] == 0


def test_draft_save_warns_about_unpaid_orders(client, app):
    """Saving a draft for a customer who owes money must ask for confirmation."""
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db, 'Owes Money')
        upsert_legacy_balance(customer_id, 'old-cust-1', '3904', 500, 'partially_paid', 'Stopped')
        seed_order(db, customer_id, 'ORD-DUE-1', total=300, due=300)
        db.commit()

    body = client.get(f'/orders/new?customer_id={customer_id}').get_data(as_text=True)
    assert 'id="save-order-button"' in body
    assert 'This customer has UNPAID orders (' in body
    assert 'Do you want to continue?' in body
    assert 'unpaidOrdersTotal(currentCustomerSummary)' in body
    # Both debts count toward the warning: R500 legacy + R300 earlier order.
    assert datalist_summary(body, customer_id)['unpaid_orders_total'] == 800.0
