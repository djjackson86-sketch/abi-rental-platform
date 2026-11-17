"""Compact owing cards: all records/totals retained, only visibility is limited."""
import os
import threading
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

from app.db import get_db
from app.routes import admin
from app.services.legacy_balances import upsert_legacy_balance
from test_ticket_341953282_dashboard import app, client, login, add_customer, add_order


def seed(app, count):
    with app.app_context():
        db = get_db()
        customer = add_customer(db, 'Synthetic owing customer with a long readable name')
        for i in range(count):
            # Increasing dates but decreasing debt: amount must not drive ordering.
            day = f'2026-10-{i + 1:02d}T09:00:00'
            add_order(db, f'T318-{i:02d}', customer, 'started', 100 - i, created_at=day)
            upsert_legacy_balance(customer, 'synthetic', f'LEG318-{i:02d}', 200 - i,
                                  source_started_at=day if i % 2 else '',
                                  source_created_at=day)
        db.commit()


@pytest.mark.parametrize('count', [0, 10, 11, 23])
@pytest.mark.parametrize('url', ['/dashboard', '/dashboard/partials/owing'])
def test_limits_totals_and_ordering(app, client, count, url):
    seed(app, count)
    login(client)
    response = client.get(url)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    if url != '/dashboard':
        body += client.get('/dashboard/partials/legacy').get_data(as_text=True)
    soup = BeautifulSoup(body, 'html.parser')
    for panel_id, prefix, base in [('owing-orders', 'T318-', 100),
                                   ('owing-legacy-balances', 'LEG318-', 200)]:
        panel = soup.find(id=panel_id)
        rows = panel.select('tbody tr')
        assert len(rows) == count
        assert sum(not row.has_attr('hidden') for row in rows) == min(count, 10)
        assert all(row.has_attr('data-owing-extra') for row in rows[10:])
        assert [row.select('td')[1].select_one('a, span').get_text(strip=True)
                if row.select('td')[1].select_one('a, span')
                else row.select('td')[1].get_text(strip=True) for row in rows] == [
            f'{prefix}{i:02d}' for i in reversed(range(count))]
        assert len(panel.select('[data-owing-toggle]')) == int(count > 10)
        if count:
            if panel_id == 'owing-orders':
                total = sum(base - i for i in range(count))
                assert f'R{total:.2f}' in panel.select_one('.pill').get_text().replace(' ', '').replace('\xa0', '')
            else:
                assert 'R' not in panel.select_one('.pill').get_text()
        else:
            assert panel.select_one('.empty-results')


def test_date_fallback_ties_and_missing_last(monkeypatch):
    orders = [dict(id=1, created_at=None), dict(id=2, created_at='2026-10-01'),
              dict(id=3, created_at='2026-10-01'), dict(id=4, created_at='2026-11-01')]
    monkeypatch.setattr(admin, 'list_orders', lambda **kwargs: orders)
    assert [r['id'] for r in admin._dashboard_owing_orders()] == [4, 3, 2, 1]
    legacy = [dict(id=1, source_started_at='', source_created_at=None),
              dict(id=2, source_started_at='', source_created_at='2026-10-01'),
              dict(id=3, source_started_at='2026-10-01', source_created_at='2027-01-01'),
              dict(id=4, source_started_at='2026-11-01', source_created_at='2025-01-01')]
    calls = []
    def balances(*, company_wide=False):
        calls.append(company_wide)
        return legacy
    monkeypatch.setattr(admin, 'outstanding_legacy_balances', balances)
    assert [r['id'] for r in admin._dashboard_owing_legacy_balances(2)] == [4, 3, 2, 1]
    assert calls == [True]
    assert [r['id'] for r in legacy] == [1, 2, 3, 4]  # shared result not mutated


@pytest.mark.parametrize('lazy', [False, True])
def test_browser_independent_controls_and_layout(app, client, lazy):
    seed(app, 23)
    login(client)
    cookie = client.get_cookie('session')
    app.config['TESTING'] = not lazy
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = Path.home() / 'AppData/Local/hermes/cache/scratch/ABI-341953318'
    output.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True)
            for width, height in [(1440, 1000), (390, 844)]:
                context = browser.new_context(viewport={'width': width, 'height': height})
                context.add_cookies([{'name': 'session', 'value': cookie.value,
                                     'domain': '127.0.0.1', 'path': '/'}])
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{server.server_port}/dashboard', wait_until='networkidle')
                orders = page.locator('#owing-orders')
                legacy = page.locator('#owing-legacy-balances')
                expect(orders.locator('tbody tr:visible')).to_have_count(10)
                expect(legacy.locator('tbody tr:visible')).to_have_count(10)
                assert orders.locator('tbody tr').first.locator('td').nth(1).inner_text() == 'T318-22'
                assert legacy.locator('tbody tr').first.locator('td').nth(1).inner_text().split('\n')[0] == 'LEG318-22'
                a, b = orders.bounding_box(), legacy.bounding_box()
                assert a and b
                assert a['y'] + a['height'] <= b['y']
                if width <= 1000:
                    expect(orders.locator('.owing-mobile-raised').first).to_be_visible()
                for card in (orders, legacy):
                    assert card.locator('.table-wrap').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
                totals = [card.locator('.pill').inner_text() for card in (orders, legacy)]
                orders.get_by_role('button', name='Show all (23)').click()
                expect(orders.locator('tbody tr:visible')).to_have_count(23)
                expect(legacy.locator('tbody tr:visible')).to_have_count(10)
                expect(orders.locator('[data-owing-toggle]')).to_have_attribute('aria-expanded', 'true')
                legacy.get_by_role('button', name='Show all (23)').click()
                expect(legacy.locator('tbody tr:visible')).to_have_count(23)
                orders.get_by_role('button', name='Show fewer').click()
                expect(orders.locator('tbody tr:visible')).to_have_count(10)
                expect(legacy.locator('tbody tr:visible')).to_have_count(23)
                legacy.get_by_role('button', name='Show fewer').click()
                expect(legacy.locator('tbody tr:visible')).to_have_count(10)
                assert totals == [card.locator('.pill').inner_text() for card in (orders, legacy)]
                assert page.locator('h2', has_text='Going out').count() == 1
                assert page.locator('h2', has_text='Late trailers').count() == 1
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('#owing-customers').screenshot(path=str(output / f'owing-{width}-lazy-{lazy}.png'))
                assert not errors, errors
                context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        assert not thread.is_alive()
