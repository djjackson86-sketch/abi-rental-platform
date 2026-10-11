"""Client portal changes - a PENDING self-registered client cannot take an order.

Backend guards for the pending twin of the blocked-customer rule: a client who
self-registered through the public portal is *pending* until staff verify the ID,
record the main vehicle and then approve - so they can never be attached to an
order before that, whatever the picker showed or a crafted POST submitted.

The server-side gate mirrors the blocked rule: an order that ALREADY had this
client keeps saving (the allowance is the order's OWN stored ``customer_id`` read
from the database, never a value from the submitted form), while pointing a new or
a different order at a pending client is refused.
"""
import os
import tempfile

import pytest
from werkzeug.datastructures import MultiDict

from app import create_app
from app.db import get_db
from app.services import customers, portal_registrations


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    application = create_app({
        'TESTING': True,
        'DATABASE': path,
        'SECRET_KEY': 'test',
        'ADMIN_EMAIL': 'admin@abi.local',
        'ADMIN_PASSWORD': 'admin123',
        'TELEGRAM_NOTIFICATIONS_ENABLED': '',
    })
    yield application
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client):
    with client.application.app_context():
        row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
    return client.post('/login', data={'user_id': str(row['id']), 'password': 'admin123'}, follow_redirects=True)


def create_customer(client, name, email, phone):
    response = client.post('/customers/new', data={
        'customer_type': 'individual', 'name': name, 'email': email, 'phone': phone,
    }, follow_redirects=True)
    assert b'Customer created' in response.data
    return int(response.request.path.rstrip('/').split('/')[-1])


def make_pending(app, customer_id, reference, name='', email='', phone=''):
    """Mark an existing client as a still-unapproved portal self-registration."""
    with app.app_context():
        portal_registrations.record_pending_registration(
            customer_id, 1, reference, name=name, email=email, phone=phone)
        assert portal_registrations.is_pending(customer_id) is True


def order_payload(customer_id):
    return MultiDict([
        ('customer_id', str(customer_id) if customer_id else ''),
        ('booking_type', 'return'),
        ('collect_branch_id', '1'),
        ('return_branch_id', '1'),
        ('deposit_option', 'security_deposit'),
        ('start_date', '2026-07-01'),
        ('start_time', '09:00'),
        ('end_date', '2026-07-03'),
        ('end_time', '15:00'),
        ('product_id', ''),
        ('custom_name', 'Single axle trailer hire'),
        ('custom_unit_price', '100'),
        ('custom_billing_mode', 'fixed'),
        ('quantity', '1'),
    ])


def create_order(client, customer_id):
    response = client.post('/orders/new', data=order_payload(customer_id), follow_redirects=False)
    assert response.status_code == 302, response.status_code
    return int(response.headers['Location'].rstrip('/').split('/')[-1])


def order_row(app, order_id):
    with app.app_context():
        return get_db().execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()


def order_count(app):
    with app.app_context():
        return get_db().execute('SELECT COUNT(*) AS c FROM orders').fetchone()['c']


# ------------------------------------------------------------------- the picker

def test_the_order_picker_hides_a_pending_client(client, app):
    login(client)
    pending_id = create_customer(client, 'Pending Portal Client', 'pending.portal@example.test', '0825550101')
    make_pending(app, pending_id, 'PRT-AAA111', name='Pending Portal Client')

    page = client.get('/orders/new').get_data(as_text=True)
    assert 'Pending Portal Client' not in page

    # The picker's own data sources agree: pending is hidden everywhere it lists clients.
    with app.app_context():
        assert all(c['id'] != pending_id for c in customers.list_customers())
        assert customers.search_customers('Pending') == []


# ------------------------------------------------------------- crafted create

def test_a_crafted_new_order_for_a_pending_client_is_refused(client, app):
    login(client)
    pending_id = create_customer(client, 'Pending Create Client', 'pending.create@example.test', '0825550102')
    make_pending(app, pending_id, 'PRT-BBB222', name='Pending Create Client')

    response = client.post('/orders/new', data=order_payload(pending_id), follow_redirects=True)
    assert response.status_code == 200
    assert portal_registrations.PENDING_BLOCKED_MESSAGE in response.get_data(as_text=True)
    assert order_count(app) == 0


