import calendar
import json
import os
import threading
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from flask import session
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from app.db import get_db
from app.services.reports import monthly_received_revenue, dashboard_day_metrics
from test_dashboard_day_metrics import app, client, login, _order, _payment


def seed(app):
    with app.app_context():
        db = get_db()
        a = _order(db, 'CHART-A', 'returned', '2023-12-01', branch_id=1)
        b = _order(db, 'CHART-B', 'started', '2024-01-01', branch_id=2)
        _payment(db, a, 500, 'cash', '2024-02-29')
        _payment(db, a, -70, 'eft', '2024-03-01')
        _payment(db, a, 12, 'card', '', created_at='2024-02-29T12:00:00')
        _payment(db, a, 999, 'cash', '2024-02-29', status='pending')
        _payment(db, a, 999, 'cash', '2024-02-29', deleted_at='2024-03-01')
        # Simulate a historic account row, predating the insert prohibition.
        db.execute('DROP TRIGGER account_payment_retired_insert')
        _payment(db, a, 999, 'account', '2024-02-29')
        _payment(db, b, 800, 'cash', '2024-02-29')
        _payment(db, a, 44, 'cash', '2025-02-28')
        db.execute("UPDATE orders SET deposit_refund_amount=30, deposit_process_method='eft', deposit_processed_at='2024-03-01T14:00:00' WHERE id=?", (a,))
        db.execute("INSERT INTO customers (name, branch_id, created_at) VALUES ('Chart legacy',1,'2024-01-01')")
        c = db.execute("SELECT id FROM customers WHERE name='Chart legacy'").fetchone()['id']
        db.execute("INSERT INTO legacy_balance_payments (customer_id, amount, method, status, payment_date, created_at, branch_id) VALUES (?, 25, 'cash', 'active', '2024-02-29', '2024-03-01', 1)", (c,))
        db.commit()


def test_receipts_refunds_and_dashboard_parity(app):
    seed(app)
    with app.test_request_context():
        session.update(user_id=1, user_role='owner')
        chart = monthly_received_revenue(2024, 1)
        assert chart[1]['values'][28] == 537
        assert chart[2]['values'][0] == -100
        assert chart[0]['values'] == [0] * 31
        for day in ['2024-02-29', '2024-03-01', '2024-01-01']:
            m, d = int(day[5:7]), int(day[8:])
            assert chart[m-1]['values'][d-1] == dashboard_day_metrics(day, 1)['revenue']
        assert monthly_received_revenue(2024)[1]['values'][28] == 1337
        assert monthly_received_revenue(2025, 1)[1]['values'][27] == 44


@pytest.mark.parametrize('year', [2024, 2025])
def test_short_months_and_empty_days(app, year):
    with app.test_request_context():
        session.update(user_id=1, user_role='owner')
        chart = monthly_received_revenue(year)
        assert len(chart) == 12
        for month in chart:
            n = calendar.monthrange(year, month['month'])[1]
            assert month['values'] == [0] * n + [None] * (31-n)


def test_branch_scope_cannot_be_widened(app):
    seed(app)
    with app.test_request_context():
        session.update(user_id=2, user_role='staff', branch_id=1, can_view_all_branches=False, branch_ids=[])
        assert monthly_received_revenue(2024)[1]['values'][28] == 537
        assert monthly_received_revenue(2024, 2)[1]['values'][28] == 537


@pytest.mark.parametrize('year', ['2024', 'bad', '0', '10000'])
def test_report_route_year_controls(client, app, year):
    login(client)
    response = client.get('/reports?chart_year='+year+'&start_date=2020-01-01&end_date=2020-02-01')
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, 'html.parser')
    assert soup.select_one('[name=chart_year]')['value'] == ('2024' if year == '2024' else str(__import__('app.services.timezone', fromlist=['local_now']).local_now().year))
    assert len(json.loads(soup.select_one('#monthly-revenue-data').string)) == 12
    assert len(soup.select('.monthly-revenue-months input')) == 12
    assert len(soup.select('.monthly-revenue-months input[checked]')) == 1
    assert soup.select_one('.monthly-revenue-year [name=start_date]')['value'] == '2020-01-01'


@pytest.mark.parametrize('width', [1440, 390])
def test_real_browser_chart_controls(app, client, width):
    seed(app)
    login(client)
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome')
            page = browser.new_page(viewport={'width': width, 'height': 1000})
            cookie = client.get_cookie('session')
            page.context.add_cookies([{'name':'session', 'value':cookie.value, 'domain':'127.0.0.1','path':'/'}])
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/reports?chart_year=2024')
            checks = page.locator('.monthly-revenue-months input')
            assert page.locator('.monthly-revenue-chart polyline').count() == 1
            for check in checks.all():
                check.uncheck()
            assert page.locator('.monthly-revenue-chart polyline').count() == 0
            assert 'Select at least one month' in page.locator('.monthly-revenue-status').inner_text()
            checks.nth(1).check()
            checks.nth(2).check()
            assert page.locator('.monthly-revenue-chart polyline').count() == 2
            lines = page.locator('.monthly-revenue-chart polyline').evaluate_all('(els)=>els.map(e=>[e.getAttribute("stroke"),e.getAttribute("stroke-dasharray"),e.getAttribute("points").split(" ").length])')
            assert lines[0][0] != lines[1][0] and lines[0][1] != lines[1][1]
            assert lines[0][2] == 29 and lines[1][2] == 31
            assert 'Revenue (R)' in page.locator('.monthly-revenue-chart').text_content()
            assert '31' in page.locator('.monthly-revenue-chart').text_content()
            page.locator('summary').filter(has_text='View daily revenue values').click()
            assert 'R1,337.00' in page.locator('.monthly-revenue-table').inner_text()
            assert 'R-100.00' in page.locator('.monthly-revenue-table').inner_text()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            for control in page.locator('.monthly-revenue-months label, .monthly-revenue-year button').all():
                bounds = control.bounding_box()
                assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width
            assert page.locator('.monthly-revenue').evaluate('(e)=>e.scrollWidth<=e.clientWidth')
            shots = Path(os.environ['LOCALAPPDATA']) / 'hermes/cache/scratch/ABI-341953309'
            shots.mkdir(parents=True, exist_ok=True)
            page.locator('.monthly-revenue').scroll_into_view_if_needed()
            page.screenshot(path=str(shots/f'report-{width}.png'), full_page=True)
            page.locator('[name=chart_year]').fill('2025')
            page.get_by_role('button',name='Show year').click()
            assert page.locator('[name=chart_year]').input_value() == '2025'
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()
