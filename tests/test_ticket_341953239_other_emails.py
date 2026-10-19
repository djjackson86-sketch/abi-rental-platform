"""Company Cc capture, persistence and review-only Outlook drafts."""
import json
from email import policy
from email.parser import BytesParser

import pytest
from test_app import app, client, login, seed_customer_and_product, draft_customer_payload
from app.db import get_db
from app.services.customer_emails import additional_emails, document_copy_emails
from app.services.customers import _clean, customer_form_values_for, get_customer, raw_custom_fields_for


@pytest.mark.parametrize('bad', ['broken', 'a@', '@example.com', 'a@b..com', '<a@b.com>', 'a@b.com.', 'a@b.com\r\nBcc: x@y.com', 'a@b.com\n', 'a@b.com\x00', 'a@b.com,other@b.com', 'Name <a@b.com>', 'a@b.com(comment)', 'a@b.com\u2028'])
def test_bad_addresses_are_validation_errors(bad):
    with pytest.raises(ValueError):
        additional_emails(bad)


def test_trim_empty_dedupe_primary_and_long_list():
    assert additional_emails(' ; A@EXAMPLE.COM; ; a@example.com; Main@EXAMPLE.COM ;', 'main@example.com') == ['a@example.com']
    values = [f'accounts{i}@example.test' for i in range(1000)]
    assert additional_emails(';'.join(values)) == values


def test_omitted_preserved_clear_and_individual_isolation():
    form = dict(name='Company', customer_type='company', email='main@example.test')
    existing = {'other_email_addresses': 'copy@example.test', 'audit': 'keep'}
    assert json.loads(_clean(form, existing)['custom_fields_json'])['other_email_addresses'] == 'copy@example.test'
    for blank in ['', '; ;']:
        data = json.loads(_clean(dict(form, other_email_addresses=blank), existing)['custom_fields_json'])
        assert data == {'audit': 'keep'}
    data = _clean(dict(form, customer_type='individual', other_email_addresses='invalid'), existing)
    assert json.loads(data['custom_fields_json']) == {'audit': 'keep'}


def test_standalone_create_edit_roundtrip(client, app):
    login(client)
    response = client.post('/customers/new', data=dict(name='Company', customer_type='company', email='main@example.test', other_email_addresses=' CC@example.test ; main@example.test; cc@example.test; second@example.test'))
    assert response.status_code == 302
    with app.app_context():
        customer = get_customer(1)
        assert customer_form_values_for(customer)['other_email_addresses'] == 'cc@example.test; second@example.test'
    page = client.get('/customers/1/edit')
    assert b'cc@example.test; second@example.test</textarea>' in page.data
    assert b'Enter additional email addresses separated by semicolons (;).' not in page.data
    assert b'placeholder="e.g. accounts@abc.com; cindy@abc.com"' in page.data
    client.post('/customers/1/edit', data=dict(name='Company renamed', customer_type='company', email='main@example.test'))
    with app.app_context():
        assert raw_custom_fields_for(get_customer(1))['other_email_addresses'] == 'cc@example.test; second@example.test'
    response = client.post('/customers/1/edit', data=dict(name='Company', customer_type='company', email='main@example.test', other_email_addresses=''))
    assert response.status_code == 302
    with app.app_context():
        assert 'other_email_addresses' not in raw_custom_fields_for(get_customer(1))


def test_inline_ajax_and_attached_order_card(client, app):
    login(client)
    seed_customer_and_product(client)
    result = client.post('/orders/new', data=dict(order_action='create_customer_continue', name='Inline Company', customer_type='company', email='inline@example.test', other_email_addresses='copy@example.test'), headers={'X-Requested-With': 'fetch', 'Accept': 'application/json'})
    assert result.status_code == 200
    summary = result.json['customer']
    assert summary['form']['other_email_addresses'] == 'copy@example.test'
    assert summary['custom_fields']['other_email_addresses'] == 'copy@example.test'
    customer_id = summary['id']
    page = client.get(f'/orders/new?customer_id={customer_id}')
    assert b'copy@example.test</textarea>' in page.data
    response = client.post('/orders/new', data=draft_customer_payload(dict(customer_id=str(customer_id), customer_type='company', name='Inline Company', email='inline@example.test', other_email_addresses='changed@example.test')))
    assert response.status_code == 302
    edit_page = client.get(response.headers['Location'] + '/edit')
    assert b'<span>Other email addresses</span><b>changed@example.test</b>' in edit_page.data
    with app.app_context():
        assert raw_custom_fields_for(get_customer(customer_id))['other_email_addresses'] == 'changed@example.test'
    bad = client.post('/orders/new', data=dict(order_action='create_customer_continue', name='Invalid', customer_type='company', other_email_addresses='<bad@example.test>'), headers={'X-Requested-With': 'fetch', 'Accept': 'application/json'})
    assert bad.status_code == 400
    assert 'Invalid other email address' in bad.json['message']


