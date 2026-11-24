"""Order-edit catalogue waiver selection: identity, totals and real browser round trips."""
import threading

import pytest
from werkzeug.serving import make_server

from app.db import get_db
from app.services.orders import create_order, update_draft_order
from test_ticket_341953333_order_notes import (
    app, client, login, seed_order, order_payload, order_row,
)

WAIVERS = [
    (811, 'Single Axle Damage Waiver', '99a2066b-17fe-43d8-8ce5-d87980ed4539', 40),
    (812, 'Double Axle Damage Waiver', 'e94848d5-1278-40cd-8905-af5f50813f05', 75),
]


def setup_order(app, product_id=811, option='security_deposit', legacy=0):
    ids = seed_order(app)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE company_settings SET tax_mode='exclusive' WHERE id=1")
        for pid, name, source_id, price in WAIVERS + [(813, 'Single Axle Damage Waiver extra', '', 30)]:
            db.execute("""INSERT INTO products
                (id,name,source_id,product_type,price_amount,price_unit,security_deposit,quantity,created_at)
                VALUES (?,?,?,'service',?,'day',0,1,'2026-07-01')""", (pid,name,source_id,price))
        db.execute("""INSERT INTO products
            (id,name,product_type,price_amount,price_unit,security_deposit,quantity,created_at)
            VALUES (810,'Test trailer','rental',100,'day',500,1,'2026-07-01')""")
        db.execute("UPDATE orders SET deposit_option=?,damage_waiver_amount=? WHERE id=?", (option,legacy,ids['order_id']))
        db.commit()
    form = order_payload(ids['customer_id'])
    form.setlist('product_id', ['810', str(product_id)] if product_id else ['810'])
    form.setlist('custom_name', ['', ''])
    form.setlist('custom_unit_price', ['', ''])
    form.setlist('custom_billing_mode', ['fixed', 'fixed'])
    form.setlist('quantity', ['1', '1'])
    form['deposit_option'] = option
    # Exactly one rental day, avoiding branch-hours/availability side effects.
    form['end_date'] = '2026-07-02'
    form['end_time'] = '09:00'
    return ids, form


@pytest.mark.parametrize('pid,price', [(811,40),(812,75)])
@pytest.mark.parametrize('option', ['security_deposit','damage_waiver','no_deposit'])
def test_edit_forces_waiver_without_duplicate_fee(app, client, pid, price, option):
    ids, form = setup_order(app, pid, option)
    form['security_deposit_amount'] = '999'
    login(client)
    response = client.post(f"/orders/{ids['order_id']}/edit", data=form, follow_redirects=True)
    assert response.status_code == 200
    assert 'Order saved' in response.text
    row = order_row(app, ids['order_id'])
    assert row['deposit_option'] == 'damage_waiver'
    assert row['deposit_total'] == 0
    assert row['damage_waiver_amount'] == 0
    assert row['total'] == 100 + price
    assert row['due_total'] == row['total']
    with app.app_context():
        items = get_db().execute('SELECT * FROM order_items WHERE order_id=?', (ids['order_id'],)).fetchall()
        assert len(items) == 2
        assert sum(x['line_total'] for x in items) == row['total']
        update_draft_order(ids['order_id'], form)
    assert order_row(app, ids['order_id'])['total'] == 100 + price


@pytest.mark.parametrize('option,deposit', [('security_deposit',500),('damage_waiver',0),('no_deposit',0)])
def test_similarly_named_ordinary_product_keeps_manual_choice(app, option, deposit):
    ids, form = setup_order(app, 813, option)
    with app.app_context():
        update_draft_order(ids['order_id'], form)
    row = order_row(app, ids['order_id'])
    assert row['deposit_option'] == option
    assert row['deposit_total'] == deposit
    assert row['total'] == 130 + deposit


@pytest.mark.parametrize('amount', [None, '', '0', '25'])
def test_legacy_waiver_amount_preserved_unless_explicit(app, amount):
    ids, form = setup_order(app, 811, 'damage_waiver', legacy=25)
    if amount is not None:
        form['damage_waiver_amount'] = amount
    with app.app_context():
        update_draft_order(ids['order_id'], form)
    row = order_row(app, ids['order_id'])
    expected = 0 if amount == '0' else 25
    assert row['damage_waiver_amount'] == expected
    assert row['total'] == 140 + expected


