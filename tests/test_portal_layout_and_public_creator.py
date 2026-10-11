import pytest
from app import create_app
from app.db import get_db
from app.services import customers, portal_intake


@pytest.fixture(autouse=True)
def open_registration(monkeypatch):
    monkeypatch.setattr(portal_intake, "registration_is_open", lambda: True)


@pytest.fixture
def app(tmp_path):
    return create_app({'TESTING': True, 'DATABASE': str(tmp_path / 'portal.db'), 'SECRET_KEY': 'test'})


def owner_session(client, app):
    with app.app_context():
        owner = get_db().execute("SELECT id FROM users WHERE role='owner'").fetchone()['id']
    with client.session_transaction() as s:
        s['user_id'] = owner
        s['user_role'] = 'owner'
    return owner


def test_lookup_is_above_customer_registration(app):
    body = app.test_client().get('/portal').get_data(as_text=True)
    assert body.index('Are you already registered with us?') < body.index('<h1>Customer registration</h1>')
    assert body.count('class="portal-lookup"') == 1
    assert body.count('name="popia_consent"') == 1
    assert 'portal-privacy-template' not in body


@pytest.mark.parametrize('logged_in', [False, True])
def test_portal_submission_is_public_in_database_and_customers_pages(app, logged_in):
    client = app.test_client()
    if logged_in:
        owner_session(client, app)
    response = client.post('/portal/register', data={
        'name': 'Portal Attribution Test', 'phone': '0825550199',
        'email': 'portal-attribution@example.test', 'popia_consent': '1',
        # Client portal changes: the SA address block and alternative-contact block are now
        # required server-side, so a complete submission must carry them.
        'address_line1': '1 Public Road', 'suburb': 'Publicville', 'city': 'Publicburg',
        'province': 'Gauteng', 'postal_code': '2001',
        'alternative_contact_name': 'Public Contact', 'alternative_contact_number': '0825550198',
        'alternative_contact_relationship': 'Friend',
        'created_by_user_id': '1', 'source_system': 'staff',
    })
    assert response.status_code == 200
    with app.app_context():
        row = get_db().execute('SELECT * FROM customers WHERE email=?', ('portal-attribution@example.test',)).fetchone()
        assert row is not None
        assert row['source_system'] == 'portal'
        assert row['created_by_user_id'] is None
        customer_id = row['id']
        assert customers.get_customer(customer_id)['created_by_name'] == 'Public'
        # Client portal changes: a self-registered client is PENDING, so it is hidden from the
        # default (approved) list until staff approve it — it is still reachable by id and shown
        # in the branch-scoped pending queue at the top of Customers.
        assert all(c['id'] != customer_id for c in customers.list_customers())
        from app.services import portal_registrations
        assert [item['customer_id'] for item in portal_registrations.list_pending_registrations()] == [customer_id]
    owner_session(client, app)
    # The pending sign-up is announced in the queue on Customers (not the approved table)...
    assert 'Portal Attribution Test' in client.get('/customers').get_data(as_text=True)
    # ...and the detail page, reached directly by staff, still credits the public creator.
    assert '<span>Created by</span><b>Public</b>' in client.get(f'/customers/{customer_id}').get_data(as_text=True)


def test_linking_existing_staff_customer_keeps_original_creator(app):
    client = app.test_client()
    owner_id = owner_session(client, app)
    with app.test_request_context('/'):
        from flask import session
        session['user_id'] = owner_id
        session['user_role'] = 'owner'
        customer_id = customers.create_customer({'name': 'Existing Staff Customer', 'phone': '0825550188'})
        original = customers.get_customer(customer_id)['created_by_name']
        # Client portal changes: a linking submission still has to satisfy the (now mandatory)
        # public form fields before the link is honoured.
        result = portal_intake.create_or_link_customer(
            {'name': 'Existing Staff Customer', 'phone': '0825550188',
             'email': 'existing.staff@example.test',
             'address_line1': '1 Link Road', 'suburb': 'Linkville', 'city': 'Linkburg',
             'province': 'Gauteng', 'postal_code': '2002',
             'alternative_contact_name': 'Link Contact', 'alternative_contact_number': '0825550187',
             'alternative_contact_relationship': 'Friend'},
            None, f'link:{customer_id}', slug='universal')
        assert result['linked']
        row = customers.get_customer(customer_id)
        assert row['created_by_user_id'] == owner_id
        assert row['created_by_name'] == original


def test_imported_or_unknown_customer_is_not_labelled_public(app):
    with app.app_context():
        customer_id = customers.create_customer({'name': 'Imported Test Customer'})
        db = get_db()
        db.execute("UPDATE customers SET source_system='booqable' WHERE id=?", (customer_id,))
        db.commit()
        assert customers.get_customer(customer_id)['created_by_name'] is None
