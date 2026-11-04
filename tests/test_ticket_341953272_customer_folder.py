"""Retention, directory navigation and staff boundary for customer lifecycle."""
import os
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

from app.db import get_db
from app.services.customers import create_customer, delete_customer, retain_customer, list_customers, customer_filtered_total
from app.services import portal_registrations
from test_ticket_341953272_inactive_customer_exclusion import app, client, login


def seed(app):
    with app.app_context():
        active = create_customer({'name': 'Active Directory Client'})
        deleted = create_customer({'name': 'Deleted Directory Client', 'customer_type': 'company', 'marketing_opt_in': '1'})
        archived = create_customer({'name': 'Archived Directory Client'})
        portal = create_customer({'name': 'Historical Portal Client'})
        portal_registrations.record_pending_registration(portal, 1, 'HISTORICAL')
        db = get_db()
        db.execute("UPDATE portal_registrations SET status='deleted' WHERE customer_id=?", (portal,))
        db.execute("INSERT INTO customer_credits(customer_id,amount,created_at) VALUES (?,75,'')", (deleted,))
        db.execute("INSERT INTO orders(customer_id,order_number,created_at) VALUES (?,'RETAIN-ORDER','')", (archived,))
        db.commit()
        delete_customer(deleted)
        retain_customer(archived, reason='<private reason>')
        return active, deleted, archived, portal


def test_retention_preserves_links_history_and_audit(app, client):
    login(client)
    active, deleted, archived, portal = seed(app)
    with app.app_context():
        db = get_db()
        assert db.execute('SELECT lifecycle_status FROM customers WHERE id=?', (active,)).fetchone()[0] == 'active'
        assert db.execute('SELECT amount FROM customer_credits WHERE customer_id=?', (deleted,)).fetchone()[0] == 75
        assert db.execute('SELECT customer_id FROM orders WHERE order_number=?', ('RETAIN-ORDER',)).fetchone()[0] == archived
        before = dict(db.execute('SELECT * FROM customers WHERE id=?', (archived,)).fetchone())
        assert not retain_customer(archived, 'deleted', 'overwrite')
        assert dict(db.execute('SELECT * FROM customers WHERE id=?', (archived,)).fetchone()) == before
        with pytest.raises(ValueError, match='history'):
            delete_customer(archived)
        assert [c['id'] for c in list_customers()] == [active]
        assert customer_filtered_total(folder='retained') == 3
        assert customer_filtered_total(folder='retained', customer_type='company', marketing='subscribed') == 1
        assert customer_filtered_total(folder='retained', query='Historical') == 1
    page = client.get(f'/customers/{archived}').data
    assert b'&lt;private reason&gt;' in page and b'RETAIN-ORDER' in page
    assert client.get(f'/customers/{archived}/statement').status_code == 200
    assert b'Deleted Directory Client' not in client.get('/customers/export.csv?folder=retained').data


def test_folder_filters_pagination_and_archive_guards(app, client):
    login(client)
    active, deleted, archived, portal = seed(app)
    with app.app_context():
        for i in range(26):
            cid = create_customer({'name': f'Retained Extra {i:02}'})
            retain_customer(cid)
    html = client.get('/customers?folder=retained').get_data(as_text=True)
    assert 'Deleted &amp; Archived (29)' in html
    assert 'of 29' in html and 'folder=retained' in html and 'limit=50' in html
    assert 'export-btn' not in html
    filtered = client.get('/customers?folder=retained&query=Deleted&customer_type=company&marketing=subscribed').data
    assert b'Deleted Directory Client' in filtered and b'Archived Directory Client' not in filtered
    assert client.post(f'/customers/{active}/archive').status_code == 400
    client.get(f'/customers/{active}')
    with client.session_transaction() as sess:
        token = sess['_csrf_tokens']['customer_archive']
    response = client.post(f'/customers/{active}/archive', data={'csrf_token': token, 'reason': 'Withdrawn'}, follow_redirects=True)
    assert response.status_code == 200 and b'Withdrawn' in response.data
    assert client.get(f'/customers/{active}/archive').status_code == 405
    anonymous = app.test_client()
    assert anonymous.get('/customers?folder=retained').status_code == 302
    assert anonymous.post(f'/customers/{active}/archive').status_code == 302
    with client.session_transaction() as sess:
        sess['user_role'] = 'staff'
        sess['staff_modules'] = ['orders']
    assert client.get('/customers?folder=retained').status_code == 403
    assert client.post(f'/customers/{deleted}/archive', data={'csrf_token': token}).status_code == 403



def test_desktop_mobile_folder_navigation(app, client):
    login(client)
    active, deleted, archived, portal = seed(app)
    cookie = client.get_cookie('session')
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = Path.home() / 'AppData/Local/hermes/cache/scratch/ABI-341953272'
    output.mkdir(exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True)
            for width, height, label in [(1440,1000,'desktop'), (390,844,'mobile')]:
                context = browser.new_context(viewport={'width':width,'height':height})
                context.add_cookies([{'name':'session','value':cookie.value,'domain':'127.0.0.1','path':'/'}])
                page = context.new_page()
                page.goto(f'http://127.0.0.1:{server.server_port}/customers')
                page.get_by_role('link', name='Deleted & Archived (3)', exact=True).click()
                expect(page.locator('.customers-table')).to_contain_text('Deleted Directory Client')
                expect(page.locator('.customers-table')).to_contain_text('Historical Portal Client')
                expect(page.locator('.customers-table')).not_to_contain_text('Active Directory Client')
                page.screenshot(path=str(output / f'{label}.png'), full_page=True)
                page.get_by_role('searchbox', name='Search customers').fill('Deleted')
                page.get_by_role('searchbox', name='Search customers').press('Enter')
                expect(page.locator('.customers-table')).to_contain_text('Deleted Directory Client')
                assert 'folder=retained' in page.url
                page.locator('.customer-name-cell').click()
                expect(page.locator('h1')).to_contain_text('Deleted Directory Client')
                expect(page.get_by_text('Deleted', exact=True)).to_be_visible()
                context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print('Desktop/mobile folder click-through passed; server closed; screenshots:', output)
