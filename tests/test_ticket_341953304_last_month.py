"""Last-month comparisons reuse the period metrics without changing headlines."""
import re
from unittest.mock import Mock

import pytest
from app.routes import admin
from app.db import get_db
from test_dashboard_day_metrics import app, client, login, _order, card_values
from test_ticket_341953282_dashboard import add_depot_user


def comparisons(body):
    return re.findall(r'class="dashboard-last-month">Last month <strong>(.*?)</strong>', body)


@pytest.mark.parametrize('today,start,end', [
    ('2026-01-01', '2025-12-01', '2025-12-31'),
    ('2024-03-01', '2024-02-01', '2024-02-29'),
    ('2025-03-31', '2025-02-01', '2025-02-28'),
    ('2026-11-15', '2026-10-01', '2026-10-31'),
])
def test_month_boundaries(app, monkeypatch, today, start, end):
    monkeypatch.setattr(admin, 'cash_today_iso', lambda: today)
    metrics = Mock(return_value={'orders': 7})
    monkeypatch.setattr(admin, 'dashboard_period_metrics', metrics)
    with app.test_request_context('/dashboard?day=2020-01-01&range=all_time'):
        from flask import session
        session['user_role'] = 'owner'
        assert admin._dashboard_last_month_metrics(2) == {'orders': 7}
    metrics.assert_called_once_with(start_date=start, end_date=end, branch_id=2)


def seed(app):
    with app.app_context():
        db = get_db()
        db.execute('DELETE FROM customers')
        for day in ('2026-09-30', '2026-10-01', '2026-10-31', '2026-11-01'):
            db.execute('INSERT INTO customers (name, created_at) VALUES (?, ?)', ('Client '+day, day))
        for number, day, branch, amount in [
            ('MONTH-1', '2026-10-01', 1, 125),
            ('MONTH-2', '2026-10-31', 2, 200),
            ('BEFORE', '2026-09-30', 1, 900),
            ('CURRENT', '2026-11-01', 1, 700),
        ]:
            oid = _order(db, number, 'returned', day, branch_id=branch)
            db.execute("INSERT INTO payments (order_id, amount, method, status, payment_date, created_at) VALUES (?, ?, 'cash', 'paid', ?, ?)", (oid, amount, day, day))
        db.commit()


@pytest.mark.parametrize('query', ['', '?range=all_time', '?range=today', '?start_date=2020-01-01&end_date=2020-01-02&day=2020-01-01'])
def test_full_and_partial_share_comparisons(app, client, monkeypatch, query):
    monkeypatch.setattr(admin, 'cash_today_iso', lambda: '2026-11-15')
    seed(app)
    login(client)
    body = client.get('/dashboard'+query).get_data(as_text=True)
    partial = client.get('/dashboard/partials/period'+query).get_data(as_text=True)
    assert comparisons(body) == comparisons(partial) == ['2', '2', 'R325.00']
    assert card_values(body) == card_values(partial)
    catalog = re.search(r'<div class="metric-card">\s*<small>Catalog size.*?</div>', partial, re.S).group()
    assert 'Last month' not in catalog


def test_branch_and_accounting_rules(app, client, monkeypatch):
    monkeypatch.setattr(admin, 'cash_today_iso', lambda: '2026-11-15')
    seed(app)
    with app.app_context():
        db = get_db()
        oid = db.execute("SELECT id FROM orders WHERE order_number='MONTH-1'").fetchone()['id']
        db.execute("UPDATE orders SET deposit_refund_amount=25, deposit_processed_at='2026-10-31' WHERE id=?", (oid,))
        db.execute("INSERT INTO payments (order_id, amount, method, status, payment_date, created_at, deleted_at) VALUES (?, 999, 'cash', 'paid', '2026-10-10', '2026-10-10', '2026-10-11')", (oid,))
        customer_id = db.execute('SELECT id FROM customers ORDER BY id LIMIT 1').fetchone()['id']
        db.execute("INSERT INTO legacy_balance_payments (customer_id, amount, method, status, payment_date, created_at, branch_id) VALUES (?, 40, 'cash', 'active', '2026-10-15', '2026-10-15', 1)", (customer_id,))
        db.commit()
    login(client)
    assert comparisons(client.get('/dashboard/partials/period?branch=1').get_data(as_text=True)) == ['1', '2', 'R140.00']
    assert comparisons(client.get('/dashboard/partials/period?branch=2').get_data(as_text=True)) == ['1', '2', 'R200.00']


def test_zero_month_and_lazy_shell(app, client, monkeypatch):
    monkeypatch.setattr(admin, 'cash_today_iso', lambda: '2020-01-01')
    login(client)
    assert comparisons(client.get('/dashboard').get_data(as_text=True)) == ['0', '0', 'R0.00']
    app.config['TESTING'] = False
    shell = client.get('/dashboard').get_data(as_text=True)
    assert 'data-dashboard-section="period"' in shell
    assert comparisons(shell) == []
    assert comparisons(client.get('/dashboard/partials/period').get_data(as_text=True)) == ['0', '0', 'R0.00']


def test_staff_unchanged_and_no_month_query(app, client, monkeypatch):
    add_depot_user(app, 'Comparison Staff', 1)
    login(client, 'Comparison Staff', 'staff123')
    spy = Mock(wraps=admin._dashboard_last_month_metrics)
    monkeypatch.setattr(admin, '_dashboard_last_month_metrics', spy)
    assert comparisons(client.get('/dashboard').get_data(as_text=True)) == []
    calls = Mock(wraps=admin.dashboard_period_metrics)
    monkeypatch.setattr(admin, 'dashboard_period_metrics', calls)
    assert comparisons(client.get('/dashboard/partials/period?branch=2').get_data(as_text=True)) == []
    assert calls.call_count == 1


def test_anonymous_cannot_load_period(client):
    response = client.get('/dashboard/partials/period')
    assert response.status_code == 302
    assert '/login' in response.location
