"""Release regressions for portal privacy and owner form security."""
import pytest
from app import create_app
from app.db import get_db
from app.services import customers, portal_intake, consent

@pytest.fixture
def app(tmp_path):
    return create_app({'TESTING': True, 'DATABASE': str(tmp_path/'security.sqlite'),
                       'TURSO_DATABASE_URL': '', 'TURSO_AUTH_TOKEN': '', 'SECRET_KEY': 'test',
                       'ADMIN_PASSWORD': 'admin123', 'TELEGRAM_NOTIFICATIONS_ENABLED': ''})

@pytest.fixture
def branch(app):
    with app.app_context():
        return dict(get_db().execute('SELECT * FROM branches ORDER BY id LIMIT 1').fetchone())

@pytest.mark.parametrize('route', ['universal','branch'])
def test_closed_gate_hides_form_and_refuses_all_writes(app,branch,route,monkeypatch):
    monkeypatch.setattr(consent,'notice_is_publishable',lambda:False)
    base='/portal' if route=='universal' else '/portal/'+branch['public_slug']
    client=app.test_client()
    payload={'name':'Synthetic Person','phone':'0825550111','popia_consent':'1'}
    for response in [client.get(base),client.get(base+'/register'),
                     client.post(base+'/register',data=payload),client.post(base+'/check',data=payload)]:
        assert response.status_code==200
        assert 'name="popia_consent"' not in response.get_data(as_text=True)
    with app.app_context():
        assert get_db().execute('SELECT COUNT(*) FROM customers').fetchone()[0]==0
        assert get_db().execute('SELECT COUNT(*) FROM consent_records').fetchone()[0]==0

@pytest.mark.parametrize('route',['universal','branch'])
def test_registration_and_lookup_share_the_rate_budget(app,branch,route,monkeypatch):
    monkeypatch.setattr(portal_intake,'registration_is_open',lambda:True)
    with app.app_context():
        customers.create_customer({'name':'Synthetic Person','phone':'0825550111'})
    base='/portal' if route=='universal' else '/portal/'+branch['public_slug']
    client=app.test_client()
    data={'name':'Synthetic Person','phone':'0825550111','popia_consent':'1'}
    for _ in range(portal_intake.LOOKUP_LIMIT):
        assert client.post(base+'/register',data=data).status_code==200
    assert client.post(base+'/register',data=data).status_code==429
    assert client.post(base+'/check',data=data).status_code==429
    with app.app_context():
        assert get_db().execute('SELECT COUNT(*) FROM customers').fetchone()[0]==1
        assert get_db().execute('SELECT COUNT(*) FROM consent_records').fetchone()[0]==0

def test_name_only_link_cannot_fill_existing_identity(app,branch,monkeypatch):
    monkeypatch.setattr(portal_intake,'registration_is_open',lambda:True)
    with app.app_context():
        victim=customers.create_customer({'name':'Synthetic Victim','phone':'0835550100'})
    response=app.test_client().post('/portal/register',data={
        'name':'Synthetic Victim','phone':'0825550999','email':'attacker@example.test',
        'suburb':'Attacker Suburb','decision':f'link:{victim}','popia_consent':'1'})
    assert response.status_code==400
    with app.app_context():
        row=customers.get_customer(victim)
        assert not row['email'] and not row['suburb']
        assert row['phone']=='0835550100'
        assert get_db().execute('SELECT COUNT(*) FROM consent_records').fetchone()[0]==0

def test_unreadable_notice_closes_instead_of_500(app,monkeypatch):
    def unreadable(): raise OSError('unreadable test notice')
    monkeypatch.setattr(consent,'notice_is_publishable',unreadable)
    with app.app_context(): assert portal_intake.registration_is_open() is False
    assert app.test_client().get('/portal').status_code==200

@pytest.mark.parametrize('path,scope', [('/settings/portal/1','portal_save'),
                                       ('/settings/popia/step/1','popia_wizard'),
                                       ('/settings/popia/publish','popia_wizard')])
def test_new_owner_posts_require_session_token(app,path,scope):
    client=app.test_client()
    with app.app_context(): owner=get_db().execute("SELECT id FROM users WHERE role='owner'").fetchone()[0]
    with client.session_transaction() as session:
        session['user_id']=owner; session['user_role']='owner'
    client.get('/settings/portal' if scope=='portal_save' else '/settings/popia')
    with client.session_transaction() as session: token=session['_csrf_tokens'][scope]
    assert token
    assert client.post(path,data={'csrf_token':'wrong'}).status_code==400
    assert client.post(path).status_code==400
    # Use the real HTML-issued token to exercise the intended write path.
    data={'csrf_token':token}
    if scope=='portal_save': data.update(public_slug='updated-test',portal_enabled='1',portal_intro='Hello')
    response=client.post(path,data=data)
    assert response.status_code==302
    if scope=='portal_save':
        with app.app_context(): assert get_db().execute('SELECT public_slug FROM branches WHERE id=1').fetchone()[0]=='updated-test'
