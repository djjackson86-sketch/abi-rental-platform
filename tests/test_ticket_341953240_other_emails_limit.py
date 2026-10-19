"""Three other addresses, excluding main; no recipient loss on legacy records."""
import json
import pytest
from bs4 import BeautifulSoup
from test_app import app, client, login
from app.db import get_db
from app.services.customers import _clean, get_customer, raw_custom_fields_for
from app.services.customer_emails import document_copy_emails


def payload(count):
    return dict(name='Test Company', customer_type='company', email='main@example.test', phone='0821234567',
                other_email_addresses='; '.join(f'copy{i}@example.test' for i in range(count)))


@pytest.mark.parametrize('count', range(4))
def test_zero_to_three_save_reload(client, app, count):
    login(client)
    data = payload(count)
    assert client.post('/customers/new', data=data).status_code == 302
    with app.app_context():
        row = get_customer(1)
        assert row['email'] == data['email']
        assert raw_custom_fields_for(row).get('other_email_addresses', '') == data['other_email_addresses']
    page = BeautifulSoup(client.get('/customers/1/edit').data, 'html.parser')
    field = page.select_one('textarea[name=other_email_addresses]')
    assert field.text == data['other_email_addresses']
    assert field['placeholder'] == 'e.g. accounts@abc.com; cindy@abc.com'
    assert not field.parent.select('small')


@pytest.mark.parametrize('bad', [payload(4)['other_email_addresses'], 'not-an-email'])
@pytest.mark.parametrize('editing', [False, True])
def test_rejected_values_retained_without_database_changes(client, app, bad, editing):
    login(client)
    if editing:
        assert client.post('/customers/new', data=payload(2)).status_code == 302
    data = dict(payload(2), name='Typed Company', other_email_addresses=bad)
    response = client.post('/customers/1/edit' if editing else '/customers/new', data=data)
    assert response.status_code == 200
    page = BeautifulSoup(response.data, 'html.parser')
    assert page.select_one('textarea[name=other_email_addresses]').text == bad
    assert page.select_one('input[name=email]')['value'] == data['email']
    assert page.select_one('input[name=name]')['value'] == data['name']
    assert page.select_one('input[name=phone]')['value'] == data['phone']
    assert page.select_one('input[name=customer_type][checked]')['value'] == 'company'
    assert b'maximum of 3' in response.data if ';' in bad else b'Invalid other email' in response.data
    with app.app_context():
        if editing:
            assert raw_custom_fields_for(get_customer(1))['other_email_addresses'] == payload(2)['other_email_addresses']
        else:
            assert get_db().execute('SELECT count(*) FROM customers').fetchone()[0] == 0


def test_main_and_duplicates_excluded():
    data = payload(3)
    data['other_email_addresses'] += '; MAIN@example.test; COPY0@example.test; ;'
    assert json.loads(_clean(data)['custom_fields_json'])['other_email_addresses'] == payload(3)['other_email_addresses']


def test_legacy_more_than_three_preserved_until_explicitly_corrected(client, app):
    login(client)
    client.post('/customers/new', data=payload(1))
    legacy = payload(4)['other_email_addresses']
    with app.app_context():
        db = get_db()
        db.execute('UPDATE customers SET custom_fields_json=? WHERE id=1', (json.dumps({'other_email_addresses': legacy}),))
        db.commit()
    assert legacy.encode() in client.get('/customers/1/edit').data
    data = payload(0)
    del data['other_email_addresses']
    assert client.post('/customers/1/edit', data=data).status_code == 302
    with app.app_context():
        row = get_customer(1)
        assert raw_custom_fields_for(row)['other_email_addresses'] == legacy
        document = dict(customer_type='company', customer_email=row['email'], custom_fields_json=row['custom_fields_json'])
        assert document_copy_emails(document, 'office@example.test') == 'office@example.test, ' + legacy.replace('; ', ', ')
    rejected = client.post('/customers/1/edit', data=payload(4))
    assert b'maximum of 3' in rejected.data


def test_inline_api_rejects_four_and_accepts_three(client, app):
    login(client)
    headers = {'X-Requested-With': 'fetch', 'Accept': 'application/json'}
    bad = client.post('/orders/new', data=dict(payload(4), order_action='create_customer_continue'), headers=headers)
    assert bad.status_code == 400
    assert 'maximum of 3' in bad.json['message']
    good = client.post('/orders/new', data=dict(payload(3), order_action='create_customer_continue'), headers=headers)
    assert good.status_code == 200
    assert good.json['customer']['form']['other_email_addresses'] == payload(3)['other_email_addresses']


@pytest.mark.parametrize('branch', [False, True])
def test_portal_limit_and_retained_values(app, monkeypatch, branch):
    from test_ticket_341953216_portal_names import endpoint
    from app.services import portal_intake
    monkeypatch.setattr(portal_intake, 'registration_is_open', lambda: True)
    monkeypatch.setattr(portal_intake, 'allow_lookup', lambda *_: True)
    data = dict(payload(4), address_line1='1 Test Road', suburb='Test', city='Johannesburg', province='Gauteng', postal_code='2194', alternative_contact_name='Test Contact', alternative_contact_number='0835550199', alternative_contact_relationship='Friend', popia_consent='1', own_use_declaration='company')
    response = app.test_client().post(endpoint(app, branch), data=data)
    assert response.status_code == 400
    assert b'maximum of 3' in response.data
    page = BeautifulSoup(response.data, 'html.parser')
    assert page.select_one('textarea[name=other_email_addresses]').text == data['other_email_addresses']
