"""Desktop navigation parity and separate, scoped legacy debt on the dashboard."""
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from app.db import get_db
from app.services.legacy_balances import outstanding_legacy_balances, upsert_legacy_balance
from test_ticket_341953282_dashboard import (
    app, client, login, add_customer, add_depot_user, seed_owing_book, owing_block,
)


def seed_legacy(app):
    ids = seed_owing_book(app)
    with app.app_context():
        db = get_db()
        ids['unassigned'] = add_customer(db, 'Unassigned Legacy', branch_id=None)
        for cid, ref, amount in [
            (ids['alice'], 'LEG-OPEN', 640),
            (ids['alice'], 'LEG-PARTIAL', 100),
            (ids['bob'], 'LEG-DEPOT-TWO', 200),
            (ids['unassigned'], 'LEG-UNASSIGNED', 50),
            (ids['alice'], 'SKIP-LEG-ZERO', 20),
            (ids['alice'], 'SKIP-LEG-OVERPAID', 20),
            (ids['alice'], 'SKIP-LEG-WAIVED', 30),
            (ids['alice'], 'SKIP-LEG-COLLECTED', 40),
            (ids['alice'], 'SKIP-LEG-INACTIVE', 50),
        ]:
            upsert_legacy_balance(cid, str(cid), ref, amount,
                                  source_created_at='2024-01-02')
        db.execute("UPDATE legacy_customer_balances SET settled_amount=25 WHERE source_order_number='LEG-PARTIAL'")
        db.execute("UPDATE legacy_customer_balances SET settled_amount=20 WHERE source_order_number='SKIP-LEG-ZERO'")
        db.execute("UPDATE legacy_customer_balances SET settled_amount=25 WHERE source_order_number='SKIP-LEG-OVERPAID'")
        for status in ('waived', 'collected', 'inactive'):
            db.execute('UPDATE legacy_customer_balances SET status=? WHERE source_order_number=?',
                       (status, 'SKIP-LEG-' + status.upper()))
        db.commit()
    return ids


def test_legacy_query_reuses_remaining_balance_and_writes_nothing(app):
    seed_legacy(app)
    with app.app_context():
        db = get_db()
        before = db.total_changes
        rows = outstanding_legacy_balances()
        assert [(r['source_order_number'], r['outstanding']) for r in rows] == [
            ('LEG-OPEN', 640), ('LEG-DEPOT-TWO', 200), ('LEG-PARTIAL', 75), ('LEG-UNASSIGNED', 50),
        ]
        assert db.total_changes == before


@pytest.mark.parametrize('lazy', [False, True])
def test_legacy_subsection_follows_orders_without_changing_order_total(app, client, lazy):
    ids = seed_legacy(app)
    login(client)
    app.config['TESTING'] = not lazy
    response = client.get('/dashboard/partials/owing' if lazy else '/dashboard')
    assert response.status_code == 200
    block = response.get_data(as_text=True)
    if not lazy:
        block = owing_block(block)
    assert block.index('Outstanding orders') < block.index('Legacy balances')
    orders, legacy = block.split('id="owing-legacy-balances"', 1)
    assert '4 orders · <strong class="balance-owing">R945.50</strong>' in orders
    assert '4 balances · <strong class="balance-owing">R965.00</strong>' in legacy
    assert f'href="/customers/{ids["alice"]}#legacy-balance"' in legacy
    assert 'class="balance-owing">R75.00<' in legacy
    assert '2024-01-02' in legacy
    for ref in ('LEG-OPEN', 'LEG-PARTIAL', 'LEG-DEPOT-TWO', 'LEG-UNASSIGNED'):
        assert ref in legacy and ref not in orders
    assert 'SKIP-LEG-' not in legacy
    assert 'href="/orders/' not in legacy


@pytest.mark.parametrize('path', ['/dashboard', '/dashboard/partials/owing'])
def test_branch_filter_and_session_never_leak_other_depot_or_unassigned(app, client, path):
    seed_legacy(app)
    login(client)
    block = client.get(path + '?branch=1').get_data(as_text=True)
    assert 'LEG-OPEN' in block and 'LEG-PARTIAL' in block
    assert 'LEG-DEPOT-TWO' not in block and 'LEG-UNASSIGNED' not in block
    client.post('/logout')
    add_depot_user(app, 'Legacy Depot Two', 2)
    login(client, 'Legacy Depot Two', password='staff123')
    for query in ('', '?branch=1', '?branch=2'):
        block = client.get(path + query).get_data(as_text=True)
        assert 'LEG-DEPOT-TWO' in block
        assert 'LEG-OPEN' not in block and 'LEG-PARTIAL' not in block
        assert 'LEG-UNASSIGNED' not in block


