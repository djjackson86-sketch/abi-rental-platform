"""Telegram "New order / booking request" timing (ticket ABI-341952988).

Client ask: "telegram notifications, new order: do not send notifications until
the order status has been updated from draft" — clarified to "suppress only
drafts created in the admin app".

So:
* an order typed in the admin app is stored as a draft and stays SILENT;
* its one announcement goes out the moment its status first leaves draft
  (Reserved, Picked up, or Save as Sales/Repairs);
* a public store booking request still announces immediately at creation, and
  is never announced a second time when staff later move it out of draft.

``app.routes.public`` is deliberately untouched; the discriminator is
``orders.created_by_user_id`` — NULL for public/background work, set for
admin-app drafts. Every send is swallowed so messaging can never block an order
write or a status change.
"""
import os
import tempfile

import pytest

from app import create_app
from app.db import get_db
from app.services import telegram

START_DATE = '2026-07-01'
START_TIME = '09:00'
END_DATE = '2026-07-03'
END_TIME = '15:00'

ORDER_MESSAGE = 'New order / booking request'


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
    })
    yield application
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def sent(monkeypatch):
    """Captured Telegram messages — the single place every send funnels through."""
    messages = []
    monkeypatch.setattr(telegram, '_send_message',
                        lambda text: messages.append(text) or {'ok': True, 'sent': True})
    return messages


def order_messages(sent):
    """Only the new-order announcements (seeding also sends customer messages)."""
    return [message for message in sent if ORDER_MESSAGE in message]


def login(client, password='admin123'):
    with client.application.app_context():
        row = get_db().execute(
            "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
        ).fetchone()
        user_id = row['id'] if row else None
    return client.post('/login', data={'user_id': str(user_id), 'password': password},
                       follow_redirects=True)


def public_client(app):
    """A fresh, signed-out visitor — separate session, so no session user id."""
    return app.test_client()


def seed_customer_and_product(client, quantity='4'):
    client.post('/customers/new', data={
        'customer_type': 'individual',
        'name': 'Order Customer',
        'email': 'order@example.com',
        'phone': '+270****0000',
    }, follow_redirects=True)
    client.post('/inventory/new', data={
        'name': 'Order Trailer',
        'sku': 'ORD-TRL',
        'quantity': quantity,
        'description': 'Order test trailer.',
        'product_type': 'rental',
        'price_amount': '200',
        'price_unit': 'day',
        'security_deposit': '750',
        'tax_profile_id': '1',
        'active': '1',
        'public_visible': '1',
    }, follow_redirects=True)
    with client.application.app_context():
        db = get_db()
        customer_id = db.execute("SELECT id FROM customers ORDER BY id LIMIT 1").fetchone()['id']
        product_id = db.execute("SELECT id FROM products WHERE sku = 'ORD-TRL'").fetchone()['id']
    return customer_id, product_id


def rental_payload(customer_id, product_id, quantity='1'):
    return {
        'customer_id': str(customer_id),
        'product_id': str(product_id),
        'quantity': quantity,
        'start_date': START_DATE,
        'start_time': START_TIME,
        'end_date': END_DATE,
        'end_time': END_TIME,
    }


def custom_line_payload(customer_id):
    """A non-rental order (sales/service line) — the only kind Sales/Repairs takes."""
    return {
        'customer_id': str(customer_id),
        'custom_name': 'Brake Adjustment',
        'custom_unit_price': '195',
        'custom_billing_mode': 'fixed',
        'quantity': '1',
        'start_date': START_DATE,
        'start_time': START_TIME,
        'end_date': END_DATE,
        'end_time': END_TIME,
    }


def admin_new_order(client, customer_id, product_id, **extra):
    """Submit the admin order form; returns the new order id."""
    data = rental_payload(customer_id, product_id)
    data.update(extra)
    res = client.post('/orders/new', data=data, follow_redirects=False)
    assert res.status_code == 302, res.status_code
    return int(res.headers['Location'].rstrip('/').split('/')[-1])


def stored_order(app, order_id):
    with app.app_context():
        row = get_db().execute(
            'SELECT order_number, status, created_by_user_id FROM orders WHERE id = ?',
            (order_id,),
        ).fetchone()
        return dict(row) if row else None


def latest_order(app):
    with app.app_context():
        row = get_db().execute(
            'SELECT id, order_number, status, created_by_user_id FROM orders ORDER BY id DESC LIMIT 1'
        ).fetchone()
        return dict(row) if row else None


# --- admin-app drafts are silent until their status leaves draft -------------

def test_an_admin_draft_is_not_announced_when_it_is_created(client, app, sent):
    login(client)
    customer_id, product_id = seed_customer_and_product(client)
    order_id = admin_new_order(client, customer_id, product_id)

    order = stored_order(app, order_id)
    assert order['status'] == 'draft'
    assert order['created_by_user_id']  # admin-app draft
    assert order_messages(sent) == []


def test_reserving_an_admin_draft_announces_it_exactly_once(client, app, sent):
    login(client)
    customer_id, product_id = seed_customer_and_product(client)
    order_id = admin_new_order(client, customer_id, product_id)
    number = stored_order(app, order_id)['order_number']
    assert order_messages(sent) == []          # silent while it is a draft

    response = client.post(f'/orders/{order_id}/reserve', follow_redirects=True)
    assert b'Order reserved' in response.data
    assert stored_order(app, order_id)['status'] == 'reserved'
    assert len(order_messages(sent)) == 1
    assert number in order_messages(sent)[0]


