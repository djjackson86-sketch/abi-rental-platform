"""Scoped customer capture and atomic waiting-vehicle allocation."""
import os
import threading
from pathlib import Path
import pytest
from flask import session
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from app import create_app
from app.db import get_db
from app.services import customers, vehicles
from test_ticket_341953268_matching import sign_in
from test_vehicle_waiting_list import scan_form

@pytest.fixture()
def app(tmp_path):
    return create_app({'TESTING':True,'DATABASE':str(tmp_path/'capture.db'),
        'TURSO_DATABASE_URL':'','TURSO_AUTH_TOKEN':'','SECRET_KEY':'test',
        'ADMIN_EMAIL':'admin@abi.local','ADMIN_PASSWORD':'admin123',
        'TELEGRAM_NOTIFICATIONS_ENABLED':''})

@pytest.fixture()
def client(app):
    return app.test_client()

def token(client):
    client.get('/customers/new')
    with client.session_transaction() as s:
        return s['_csrf_tokens']['new_customer_vehicle']

def queue(app):
    with app.test_request_context('/scan-vehicle'):
        session['branch_id']=1
        session['user_id']=1
        session['user_role']='owner'
        return vehicles.save_waiting_vehicle(scan_form('TEST314GP'))

def counts(app):
    with app.app_context():
        return tuple(get_db().execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('customers','vehicles','vehicle_waiting_list','orders'))

@pytest.mark.parametrize('form',[{}, {'suspect_fraud':'1'}, {'blocking_panel':'1','action':'save_suspect','suspect_fraud':'0'}])
def test_normal_name_required(app,client,form):
    sign_in(client)
    before=counts(app)
    result=client.post('/customers/new',data=form)
    assert result.status_code==200
    assert 'Customer name is required' in result.text
    assert counts(app)==before

def test_unnamed_flagged_save_edit_and_display(app,client):
    sign_in(client)
    data={'blocking_panel':'1','action':'save_suspect','suspect_fraud':'1','phone':'0823140000','customer_id_number':'TEST314','suspect_reason':'Synthetic evidence','address_line1':'Test road'}
    result=client.post('/customers/new',data=data)
    assert result.status_code==302
    cid=int(result.location.rsplit('/',1)[1])
    with app.app_context():
        c=customers.get_customer(cid)
        assert c['name']=='' and c['is_blocked']==1
        assert customers.customer_is_suspect(c)
        assert customers.customer_id_number_for(c)=='TEST314'
        assert c['address_line1']=='Test road'
    assert f'Suspect/Fraud #{cid}' in client.get(result.location).text
    assert f'Suspect/Fraud #{cid}' in client.get('/customers').text
    assert client.post(f'/customers/{cid}/edit',data={'blocking_panel':'1','phone':'0823140001'}).status_code==302
    # Explicitly clearing classification restores ordinary validation.
    result=client.post(f'/customers/{cid}/edit',data={'blocking_panel':'1','action':'save_suspect','suspect_fraud':'0'})
    assert 'Customer name is required' in result.text
    with app.test_request_context('/store',method='POST'):
        with pytest.raises(ValueError,match='name is required'):
            customers.create_customer(data)

@pytest.mark.parametrize('mode',['valid','stale','invalid','missing_csrf','bad_name','conflict'])
def test_atomic_vehicle_save(app,client,mode):
    sign_in(client)
    wid=queue(app)
    csrf=token(client)
    before=counts(app)
    data={'name':'Vehicle Test','waiting_id':str(wid),'csrf_token':csrf}
    if mode=='stale':
        with app.app_context():
            vehicles.remove_waiting_vehicle(wid)
        before=counts(app)
    if mode=='invalid': data['waiting_id']='999999'
    if mode=='missing_csrf': data.pop('csrf_token')
    if mode=='bad_name': data['name']=''
    if mode=='conflict':
        with app.app_context():
            cid=customers.create_customer({'name':'Existing owner'},notify=False)
            vehicles.create_vehicle(scan_form('TEST314GP'),customer_id=cid)
            get_db().commit()
        before=counts(app)
    result=client.post('/customers/new',data=data)
    if mode=='valid':
        assert result.status_code==302
        cid=int(result.location.rsplit('/',1)[1])
        with app.app_context():
            assert len(vehicles.list_vehicles(cid))==1
            assert customers.custom_fields_for(customers.get_customer(cid))['vehicle_reg_no']=='TEST314GP'
        assert counts(app)==(before[0]+1,before[1]+1,before[2]-1,before[3])
        assert 'TEST314GP' in client.get(f'/customers/{cid}/edit').text
    else:
        assert result.status_code in (200,400)
        assert counts(app)==before

