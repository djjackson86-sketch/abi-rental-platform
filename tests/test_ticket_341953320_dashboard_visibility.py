"""ABI-341953320: universal read-only visibility without expanding other grants."""
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from app.db import get_db
from app.services.legacy_balances import company_outstanding_legacy_total
from test_ticket_341953282_dashboard import app, client, login, add_depot_user, add_order
from test_ticket_341953284_dashboard import seed_legacy


def seed(app, kind):
    ids = seed_legacy(app)
    with app.app_context():
        db = get_db()
        balance = db.execute("SELECT id,customer_id FROM legacy_customer_balances WHERE source_order_number='LEG-PARTIAL'").fetchone()
        db.execute("""INSERT INTO legacy_balance_writeoffs
            (customer_id,legacy_balance_id,ticket_id,amount,reason,writeoff_date,created_at)
            VALUES (?,?,'TEST-320',15,'Test','2026-11-01','2026-11-01')""",
            (balance['customer_id'],balance['id']))
        # Separate depots, both pickup and return paths, no limit-dependent fixtures.
        db.execute("UPDATE orders SET status='cancelled'")
        ids['one'] = add_order(db, 'MOVEMENT-ONE', ids['alice'], 'reserved', 0, branch_id=1)
        ids['two'] = add_order(db, 'MOVEMENT-TWO', ids['bob'], 'reserved', 0, branch_id=2)
        db.execute("INSERT INTO branches(name,active,created_at,updated_at) VALUES ('Third',1,'2026-11-01','2026-11-01')")
        third = db.execute("SELECT id FROM branches WHERE name='Third'").fetchone()['id']
        add_order(db, 'MOVEMENT-THREE', ids['alice'], 'reserved', 0, branch_id=third)
        db.commit()
    if kind != 'owner':
        add_depot_user(app, 'Visibility Test', 1)
        with app.app_context():
            db = get_db()
            uid = db.execute("SELECT id FROM users WHERE name='Visibility Test'").fetchone()['id']
            modules = [] if kind == 'restricted' else ['dashboard', 'orders', 'customers']
            db.execute('UPDATE users SET modules_json=? WHERE id=?', (json.dumps(modules),uid))
            if kind == 'multi':
                for bid in (1,2):
                    db.execute('INSERT INTO user_branch_access(user_id,branch_id) VALUES (?,?)',(uid,bid))
            db.commit()
    return ids


def sign_in(client, kind):
    login(client, None if kind == 'owner' else 'Visibility Test',
          password='admin123' if kind == 'owner' else 'staff123')


