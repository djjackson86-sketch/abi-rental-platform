"""Approved universal legacy list and overdue-only movement, without wider grants."""
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server
from app.db import get_db
from app.services.orders import dashboard_schedule
from app.services.legacy_balances import outstanding_legacy_balances
from test_ticket_341953282_dashboard import app, client, add_order
from test_ticket_341953320_dashboard_visibility import seed, sign_in


@pytest.mark.parametrize('kind', ['owner', 'staff', 'restricted', 'multi'])
@pytest.mark.parametrize('lazy', [False, True])
def test_legacy_list_global_and_permissions_unchanged(app, client, kind, lazy):
    ids = seed(app, kind)
    sign_in(client, kind)
    app.config['TESTING'] = not lazy
    for query in ('', '?branch=1', '?branch=2', '?branch=999999'):
        shell = client.get('/dashboard' + query)
        assert shell.status_code == 200
        assert b'Company-wide outstanding legacy balance' not in shell.data
        response = client.get('/dashboard/partials/legacy' + query)
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert 'Legacy orders' in body
        assert 'LEG-OPEN' in body and 'LEG-PARTIAL' in body
        assert 'R950.00' not in body and 'href=' not in body
        assert 'LEG-SETTLED' not in body
        if not lazy:
            assert 'LEG-OPEN' in shell.get_data(as_text=True)
        if kind in ('restricted', 'staff'):
            # Customers are already shared for customer-enabled staff; no new grant.
            assert client.get('/customers/' + str(ids['bob'])).status_code == (403 if kind == 'restricted' else 200)
            assert client.get('/orders/' + str(ids['two'])).status_code in (403,404)
        if kind == 'restricted':
            for path in ['/orders','/payments','/reports','/dashboard/partials/owing']:
                assert client.get(path).status_code == 403
    if kind == 'restricted':
        with client.application.test_request_context():
            from flask import session
            with client.session_transaction() as state:
                session.update(state)
            assert all(row['customer_id'] != ids['bob'] for row in outstanding_legacy_balances())


def test_late_only_no_prelimit_and_branch_scope(app, client, monkeypatch):
    ids = seed(app, 'staff')
    with app.app_context():
        db = get_db()
        db.execute("UPDATE orders SET status='cancelled'")
        for i in range(105):
            add_order(db, f'NOT-LATE-{i}', ids['alice'], 'reserved', 0, created_at='2020-01-01T00:00:00')
        for number, status, end, branch in [
            ('LATE-ONE','started','2026-01-01T10:00:00',1),
            ('LATE-TWO','started','2026-01-01T10:00:00',2),
            ('FUTURE','started','2099-01-01T10:00:00',1),
            ('RETURNED','returned','2026-01-01T10:00:00',1),
            ('CANCELLED','cancelled','2026-01-01T10:00:00',1),
        ]:
            oid=add_order(db,number,ids['alice'],status,0,branch_id=branch,created_at=end)
        db.commit()
    sign_in(client,'staff')
    for path in ['/dashboard','/dashboard/partials/movement']:
        body=client.get(path).get_data(as_text=True)
        assert 'Late trailers' in body and 'Coming back' not in body
        # Only inspect return card, not unrelated owing/pickup rows.
        card=body.split('<h2>Late trailers</h2>')[1].split('</article>')[0]
        assert 'LATE-ONE' in card and 'LATE-TWO' not in card
        for forbidden in ['NOT-LATE','FUTURE','RETURNED','CANCELLED']:
            assert forbidden not in card


def test_anonymous_legacy_list(client):
    assert client.get('/dashboard/partials/legacy').status_code == 302
    assert client.post('/dashboard/partials/legacy').status_code == 405


def test_browser_lazy_mobile_desktop(app,client):
    seed(app,'restricted')
    sign_in(client,'restricted')
    cookie=client.get_cookie('session')
    app.config['TESTING']=False
    server=make_server('127.0.0.1',0,app,threaded=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    output=Path('C:/Users/djjac/AppData/Local/hermes/cache/scratch/ABI-341953323')
    output.mkdir(parents=True,exist_ok=True)
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(channel='chrome',headless=True)
            for width,height in [(1440,1000),(390,844)]:
                context=browser.new_context(viewport={'width':width,'height':height})
                context.add_cookies([{'name':'session','value':cookie.value,'domain':'127.0.0.1','path':'/'}])
                page=context.new_page()
                errors=[]
                page.on('pageerror',lambda err:errors.append(str(err)))
                page.goto(f'http://127.0.0.1:{server.server_port}/dashboard',wait_until='networkidle')
                page.locator('#owing-legacy-rows').wait_for()
                assert page.locator('#owing-legacy-rows').get_by_text('LEG-OPEN',exact=True).count()==1
                assert page.get_by_role('heading',name='Late trailers').count()==1
                assert page.locator('#legacy-company-summary').count()==0
                assert page.locator('#owing-legacy-rows a').count()==0
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert not errors
                page.locator('#legacy-orders').scroll_into_view_if_needed()
                page.screenshot(path=str(output/f'dashboard-{width}.png'),full_page=True)
                context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        assert not thread.is_alive()
