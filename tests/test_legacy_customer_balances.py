import os
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
    assert 'Customer owes from previous system' in body
    assert 'R30311.00' in body
    assert 'Old order #3904' in body
    assert 'Total R32386.00 · Paid R2075.00 · 2 invoices' in body


def test_customer_without_legacy_balance_hides_panel(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db, 'No Legacy Balance')
        db.commit()

    body = client.get(f'/customers/{customer_id}').get_data(as_text=True)
    assert 'Legacy balance from old system' not in body
    assert 'Legacy balance</small><strong class="balance-owing">R0.00' in body
