import os
import threading
from pathlib import Path
import pytest
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright, expect
from test_ticket_341953268_matching import app, client, sign_in, customer_row


def test_desktop_mobile_suspect_workflow(app, client):
    sign_in(client)
    cookie=client.get_cookie('session')
    server=make_server('127.0.0.1',0,app,threaded=True)
    base=f'http://127.0.0.1:{server.server_port}'
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    output=Path(os.environ.get('TMPDIR',str(Path.home()))) / 'ABI-341953268'
    output.mkdir(exist_ok=True)
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(channel='chrome',headless=True)
            context=browser.new_context(viewport={'width':1440,'height':1000})
            context.add_cookies([{'name':'session','value':cookie.value,'domain':'127.0.0.1','path':'/'}])
            page=context.new_page()
            errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(base+'/customers/new')
            assert page.locator('[name=name]').count(), (page.url,page.locator('body').inner_text()[:1200])
            page.locator('[name=name]').fill('Synthetic Suspect')
            page.locator('[name=email]').fill('synthetic@example.test')
            page.locator('[name=phone]').fill('0825559876')
            page.locator('#save-suspect').click()
            page.locator('#suspect-capture-id').fill('8001015000081')
            page.locator('#suspect-capture-form button[type=submit]').click()
            page.wait_for_url('**/customers/*')
            expect(page.locator('h1')).to_contain_text('Suspect/Fraud')
            flagged=int(page.url.rsplit('/',1)[1])
            assert customer_row(app,flagged)['is_blocked']==1
            page.goto(base+'/customers/new')
            page.locator('[name=name]').fill('Synthetic Suspekt')
            page.locator('[name=phone]').fill('+27 82 555 9876')
            page.locator('[name=phone]').dispatch_event('change')
            expect(page.locator('#suspect-warning')).to_be_visible()
            expect(page.locator('#suspect-warning-list')).to_contain_text('Synthetic Suspect')
            # Ticket ABI-341953269: the popup row shows the ID captured on the flagged
            # record so staff can compare it with the number they are entering.
            warning_text=page.locator('#suspect-warning').inner_text()
            assert '8001015000081' in warning_text, warning_text
            assert 'Synthetic Suspect (review record) · ID 8001015000081' in warning_text, warning_text
            page.screenshot(path=str(output/'desktop.png'),full_page=False)
            page.set_viewport_size({'width':390,'height':844})
            page.screenshot(path=str(output/'mobile.png'),full_page=False)
            box=page.locator('#suspect-warning').bounding_box()
            assert box['x']>=0 and box['x']+box['width']<=390
            page.locator('#suspect-warning button').click()
            # Similarity warns but does not block ordinary Save.
            page.locator('form.customer-form .btn.primary').click()
            page.wait_for_url('**/customers/*')
            duplicate=int(page.url.rsplit('/',1)[1])
            assert customer_row(app,duplicate)['is_blocked']==0
            expect(page.locator('#suspect-warning')).to_be_visible()
            # Following the popup link to the flagged record shows the captured number.
            page.goto(base+'/customers/'+str(flagged))
            expect(page.locator('.customer-info-card')).to_contain_text('Captured ID number')
            expect(page.locator('.customer-info-card')).to_contain_text('8001015000081')
            page.screenshot(path=str(output/'flagged-record.png'),full_page=False)
            # Re-opening the capture prompt on that record prefills the stored number.
            page.goto(base+'/customers/'+str(flagged)+'/edit')
            page.locator('#save-suspect').click()
            expect(page.locator('#suspect-capture-id')).to_have_value('8001015000081')
            page.locator('#suspect-capture-cancel').click()
            # An entry with no id_number key at all (the order form's "this client is a
            # suspect" row) prints no ID text, while a matched client with nothing
            # captured says so - never a blank ' · ID '.
            page.evaluate("window.showSuspectWarnings([{id:1,name:'No Key Client',reasons:['name']}])")
            assert page.locator('#suspect-warning-list').inner_text()=='No Key Client (review record) — name'
            page.evaluate("window.showSuspectWarnings([{id:1,name:'No Id Client',id_number:'',reasons:['phone']}])")
            assert 'No Id Client (review record) · no ID captured — phone' in page.locator('#suspect-warning-list').inner_text()
            page.locator('#suspect-warning button').click()
            page.goto(base+'/customers?blocked=1')
            expect(page.locator('.customers-table')).to_contain_text('Synthetic Suspect')
            expect(page.locator('.customers-table')).not_to_contain_text('Synthetic Suspekt')
            assert not errors,errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print('Desktop/mobile Chrome passed; screenshots:', output)
