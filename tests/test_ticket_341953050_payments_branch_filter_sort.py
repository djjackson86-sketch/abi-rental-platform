import os
import tempfile
from pathlib import Path

import pytest

from app import create_app
from app.db import get_db, now
from app.services.access import create_additional_user
from app.services.payments import list_payments, normalise_payment_sort


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    app = create_app({
        'TESTING': True,
        'DATABASE': path,
        'SECRET_KEY': 'test',
        'ADMIN_EMAIL': 'admin@abi.local',
        'ADMIN_PASSWORD': 'admin123',
    })
    yield app
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client, name=None, password='admin123'):
    with client.application.app_context():
        if name is None:
            row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
        else:
            row = get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
    assert row is not None
    return client.post('/login', data={'user_id': str(row['id']), 'password': password}, follow_redirects=True)


def seed_payment_rows(app):
    with app.app_context():
        db = get_db()
        stamp = now()
        # Keep IDs deterministic even if the app seed only made one branch.
        for branch_id, name in [(1, 'Midrand'), (2, 'Pretoria'), (3, 'Rooderport')]:
            existing = db.execute('SELECT id FROM branches WHERE id = ?', (branch_id,)).fetchone()
            if existing:
                db.execute('UPDATE branches SET name = ?, active = 1 WHERE id = ?', (name, branch_id))
            else:
                db.execute(
                    "INSERT INTO branches (id, name, code, active, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
                    (branch_id, name, name[:3].upper(), stamp, stamp),
                )
        customers = []
        for name in ['Zed Customer', 'Alpha Customer', 'Cross Customer', 'Archived Customer']:
            cur = db.execute(
                "INSERT INTO customers (name, email, created_at) VALUES (?, ?, ?)",
                (name, f"{name.split()[0].lower()}@example.test", stamp),
            )
            customers.append(cur.lastrowid)
        orders = [
            ('ORD-PAY-1', customers[0], 1, 1),
            ('ORD-PAY-2', customers[1], 2, 2),
            ('ORD-PAY-3', customers[2], 1, 2),
            ('ORD-PAY-4', customers[3], 3, 3),
        ]
        order_ids = []
        for number, customer_id, collect_branch_id, return_branch_id in orders:
            cur = db.execute(
                """INSERT INTO orders (order_number, customer_id, collect_branch_id, return_branch_id,
                   status, total, due_total, created_at)
                   VALUES (?, ?, ?, ?, 'returned', 1000, 0, ?)""",
                (number, customer_id, collect_branch_id, return_branch_id, stamp),
            )
            order_ids.append(cur.lastrowid)
        payments = [
            (order_ids[0], 300, 'cash', 'MID-CASH', 'paid', '2026-07-03T09:00:00', ''),
            (order_ids[1], 100, 'eft', 'PRE-EFT', 'paid', '2026-07-01T09:00:00', ''),
            (order_ids[1], -75, 'eft', 'REFUND-PRE', 'paid', '2026-07-02T10:00:00', ''),
            (order_ids[2], 200, 'card', 'CROSS-CARD', 'paid', '2026-07-02T09:00:00', ''),
            (order_ids[3], 400, 'cash', 'ARCHIVED-ROOD', 'archived', '2026-07-04T09:00:00', '2026-07-05T10:00:00'),
        ]
        for order_id, amount, method, reference, status, payment_date, deleted_at in payments:
            db.execute(
                """INSERT INTO payments (order_id, amount, method, reference, status, payment_date, deleted_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, amount, method, reference, status, payment_date, deleted_at, stamp),
            )
        db.commit()


def add_payments_staff(app, name='Pretoria Payments', branch_ids=(2,)):
    with app.app_context():
        user_id, error = create_additional_user(name, 'staff123', branch_ids=[str(b) for b in branch_ids])
        assert error is None
        get_db().execute("UPDATE users SET modules_json = ? WHERE id = ?", ('["payments"]', user_id))
        get_db().commit()
    return name


def body(response):
    return response.data.decode('utf-8')


def test_payments_page_renders_branch_column_and_cross_branch_label(client, app):
    seed_payment_rows(app)
    login(client)

    html = body(client.get('/payments'))

    assert '<th><a class="table-sort' in html
    assert 'Branch' in html
    assert 'Midrand' in html
    assert 'Pretoria' in html
    assert 'Midrand → Pretoria' in html
    assert 'REFUND-PRE' in html
    assert '-R75.00' in html
    assert 'Refund row' in html
    assert '<span class="pill">Refund</span>' in html
    assert 'ARCHIVED-ROOD' not in html


def test_branch_filter_narrows_payments_and_preserves_sort_links(client, app):
    seed_payment_rows(app)
    login(client)

    html = body(client.get('/payments?branch=2&sort=customer&dir=asc'))

    assert 'PRE-EFT' in html
    assert 'REFUND-PRE' in html
    assert 'CROSS-CARD' in html
    assert 'MID-CASH' not in html
    assert 'value="customer"' in html
    assert 'value="asc"' in html
    assert 'branch=2' in html
    assert 'sort=amount' in html


def test_restricted_staff_cannot_widen_branch_filter(client, app):
    seed_payment_rows(app)
    staff_name = add_payments_staff(app, branch_ids=(2,))
    login(client, staff_name, 'staff123')

    html = body(client.get('/payments?branch=1'))

    assert 'PRE-EFT' in html
    assert 'REFUND-PRE' in html
    assert 'CROSS-CARD' in html
    assert 'MID-CASH' not in html
    assert 'Your account only sees this branch' in html


def test_invalid_branch_filter_is_ignored_for_owner(client, app):
    seed_payment_rows(app)
    login(client)

    html = body(client.get('/payments?branch=999'))

    assert 'MID-CASH' in html
    assert 'PRE-EFT' in html
    assert 'REFUND-PRE' in html
    assert 'CROSS-CARD' in html


def test_payment_sort_whitelist_default_and_amount_order(app):
    seed_payment_rows(app)
    with app.test_request_context('/payments'):
        rows = list_payments(sort='amount', direction='asc')
        assert [row['reference'] for row in rows[:4]] == ['REFUND-PRE', 'PRE-EFT', 'CROSS-CARD', 'MID-CASH']
        assert normalise_payment_sort('amount', 'asc') == ('amount', 'asc')
        assert normalise_payment_sort('amount; DROP TABLE payments', 'sideways') == ('date', 'desc')
        default_rows = list_payments(sort='amount; DROP TABLE payments', direction='sideways')
        assert [row['reference'] for row in default_rows[:4]] == ['MID-CASH', 'REFUND-PRE', 'CROSS-CARD', 'PRE-EFT']


def test_date_filter_includes_refunds_and_excludes_outside_range(client, app):
    seed_payment_rows(app)
    login(client)

    html = body(client.get('/payments?date_from=2026-07-02&date_to=2026-07-02'))

    assert 'REFUND-PRE' in html
    assert 'CROSS-CARD' in html
    assert 'PRE-EFT' not in html
    assert 'MID-CASH' not in html
    assert 'name="date_from" value="2026-07-02"' in html
    assert 'name="date_to" value="2026-07-02"' in html
    assert 'Clear dates' in html


def test_date_filters_are_preserved_in_links(client, app):
    seed_payment_rows(app)
    login(client)

    html = body(client.get('/payments?branch=2&date_from=2026-07-02&date_to=2026-07-03&sort=amount&dir=asc'))

    assert 'date_from=2026-07-02' in html
    assert 'date_to=2026-07-03' in html
    assert 'branch=2' in html
    assert 'status=archived' in html
    assert 'sort=customer' in html


def test_sort_direction_toggle_markup_preserves_archived_and_branch(client, app):
    seed_payment_rows(app)
    login(client)

    html = body(client.get('/payments?status=archived&branch=3&sort=amount&dir=asc'))

    assert 'ARCHIVED-ROOD' in html
    assert 'MID-CASH' not in html
    assert 'amount' in html
    assert 'Amount ↑' in html
    assert 'status=archived' in html
    assert 'branch=3' in html
    assert 'dir=desc' in html


def test_archived_payments_still_render_with_branch_filter(client, app):
    seed_payment_rows(app)
    login(client)

    html = body(client.get('/payments?status=archived&branch=3'))

    assert 'ARCHIVED-ROOD' in html
    assert 'Rooderport' in html
    assert 'Deleted 2026-07-05' in html
    assert 'MID-CASH' not in html


def test_refund_rows_do_not_offer_edit_and_direct_edit_is_refused(client, app):
    seed_payment_rows(app)
    login(client)

    html = body(client.get('/payments'))
    refund_row = html.split('REFUND-PRE', 1)[1].split('</tr>', 1)[0]
    assert 'Refund row' in refund_row
    assert '/edit' not in refund_row

    with app.app_context():
        refund_id = get_db().execute("SELECT id FROM payments WHERE reference = 'REFUND-PRE'").fetchone()['id']
    response = client.get(f'/payments/{refund_id}/edit', follow_redirects=True)
    assert response.status_code == 200
    assert b'Refund rows cannot be edited from the payments ledger' in response.data


def test_refund_amount_carries_the_danger_class_and_it_is_styled(client, app):
    seed_payment_rows(app)
    login(client)

    html = body(client.get('/payments'))
    assert '<strong class="text-danger">-R75.00</strong>' in html
    assert '<strong>R300.00</strong>' in html

    stylesheet = Path(__file__).resolve().parents[1] / 'static' / 'css' / 'app.css'
    css = stylesheet.read_text(encoding='utf-8')
    assert '.text-danger{color:var(--danger)}' in css


def test_invalid_date_filters_are_ignored(client, app):
    seed_payment_rows(app)
    login(client)

    html = body(client.get('/payments?date_from=not-a-date&date_to=9999-99-99'))

    assert 'REFUND-PRE' in html
    assert 'MID-CASH' in html
    assert 'name="date_from" value=""' in html
    assert 'name="date_to" value=""' in html
    assert 'Clear dates' not in html