def test_picking_up_an_admin_draft_announces_it_exactly_once(client, app, sent):
    login(client)
    customer_id, product_id = seed_customer_and_product(client)
    order_id = admin_new_order(client, customer_id, product_id)
    number = stored_order(app, order_id)['order_number']
    assert order_messages(sent) == []          # silent while it is a draft

    response = client.post(f'/orders/{order_id}/start', follow_redirects=True)
    assert b'Order started' in response.data
    assert stored_order(app, order_id)['status'] == 'started'
    assert len(order_messages(sent)) == 1
    assert number in order_messages(sent)[0]


def test_saving_a_new_order_as_sales_repairs_announces_it_once(client, app, sent):
    login(client)
    customer_id, _ = seed_customer_and_product(client)
    data = custom_line_payload(customer_id)
    data['order_action'] = 'sales_repairs'
    res = client.post('/orders/new', data=data, follow_redirects=True)
    assert b'Order saved as Sales/Repairs' in res.data

    order = latest_order(app)
    assert order['status'] == 'sales_repairs'
    assert len(order_messages(sent)) == 1      # exactly one, from the status change
    assert order['order_number'] in order_messages(sent)[0]


def test_cancelling_an_admin_draft_stays_silent(client, app, sent):
    login(client)
    customer_id, product_id = seed_customer_and_product(client)
    order_id = admin_new_order(client, customer_id, product_id)

    response = client.post(f'/orders/{order_id}/cancel', follow_redirects=True)
    assert b'Order canceled' in response.data
    assert stored_order(app, order_id)['status'] == 'canceled'
    assert order_messages(sent) == []


def test_marking_sales_repairs_back_to_draft_stays_silent(client, app, sent):
    login(client)
    customer_id, _ = seed_customer_and_product(client)
    data = custom_line_payload(customer_id)
    data['order_action'] = 'sales_repairs'
    client.post('/orders/new', data=data, follow_redirects=True)
    order_id = latest_order(app)['id']
    assert len(order_messages(sent)) == 1

    response = client.post(f'/orders/{order_id}/draft', follow_redirects=True)
    assert b'Order saved as draft' in response.data
    assert stored_order(app, order_id)['status'] == 'draft'
    assert len(order_messages(sent)) == 1


def test_an_admin_draft_is_not_treated_as_a_public_booking(client, app, sent):
    """The discriminator is the creator, not the status: a draft an admin typed
    and a draft a visitor requested are both stored as 'draft'."""
    login(client)
    customer_id, product_id = seed_customer_and_product(client)
    admin_order_id = admin_new_order(client, customer_id, product_id)
    assert stored_order(app, admin_order_id)['created_by_user_id'] is not None

    visitor = public_client(app)
    visitor.post('/store/products/1/book', data={
        'customer_name': 'Public Booker',
        'customer_email': 'public@example.test',
        'customer_phone': '+270****0003',
        'quantity': '1',
        'start_date': '2026-10-01',
        'start_time': START_TIME,
        'end_date': '2026-10-03',
        'end_time': END_TIME,
    }, follow_redirects=True)
    public_order = latest_order(app)
    assert public_order['status'] == 'draft'
    assert public_order['created_by_user_id'] is None


# --- public store bookings keep notifying immediately -----------------------

def test_a_public_booking_request_is_announced_immediately(client, app, sent):
    login(client)
    seed_customer_and_product(client)

    visitor = public_client(app)
    response = visitor.post('/store/products/1/book', data={
        'customer_name': 'Public Booker',
        'customer_email': 'public@example.test',
        'customer_phone': '+270****0003',
        'quantity': '1',
        'start_date': '2026-10-01',
        'start_time': START_TIME,
        'end_date': '2026-10-03',
        'end_time': END_TIME,
        'notes': 'Public booking request',
    }, follow_redirects=True)
    assert b'Booking request received' in response.data

    order = latest_order(app)
    assert order['status'] == 'draft'
    assert order['created_by_user_id'] is None
    assert len(order_messages(sent)) == 1
    assert order['order_number'] in order_messages(sent)[0]

    # Staff later reserving the request must not announce it a second time: it
    # already went out at creation, and created_by_user_id is NULL.
    login(client)
    client.post(f"/orders/{order['id']}/reserve", follow_redirects=True)
    assert len(order_messages(sent)) == 1


# --- messaging can never break an order write or a status change -------------

def test_a_telegram_failure_never_blocks_a_status_change(client, app, monkeypatch):
    login(client)
    customer_id, product_id = seed_customer_and_product(client)
    order_id = admin_new_order(client, customer_id, product_id)

    def boom(_text):
        raise RuntimeError('telegram is down')

    monkeypatch.setattr(telegram, '_send_message', boom)
    response = client.post(f'/orders/{order_id}/reserve', follow_redirects=True)
    assert b'Order reserved' in response.data
    assert stored_order(app, order_id)['status'] == 'reserved'
