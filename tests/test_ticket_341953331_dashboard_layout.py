"""Equal-width debt panels, desktop order, mobile stacking and lazy parity."""
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from app.db import get_db
from app.services.legacy_balances import upsert_legacy_balance
from test_ticket_341953282_dashboard import app, client, add_order
from test_ticket_341953320_dashboard_visibility import seed, sign_in


@pytest.mark.parametrize('kind', ['owner', 'restricted'])
@pytest.mark.parametrize('lazy', [False, True])
@pytest.mark.parametrize('populated', [False, True])
def test_debt_panels_browser(app, client, kind, lazy, populated):
    ids = seed(app, kind)
    with app.app_context():
        db = get_db()
        if populated:
            for i in range(11):
                add_order(db, f'LAYOUT-{i}', ids['alice'], 'started', 25)
                upsert_legacy_balance(ids['alice'], str(ids['alice']), f'LEG-LAYOUT-{i}', 15)
        else:
            db.execute("UPDATE orders SET due_total=0,total=0,status='cancelled'")
            db.execute("UPDATE legacy_customer_balances SET status='waived'")
        db.commit()
    sign_in(client, kind)
    cookie = client.get_cookie('session')
    app.config['TESTING'] = not lazy
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = Path('C:/Users/djjac/AppData/Local/hermes/cache/scratch/ABI-341953331')
    output.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True)
            for width in [1440, 1100, 1000, 390]:
                context = browser.new_context(viewport={'width': width, 'height': 1000})
                context.add_cookies([{'name': 'session', 'value': cookie.value, 'domain': '127.0.0.1', 'path': '/'}])
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda err: errors.append(str(err)))
                response = page.goto(f'http://127.0.0.1:{server.server_port}/dashboard', wait_until='networkidle')
                assert response.status == 200
                legacy = page.locator('#owing-legacy-balances')
                legacy.wait_for()
                assert page.locator('.dashboard-debt-row > #legacy-orders').count() == 1
                if kind == 'owner':
                    owing = page.locator('#owing-orders')
                    owing.wait_for()
                    assert page.locator('.dashboard-debt-row > #owing-customers + #legacy-orders').count() == 1
                    a, b = owing.bounding_box(), legacy.bounding_box()
                    assert abs(a['width'] - b['width']) < 1
                    if width > 1000:
                        assert abs(a['y'] - b['y']) < 1
                        assert b['x'] >= a['x'] + a['width']
                    else:
                        assert abs(a['x'] - b['x']) < 1
                        assert b['y'] >= a['y'] + a['height']
                    styles = page.evaluate("""['#owing-orders','#owing-legacy-balances'].map(s => {
                        const p=getComputedStyle(document.querySelector(s));
                        const h=getComputedStyle(document.querySelector(s+' h2'));
                        return [p.padding,p.borderRadius,h.fontSize,h.fontWeight];
                    })""")
                    assert styles[0] == styles[1]
                else:
                    assert page.locator('#owing-orders').count() == 0
                    assert legacy.bounding_box()['width'] == pytest.approx(page.locator('.dashboard-debt-row').bounding_box()['width'], abs=1)
                    assert legacy.locator('a').count() == 0
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                if populated:
                    assert legacy.get_by_text('LEG-LAYOUT-0', exact=True).count() == 1
                    for panel, rows in [(legacy, '#owing-legacy-rows')] + ([(page.locator('#owing-orders'), '#owing-orders-rows')] if kind == 'owner' else []):
                        button = panel.locator('[data-owing-toggle]')
                        assert page.locator(rows + ' tr:visible').count() == 10
                        button.click()
                        assert page.locator(rows + ' tr:visible').count() > 10
                        assert button.get_attribute('aria-expanded') == 'true'
                        button.click()
                        assert page.locator(rows + ' tr:visible').count() == 10
                    if kind == 'owner':
                        assert legacy.locator('a[href$="#legacy-balance"]').count() > 0
                        assert page.locator('#owing-orders a[href^="/orders/"]').count() > 0
                else:
                    assert legacy.get_by_text('No outstanding legacy balances.', exact=True).count() == 1
                    if kind == 'owner':
                        assert page.get_by_text('No outstanding orders right now.', exact=True).count() == 1
                assert not errors
                page.locator('.dashboard-debt-row').screenshot(path=str(output / f'{kind}-{lazy}-{populated}-{width}.png'))
                context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        assert not thread.is_alive()
