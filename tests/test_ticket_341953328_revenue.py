"""Other revenue reconciles receipt-date totals and the branch-filtered graph."""
import json
import re
import threading
from pathlib import Path
import pytest
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server
from app.db import get_db
from app.services.reports import received_revenue_by_category, monthly_received_revenue, dashboard_period_metrics
from test_ticket_341953282_dashboard import app, client, login, add_depot_user
from test_ticket_341953326_revenue import seed_revenue


def chart_data(body):
    return json.loads(re.search(r'id="monthly-revenue-data">(.*?)</script>', body, re.S)[1])


@pytest.mark.parametrize('branch', [None, 1, 2])
def test_each_bucket_reconciles_with_dashboard(app, branch):
    seed_revenue(app)
    with app.app_context():
        for start, end in [('2026-11-17','2026-11-17'), ('2026-10-01','2026-11-17'), ('2026-12-01','2026-12-31')]:
            series = {key: monthly_received_revenue(2026, branch, key, start, end)
                      for key in ['trailer_rentals','sales_repairs','unattributed','gross']}
            for month in range(12):
                for day in range(31):
                    values = [series[key][month]['values'][day] for key in series]
                    if values[-1] is not None:
                        assert round(sum(values[:3]), 2) == values[-1]
            totals = received_revenue_by_category(start, end, branch)
            for key in totals:
                assert round(sum(v or 0 for m in series[key] for v in m['values']), 2) == totals[key]
            assert round(sum(v or 0 for m in series['gross'] for v in m['values']), 2) == dashboard_period_metrics(start, end, branch)['revenue']


def test_negative_other_and_capture_date_fallback(app):
    ids = seed_revenue(app)
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO payments (order_id, amount, method, status, payment_date, created_at) VALUES (?, -100, 'cash', 'paid', '', '2026-11-19T23:59:59')", (ids['DRAFT'],))
        db.commit()
        data = monthly_received_revenue(2026, None, 'unattributed', '2026-11-19', '2026-11-19')
        assert data[10]['values'][18] == -100
        assert data[10]['values'][17] == 0
        assert data[1]['values'][28] is None


def test_reports_default_order_filters_and_other_card(app, client):
    seed_revenue(app); login(client)
    query = '?chart_year=2026&start_date=2026-11-01&end_date=2026-11-17'
    body = client.get('/reports'+query).get_data(as_text=True)
    options = re.search(r'<select name="revenue_type".*?</select>', body, re.S)[0]
    assert re.findall(r'<option value="(.*?)"', options) == ['trailer_rentals','sales_repairs','unattributed','gross']
    assert 'value="trailer_rentals" selected' in options
    assert chart_data(body)[10]['values'][16] == 300
    for key, expected in [('sales_repairs',190),('unattributed',80),('gross',570),('invalid',300)]:
        response = client.get('/reports'+query+'&revenue_type='+key)
        assert response.status_code == 200
        assert chart_data(response.get_data(as_text=True))[10]['values'][16] == expected
    body = client.get('/reports'+query+'&branch=2&revenue_type=gross').get_data(as_text=True)
    assert chart_data(body)[10]['values'][16] == 190
    assert 'aria-label="Chart branch"' in body
    cards = client.get('/dashboard/partials/revenue'+query).get_data(as_text=True)
    assert cards.index('Sales/repairs revenue') < cards.index('OTHER REVENUE')
    assert 'R80.00' in cards


def test_scoped_staff_cannot_expand_graph(app, client):
    seed_revenue(app)
    add_depot_user(app, 'Chart staff', 1)
    login(client, 'Chart staff', 'staff123')
    response = client.get('/reports?chart_year=2026&branch=2&revenue_type=gross')
    # Reports may be denied by the existing staff module gate; never expose branch 2.
    if response.status_code == 200:
        body = response.get_data(as_text=True)
        assert chart_data(body)[10]['values'][16] == 380
        assert 'Chart branch (fixed to your branch)' in body
    else:
        assert response.status_code == 403


def test_browser_filters_desktop_and_mobile(app, client):
    seed_revenue(app); login(client)
    cookie = client.get_cookie('session')
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    output = Path('C:/Users/djjac/AppData/Local/hermes/cache/scratch/ABI-341953328')
    output.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True)
            for width, height in [(1440,1000),(390,844)]:
                context = browser.new_context(viewport={'width':width,'height':height})
                context.add_cookies([{'name':'session','value':cookie.value,'domain':'127.0.0.1','path':'/'}])
                page = context.new_page(); errors = []
                page.on('pageerror', lambda err: errors.append(str(err)))
                page.goto(f'http://127.0.0.1:{server.server_port}/reports?chart_year=2026&start_date=2026-11-01&end_date=2026-11-17', wait_until='networkidle')
                chart = page.locator('.monthly-revenue')
                assert chart.locator('[name=revenue_type]').input_value() == 'trailer_rentals'
                for key, label, value in [('sales_repairs','Sales/Repairs','R190.00'),('unattributed','Other Revenue','R80.00'),('gross','Gross Revenue','R570.00'),('trailer_rentals','Rentals','R300.00')]:
                    with page.expect_navigation(wait_until='networkidle'):
                        chart.locator('[name=revenue_type]').select_option(key)
                    assert f'{label} · November 17: {value}' in chart.locator('circle title').all_text_contents()
                    assert label in chart.locator('.monthly-revenue-status').inner_text()
                with page.expect_navigation(wait_until='networkidle'):
                    chart.locator('[name=branch]').select_option('2')
                assert 'Rentals · November 17: R0.00' in chart.locator('circle title').all_text_contents()
                assert page.locator('.reports-filter-form [name=branch]').input_value() == '2'
                with page.expect_navigation(wait_until='networkidle'):
                    chart.locator('[name=revenue_type]').select_option('gross')
                assert 'Gross Revenue · November 17: R190.00' in chart.locator('circle title').all_text_contents()
                chart.scroll_into_view_if_needed()
                page.screenshot(path=str(output/f'reports-{width}.png'))
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert not errors, errors
                context.close()
            browser.close()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=10)
        assert not thread.is_alive()