def test_multidepot_legacy_scope_and_filter_intersection(app, client):
    seed_legacy(app)
    add_depot_user(app, 'Multi Legacy', 1)
    with app.app_context():
        db = get_db()
        user = db.execute("SELECT id FROM users WHERE name='Multi Legacy'").fetchone()
        assert user is not None
        db.execute('INSERT OR IGNORE INTO user_branch_access(user_id,branch_id) VALUES (?,?)', (user['id'], 1))
        db.execute('INSERT OR IGNORE INTO user_branch_access(user_id,branch_id) VALUES (?,?)', (user['id'], 2))
        db.commit()
    login(client, 'Multi Legacy', password='staff123')
    page = client.get('/dashboard/partials/owing').get_data(as_text=True)
    assert 'LEG-OPEN' in page and 'LEG-DEPOT-TWO' in page
    assert 'LEG-UNASSIGNED' not in page
    filtered = client.get('/dashboard/partials/owing?branch=2').get_data(as_text=True)
    assert 'LEG-DEPOT-TWO' in filtered and 'LEG-OPEN' not in filtered


def test_legacy_empty_state_and_authentication(client):
    assert client.get('/dashboard/partials/owing').status_code == 302
    login(client)
    for path in ('/dashboard', '/dashboard/partials/owing'):
        block = client.get(path).get_data(as_text=True)
        assert 'Legacy balances' in block
        assert 'No outstanding legacy balances.' in block


def test_dashboard_module_gate_still_applies(app, client):
    seed_legacy(app)
    add_depot_user(app, 'No Dashboard', 1)
    with app.app_context():
        db = get_db()
        db.execute('UPDATE users SET modules_json=? WHERE name=?', (json.dumps(['inventory']), 'No Dashboard'))
        db.commit()
    login(client, 'No Dashboard', password='staff123')
    assert client.get('/dashboard').status_code == 403
    assert client.get('/dashboard/partials/owing').status_code == 403


def test_real_browser_desktop_mobile(app, client):
    seed_legacy(app)
    login(client)
    cookie = client.get_cookie('session')
    # Exercise the production lazy-fetch path, not just TESTING's eager page.
    app.config['TESTING'] = False
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = Path('C:/Users/djjac/AppData/Local/hermes/cache/scratch/ABI-341953284')
    output.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True)
            for width, height in [(1440, 1000), (390, 844)]:
                context = browser.new_context(viewport={'width': width, 'height': height})
                context.add_cookies([{'name': 'session', 'value': cookie.value, 'domain': '127.0.0.1', 'path': '/'}])
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda err: errors.append(str(err)))
                page.goto(f'http://127.0.0.1:{server.server_port}/dashboard', wait_until='networkidle')
                page.locator('#owing-legacy-balances').wait_for()
                if width > 860:
                    nav = page.locator('.sidebar')
                    notification = nav.get_by_role('link', name='Notifications', exact=False)
                    assert notification.is_visible()
                    dims = []
                    for name in ('Notifications', 'Inventory', 'Payments'):
                        link = nav.get_by_role('link', name=name, exact=False)
                        dims.append(link.evaluate('(e)=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return [r.width,r.height,s.padding,s.fontSize,s.fontWeight,s.lineHeight,s.borderRadius]}'))
                    assert dims[0] == dims[1] == dims[2], dims
                    assert not page.locator('.notify-entry').is_visible()
                    nav.screenshot(path=str(output / 'desktop-navigation.png'))
                else:
                    notification = page.locator('.notify-entry a')
                    assert notification.is_visible()
                assert notification.get_attribute('href') == '/settings/notifications'
                panel = page.locator('#owing-legacy-balances')
                orders = page.locator('#owing-customers .owing-customers-panel').first
                assert orders.bounding_box()['y'] + orders.bounding_box()['height'] <= panel.bounding_box()['y']
                assert panel.locator('tbody tr').count() == 4
                assert panel.locator('td.owing-amount').first.evaluate('(e)=>getComputedStyle(e.querySelector("strong")).color') == 'rgb(180, 35, 24)'
                for amount in panel.locator('td.owing-amount').all():
                    box = amount.bounding_box()
                    assert box['x'] >= 0 and box['x'] + box['width'] <= width
                page.locator('#owing-customers').screenshot(path=str(output / f'owing-{width}.png'))
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                panel.get_by_role('link', name='Alice Owing').first.click()
                page.wait_for_load_state('networkidle')
                assert page.locator('#legacy-balance').is_visible()
                page.goto(f'http://127.0.0.1:{server.server_port}/dashboard', wait_until='networkidle')
                notification = page.locator('.notify-entry a' if width < 860 else '.sidebar a[href="/settings/notifications"]')
                notification.click()
                page.wait_for_url('**/settings/notifications')
                assert not errors, errors
                context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        assert not thread.is_alive()
