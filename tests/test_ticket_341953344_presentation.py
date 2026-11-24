"""Customer actions ordering, batched trailer SKUs and equal dashboard cards."""
import os
import threading
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from app.db import get_db
from app.services.orders import list_orders, order_trailer_prefixes
from test_ticket_341953282_dashboard import app, client, login, add_order
from test_ticket_341953340_staff_edits import make_customer


def seed(app):
    with app.app_context():
        db = get_db()
        cid = make_customer(db, 'Presentation Client')
        empty = make_customer(db, 'No History Client')
        oid = add_order(db, 'PRESENTATION-344', cid, 'returned', 100)
        no_trailer = add_order(db, 'NO-TRAILER-344', cid, 'draft', 0)
        for sku, kind in [('TR-001-999', 'rental'), ('A9B8C7D6', 'rental'),
                          ('12', 'rental'), ('', 'rental'), ('ABC', 'rental'),
                          ('SALE-555', 'sale'), ('SERVICE-666', 'service')]:
            pid = db.execute('INSERT INTO products(name,sku,product_type,created_at) VALUES (?,?,?,?)',
                             ('Trailer '+sku, sku, kind, '2026-11-01')).lastrowid
            db.execute('INSERT INTO order_items(order_id,product_id,quantity) VALUES (?,?,1)', (oid,pid))
        db.execute('INSERT INTO order_items(order_id,custom_name) VALUES (?,?)', (no_trailer,'Custom repair'))
        db.commit()
    return cid, empty, oid, no_trailer


def test_prefixes_display_only_batched_and_stable(app, client):
    cid, empty, oid, no_trailer = seed(app)
    with app.app_context():
        db = get_db()
        before = [tuple(row) for row in db.execute('SELECT * FROM products').fetchall()]
        orders = list_orders()
        queries = []
        db.set_trace_callback(queries.append)
        result = order_trailer_prefixes(orders)
        db.set_trace_callback(None)
        assert result == {oid: '001, 987, 12, —, —'}
        assert len(queries) == 1
        assert no_trailer not in result
        assert order_trailer_prefixes([]) == {}
        assert [tuple(row) for row in db.execute('SELECT * FROM products').fetchall()] == before
        # Repeated product lines must not repeat the same linked trailer.
        pid = db.execute('SELECT product_id FROM order_items WHERE order_id=? ORDER BY id', (oid,)).fetchone()[0]
        db.execute('INSERT INTO order_items(order_id,product_id) VALUES (?,?)',(oid,pid))
        db.commit()
        assert order_trailer_prefixes(orders) == result
    login(client)
    response = client.get('/orders')
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, 'html.parser')
    assert 'TRAILER' in [th.text for th in soup.select('.orders-table th')]
    rows = soup.select('.orders-table tbody tr')
    values = {row.select_one('td strong').text: row.select('td')[4].get_text(strip=True) for row in rows}
    assert values == {'PRESENTATION-344':'001, 987, 12, —, —','NO-TRAILER-344':'—'}
    filtered = client.get('/orders?query=PRESENTATION&status=returned&branch=1').text
    assert 'PRESENTATION-344' in filtered and 'NO-TRAILER-344' not in filtered
    assert '001, 987, 12, —, —' in filtered


def test_prefixes_chunk_large_visible_list(app):
    with app.app_context():
        db = get_db()
        queries = []
        db.set_trace_callback(queries.append)
        assert order_trailer_prefixes([{'id': i} for i in range(805)]) == {}
        db.set_trace_callback(None)
        assert len(queries) == 3


def test_load_more_keeps_trailer_values(app, client):
    cid, _, oid, _ = seed(app)
    with app.app_context():
        db = get_db()
        pid = db.execute('SELECT product_id FROM order_items WHERE order_id=? ORDER BY id',(oid,)).fetchone()[0]
        for i in range(27):
            new_id = add_order(db,f'PAGE-{i:02d}',cid,'returned',10)
            db.execute('INSERT INTO order_items(order_id,product_id) VALUES (?,?)',(new_id,pid))
        db.commit()
    login(client)
    soup = BeautifulSoup(client.get('/orders?query=PAGE-&status=returned').text,'html.parser')
    assert len(soup.select('.orders-table tbody tr')) == 25
    link = soup.select_one('.load-more-row a')['href']
    more = BeautifulSoup(client.get(link).text,'html.parser')
    assert len(more.select('.orders-table tbody tr')) == 27
    assert all(row.select('td')[4].text == '001' for row in more.select('.orders-table tbody tr'))
    assert more.select_one('#new-items') is not None


