"""ABI-341953144: borrowing capacity and retained funds are independent."""
import importlib.util
from pathlib import Path
import threading
import time

import pytest

from app.db import get_db
from app.routes.orders import _customers, _customer_summary_by_id
from app.services.credit_limits import outstanding_debt, ensure_credit_capacity

spec = importlib.util.spec_from_file_location('card_helpers', Path(__file__).with_name('test_ticket_341953074_customer_credit_card.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
app = helpers.app
client = helpers.client


def seed(app, debt=400, limit=1000, prepaid=250):
    with app.app_context():
        db = get_db()
        active = helpers.seed_customer(db, 'Activated')
        inactive = helpers.seed_customer(db, 'Inactive')
        db.execute('UPDATE customers SET credit_allowed=1, credit_limit=? WHERE id=?', (limit, active))
        if debt:
            oid = helpers.seed_order(db, active, 'CARD-INVOICE', total=debt)
            db.execute("INSERT INTO documents(order_id,document_type,number,status,created_at) VALUES (?,'invoice','CARD-INV','finalized','2026-10-01')", (oid,))
        helpers.seed_credit(db, active, prepaid)
        db.commit()
        return active, inactive


@pytest.mark.parametrize('debt,limit,available', [(0,1000,1000),(400,1000,600),(1000,1000,0)])
def test_capacity_matches_enforcement_and_prepaid_is_independent(app, client, debt, limit, available):
    helpers.login(client)
    cid, inactive = seed(app, debt, limit, 250)
    with app.app_context():
        summary = _customer_summary_by_id(cid)
        assert summary['credit_allowed'] is True
        assert summary['credit_limit'] == limit
        assert summary['available_account_credit'] == available
        assert summary['customer_credit_balance'] == 250
        assert outstanding_debt(cid) == debt
        with pytest.raises(ValueError, match='credit limit'):
            ensure_credit_capacity(cid, available + .01)
        if available:
            ensure_credit_capacity(cid, available)
        plain = _customer_summary_by_id(inactive)
        assert plain['credit_allowed'] is False
        assert plain['available_account_credit'] == plain['customer_credit_balance'] == 0
    response = client.get(f'/orders/new?customer_id={cid}')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert f'id="account-credit-available">R{available:.2f}' in body
    assert body.count('id="customer-credit-card"') == 1
    assert 'id="customer-credit-value" class="balance-credit">R250.00' in body


def test_inactive_zero_and_unselected_placeholders(app, client):
    helpers.login(client)
    _, cid = seed(app)
    body = client.get(f'/orders/new?customer_id={cid}').get_data(as_text=True)
    assert 'Not activated</strong>' in body
    assert 'id="customer-credit-value" class="balance-credit">R0.00' in body
    body = client.get('/orders/new').get_data(as_text=True)
    assert 'id="customer-credit-value" class="balance-credit">—' in body
    assert '>—</strong>' in body


def test_payment_rules_retained_credit_and_single_query(app, client):
    helpers.login(client)
    cid, _ = seed(app)
    with app.app_context():
        db = get_db()
        oid = db.execute("SELECT id FROM orders WHERE order_number='CARD-INVOICE'").fetchone()['id']
        for amount, status, deleted in [(100,'paid',''),(-20,'paid',''),(200,'archived',''),(200,'paid','deleted')]:
            db.execute("INSERT INTO payments(order_id,amount,method,status,deleted_at,created_at) VALUES (?,?,'eft',?,?,'2026-10-01')", (oid, amount, status, deleted))
        db.execute("UPDATE orders SET status='archived' WHERE id=?", (oid,))
        helpers.seed_order(db, cid, 'CARD-DRAFT', total=500, status='draft')
        db.execute("INSERT INTO customer_credits(customer_id,amount,source_type,status,created_at) VALUES (?,-50,'order_payment','active','2026-10-01')", (cid,))
        db.execute("INSERT INTO customer_credits(customer_id,amount,source_type,status,created_at) VALUES (?,900,'order_refund','archived','2026-10-01')", (cid,))
        db.commit()
        queries = []
        db.set_trace_callback(queries.append)
        started = time.perf_counter()
        summaries = _customers()
        elapsed = time.perf_counter() - started
        db.set_trace_callback(None)
        assert len(queries) == 1
        summary = next(s for s in summaries if s['id'] == cid)
        assert summary['available_account_credit'] == 180
        assert outstanding_debt(cid) == 820
        assert summary['customer_credit_balance'] == 200
        print(f'Customer summaries: one SQL query, {elapsed:.4f}s')


def test_inline_create_and_validation_rerender(app, client):
    helpers.login(client)
    cid, _ = seed(app)
    response = client.post('/orders/new', data={'order_action':'create_customer_continue', 'name':'Inline Card Customer', 'customer_type':'individual'}, headers={'Accept':'application/json', 'X-Requested-With':'fetch'})
    assert response.status_code == 200
    summary = response.get_json()['customer']
    assert summary['credit_allowed'] is False
    assert summary['credit_limit'] == summary['available_account_credit'] == summary['customer_credit_balance'] == 0
    response = client.post('/orders/new', data={'customer_id':str(cid)})
    body = response.get_data(as_text=True)
    assert 'id="account-credit-limit">R1000.00' in body
    assert 'id="account-credit-available">R600.00' in body


@pytest.mark.parametrize('viewport', [{'width':1440,'height':1000},{'width':390,'height':844}])
def test_real_browser_switch_clear_and_unmatched(app, client, viewport):
    from playwright.sync_api import sync_playwright
    from werkzeug.serving import make_server
    helpers.login(client)
    active, inactive = seed(app)
    # Use an ephemeral port; shut down the exact server, never a pattern kill.
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            context = browser.new_context(viewport=viewport)
            session_cookie = client.get_cookie('session')
            context.add_cookies([{'name':'session','value':session_cookie.value,'url':f'http://127.0.0.1:{server.server_port}'}])
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}/orders/new')
            assert page.locator('#account-credit-status').inner_text() == '—'
            assert page.locator('#customer-credit-value').inner_text() == '—'
            options = page.locator('#customer-options option').evaluate_all('(els)=>els.map(e=>({id:e.dataset.id,value:e.value}))')
            for cid in [active, inactive, active]:
                value = next(o['value'] for o in options if o['id'] == str(cid))
                page.locator('#customer-search').fill(value)
                assert page.locator('#customer-id').input_value() == str(cid)
                if cid == active:
                    assert page.locator('#account-credit-amounts').is_visible()
                    assert '600' in page.locator('#account-credit-available').inner_text()
                    assert '250' in page.locator('#customer-credit-value').inner_text()
                else:
                    assert page.locator('#account-credit-status').inner_text() == 'Not activated'
                    assert page.locator('#account-credit-amounts').is_hidden()
                    assert '0,00' in page.locator('#customer-credit-value').inner_text() or '0.00' in page.locator('#customer-credit-value').inner_text()
            for value in ['Unmatched customer', '']:
                page.locator('#customer-search').fill(value)
                assert page.locator('#customer-id').input_value() == ''
                assert page.locator('#account-credit-status').inner_text() == '—'
                assert page.locator('#customer-credit-value').inner_text() == '—'
            # Create only in the isolated local test database, then switch away/back.
            page.locator('#inline-customer-section input[name="name"]').fill('Browser Inline Customer')
            page.locator('#inline-customer-submit').click()
            page.wait_for_function("document.getElementById('customer-id').value !== ''")
            inline_id = page.locator('#customer-id').input_value()
            assert page.locator('#account-credit-status').inner_text() == 'Not activated'
            page.locator('#customer-search').fill(next(o['value'] for o in options if o['id'] == str(active)))
            page.locator('#customer-search').fill('Browser Inline Customer')
            assert page.locator('#customer-id').input_value() == inline_id
            assert page.locator('#account-credit-status').inner_text() == 'Not activated'
            assert page.locator('#customer-credit-card').count() == 1
            assert not errors, errors
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            page.screenshot(path=str(Path(__file__).parents[1] / f'card-smoke-{viewport["width"]}.png'), full_page=True)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
