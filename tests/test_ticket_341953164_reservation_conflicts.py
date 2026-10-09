import re
import pytest
from test_app import app, client, login, seed_customer_and_product, create_order_for_status
from app.db import get_db
from app.services.orders import get_order, reservation_notices, transition_order, ReservationConfirmationRequired


@pytest.fixture
def bookings(client, app):
    login(client)
    seed_customer_and_product(client)
    with app.app_context():
        get_db().execute('UPDATE products SET quantity = 1 WHERE id = 1')
        get_db().commit()
    first = create_order_for_status(client, quantity='1', start_date='2099-07-10', end_date='2099-07-11')
    second = create_order_for_status(client, quantity='1', start_date='2099-07-12', end_date='2099-07-13')
    assert b'Order reserved' in client.post(f'/orders/{first}/reserve', follow_redirects=True).data
    return first, second


def token(response):
    return re.search(rb'name="reservation_confirm" value="([a-f0-9]+)"', response.data).group(1).decode()


@pytest.mark.parametrize('action,status', [('reserve', 'reserved'), ('start', 'started')])
def test_warning_cancel_confirm(client, app, bookings, action, status):
    first, second = bookings
    response = client.post(f'/orders/{second}/{action}')
    assert response.status_code == 200
    assert b'Warning: ORD-TRL is Reserved by Order Customer - ORD-10145 from 2099-07-10' in response.data
    assert b'to 2099-07-11' in response.data
    assert b'Are you sure you want to book this item for another customer?' in response.data
    assert b'>Cancel</a>' in response.data
    client.get(f'/orders/{second}')
    with app.app_context():
        assert get_order(second)['status'] == 'draft'
    accepted = client.post(f'/orders/{second}/{action}', data={'reservation_confirm': token(response)}, follow_redirects=True)
    assert (b'Order reserved' if action == 'reserve' else b'Order started') in accepted.data
    with app.app_context():
        assert get_order(second)['status'] == status


@pytest.mark.parametrize('action', ['reserve', 'start'])
def test_new_overlap_blocks_confirmation(client, app, bookings, action):
    first, second = bookings
    warning = client.post(f'/orders/{second}/{action}')
    with app.app_context():
        get_db().execute('UPDATE orders SET start_at = ?, end_at = ? WHERE id = ?', ('2099-07-12T08:00:00', '2099-07-13T18:00:00', first))
        get_db().commit()
    response = client.post(f'/orders/{second}/{action}', data={'reservation_confirm': token(warning)}, follow_redirects=True)
    assert b'Error: ORD-TRL is Reserved by Order Customer - ORD-10145 from 2099-07-12 08:00:00 to 2099-07-13 18:00:00' in response.data
    with app.app_context():
        assert get_order(second)['status'] == 'draft'


def test_changed_warning_requires_fresh_confirmation(client, app, bookings):
    first, second = bookings
    warning = client.post(f'/orders/{second}/reserve')
    with app.app_context():
        get_db().execute('UPDATE orders SET end_at = ? WHERE id = ?', ('2099-07-11T18:00:00', first))
        get_db().commit()
    response = client.post(f'/orders/{second}/reserve', data={'reservation_confirm': token(warning)})
    assert token(response) != token(warning)
    with app.app_context():
        assert get_order(second)['status'] == 'draft'


@pytest.mark.parametrize('inactive', ['draft', 'returned', 'canceled', 'cancelled', 'archived'])
def test_inactive_bookings_ignored(client, app, bookings, inactive):
    first, second = bookings
    with app.app_context():
        get_db().execute('UPDATE orders SET status = ? WHERE id = ?', (inactive, first))
        get_db().commit()
        assert reservation_notices(second) == []
        assert transition_order(second, 'reserve') == 'Order reserved'


def test_boundary_and_self_exclusion(client, app, bookings):
    first, second = bookings
    with app.app_context():
        db = get_db()
        db.execute('UPDATE orders SET start_at = (SELECT end_at FROM orders WHERE id = ?) WHERE id = ?', (first, second))
        db.commit()
        with pytest.raises(ReservationConfirmationRequired) as exc:
            transition_order(second, 'reserve')
        transition_order(second, 'reserve', exc.value.token)
        assert all('ORD-10146' not in text for text in reservation_notices(second))


def test_multiple_reservations_and_pickup_details(client, app, bookings):
    first, second = bookings
    third = create_order_for_status(client, quantity='1', start_date='2099-07-15', end_date='2099-07-16')
    response = client.post(f'/orders/{third}/reserve')
    client.post(f'/orders/{third}/reserve', data={'reservation_confirm': token(response)})
    with app.app_context():
        get_db().execute("UPDATE customers SET client_verified = 0 WHERE id = 1")
        get_db().commit()
    response = client.post(f'/orders/{second}/start', follow_redirects=True)
    assert b'Critical customer details are missing for pickup' in response.data
    response = client.post(f'/orders/{second}/start', data={'pickup_critical_confirm': '1'})
    assert response.data.count(b'class="reservation-warning"') == 2
    assert b'name="pickup_critical_confirm" value="1"' in response.data
    response = client.post(f'/orders/{second}/start', data={'reservation_confirm': token(response), 'pickup_critical_confirm': '1'}, follow_redirects=True)
    assert b'Order started' in response.data


def test_pooled_stock_keeps_capacity_semantics(client, app, bookings):
    first, second = bookings
    with app.app_context():
        db = get_db()
        db.execute('UPDATE products SET quantity = 2 WHERE id = 1')
        db.execute('UPDATE orders SET start_at = (SELECT start_at FROM orders WHERE id = ?), end_at = (SELECT end_at FROM orders WHERE id = ?) WHERE id = ?', (first, first, second))
        db.commit()
        assert transition_order(second, 'reserve') == 'Order reserved'


def test_maintenance_and_overdue_remain_blockers(client, app, bookings):
    first, second = bookings
    with app.app_context():
        db = get_db()
        db.execute('UPDATE products SET under_maintenance = 1 WHERE id = 1')
        db.commit()
        with pytest.raises(ValueError, match='maintenance'):
            transition_order(second, 'reserve')
        db.execute('UPDATE products SET under_maintenance = 0 WHERE id = 1')
        db.execute("UPDATE orders SET status = 'started', end_at = '2000-01-01T00:00:00' WHERE id = ?", (first,))
        db.commit()
        with pytest.raises(ValueError, match='still out'):
            transition_order(second, 'start')
