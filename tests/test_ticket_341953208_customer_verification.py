"""ABI-341953208: reserve pending customers, never override verification at pickup."""
import pytest
from test_app import app, client, login, seed_customer_and_product
from app.db import get_db
from app.services import portal_registrations
from app.services.orders import get_order, transition_order


def draft(client):
    response = client.post('/orders/new', data={
        'customer_id': '1', 'product_id': '1', 'quantity': '1',
        'start_date': '2026-07-01', 'start_time': '09:00',
        'end_date': '2026-07-03', 'end_time': '15:00',
        'deposit_option': 'no_deposit',
    })
    assert response.status_code == 302
    return int(response.headers['Location'].split('/')[-1])


@pytest.mark.parametrize('verification', [None, 0])
def test_pending_customer_reserves_stays_pending_and_cannot_pick_up(client, app, verification):
    login(client)
    seed_customer_and_product(client)
    with app.app_context():
        db = get_db()
        db.execute('UPDATE customers SET client_verified = ? WHERE id = 1', (verification,))
        db.commit()
        registration_id = portal_registrations.record_pending_registration(1, 1, 'ABI-341953208-TEST', name='Order Customer')
    page = client.get('/customers/1').get_data(as_text=True)
    assert 'href="/orders/new?customer_id=1">Create order for this customer</a>' in page
    assert 'CUSTOMER NOT VERIFIED' in client.get('/orders/new?customer_id=1').get_data(as_text=True)
    order_id = draft(client)
    assert b'Order reserved' in client.post(f'/orders/{order_id}/reserve', follow_redirects=True).data
    with app.app_context():
        assert portal_registrations.registration_by_id(registration_id)['status'] == 'pending'
        assert get_db().execute('SELECT client_verified FROM customers WHERE id=1').fetchone()[0] == verification
        assert any(r['customer_id'] == 1 for r in portal_registrations.list_pending_registrations())
    for _ in range(2):
        body = client.get(f'/orders/{order_id}').get_data(as_text=True)
        assert 'CUSTOMER NOT VERIFIED' in body
        assert 'disabled title="CUSTOMER NOT VERIFIED' in body
        assert 'CUSTOMER NOT VERIFIED' in client.get(f'/orders/{order_id}/edit').get_data(as_text=True)
    assert b'<small class="customer-not-verified">NOT VERIFIED</small>' in client.get('/orders').data
    for data in ({}, {'pickup_critical_confirm': '1'}, {'pickup_critical_confirm': '1', 'reservation_confirm': 'forged'}):
        response = client.post(f'/orders/{order_id}/start', data=data, follow_redirects=True)
        assert b'CUSTOMER NOT VERIFIED' in response.data
        with app.app_context():
            assert get_order(order_id)['status'] == 'reserved'
            assert not get_order(order_id)['picked_up_at']
            with pytest.raises(ValueError, match='CUSTOMER NOT VERIFIED'):
                transition_order(order_id, 'start', reservation_confirm='forged')
    # Verification is read from the current customer; no order snapshot/backfill.
    with app.app_context():
        get_db().execute('UPDATE customers SET client_verified = 1 WHERE id = 1')
        get_db().commit()
    assert b'CUSTOMER NOT VERIFIED' not in client.get(f'/orders/{order_id}').data
    assert b'customer-not-verified">NOT VERIFIED' not in client.get('/orders').data
    assert b'Order started' in client.post(f'/orders/{order_id}/start', follow_redirects=True).data
    with app.app_context():
        assert get_order(order_id)['status'] == 'started'


def test_ordinary_unverified_customer_is_also_blocked_from_direct_pickup(client, app):
    login(client)
    seed_customer_and_product(client)
    with app.app_context():
        get_db().execute('UPDATE customers SET client_verified = NULL WHERE id = 1')
        get_db().commit()
    order_id = draft(client)
    result = client.post(f'/orders/{order_id}/start', data={'pickup_critical_confirm': '1'}, follow_redirects=True)
    assert b'CUSTOMER NOT VERIFIED' in result.data
    with app.app_context():
        assert get_order(order_id)['status'] == 'draft'


def test_deleted_portal_customer_stays_unbookable(client, app):
    login(client)
    seed_customer_and_product(client)
    with app.app_context():
        rid = portal_registrations.record_pending_registration(1, 1, 'DELETED-341953208', name='Deleted ticket customer')
        get_db().execute("UPDATE portal_registrations SET status='deleted' WHERE id=?", (rid,))
        get_db().commit()
    assert b'Order Customer' not in client.get('/orders/new').data
    response = client.post('/orders/new', data={
        'customer_id': '1', 'product_id': '1', 'quantity': '1',
        'start_date': '2026-07-01', 'start_time': '09:00',
        'end_date': '2026-07-03', 'end_time': '15:00',
    })
    assert response.status_code == 200
    with app.app_context():
        assert get_db().execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 0