@pytest.mark.parametrize('width,height',[(1440,1000),(390,844)])
def test_browser_capture(app,client,width,height):
    sign_in(client)
    wid=queue(app)
    server=make_server('127.0.0.1',0,app,threaded=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    out=Path(os.environ['LOCALAPPDATA'])/'hermes'/'cache'/'scratch'/'ABI-341953314';out.mkdir(exist_ok=True)
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(channel='chrome',headless=True)
            context=browser.new_context(viewport={'width':width,'height':height})
            context.add_cookies([{'name':'session','value':client.get_cookie('session').value,'domain':'127.0.0.1','path':'/'}])
            page=context.new_page();errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}/customers/new')
            page.locator('[name=phone]').fill('0823140000')
            page.locator('#save-suspect').click()
            expect(page.locator('#suspect-capture')).to_be_visible()
            page.locator('#suspect-capture-cancel').click()
            assert page.locator('[name=name]').evaluate('(e)=>e.required')
            page.locator('#allocate-new-vehicle').click()
            page.locator('#new-vehicle-choice').select_option(str(wid))
            page.locator('#cancel-new-allocation').click()
            assert page.locator('#new-waiting-id').input_value()==''
            page.locator('#allocate-new-vehicle').click()
            page.locator('#new-vehicle-choice').select_option(str(wid))
            page.screenshot(path=str(out/f'allocation-{width}.png'))
            page.locator('#confirm-new-allocation').click()
            assert counts(app)[2]==1
            page.locator('#save-suspect').click()
            page.locator('#suspect-capture-id').fill('TEST314')
            page.locator('#suspect-capture-reason').fill('Synthetic evidence')
            page.locator('#suspect-capture-form button[type=submit]').click()
            page.wait_for_url(lambda u:u.rsplit('/',1)[-1].isdigit())
            cid=int(page.url.rsplit('/',1)[1])
            expect(page.locator('h1')).to_contain_text(f'Suspect/Fraud #{cid}')
            page.goto(f'http://127.0.0.1:{server.server_port}/customers/{cid}/edit')
            expect(page.locator('[name=vehicle_reg_no]')).to_have_value('TEST314GP')
            assert page.locator('[name=name]').input_value()==''
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(out/f'saved-{width}.png'))
            with app.app_context():
                assert vehicles.list_vehicles(cid)[0]['registration']=='TEST314GP'
                assert customers.customer_suspect_reason_for(customers.get_customer(cid))=='Synthetic evidence'
            assert not errors,errors
            browser.close()
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)


def test_allocation_permissions_and_branch_boundary(app,client):
    sign_in(client)
    wid=queue(app)
    csrf=token(client)
    before=counts(app)
    with app.app_context():
        get_db().execute('UPDATE vehicle_waiting_list SET branch_id=1 WHERE id=?',(wid,))
        get_db().commit()
        other=get_db().execute('SELECT id FROM branches WHERE id != 1 LIMIT 1').fetchone()[0]
    with client.session_transaction() as s:
        s['branch_id']=other; s['active_branch_id']=other
    result=client.post('/customers/new',data={'name':'Cross branch','waiting_id':str(wid),'csrf_token':csrf})
    assert result.status_code==200
    assert counts(app)==before
    with client.session_transaction() as s:
        s['user_role']='staff';s['staff_modules']=['customers']
    result=client.post('/customers/new',data={'name':'No permission','waiting_id':str(wid),'csrf_token':csrf})
    assert result.status_code==403
    assert counts(app)==before


def test_late_allocation_failure_rolls_back_claim_and_customer(app,client,monkeypatch):
    sign_in(client)
    wid=queue(app)
    csrf=token(client)
    before=counts(app)
    def fail(*args,**kwargs):
        raise ValueError('Synthetic failure after queue claim')
    monkeypatch.setattr(vehicles,'create_vehicle',fail)
    result=client.post('/customers/new',data={'name':'Rollback test','waiting_id':str(wid),'csrf_token':csrf})
    assert result.status_code==200
    assert 'Synthetic failure after queue claim' in result.text
    assert counts(app)==before


def test_unnamed_match_uses_record_reference_not_personal_name(app,client):
    sign_in(client)
    result=client.post('/customers/new',data={'blocking_panel':'1','action':'save_suspect','suspect_fraud':'1','phone':'0823140000'})
    cid=int(result.location.rsplit('/',1)[1])
    matches=client.post('/customers/suspect-check',data={'phone':'0823140000'}).json['matches']
    assert matches[0]['name']==f'Suspect/Fraud #{cid}'
    assert matches[0]['reasons']==['phone']
    assert client.post('/customers/suspect-check',data={'name':''}).json['matches']==[]

