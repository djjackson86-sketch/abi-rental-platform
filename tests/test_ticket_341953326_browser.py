"""Real Chrome: lazy dashboard -> legacy client -> payment/order, both sizes."""
import threading
from pathlib import Path
import pytest
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server
from app.db import get_db
from test_ticket_341953282_dashboard import app,client,login
from test_ticket_341953326_legacy import seed_references
from test_ticket_341953326_revenue import seed_revenue


def test_browser_revenue_and_legacy_workflow(app,client):
    ids=seed_references(app)
    seed_revenue(app)
    login(client)
    cookie=client.get_cookie('session')
    app.config['TESTING']=False
    server=make_server('127.0.0.1',0,app,threaded=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    output=Path('C:/Users/djjac/AppData/Local/hermes/cache/scratch/ABI-341953326'); output.mkdir(parents=True,exist_ok=True)
    origin=f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(channel='chrome',headless=True)
            for width,height in [(1440,1000),(390,844)]:
                context=browser.new_context(viewport={'width':width,'height':height})
                context.add_cookies([{'name':'session','value':cookie.value,'domain':'127.0.0.1','path':'/'}])
                page=context.new_page(); errors=[]
                page.on('pageerror',lambda err:errors.append(str(err)))
                page.goto(origin+'/dashboard?start_date=2026-11-01&end_date=2026-11-17',wait_until='networkidle')
                cards=page.locator('.dashboard-revenue-metrics')
                cards.locator('.revenue-last-month').first.wait_for()
                assert cards.locator('.metric-card').count()==3
                assert cards.locator('.revenue-last-month').count()==3
                assert 'R300.00' in cards.inner_text() and 'R190.00' in cards.inner_text()
                cards.scroll_into_view_if_needed()
                page.screenshot(path=str(output/f'dashboard-{width}.png'))
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                # Follow the exact new legacy-customer link (not a manual URL).
                link=page.locator(f'#owing-legacy-rows a[href="/customers/{ids["alice"]}#legacy-balance"]').first
                link.click(); page.wait_for_url('**#legacy-balance')
                legacy=page.locator('#legacy-balance')
                assert legacy.locator('form.legacy-payment-form').count()==1
                assert 'Imported source only' in legacy.inner_text()
                legacy.scroll_into_view_if_needed()
                page.screenshot(path=str(output/f'legacy-{width}.png'))
                # Local throwaway database only: exercise real payment save and reload.
                form=legacy.locator('form.legacy-payment-form')
                form.locator('[name=amount]').fill('10')
                form.locator('[name=method]').select_option('cash')
                form.locator('[name=reference]').fill(f'BROWSER-326-{width}')
                form.get_by_role('button',name='Record payment',exact=True).scroll_into_view_if_needed()
                page.screenshot(path=str(output/f'payment-{width}.png'))
                form.get_by_role('button',name='Record payment',exact=True).click()
                page.wait_for_load_state('networkidle')
                assert f'BROWSER-326-{width}' in page.locator('#legacy-balance').inner_text()
                page.locator(f'#legacy-balance a[href="/orders/{ids["open_order"]}"]').click()
                page.wait_for_url(f'**/orders/{ids["open_order"]}')
                assert 'LEG-OPEN' in page.inner_text('body')
                assert not errors,errors
                context.close()
            browser.close()
        with app.app_context():
            assert get_db().execute("SELECT COUNT(*) FROM legacy_balance_payments WHERE reference LIKE 'BROWSER-326-%'").fetchone()[0]==2
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=10)
        assert not thread.is_alive()