def test_a_submitted_allowance_field_cannot_whitelist_a_pending_client(client, app):
    """The allowance is server-derived: a posted allow_* field must be ignored."""
    login(client)
    pending_id = create_customer(client, 'Forged Allow Client', 'forged.allow@example.test', '0825550106')
    make_pending(app, pending_id, 'PRT-EEE555', name='Forged Allow Client')

    payload = order_payload(pending_id)
    payload['allow_pending_customer_id'] = str(pending_id)
    payload['allow_blocked_customer_id'] = str(pending_id)

    response = client.post('/orders/new', data=payload, follow_redirects=True)
    assert portal_registrations.PENDING_BLOCKED_MESSAGE in response.get_data(as_text=True)
    assert order_count(app) == 0


# ------------------------------------------------------------- crafted update

def test_pointing_an_existing_order_at_a_pending_client_is_refused(client, app):
    login(client)
    open_id = create_customer(client, 'Open Client', 'open.pending@example.test', '0825550103')
    pending_id = create_customer(client, 'Pending Switch Client', 'pending.switch@example.test', '0825550104')
    make_pending(app, pending_id, 'PRT-CCC333', name='Pending Switch Client')
    order_id = create_order(client, open_id)

    response = client.post(f'/orders/{order_id}/edit', data=order_payload(pending_id), follow_redirects=True)
    assert portal_registrations.PENDING_BLOCKED_MESSAGE in response.get_data(as_text=True)
    assert order_row(app, order_id)['customer_id'] == open_id


def test_an_order_already_attached_to_a_pending_client_still_saves(client, app):
    """The order's OWN stored customer is the allowance - even once it turns pending."""
    login(client)
    customer_id = create_customer(client, 'Later Pending Client', 'later.pending@example.test', '0825550105')
    order_id = create_order(client, customer_id)
    # The client is marked pending AFTER the order already carries them.
    make_pending(app, customer_id, 'PRT-DDD444', name='Later Pending Client')

    response = client.post(f'/orders/{order_id}/edit', data=order_payload(customer_id), follow_redirects=True)
    assert 'Order saved' in response.get_data(as_text=True)
    assert order_row(app, order_id)['customer_id'] == customer_id


# ------------------------------------------------- atomic create (no leak)

def test_a_failed_pending_insert_leaves_no_ordinary_customer(app, monkeypatch):
    """If recording the pending registration fails, the customer row must not survive.

    portal_intake writes the customer, its portal source keys and the pending registration as
    one submission. A failure while recording the registration rolls the whole thing back, so a
    self-registered client can never leak as an ordinary (approved-looking) customer.
    """
    from app.services import portal_intake, portal_registrations

    def boom(*args, **kwargs):
        raise RuntimeError('injected pending-insert failure')

    monkeypatch.setattr(portal_registrations, 'record_pending_registration', boom)

    form = {
        'name': 'Atomic Leak Client', 'email': 'atomic.leak@example.test', 'phone': '0825550177',
        'address_line1': '1 Atomic Road', 'suburb': 'Atomicville', 'city': 'Atomicburg',
        'province': 'Gauteng', 'postal_code': '2004',
        'alternative_contact_name': 'Atomic Contact', 'alternative_contact_number': '0825550176',
        'alternative_contact_relationship': 'Friend',
    }
    with app.test_request_context('/'):
        with pytest.raises(RuntimeError):
            portal_intake.create_or_link_customer(form, 1, '', slug='universal')

    with app.app_context():
        db = get_db()
        assert db.execute('SELECT COUNT(*) AS c FROM customers').fetchone()['c'] == 0
        assert db.execute('SELECT COUNT(*) AS c FROM portal_registrations').fetchone()['c'] == 0