@pytest.mark.parametrize('kind', ['owner','staff','restricted','multi'])
@pytest.mark.parametrize('lazy', [False,True])
def test_visibility_and_scope(app, client, kind, lazy):
    ids = seed(app, kind)
    sign_in(client, kind)
    app.config['TESTING'] = not lazy
    for query in ('', '?branch=1','?branch=2','?branch=999999','?branch=all'):
        response = client.get('/dashboard'+query)
        assert response.status_code == 200
        shell = response.get_data(as_text=True)
        assert '>Movement</h2>' in shell
        assert 'legacy-orders' in shell
        total = client.get('/dashboard/partials/legacy'+query)
        assert total.status_code == 200
        html = total.get_data(as_text=True)
        assert 'LEG-OPEN' in html and 'LEG-DEPOT-TWO' in html
        assert 'All branches · read-only' in html
        assert 'Alice' in html and 'href=' not in html and 'R950.00' not in html
        if not lazy:
            assert 'LEG-OPEN' in shell
        else:
            assert 'data-dashboard-section="movement"' in shell
            assert 'data-dashboard-section="legacy"' in shell
        movement = client.get('/dashboard/partials/movement'+query)
        assert movement.status_code == 200
        body = movement.get_data(as_text=True)
        if kind in ('staff','restricted'):
            assert 'MOVEMENT-ONE' in body
            assert 'MOVEMENT-TWO' not in body and 'MOVEMENT-THREE' not in body
            if not lazy:
                assert 'MOVEMENT-ONE' in shell and 'MOVEMENT-TWO' not in shell
        elif kind == 'multi':
            assert 'MOVEMENT-THREE' not in body
            if query == '':
                assert 'MOVEMENT-ONE' in body and 'MOVEMENT-TWO' in body
            elif query == '?branch=2':
                assert 'MOVEMENT-TWO' in body and 'MOVEMENT-ONE' not in body
        elif query == '':
            assert all(v in body for v in ['MOVEMENT-ONE','MOVEMENT-TWO','MOVEMENT-THREE'])
        if kind == 'restricted':
            assert 'href="/orders/' not in body
            assert 'href="/calendar' not in body and 'href="/reports' not in body
            assert 'Cash up · end of day' not in shell
            assert client.get('/orders/'+str(ids['one'])).status_code == 403
            assert client.get('/customers/'+str(ids['alice'])).status_code == 403
            for path in ['/payments','/reports','/calendar','/dashboard/partials/owing','/dashboard/partials/cash','/dashboard/partials/day']:
                assert client.get(path).status_code == 403
        elif kind == 'staff':
            assert client.get('/orders/'+str(ids['two'])).status_code in (403,404)
    if kind == 'multi':
        with client.session_transaction() as sess:
            sess['active_branch_id'] = 2
        body = client.get('/dashboard/partials/movement?branch=1').get_data(as_text=True)
        assert 'MOVEMENT-TWO' in body and 'MOVEMENT-ONE' not in body
        assert 'LEG-OPEN' in client.get('/dashboard/partials/legacy?branch=1').get_data(as_text=True)


@pytest.mark.parametrize('path', ['/dashboard','/dashboard/partials/movement','/dashboard/partials/legacy'])
def test_anonymous_and_post_denied(client, path):
    assert client.get(path).status_code == 302
    assert client.post(path).status_code == 405


def test_aggregate_is_read_only_and_zero(app,client):
    with app.app_context():
        db = get_db()
        before = db.total_changes
        assert company_outstanding_legacy_total() == 0
        assert db.total_changes == before
    seed(app,'owner')
    with app.app_context():
        db = get_db()
        before = db.total_changes
        assert company_outstanding_legacy_total() == 950
        assert db.total_changes == before


@pytest.mark.parametrize('kind',['restricted','staff'])
def test_browser_lazy_desktop_mobile(app,client,kind):
    seed(app,kind)
    sign_in(client,kind)
    cookie = client.get_cookie('session')
    app.config['TESTING'] = False
    server = make_server('127.0.0.1',0,app,threaded=True)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    output = Path('C:/Users/djjac/AppData/Local/hermes/cache/scratch/ABI-341953320')
    output.mkdir(parents=True,exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome',headless=True)
            for width,height in [(1440,1000),(390,844)]:
                context = browser.new_context(viewport={'width':width,'height':height})
                context.add_cookies([{'name':'session','value':cookie.value,'domain':'127.0.0.1','path':'/'}])
                page = context.new_page()
                errors=[]
                page.on('pageerror',lambda err:errors.append(str(err)))
                page.goto(f'http://127.0.0.1:{server.server_port}/dashboard?branch=2',wait_until='networkidle')
                page.locator('#owing-legacy-rows').wait_for()
                page.locator('.movement-row').first.wait_for()
                assert page.locator('.movement-row').filter(has_text='MOVEMENT-ONE').count() == 1
                assert page.locator('.movement-row').filter(has_text='MOVEMENT-TWO').count() == 0
                if kind == 'restricted':
                    assert page.locator('a.movement-row').count() == 0
                else:
                    page.locator('a.movement-row').first.click()
                    page.wait_for_url('**/orders/*')
                    page.go_back(wait_until='networkidle')
                page.locator('#legacy-orders').scroll_into_view_if_needed()
                assert page.locator('#legacy-orders').is_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert not errors
                page.screenshot(path=str(output/f'{kind}-{width}.png'),full_page=True)
                context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        assert not thread.is_alive()
