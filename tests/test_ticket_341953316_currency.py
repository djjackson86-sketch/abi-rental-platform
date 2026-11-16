"""Currency presentation contract; all records below are throwaway SQLite data."""
import os
from pathlib import Path
from threading import Thread

import pymupdf
import pytest
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from app.db import get_db
from app.services.currency import format_amount, format_currency
from app.services import cash
from app.services.customers import customer_statement
from app.services.pdf_documents import (
    _invoice_template_pdf, customer_statement_pdf_bytes,
    RATE_COLUMN_X, RATE_VALUE_RIGHT_EDGE, SUBTOTAL_COLUMN_X,
    SUBTOTAL_VALUE_RIGHT_EDGE, TAX_COLUMN_X, TAX_VALUE_RIGHT_EDGE,
    TOTAL_INCL_COLUMN_X, TOTAL_INCL_VALUE_RIGHT_EDGE,
)
from app.services.telegram import _money
from test_ticket_341953085_customer_statement import (
    app, client, login, seed_customer, seed_order, seed_invoice, seed_payment,
)
from test_pdf_text_encoding import _sample_document, _sample_settings

ARTIFACTS = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.cache'))) / 'hermes/cache/scratch/ABI-341953316'


@pytest.mark.parametrize('value,expected', [
    (0, 'R0.00'), (0.5, 'R0.50'), (999.99, 'R999.99'),
    (1000, 'R1 000.00'), (200134, 'R200 134.00'),
    (1234567890.12, 'R1 234 567 890.12'), (-200134.5, '-R200 134.50'),
    ('1.005', 'R1.01'), ('-1.005', '-R1.01'),
    ('999.995', 'R1 000.00'), (-0.001, 'R0.00'),
    (None, 'R0.00'), ('invalid', 'R0.00'), ('NaN', 'R0.00'),
])
def test_currency_contract(value, expected):
    assert format_currency(value) == expected


def test_symbols_templates_telegram_and_numeric_helpers(app):
    with app.app_context():
        app.config['CURRENCY_SYMBOL'] = '$'
        assert format_currency(-200134) == '-$200 134.00'
        assert _money(200134) == '$200 134.00'
        assert app.jinja_env.from_string('{{ amount|currency }}').render(amount=200134) == '$200 134.00'
        assert format_currency(200134, symbol='R') == 'R200 134.00'
    assert format_amount(200134) == '200 134.00'
    assert cash.money(200134.5) == 200134.5
    from app.services.reports import money
    assert money(200134.5) == 200134.5


def test_all_templates_compile(app):
    for name in app.jinja_env.list_templates():
        app.jinja_env.get_template(name)


def test_cash_csv_keeps_legacy_amounts(app):
    with app.app_context():
        report = cash.day_report('2026-11-16')
        report['cash']['opening'] = 200134.5
        assert any(row[2] == 'R200 134.50' for row in cash.day_report_rows(report))
        assert any(row[2] == 'R200134.50' for row in cash.day_report_csv_rows(report))
        assert not any('200 134' in row[2] for row in cash.day_report_csv_rows(report))


@pytest.mark.parametrize('kind', ['quote', 'invoice'])
def test_large_pdf_values_remain_inside_columns(app, kind):
    document = _sample_document(kind)
    document.update(subtotal=200134, tax_total=0, total=200134, due_total=200134)
    item = dict(product_name='Large amount layout check', product_sku='LAYOUT', custom_name='',
                quantity=1, unit_price=200134, line_subtotal=200134, line_tax=0, line_total=200134)
    with app.app_context():
        data = _invoice_template_pdf(document, [item], _sample_settings())
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / f'{kind}.pdf').write_bytes(data)
    pdf = pymupdf.open(stream=data, filetype='pdf')
    pdf[0].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5)).save(ARTIFACTS / f'{kind}.png')
    spans = [s for b in pdf[0].get_text('dict')['blocks'] for l in b.get('lines', []) for s in l['spans']]
    values = [s for s in spans if s['text'] == 'R200 134.00']
    assert len(values) >= 5
    row = sorted(values, key=lambda s:s['bbox'][1])[0]['bbox'][1]
    cells = sorted([s for s in spans if abs(s['bbox'][1]-row)<2 and s['text'].startswith('R')], key=lambda s:s['bbox'][0])
    assert len(cells) == 4
    for span, (left, right) in zip(cells, [(RATE_COLUMN_X,RATE_VALUE_RIGHT_EDGE),
            (SUBTOTAL_COLUMN_X,SUBTOTAL_VALUE_RIGHT_EDGE), (TAX_COLUMN_X,TAX_VALUE_RIGHT_EDGE),
            (TOTAL_INCL_COLUMN_X,TOTAL_INCL_VALUE_RIGHT_EDGE)]):
        assert span['bbox'][0] >= left - 0.1
        assert span['bbox'][2] <= right + 0.1


