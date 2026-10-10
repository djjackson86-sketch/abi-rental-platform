from contextlib import contextmanager
from html import unescape
import re

import pytest
from flask import template_rendered
from app.db import get_db
from test_ticket_341953050_payments_branch_filter_sort import (
    app, client, login, seed_payment_rows, add_payments_staff,
)


@contextmanager
def rendered(app):
    contexts = []
    def capture(sender, template, context, **extra):
        contexts.append(context)
    template_rendered.connect(capture, app)
    try:
        yield contexts
    finally:
        template_rendered.disconnect(capture, app)


def get_page(client, app, url):
    with rendered(app) as contexts:
        response = client.get(url)
    assert response.status_code == 200
    return response.get_data(as_text=True), contexts[-1]


def test_day_overrides_range_and_clear_restores_it(client, app):
    seed_payment_rows(app)
    login(client)
    html, ctx = get_page(client, app, '/payments?day=2026-07-02&date_from=2026-07-03&date_to=2026-07-03&branch=2&sort=amount&dir=asc')
    assert [p['reference'] for p in ctx['payments']] == ['REFUND-PRE', 'CROSS-CARD']
    assert ctx['total_payments'] == 2
    assert ctx['payment_totals']['card'] == 200
    assert ctx['payment_totals']['eft'] == -75
    assert ctx['payment_totals']['cash'] == 0
    assert 'name="date_from" value="2026-07-03"' in html
    links = [(unescape(url), label) for url, label in re.findall(r'href="([^"]+)"[^>]*>([^<]*)</a>', html)]
    clear = next(url for url, label in links if label == 'Clear business day')
    assert 'day=' not in clear and 'date_from=2026-07-03' in clear
    _, ctx = get_page(client, app, clear)
    assert ctx['total_payments'] == 0
    assert all('day=2026-07-02' in url for url, label in links if 'sort=' in url and label != 'Clear business day')


@pytest.mark.parametrize('day', ['not-a-date', '2026-02-30', '9999-99-99', ''])
def test_invalid_or_cleared_day_uses_range(client, app, day):
    seed_payment_rows(app)
    login(client)
    html, ctx = get_page(client, app, f'/payments?day={day}&date_from=2026-07-03&date_to=2026-07-03')
    assert ctx['filters']['day'] == ''
    assert [p['reference'] for p in ctx['payments']] == ['MID-CASH']
    assert 'name="day" value=""' in html


def seed_boundaries(app):
    seed_payment_rows(app)
    with app.app_context():
        db = get_db()
        oid = db.execute("SELECT id FROM orders WHERE order_number='ORD-PAY-2'").fetchone()['id']
        cid = db.execute("SELECT customer_id FROM orders WHERE id=?", (oid,)).fetchone()['customer_id']
        for reference, date, created in [
            ('BEFORE', '2026-07-01T23:59:59', '2026-07-02T10:00:00'),
            ('START', '2026-07-02T00:00:00', '2026-07-01T10:00:00'),
            ('END', '2026-07-02T23:59:59', '2026-07-03T10:00:00'),
            ('AFTER', '2026-07-03T00:00:00', '2026-07-02T10:00:00'),
            ('FALLBACK', '', '2026-07-02T12:00:00'),
        ]:
            db.execute("INSERT INTO payments (order_id, amount, method, reference, status, payment_date, created_at) VALUES (?, 10, 'cash', ?, 'paid', ?, ?)", (oid, reference, date, created))
        db.execute("UPDATE orders SET deposit_refund_amount=20, deposit_process_method='cash', deposit_processed_at='2026-07-02T23:59:59' WHERE id=?", (oid,))
        for reference, date in [('LEGACY-DAY', '2026-07-02T00:00:00'), ('LEGACY-AFTER', '2026-07-03T00:00:00')]:
            db.execute("INSERT INTO legacy_balance_payments (customer_id, branch_id, amount, method, reference, payment_date, status, created_at) VALUES (?, 2, 40, 'cash', ?, ?, 'active', ?)", (cid, reference, date, date))
        db.commit()


def test_inclusive_boundaries_fallback_refunds_legacy_count_and_totals(client, app):
    seed_boundaries(app)
    login(client)
    _, ctx = get_page(client, app, '/payments?day=2026-07-02&branch=2')
    refs = {p['reference'] for p in ctx['payments']}
    assert refs == {'START', 'END', 'FALLBACK', 'REFUND-PRE', 'CROSS-CARD', 'Refunded deposit', 'LEGACY-DAY'}
    assert ctx['total_payments'] == len(ctx['payments']) == 7
    # Preserve existing reporting semantics: cash payouts reduce the cash card.
    assert ctx['payment_totals'] == {'cash': 50.0, 'card': 200.0, 'eft': -75.0, 'other': 0.0}


def test_day_cannot_widen_staff_scope(client, app):
    seed_payment_rows(app)
    name = add_payments_staff(app)
    login(client, name, 'staff123')
    _, ctx = get_page(client, app, '/payments?day=2026-07-03&branch=1')
    assert ctx['total_payments'] == 0
    assert ctx['payment_totals']['cash'] == 0


def test_load_more_preserves_day_branch_range_sort(client, app):
    seed_payment_rows(app)
    with app.app_context():
        db = get_db()
        oid = db.execute("SELECT id FROM orders WHERE order_number='ORD-PAY-2'").fetchone()['id']
        for i in range(30):
            db.execute("INSERT INTO payments (order_id, amount, method, reference, status, payment_date, created_at) VALUES (?, 1, 'cash', ?, 'paid', '2026-07-02T12:00:00', '2026-07-02T12:00:00')", (oid, f'EXTRA-{i}'))
        db.commit()
    login(client)
    html, ctx = get_page(client, app, '/payments?day=2026-07-02&branch=2&date_from=2026-07-01&sort=amount&dir=asc')
    assert len(ctx['payments']) == 25 and ctx['total_payments'] == 32
    url = unescape(re.search(r'href="([^"]+)">Load more payments', html).group(1))
    for value in ['day=2026-07-02', 'branch=2', 'date_from=2026-07-01', 'sort=amount', 'dir=asc', 'limit=50']:
        assert value in url
    _, ctx = get_page(client, app, url)
    assert len(ctx['payments']) == ctx['total_payments'] == 32
    assert ctx['payment_totals']['cash'] == 30


def test_removed_controls_and_archived_day_still_accessible(client, app):
    seed_payment_rows(app)
    login(client)
    html, ctx = get_page(client, app, '/payments?status=archived&day=2026-07-04&branch=3')
    assert '>Active</a>' not in html and '>Archived</a>' not in html
    assert 'ARCHIVED-ROOD' in html and ctx['payment_totals']['cash'] == 400
    assert ctx['total_payments'] == 1
    html, _ = get_page(client, app, '/orders')
    assert '<small>Items ordered</small>' not in html
    assert '<small>Orders</small>' in html
    assert 'Revenue received' in html and '<th>Items</th>' in html
