import json
import sqlite3
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server
from app.db import get_db
from app.services.legacy_balances import upsert_legacy_balance
from scripts.abi_341953282_waive_legacy import correct, TARGETS
from test_ticket_341953282_dashboard import app, client, login, seed_owing_book


def test_correction_preserves_money_and_is_idempotent(app, tmp_path):
    with app.app_context():
        db = get_db()
        for cid, name in TARGETS.items():
            db.execute('INSERT INTO customers (id,name,created_at) VALUES (?,?,?)', (cid,name,'2026-11-04'))
            upsert_legacy_balance(cid, 'source'+str(cid), str(cid), 900 if cid == 2533 else 225)
        db.commit()
        correct(db, tmp_path)
        first = [dict(r) for r in db.execute('SELECT * FROM legacy_customer_balances')]
        assert all(r['status'] == 'waived' and r['settled_amount'] == 0 for r in first)
        correct(db, tmp_path)
        assert first == [dict(r) for r in db.execute('SELECT * FROM legacy_customer_balances')]
        assert db.execute('SELECT COUNT(*) FROM legacy_balance_payments').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM payments').fetchone()[0] == 0


def test_ambiguous_identity_aborts(app, tmp_path):
    with app.app_context():
        db = get_db()
        for cid,name in TARGETS.items():
            db.execute('INSERT INTO customers(id,name,created_at) VALUES(?,?,?)',(cid,name,'2026-11-04'))
        db.execute("INSERT INTO customers(name,created_at) VALUES('Gordon Nzalo','2026-11-04')")
        with pytest.raises(ValueError, match='Ambiguous'):
            correct(db, tmp_path)
        assert not list(tmp_path.iterdir())


def test_desktop_mobile_browser(app, client):
    ids = seed_owing_book(app)
    with app.app_context():
        upsert_legacy_balance(ids['alice'], 'alice', 'LEGACY-TEST', 640)
        get_db().commit()
    login(client)
    cookie = client.get_cookie('session')
    server = make_server('127.0.0.1', 0, app, threaded=True)
    base = f'http://127.0.0.1:{server.server_port}'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = Path('C:/Users/djjac/AppData/Local/hermes/cache/scratch/ABI-341953282')
    output.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True)
            for width,height in [(1440,1000),(390,844)]:
                context = browser.new_context(viewport={'width':width,'height':height})
                context.add_cookies([{'name':'session','value':cookie.value,'domain':'127.0.0.1','path':'/'}])
                page = context.new_page()
                errors=[]
                page.on('pageerror', lambda err: errors.append(str(err)))
                page.goto(base + '/customers', wait_until='networkidle')
                assert page.locator('#vehicles-awaiting-allocation h2').count(), (page.url, page.locator('body').inner_text()[:1500])
                heading=page.locator('#vehicles-awaiting-allocation h2')
                empty=page.locator('.pending-queue-empty')
                assert abs(heading.bounding_box()['x']-empty.evaluate('(e)=>e.getBoundingClientRect().x + parseFloat(getComputedStyle(e).paddingLeft)')) < 1
                notification_radius=page.locator('.notify-entry a').evaluate('(e)=>getComputedStyle(e).borderRadius')
                links=page.locator('.mobile-nav a' if width < 860 else '.sidebar>a:not(.logo)')
                assert all(radius == notification_radius for radius in links.evaluate_all('(es)=>es.map(e=>getComputedStyle(e).borderRadius)'))
                page.get_by_label('With legacy balance').check()
                page.wait_for_url('**/*legacy=1*')
                page.wait_for_load_state('networkidle')
                assert 'legacy=1' in page.url
                assert page.locator('td.legacy-balance-cell').all_text_contents() == ['R640.00']
                page.get_by_role('link', name='Legacy balance', exact=False).click()
                page.wait_for_load_state('networkidle')
                assert 'sort=legacy_balance' in page.url and 'dir=asc' in page.url and 'legacy=1' in page.url
                page.get_by_role('link', name='Legacy balance', exact=False).click()
                page.wait_for_load_state('networkidle')
                assert 'dir=desc' in page.url
                page.screenshot(path=str(output / f'customers-{width}.png'), full_page=True)
                page.goto(base + '/dashboard', wait_until='networkidle')
                panel=page.locator('#owing-customers')
                assert 'OWE-OLD' in panel.inner_text()
                assert panel.locator('.balance-owing').first.evaluate('(e)=>getComputedStyle(e).color') == 'rgb(180, 35, 24)'
                panel.screenshot(path=str(output / f'owing-{width}.png'))
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert not errors, errors
                context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