def test_browser_desktop_mobile_and_javascript(app, client):
    login(client)
    with app.app_context():
        db = get_db()
        cid = seed_customer(db, name='Currency Layout Check')
        oid = seed_order(db, cid, 'ORD-CURRENCY-CHECK', 200134)
        did = seed_invoice(db, oid, 'INV-CURRENCY-CHECK')
        db.execute("INSERT INTO order_items (order_id, custom_name, quantity, unit_price, line_subtotal, line_tax, line_total, billing_mode) VALUES (?, 'Currency layout item', 1, 200134, 200134, 0, 200134, 'fixed')", (oid,))
        seed_payment(db, oid, 1234.5, '2026-11-16')
        db.commit()
        view = customer_statement(cid)
        statement = customer_statement_pdf_bytes(view)
        js = app.jinja_env.get_template('admin/orders/_currency.js').render(config=app.config)
        before = tuple(db.execute('SELECT total, due_total FROM orders WHERE id=?',(oid,)).fetchone())
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS/'statement.pdf').write_bytes(statement)
    pymupdf.open(stream=statement, filetype='pdf')[0].get_pixmap(matrix=pymupdf.Matrix(1.5,1.5)).save(ARTIFACTS/'statement.png')
    assert b'(R200 134.00)' in statement and b'(-R1 234.50)' in statement
    cookie=client.get_cookie('session')
    server=make_server('127.0.0.1',5057,app,threaded=True)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='chrome', headless=True)
            context=browser.new_context()
            context.add_cookies([dict(name='session',value=cookie.value,url='http://127.0.0.1:5057')])
            page=context.new_page()
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            assert page.evaluate('(function(){'+js+';return [fmt(200134),fmt(-200134.5),fmt(1.005),fmt(-0.001)];})()') == ['R200 134.00','-R200 134.50','R1.01','R0.00']
            for width in (1440,390):
                page.set_viewport_size(dict(width=width,height=1000))
                for route,label in [(f'/orders/{oid}','order'),(f'/documents/{did}','document'),(f'/customers/{cid}','customer'),('/reports','reports'),('/orders/new','new-order')]:
                    assert page.goto('http://127.0.0.1:5057'+route).status == 200
                    page.wait_for_timeout(150)
                    if label=='order':
                        assert page.locator('[data-total-field="total"]').inner_text() == 'R200 134.00'
                        assert page.locator('.money-amount').evaluate('(e)=>getComputedStyle(e).whiteSpace') == 'nowrap'
                        assert page.locator('.order-money-lines tbody td:nth-child(n+4)').evaluate_all('(els)=>els.every(e=>getComputedStyle(e).whiteSpace==="nowrap" && e.scrollWidth<=e.clientWidth+1)')
                    if label=='document':
                        cells=page.locator('.document-line-items tbody td:nth-child(n+4)')
                        assert cells.count()==4
                        assert cells.evaluate_all('(els)=>els.every(e=>getComputedStyle(e).whiteSpace==="nowrap" && e.scrollWidth<=e.clientWidth+1)')
                    if label=='new-order':
                        page.locator('input[name=custom_name]').first.fill('Preview only')
                        page.locator('input[name=custom_unit_price]').first.fill('200134')
                        page.locator('input[name=quantity]').first.fill('1')
                        assert page.locator('.line-total-preview').first.inner_text() == 'R200 134.00'
                        assert page.locator('#estimate-order-total').inner_text().startswith('R')
                        # Number-valued input attributes must never acquire grouping or symbols.
                        for v in page.locator('input[type=number]').evaluate_all('(els)=>els.map(e=>e.value).filter(Boolean)'):
                            float(v)
                    page.screenshot(path=str(ARTIFACTS/f'{label}-{width}.png'),full_page=True)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            assert not errors,errors
            shop_js=(Path(app.static_folder)/'js/shop.js').read_text(encoding='utf-8')
            page.set_content('<select data-hire-product><option value="1" data-rate="200134" data-deposit="1234.50" selected>Test trailer</option></select><div data-hire-summary></div>')
            page.add_script_tag(content=shop_js)
            assert page.locator('[data-hire-summary]').inner_text() == 'Inventory rate: R200 134.00\n\nRefundable deposit: R1 234.50'
            browser.close()
    finally:
        server.shutdown();server.server_close();thread.join(timeout=10)
    with app.app_context():
        assert tuple(get_db().execute('SELECT total, due_total FROM orders WHERE id=?',(oid,)).fetchone()) == before
