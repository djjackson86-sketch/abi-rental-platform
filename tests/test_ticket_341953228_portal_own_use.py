"""Portal-only own-use wording has a separate required acknowledgement."""
import re

import pytest
from flask import render_template

from app import create_app
from app.db import get_db
from app.services import branches, consent, portal_intake

INDIVIDUAL = 'I confirm that I am renting the trailer for my own use.'
COMPANY = 'We confirm that the company is renting the trailer for its own business use.'


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(portal_intake, 'registration_is_open', lambda: True)
    monkeypatch.setattr(portal_intake, 'allow_lookup', lambda *_: True)
    return create_app({'TESTING': True, 'DATABASE': str(tmp_path / 'own-use.db'),
                       'TURSO_DATABASE_URL': '', 'SECRET_KEY': 'test',
                       'TELEGRAM_NOTIFICATIONS_ENABLED': ''})


def endpoint(app, branch):
    if not branch:
        return '/portal/register'
    with app.app_context():
        branch_id = branches.create_branch({'name': 'Declaration test branch', 'active': 1})
        slug = get_db().execute('SELECT public_slug FROM branches WHERE id=?', (branch_id,)).fetchone()[0]
    return f'/portal/{slug}/register'


def payload(customer_type):
    return dict(customer_type=customer_type, first_names='Test', surname='Customer',
                name='Test Company', email='own-use@example.test', phone='0825550188',
                address_line1='1 Test Road', suburb='Test', city='Johannesburg',
                province='Gauteng', postal_code='2194', alternative_contact_name='Test Contact',
                alternative_contact_number='0835550188', alternative_contact_relationship='Friend',
                popia_consent='1', own_use_declaration=customer_type)


def assert_declaration(body, customer_type):
    # Limit to markup, not the script containing the two possible replacement strings.
    privacy_label = re.search(r'<label for="popia-consent">(.*?)</label>', body, re.S).group(1)
    label = re.search(r'<label for="own-use-declaration"[^>]*>(.*?)</label>', body, re.S).group(1)
    expected, other = (COMPANY, INDIVIDUAL) if customer_type == 'company' else (INDIVIDUAL, COMPANY)
    assert label == expected
    assert other not in label
    assert INDIVIDUAL not in privacy_label and COMPANY not in privacy_label
    assert body.count('name="popia_consent"') == 1
    assert body.count('name="own_use_declaration"') == 1
    checkbox = re.search(r'<input[^>]+name="own_use_declaration"[^>]*>', body).group(0)
    assert f'value="{customer_type}"' in checkbox
    assert 'required' in checkbox
    assert 'checked' not in checkbox


@pytest.mark.parametrize('branch', [False, True])
def test_initial_portal_defaults_to_individual(app, branch):
    response = app.test_client().get(endpoint(app, branch))
    assert response.status_code == 200
    assert_declaration(response.get_data(as_text=True), 'individual')


@pytest.mark.parametrize('branch', [False, True])
@pytest.mark.parametrize('customer_type', ['individual', 'company'])
def test_validation_rerender_keeps_type_wording_and_requires_acceptance(app, branch, customer_type):
    data = payload(customer_type)
    data.pop('popia_consent')
    response = app.test_client().post(endpoint(app, branch), data=data)
    assert response.status_code == 400
    assert_declaration(response.get_data(as_text=True), customer_type)
    with app.app_context():
        assert get_db().execute('SELECT COUNT(*) FROM customers').fetchone()[0] == 0
        assert get_db().execute('SELECT COUNT(*) FROM consent_records').fetchone()[0] == 0


@pytest.mark.parametrize('branch', [False, True])
@pytest.mark.parametrize('customer_type', ['individual', 'company'])
def test_existing_acknowledgement_save_reload_unchanged(app, branch, customer_type):
    client = app.test_client()
    response = client.post(endpoint(app, branch), data=payload(customer_type))
    assert response.status_code == 200
    with app.app_context():
        row = get_db().execute('SELECT id, customer_type FROM customers').fetchone()
        assert row['customer_type'] == customer_type
        evidence = consent.consent_for(row['id'])
        assert evidence is not None
        assert evidence['accepted_at']
        assert evidence['channel'] == consent.CHANNEL_PORTAL
        assert get_db().execute('SELECT COUNT(*) FROM consent_records').fetchone()[0] == 2
        own_use = consent.consent_for(row['id'], 'trailer_own_use')
        assert own_use['notice_version'] == f'own-use-{customer_type}-v1'
        assert own_use['accepted_at']
    # Reloading the public form must never pre-acknowledge it.
    assert_declaration(client.get('/portal').get_data(as_text=True), 'individual')


def test_shared_booking_widget_is_unchanged(app):
    with app.test_request_context('/'):
        body = render_template('public/_consent_block.html', settings={})
    assert INDIVIDUAL not in body
    assert COMPANY not in body
    assert 'data-own-use-declaration' not in body
    assert 'process this booking' in body
