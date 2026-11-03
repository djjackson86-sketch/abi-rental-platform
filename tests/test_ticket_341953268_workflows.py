import json
import pytest
from test_ticket_341953268_matching import app, client, sign_in, make_customer, post_suspect_form, created_id, customer_row, ID_NUMBER
from test_programme_20260923_portal_intake import submission
from app.db import get_db
from app.services import portal_intake, branches
from app.services.customers import suspect_matches, customer_filtered_total, list_customers


def test_ordinary_duplicate_never_warns(app):
    make_customer(app, 'Ordinary Client', email='normal@example.test', phone='0825551234', id_number=ID_NUMBER)
    with app.app_context():
        assert suspect_matches({'name':'Ordinary Client','email':'normal@example.test','phone':'0825551234','customer_id_number':ID_NUMBER}) == []


def test_staff_check_and_grouping(client, app):
    assert client.post('/customers/suspect-check', data={'name':'Suspect Client'}).status_code == 302
    sign_in(client)
    flagged=created_id(post_suspect_form(client))
    make_customer(app, 'Not Flagged')
    r=client.post('/customers/suspect-check',data={'email':'SUSPECT@example.test'})
    assert r.status_code == 200
    assert r.json['matches'][0]['id']==flagged
    # Ticket ABI-341953269: the staff check carries the number captured on the flagged
    # record so the popup can show it beside the number being entered.
    assert r.json['matches'][0]['id_number']==ID_NUMBER
    with app.app_context():
        assert customer_filtered_total(blocked='1') == 1
        assert [c['id'] for c in list_customers(blocked='1')] == [flagged]
    body=client.get('/customers?blocked=1').get_data(as_text=True)
    assert 'Suspect/Fraud' in body and 'Not Flagged' not in body


@pytest.mark.parametrize('decision', ['create', 'link'])
def test_portal_accepts_suspect_match_privately_for_staff_review(client, app, monkeypatch, decision):
    flagged=make_customer(app, 'Pieter van Wyk',phone='0835551234',email='pieter@example.co.za',id_number=ID_NUMBER,suspect=True)
    monkeypatch.setattr(portal_intake,'registration_is_open',lambda:True)
    with app.app_context():
        branch=branches.create_branch({'name':'Test Branch','active':1})
        slug=get_db().execute('SELECT public_slug FROM branches WHERE id=?',(branch,)).fetchone()['public_slug']
    response=client.post(f'/portal/{slug}/register',data=submission(own_use_declaration='individual',decision=(f'link:{flagged}' if decision=='link' else decision)))
    assert response.status_code in (200,302,303), __import__('bs4').BeautifulSoup(response.data,'html.parser').get_text(' ',strip=True)[-1800:]
    assert 'Suspect/Fraud' not in response.get_data(as_text=True)
    assert ID_NUMBER not in response.get_data(as_text=True)
    with app.app_context():
        new=get_db().execute('SELECT * FROM customers WHERE id != ?',(flagged,)).fetchone()
        assert new is not None
        assert get_db().execute('SELECT status FROM portal_registrations WHERE customer_id=?',(new['id'],)).fetchone()['status']=='pending'
        assert new['is_blocked']==0
        assert suspect_matches(new,exclude_id=new['id'])[0]['id']==flagged
    assert customer_row(app,flagged)['is_blocked']==1
    sign_in(client)
    for path in (f'/customers/{new["id"]}',f'/customers/{new["id"]}/edit'):
        page=client.get(path)
        assert page.status_code==200
        body=page.get_data(as_text=True)
        assert '"email", "phone", "name"' in body
        # Ticket ABI-341953269: the number now shows, but ONLY inside the staff
        # similarity popup's match entries - never as a plain page value.
        assert body.count(ID_NUMBER)==body.count('"id_number": "%s"'%ID_NUMBER)
        assert body.count(ID_NUMBER)>=1


@pytest.mark.parametrize('blocked', [False, True])
def test_ordinary_portal_duplicate_policy_unchanged(client, app, monkeypatch, blocked):
    existing=make_customer(app,'Pieter van Wyk',phone='0835551234',email='pieter@example.co.za')
    monkeypatch.setattr(portal_intake,'registration_is_open',lambda:True)
    with app.app_context():
        branch=branches.create_branch({'name':'Normal Branch','active':1})
        db=get_db()
        if blocked:
            db.execute('UPDATE customers SET is_blocked=1 WHERE id=?',(existing,))
            db.commit()
        slug=db.execute('SELECT public_slug FROM branches WHERE id=?',(branch,)).fetchone()['public_slug']
    response=client.post(f'/portal/{slug}/register',data=submission(own_use_declaration='individual',decision='create'))
    assert response.status_code == (400 if blocked else 200)
    with app.app_context():
        assert get_db().execute('SELECT COUNT(*) AS n FROM customers').fetchone()['n']==1
    assert 'Suspect/Fraud' not in response.get_data(as_text=True)


def test_invalid_supplementary_id_cannot_partially_save(client, app):
    sign_in(client)
    post_suspect_form(client, customer_id_number='x'*65)
    with app.app_context():
        assert get_db().execute('SELECT COUNT(*) AS n FROM customers').fetchone()['n']==0