def test_identity_survives_catalogue_rename(app):
    ids, form = setup_order(app)
    with app.app_context():
        get_db().execute("UPDATE products SET name='Renamed waiver' WHERE id=811")
        get_db().commit()
        update_draft_order(ids['order_id'], form)
    assert order_row(app, ids['order_id'])['deposit_option'] == 'damage_waiver'


def test_removing_waiver_does_not_restore_security(app):
    ids, form = setup_order(app)
    with app.app_context():
        update_draft_order(ids['order_id'], form)
        form.setlist('product_id', ['810'])
        form['deposit_option'] = 'damage_waiver'
        update_draft_order(ids['order_id'], form)
    row = order_row(app, ids['order_id'])
    assert row['deposit_option'] == 'damage_waiver'
    assert row['deposit_total'] == 0 and row['total'] == 100


def test_stored_discount_is_recalculated_without_security(app):
    ids, form = setup_order(app)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE orders SET discount_mode='percent', discount_value=10 WHERE id=?", (ids['order_id'],))
        db.commit()
        update_draft_order(ids['order_id'], form)
    row = order_row(app, ids['order_id'])
    assert row['discount_total'] == 14
    assert row['deposit_total'] == 0 and row['total'] == 126


def test_new_order_still_respects_manual_security_choice(app):
    ids, form = setup_order(app)
    with app.app_context():
        oid = create_order(form, notify=False)
    row = order_row(app, oid)
    assert row['deposit_option'] == 'security_deposit'
    assert row['deposit_total'] == 500


@pytest.mark.parametrize('pid,price', [(811,40),(812,75)])
@pytest.mark.parametrize('width', [1440,390])
def test_real_browser_load_add_remove_save_reload(app, client, pid, price, width, tmp_path):
    from playwright.sync_api import sync_playwright
    ids, form = setup_order(app, pid)
    with app.app_context():
        # Seed a historically mismatched order without the edit-only correction.
        oid = create_order(form, notify=False)
    login(client)
    cookie = client.get_cookie('session')
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            context = browser.new_context(viewport={'width':width,'height':1000})
            context.add_cookies([{'name':'session','value':cookie.value,'url':origin}])
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(f'{origin}/orders/{oid}/edit')
            assert page.locator('#deposit-option').input_value() == 'damage_waiver'
            assert page.locator('#estimate-deposit-total').inner_text().endswith('0.00')
            assert not page.locator('#security-deposit-override-wrap').is_visible()
            rows = page.locator('#order-lines-body .enhanced-line:not(#order-line-template)')
            rows.nth(1).locator('.line-remove-row').click()
            assert page.locator('#deposit-option').input_value() == 'damage_waiver'
            page.locator('#deposit-option').select_option('security_deposit')
            assert page.locator('#estimate-deposit-total').inner_text().endswith('500.00')
            page.locator('#add-product-line').click()
            value = page.locator(f'#product-options option[data-id="{pid}"]').get_attribute('value')
            rows.nth(1).locator('.product-search').fill(value)
            rows.nth(1).locator('.product-search').dispatch_event('change')
            assert page.locator('#deposit-option').input_value() == 'damage_waiver'
            assert page.locator('#estimate-deposit-total').inner_text().endswith('0.00')
            assert page.locator('#estimate-order-total').inner_text().endswith(f'{100+price}.00')
            # An attempted security override is immediately corrected while the product is present.
            page.locator('#deposit-option').select_option('security_deposit')
            assert page.locator('#deposit-option').input_value() == 'damage_waiver'
            page.locator('#deposit-option').evaluate('(el)=>el.closest("section").scrollIntoView()')
            page.screenshot(path=str(tmp_path / f'waiver-{pid}-{width}.png'))
            page.on('dialog', lambda dialog: dialog.accept())
            page.locator('#save-order-button').click()
            page.wait_for_url(f'{origin}/orders/{oid}')
            row = order_row(app, oid)
            assert row['deposit_option'] == 'damage_waiver'
            assert row['deposit_total'] == 0 and row['total'] == 100+price
            page.goto(f'{origin}/orders/{oid}/edit')
            assert page.locator('#deposit-option').input_value() == 'damage_waiver'
            assert page.locator('#estimate-order-total').inner_text().endswith(f'{100+price}.00')
            assert errors == []
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
