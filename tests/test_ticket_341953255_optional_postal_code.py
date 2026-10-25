"""Postal code is optional on both public registration entry points."""
import re

import pytest

from app import create_app
from app.db import get_db
from app.services import branches, portal_intake


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(portal_intake, 'registration_is_open', lambda: True)
    return create_app({'TESTING': True, 'DATABASE': str(tmp_path / 'postal.db'),
                       'SECRET_KEY': 'test', 'TELEGRAM_NOTIFICATIONS_ENABLED': ''})


@pytest.fixture(params=[False, True], ids=['universal', 'branch'])
def endpoint(app, request):
    if not request.param:
        return '/portal/register'
    with app.app_context():
        branch_id = branches.create_branch({'name': 'Postal test branch', 'active': 1})
        slug = get_db().execute('SELECT public_slug FROM branches WHERE id=?', (branch_id,)).fetchone()[0]
    return f'/portal/{slug}/register'


@pytest.fixture()
def payload():
    return dict(customer_type='individual', first_names='Postal', surname='Tester',
                email='postal@example.test', phone='0825550199',
                address_line1='1 Test Road', suburb='Test', city='Johannesburg',
                province='Gauteng', alternative_contact_name='Test Contact',
                alternative_contact_number='0835550199', alternative_contact_relationship='Friend',
                popia_consent='1', own_use_declaration='individual')


def test_postal_field_visible_without_star_or_required(app, endpoint):
    body = app.test_client().get(endpoint).get_data(as_text=True)
    label = re.search(r'<label>[^<]*<span class="field-label">Postal code.*?</label>', body, re.S)
    assert label
    assert 'req-star' not in label.group()
    assert not re.search(r'\brequired\b', label.group())
    assert 'autocomplete="postal-code"' in label.group()
    assert 'inputmode="numeric"' in label.group()
    assert re.search(r'name="city"[^>]*\brequired\b', body)


@pytest.mark.parametrize('postal', [None, '', ' \t ', ' 0182 '], ids=['omitted', 'blank', 'whitespace', 'populated'])
@pytest.mark.parametrize('customer_type', ['individual', 'company'])
def test_optional_postal_submission_persists(app, endpoint, payload, postal, customer_type):
    payload.update(customer_type=customer_type, name='Postal Test Company', own_use_declaration=customer_type)
    if postal is not None:
        payload['postal_code'] = postal
    response = app.test_client().post(endpoint, data=payload)
    assert response.status_code == 200
    with app.app_context():
        db = get_db()
        customer = db.execute('SELECT id,postal_code FROM customers WHERE email=?', (payload['email'],)).fetchone()
        assert customer is not None
        assert customer['postal_code'] == (postal or '').strip()
        assert db.execute('SELECT COUNT(*) FROM portal_registrations WHERE customer_id=?', (customer['id'],)).fetchone()[0] == 1


@pytest.mark.parametrize('field', ['first_names', 'surname', 'email', 'phone', 'address_line1', 'suburb',
                                   'city', 'province', 'alternative_contact_name',
                                   'alternative_contact_number', 'alternative_contact_relationship',
                                   'popia_consent', 'own_use_declaration'])
def test_other_mandatory_fields_still_reject(app, endpoint, payload, field):
    payload.pop(field)
    response = app.test_client().post(endpoint, data=payload)
    assert response.status_code == 400
    with app.app_context():
        assert get_db().execute('SELECT COUNT(*) FROM customers').fetchone()[0] == 0


def test_populated_postal_retained_on_validation_error(app, endpoint, payload):
    payload.update(postal_code='0182', city='')
    response = app.test_client().post(endpoint, data=payload)
    assert response.status_code == 400
    assert re.search(r'name="postal_code"[^>]*value="0182"', response.get_data(as_text=True))
