"""Explicit individual names at the public boundary; no historical name migration."""
import pytest
from app import create_app
from app.db import get_db
from app.services import portal_intake, branches


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(portal_intake, 'registration_is_open', lambda: True)
    monkeypatch.setattr(portal_intake, 'allow_lookup', lambda *_: True)
    return create_app({'TESTING': True, 'DATABASE': str(tmp_path / 'names.db'),
                       'SECRET_KEY': 'test', 'TELEGRAM_NOTIFICATIONS_ENABLED': ''})


@pytest.fixture()
def payload():
    return dict(customer_type='individual', first_names='  Mary Jane  ', surname='  van der Merwe  ',
                name='Ignored forged name', email='names@example.test', phone='0825550199',
                address_line1='1 Test Road', suburb='Test', city='Johannesburg',
                province='Gauteng', postal_code='2194', alternative_contact_name='Test Contact',
                alternative_contact_number='0835550199', alternative_contact_relationship='Friend',
                popia_consent='1')


def endpoint(app, branch):
    if not branch:
        return '/portal/register'
    with app.app_context():
        branch_id = branches.create_branch({'name': 'Names test branch', 'active': 1})
        slug = get_db().execute('SELECT public_slug FROM branches WHERE id=?', (branch_id,)).fetchone()[0]
    return f'/portal/{slug}/register'


@pytest.mark.parametrize('branch', [False, True])
@pytest.mark.parametrize('field,message', [('first_names', 'Please enter your name(s).'),
                                         ('surname', 'Please enter your surname.')])
@pytest.mark.parametrize('blank', [None, '', ' \t\n '])
def test_missing_name_parts_refused_and_preserved(app, payload, branch, field, message, blank):
    if blank is None:
        payload.pop(field)
    else:
        payload[field] = blank
    response = app.test_client().post(endpoint(app, branch), data=payload)
    assert response.status_code == 400
    body = response.get_data(as_text=True)
    assert message in body
    assert f'id="portal-{field}-error"' in body
    assert 'aria-invalid="true"' in body
    assert 'value="names@example.test"' in body
    other = 'surname' if field == 'first_names' else 'first_names'
    assert f'value="{payload[other]}"' in body
    with app.app_context():
        assert get_db().execute('SELECT COUNT(*) FROM customers').fetchone()[0] == 0
        assert get_db().execute('SELECT COUNT(*) FROM portal_registrations').fetchone()[0] == 0


@pytest.mark.parametrize('branch', [False, True])
def test_complete_multipart_name_saved_and_displayed(app, payload, branch):
    client = app.test_client()
    response = client.post(endpoint(app, branch), data=payload)
    assert response.status_code == 200
    assert 'Mary' in response.get_data(as_text=True)
    with app.app_context():
        row = get_db().execute('SELECT * FROM customers WHERE email=?', (payload['email'],)).fetchone()
        assert row['name'] == 'Mary Jane van der Merwe'
        assert row['customer_type'] == 'individual'
        user_id = get_db().execute("SELECT id FROM users WHERE role='owner' LIMIT 1").fetchone()[0]
        customer_id = row['id']
    with client.session_transaction() as session:
        session['user_id'] = user_id
        session['user_role'] = 'owner'
    detail = client.get(f'/customers/{customer_id}')
    assert detail.status_code == 200
    assert 'Mary Jane van der Merwe' in detail.get_data(as_text=True)


@pytest.mark.parametrize('branch', [False, True])
def test_company_unchanged_without_individual_names(app, payload, branch):
    payload.update(customer_type='company', name='  ABC Trading (Pty) Ltd.  ')
    payload.pop('first_names')
    payload.pop('surname')
    response = app.test_client().post(endpoint(app, branch), data=payload)
    assert response.status_code == 200
    with app.app_context():
        row = get_db().execute('SELECT name,customer_type FROM customers').fetchone()
        assert tuple(row) == ('ABC Trading (Pty) Ltd.', 'company')


@pytest.mark.parametrize('customer_type', ['', 'unknown', 'individual'])
def test_combined_name_cannot_bypass_split_fields(app, payload, customer_type):
    payload.update(customer_type=customer_type, name='Onlyname')
    payload.pop('surname')
    with app.app_context(), pytest.raises(ValueError, match='surname'):
        portal_intake.submission_values(payload)


def test_server_rendered_company_error_retains_correct_fields(app, payload):
    payload.update(customer_type='company', name='My Company', email='invalid')
    body = app.test_client().post('/portal/register', data=payload).get_data(as_text=True)
    assert 'data-company-name >' in body
    assert 'name="name" required' in body
    assert 'name="first_names" disabled' in body
    assert 'value="My Company"' in body