def test_retained_actions_keep_confirmation_and_csrf(app, client):
    _, cid, _, _ = seed(app)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE customers SET lifecycle_status='deleted' WHERE id=?",(cid,))
        db.commit()
    login(client)
    soup = BeautifulSoup(client.get(f'/customers/{cid}').text,'html.parser')
    actions = soup.select('main.main > section')[-1]
    assert actions.select_one('h2').text == 'Customer actions'
    restore = actions.select_one(f'form[action="/customers/{cid}/restore"]')
    assert restore.select_one('[name=csrf_token]')['value']
    permanent = actions.find('button',string='Permanently delete').find_parent('form')
    assert permanent.select_one('[name=csrf_token]')['value']
    assert permanent.select_one('[name=confirmation]')['required'] == ''
    assert 'confirm(' in permanent['onsubmit']


@pytest.mark.parametrize('history', [False, True])
def test_actions_last_and_delete_safeguards(app, client, history):
    cid, empty, oid, _ = seed(app)
    target = cid if history else empty
    login(client)
    response = client.get(f'/customers/{target}')
    assert response.status_code == 200
    soup = BeautifulSoup(response.text,'html.parser')
    headings = soup.select('main.main > section h2')
    assert headings[-1].get_text(strip=True) == 'Customer actions'
    assert len(soup.find_all('h2',string='Customer actions')) == 1
    actions = headings[-1].find_parent('section')
    assert actions.find_previous_sibling('section')['class'] == ['customer-profile-grid']
    delete = actions.select_one(f'form[action="/customers/{target}/delete"]')
    if history:
        assert delete is None
        assert 'existing orders/history, so deletion is blocked' in actions.text
        client.post(f'/customers/{target}/delete')
        with app.app_context():
            assert get_db().execute('SELECT lifecycle_status FROM customers WHERE id=?',(target,)).fetchone()[0] == 'active'
    else:
        assert delete is not None and 'confirm(' in delete['onsubmit']
    assert soup.select_one('.customer-detail-header .statement-form') is not None
    assert soup.select_one(f'.customer-detail-header a[href="/customers/{target}/edit"]') is not None


@pytest.mark.parametrize('lazy', [False,True])
@pytest.mark.parametrize('query', ['', '?range=all_time', '?branch=1&start_date=2026-11-01&end_date=2026-11-24'])
def test_browser_desktop_mobile(app, client, lazy, query):
    cid, _, _, _ = seed(app)
    login(client)
    cookie = client.get_cookie('session')
    app.config['TESTING'] = not lazy
    server = make_server('127.0.0.1',0,app,threaded=True)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    output = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'hermes/cache/scratch/ABI-341953344'
    output.mkdir(parents=True,exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome',headless=True)
            for width in [1440,1100,1000,390]:
                context = browser.new_context(viewport={'width':width,'height':1000})
                context.add_cookies([{'name':'session','value':cookie.value,'domain':'127.0.0.1','path':'/'}])
                page = context.new_page()
                errors=[]
                page.on('pageerror',lambda err:errors.append(str(err)))
                assert page.goto(f'http://127.0.0.1:{server.server_port}/dashboard'+query,wait_until='networkidle').status == 200
                cards = page.locator('.dashboard-revenue-metrics .metric-card')
                assert cards.count() == 3
                assert all('Loading' not in text for text in cards.all_inner_texts())
                total = page.locator('.dashboard-period-metrics .metric-card').first.bounding_box()
                for card in cards.all():
                    box = card.bounding_box()
                    assert box['width'] == pytest.approx(total['width'],abs=1)
                    assert box['height'] == pytest.approx(total['height'],abs=1), (width,lazy,query,box,total)
                    assert card.evaluate('(e)=>e.scrollHeight<=e.clientHeight && e.scrollWidth<=e.clientWidth')
                styles=page.locator('.dashboard-period-metrics .metric-card small,.dashboard-revenue-metrics .metric-card small').evaluate_all('(els)=>els.map(e=>{const s=getComputedStyle(e);return [s.fontSize,s.fontWeight,s.lineHeight]})')
                assert all(style == styles[0] for style in styles)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                if not query:
                    page.locator('.dashboard-revenue-metrics').screenshot(path=str(output/f'dashboard-{lazy}-{width}.png'))
                assert page.goto(f'http://127.0.0.1:{server.server_port}/customers/{cid}',wait_until='networkidle').status == 200
                actions=page.locator('section.panel').filter(has=page.get_by_role('heading',name='Customer actions',exact=True))
                assert actions.count() == 1
                assert actions.bounding_box()['y'] >= page.locator('.customer-profile-grid').bounding_box()['y'] + page.locator('.customer-profile-grid').bounding_box()['height']
                if not query:
                    actions.screenshot(path=str(output/f'customer-{lazy}-{width}.png'))
                assert page.goto(f'http://127.0.0.1:{server.server_port}/orders?query=PRESENTATION&status=returned&branch=1',wait_until='networkidle').status == 200
                assert page.locator('.orders-table th').nth(4).inner_text() == 'TRAILER'
                assert page.locator('.orders-table tbody td').nth(4).inner_text() == '001, 987, 12, —, —'
                assert page.get_by_role('searchbox',name='Search orders').input_value() == 'PRESENTATION'
                assert not errors
                context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        assert not thread.is_alive()
