"""Exercise production libSQL row objects without connecting to any remote DB."""
import sqlite3
import pytest
from libsql_client.result import Row
from app import create_app
from app.db import get_db
from app.services import customers, vehicles, portal, popia_wizard

@pytest.fixture
def app(tmp_path,monkeypatch):
    def sdk_rows(cursor,values):
        return Row({item[0]:i for i,item in enumerate(cursor.description)},tuple(values))
    monkeypatch.setattr(sqlite3,'Row',sdk_rows)
    return create_app({'TESTING':True,'DATABASE':str(tmp_path/'sdk.sqlite'),
        'TURSO_DATABASE_URL':'','TURSO_AUTH_TOKEN':'','SECRET_KEY':'test',
        'ADMIN_PASSWORD':'admin123','TELEGRAM_NOTIFICATIONS_ENABLED':''})

@pytest.fixture
def owner(app):
    client=app.test_client()
    with app.app_context(): user=get_db().execute("SELECT id FROM users WHERE role='owner'").fetchone()[0]
    with client.session_transaction() as s:
        s.update(user_id=user,user_role='owner',user_name='Synthetic Owner',can_view_all_branches=True)
    return client

def test_portal_pages_links_and_qr_with_real_sdk_rows(app,owner):
    with app.app_context():
        branch=get_db().execute('SELECT * FROM branches ORDER BY id LIMIT 1').fetchone()
        assert isinstance(branch,Row) and not hasattr(branch,'keys')
        slug=branch['public_slug']
        assert portal.universal_portal_url('https://example.test/')=='https://example.test/portal'
        assert portal.portal_url(branch,'https://example.test/')=='https://example.test/portal/'+slug
    for path in ['/portal','/portal/register','/portal/qr.png','/portal/qr.pdf',
                 '/portal/'+slug,'/portal/'+slug+'/qr.png','/settings/portal']:
        assert owner.get(path).status_code==200,path

def test_wizard_prefill_saved_state_and_page_with_sdk_rows(app,owner):
    with app.app_context():
        assert popia_wizard._sval(Row({'company_name':0},('Synthetic Company',)),'company_name')=='Synthetic Company'
        popia_wizard.save_step(1,{'business_name':'Synthetic Company','address':'1 Test Street',
            'telephone':'0105550100','contact_email':'test@example.test'})
        state=popia_wizard.state()
        assert state['business_name']=='Synthetic Company'
        assert state['published'] is False
    assert owner.get('/settings/popia').status_code==200
    assert owner.get('/settings/popia/5').status_code==200

def test_vehicle_mirror_and_json_feed_with_sdk_rows(app,owner):
    with app.app_context():
        customer=customers.create_customer({'name':'Synthetic SDK Customer','phone':'0825550100'})
    response=owner.post('/scan-vehicle/save',data={'customer_id':str(customer),'registration':'SDK123GP',
        'make':'TEST MAKE','vin':'AHTFR22G10L123456','licence_disk_expiry':'2027-01-31','make_main':'yes'},follow_redirects=True)
    assert response.status_code==200
    import re
    assert 'allocated to' in response.get_data(as_text=True), re.findall(r'<[^>]*class="[^\"]*(?:flash|alert)[^\"]*"[^>]*>(.*?)</[^>]+>', response.get_data(as_text=True), re.S)
    with app.app_context():
        fields=customers.raw_custom_fields_for(customers.get_customer(customer))
        assert fields['vehicle_reg_no']=='SDK123GP'
        assert fields['vehicle_make']=='TEST MAKE'
    response=owner.get('/customers/'+str(customer)+'/vehicles')
    assert response.status_code==200
    assert response.json['count']==1
    assert response.json['vehicles'][0]['registration']=='SDK123GP'