@pytest.mark.parametrize('kind', ['invoice', 'quote'])
def test_generated_eml_copies_company_only_and_keeps_pdf(client, app, kind):
    login(client)
    seed_customer_and_product(client)
    with app.app_context():
        get_db().execute('UPDATE customers SET customer_type=?, custom_fields_json=? WHERE id=1', ('company', json.dumps({'other_email_addresses': 'copy@example.test; info@abi-solutions.local; order@example.com; COPY@example.test'})))
        get_db().commit()
    response = client.post('/orders/new', data=draft_customer_payload(dict(customer_edit_active='', customer_type='company')))
    assert response.status_code == 302
    response = client.post('/documents/orders/1', data={'document_type': kind})
    assert response.status_code == 302
    detail = client.get('/documents/1')
    assert b'copy@example.test' in detail.data
    original_pdf = client.get('/documents/1/download.pdf').data
    draft = client.post('/documents/1/send-email', data={'to_email': 'order@example.com'})
    assert draft.status_code == 200
    parsed = BytesParser(policy=policy.default).parsebytes(draft.data)
    assert str(parsed['To']) == 'order@example.com'
    assert [a.addr_spec for a in parsed['Cc'].addresses] == ['info@abi-solutions.local', 'copy@example.test']
    assert str(parsed['X-Unsent']) == '1'
    assert parsed['From'] is None
    attachments = list(parsed.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_payload(decode=True) == original_pdf
    with app.app_context():
        get_db().execute("UPDATE customers SET customer_type='individual' WHERE id=1")
        get_db().commit()
    draft = client.post('/documents/1/send-email', data={'to_email': 'order@example.com'})
    parsed = BytesParser(policy=policy.default).parsebytes(draft.data)
    assert str(parsed['Cc']) == 'info@abi-solutions.local'


def test_legacy_thousand_recipient_eml_roundtrip():
    from app.services.email_delivery import build_outlook_draft_eml
    addresses = [f'copy{i}@example.test' for i in range(1000)]
    # Legacy data is retained and still usable, but new oversized submissions are refused.
    with pytest.raises(ValueError, match='maximum of 3'):
        _clean(dict(name='Company', customer_type='company', email='main@example.test', other_email_addresses='; '.join(addresses)))
    cleaned = {'custom_fields_json': json.dumps({'other_email_addresses': '; '.join(addresses)})}
    document = dict(customer_type='company', customer_email='main@example.test', custom_fields_json=cleaned['custom_fields_json'])
    eml = build_outlook_draft_eml('main@example.test', 'Test', 'Review only', b'%PDF-test', 'test.pdf', cc_email=document_copy_emails(document, 'office@example.test', 'main@example.test'))
    parsed = BytesParser(policy=policy.default).parsebytes(eml)
    assert [a.addr_spec for a in parsed['Cc'].addresses] == ['office@example.test'] + addresses
    assert list(parsed.iter_attachments())[0].get_payload(decode=True) == b'%PDF-test'


def test_cc_dedupe_overridden_to_and_office_fallback():
    document = dict(customer_type='company', customer_email='main@example.test', custom_fields_json=json.dumps({'other_email_addresses': 'copy@example.test; office@example.test; main@example.test'}))
    assert document_copy_emails(document, 'office@example.test', 'copy@example.test') == 'office@example.test'


@pytest.mark.parametrize('branch', [False, True])
def test_public_company_portal_persists_copies(app, monkeypatch, branch):
    from test_ticket_341953216_portal_names import endpoint
    from app.services import portal_intake
    monkeypatch.setattr(portal_intake, 'registration_is_open', lambda: True)
    monkeypatch.setattr(portal_intake, 'allow_lookup', lambda *_: True)
    payload = dict(customer_type='company', name='Portal Company', email='portal@example.test', phone='0825550199', address_line1='1 Test Road', suburb='Test', city='Johannesburg', province='Gauteng', postal_code='2194', alternative_contact_name='Test Contact', alternative_contact_number='0835550199', alternative_contact_relationship='Friend', popia_consent='1', own_use_declaration='company', other_email_addresses=' Copy@example.test ; portal@example.test')
    client = app.test_client()
    url = endpoint(app, branch)
    form = client.get(url.removesuffix('/register'))
    assert b'name="other_email_addresses"' in form.data
    response = client.post(url, data=payload)
    assert response.status_code == 200
    with app.app_context():
        row = get_db().execute('SELECT * FROM customers WHERE email=?', ('portal@example.test',)).fetchone()
        assert raw_custom_fields_for(row)['other_email_addresses'] == 'copy@example.test'
    bad = client.post(url, data=dict(payload, email='different@example.test', other_email_addresses='<bad@example.test>'))
    assert bad.status_code == 400
    assert b'Invalid other email address' in bad.data
