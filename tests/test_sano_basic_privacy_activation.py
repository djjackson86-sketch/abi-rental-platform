"""The owner-approved SIDE notice genuinely opens collection with versioned consent."""
import hashlib
import sqlite3
from pathlib import Path
import pytest
from libsql_client.result import Row
from app import create_app
from app.db import get_db
from app.services import consent,portal_intake

VERSION='sano-basic-privacy-v1'
TEXT=(Path(__file__).resolve().parents[1]/'docs/popia/SANO-BASIC-CUSTOMER-PRIVACY-NOTICE.md').read_text(encoding='utf-8')

@pytest.fixture(params=[False,True],ids=['sqlite','libsql-row'])
def app(tmp_path,monkeypatch,request):
    if request.param:
        monkeypatch.setattr(sqlite3,'Row',lambda c,v:Row({x[0]:i for i,x in enumerate(c.description)},tuple(v)))
    app=create_app({'TESTING':True,'DATABASE':str(tmp_path/'basic.sqlite'),'SECRET_KEY':'test',
                    'TURSO_DATABASE_URL':'','TURSO_AUTH_TOKEN':'','ADMIN_PASSWORD':'admin123',
                    'TELEGRAM_NOTIFICATIONS_ENABLED':''})
    with app.app_context():
        assert portal_intake.registration_is_open() is False
        get_db().execute('INSERT INTO popia_notice_versions (notice_version,notice_content_hash,notice_text,published_at,published_by_user_id) VALUES (?,?,?,?,?)',
                         (VERSION,hashlib.sha256(TEXT.encode()).hexdigest(),TEXT,'2026-10-10T20:00:00+02:00',None))
        get_db().commit()
        assert consent.notice_is_publishable() is True
        assert portal_intake.registration_is_open() is True
        assert consent.current_notice_version()==VERSION
    return app

def test_notice_and_forms_are_live_without_setup_wording(app):
    client=app.test_client()
    with app.app_context():
        branch=get_db().execute('SELECT * FROM branches ORDER BY id LIMIT 1').fetchone()
        slug=branch['public_slug'];owner=get_db().execute("SELECT id FROM users WHERE role='owner'").fetchone()[0]
    for path in ['/privacy','/portal','/portal/register','/portal/'+slug]:
        r=client.get(path);assert r.status_code==200
        body=r.get_data(as_text=True).lower()
        for removed in ['being finalised','reviewed for publication','not open yet','short notice wording for review']:
            assert removed not in body
        if path=='/privacy':
            assert VERSION in body and 'sano trailers' in body
            assert 'info@sanotrailers.co.za' in body and '229 summit road' in body
        else:
            assert 'name="popia_consent"' in body
    with client.session_transaction() as s:s.update(user_id=owner,user_role='owner',can_view_all_branches=True)
    for path in ['/settings/portal','/settings/portal/1/print']:
        r=client.get(path);assert r.status_code==200
        body=r.get_data(as_text=True).lower()
        assert 'being finalised' not in body and 'not open yet' not in body
        assert 'complete privacy notice setup' not in body

@pytest.mark.parametrize('channel',['universal','branch'])
def test_real_registration_records_basic_version_and_requires_consent(app,channel):
    client=app.test_client()
    with app.app_context():slug=get_db().execute('SELECT public_slug FROM branches ORDER BY id LIMIT 1').fetchone()[0]
    path='/portal/register' if channel=='universal' else '/portal/'+slug+'/register'
    data={'name':'Synthetic Basic Customer','phone':'0825550119','email':'basic@example.test'}
    refused=client.post(path,data=data)
    assert 'Please tick the box' in refused.get_data(as_text=True)
    with app.app_context():assert get_db().execute('SELECT COUNT(*) FROM customers').fetchone()[0]==0
    response=client.post(path,data={**data,'popia_consent':'1'})
    assert response.status_code==200
    with app.app_context():
        row=get_db().execute('SELECT * FROM customers').fetchone();assert row['source_system']=='portal'
        evidence=consent.consent_for(row['id']);assert evidence['notice_version']==VERSION
        assert evidence['consent_type']=='popia_privacy'
        assert not row['marketing_opt_in']
