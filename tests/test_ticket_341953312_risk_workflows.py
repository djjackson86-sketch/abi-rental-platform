"""Real staff workflows, synthetic local data only, ABI-341953312."""
import os
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from app.db import get_db
from app.services.customers import create_customer, get_customer, raw_custom_fields_for
from test_ticket_341953268_matching import app, client, sign_in


def seed(app):
    with app.app_context():
        source = create_customer({'name':'Flagged Source', 'phone':'0825559876', 'vehicle_reg_no':'CA 123-456', 'vehicle_vin':'ABCD1234567890123'})
        get_db().execute('UPDATE customers SET is_blocked=1, blocked_reason=? WHERE id=?', ('Synthetic risk reason', source))
        get_db().commit()
    return source


def test_staff_identity_persistence_and_public_isolation(app, client):
    seed(app)
    sign_in(client)
    result=client.post('/customers/new', data={'name':'Different Person', 'risk_identity_panel':'1', 'customer_id_number':'P12345678', 'vehicle_reg_no':'CA123456'})
    assert result.status_code == 302
    cid=int(result.location.rsplit('/',1)[1])
    with app.app_context():
        row=get_customer(cid)
        assert row['is_blocked']==0
        assert raw_custom_fields_for(row)['customer_id_number']=='P12345678'
    page=client.get(result.location).text
    assert 'vehicle_registration' in page
    assert 'suspect-warning-banner' in page
    summary=client.get(f'/orders/customer-summary/{cid}').json['customer']
    assert summary['suspect_matches'][0]['reasons']==['vehicle_registration']
    assert summary['form']['customer_id_number']=='P12345678'
    client.post('/logout')
    assert client.post('/customers/suspect-check',data={'vehicle_reg_no':'CA123456'}).status_code==302
    with app.test_request_context('/store',method='POST'):
        public=create_customer({'name':'Public Person','risk_identity_panel':'1','customer_id_number':'SECRET'})
        assert 'customer_id_number' not in raw_custom_fields_for(get_customer(public))


@pytest.mark.parametrize('width,height',[(1440,1000),(390,844)])
def test_browser_vehicle_address_and_order_warning(app,client,width,height):
    seed(app)
    sign_in(client)
    server=make_server('127.0.0.1',0,app,threaded=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    out=Path(os.environ['TMPDIR'])/'ABI-341953312'
    out.mkdir(exist_ok=True)
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(channel='chrome',headless=True)
            context=browser.new_context(viewport={'width':width,'height':height})
            context.add_cookies([{'name':'session','value':client.get_cookie('session').value,'domain':'127.0.0.1','path':'/'}])
            page=context.new_page()
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(base+'/customers/new')
            page.locator('[name=name]').fill('Unrelated Person')
            page.locator('[name=vehicle_reg_no]').fill('ca123456')
            expect(page.locator('#suspect-warning')).to_be_visible()
            expect(page.locator('#suspect-warning-list')).to_contain_text('vehicle registration')
            page.screenshot(path=str(out/f'warning-{width}.png'))
            page.locator('#suspect-warning button').click()
            expect(page.locator('#suspect-warning-banner')).to_be_visible()
            page.locator('#review-suspect-warning').click()
            expect(page.locator('#suspect-warning')).to_be_visible()
            page.locator('#suspect-warning button').click()
            page.locator('#suspect-id').fill('P12345678')
            page.locator('form.customer-form .btn.primary').click()
            page.wait_for_url(lambda url:url.rsplit('/',1)[-1].isdigit())
            cid=int(page.url.rsplit('/',1)[1])
            expect(page.locator('#suspect-warning')).to_be_visible()
            page.goto(base+f'/orders/new?customer_id={cid}')
            expect(page.locator('#suspect-warning')).to_be_visible()
            page.locator('#suspect-warning button').click()
            expect(page.locator('#suspect-warning-banner')).to_be_visible()
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
