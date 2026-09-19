"""Revert to Draft on the main profile (ticket ABI-341952993, item 1).

Client ask: "on main profile add a button 'Revert to Draft' this should allow
already picked or reversed or sales/repair orders to go back to draft".
Clarifications: returned ones too, main profile only, and payments/invoices stay
intact while the stock is released.

So the rules these tests pin are:

* the button exists for reserved, picked up, returned and Sales/Repairs orders —
  never for a cancelled or archived one (nothing is resurrected);
* the stock the order held is released (availability only counts reserved and
  started orders), so another booking can take it straight away;
* payments and the finalized invoice are untouched by the revert;
* posting the action is main-profile only, through the dedicated endpoint AND
  through the generic status catch-all a crafted URL would reach;
* an order that is reverted and reserved again is not announced to the client
  Telegram group a second time.
"""
import os
import re
import tempfile

import pytest

from app import create_app
from app.db import get_db, run_migrations
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
    return [message for message in sent if ORDER_MESSAGE in message]


def login(client, password='admin123'):
    with client.application.app_context():
        row = get_db().execute(
            "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
        ).fetchone()
        user_id = row['id'] if row else None
    return client.post('/login', data={'user_id': str(user_id), 'password': password},
                       follow_redirects=True)


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


def new_order(client, customer_id, product_id, quantity='1', **extra):
    """Submit the admin order form; returns the new order id."""
    data = {
        'customer_id': str(customer_id),
        'product_id': str(product_id),
        'quantity': quantity,
        'collect_branch_id': '1',
        'start_date': START_DATE,
        'start_time': START_TIME,
        'end_date': END_DATE,
        'end_time': END_TIME,
    }
    data.update(extra)
    res = client.post('/orders/new', data=data, follow_redirects=False)
    assert res.status_code == 302, res.status_code
    return int(res.headers['Location'].rstrip('/').split('/')[-1])


def one_way_order(client, customer_id, product_id, collect='1', return_to='2'):
    return new_order(client, customer_id, product_id, booking_type='oneway',
                     collect_branch_id=collect, return_branch_id=return_to)


def order_row(app, order_id, *columns):
    with app.app_context():
        row = get_db().execute(
            f"SELECT {', '.join(columns)} FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        return dict(row) if row else None


def status_of(app, order_id):
    return order_row(app, order_id, 'status')['status']


def product_branch(app, product_id):
    with app.app_context():
        return get_db().execute("SELECT branch_id FROM products WHERE id = ?",
                                (product_id,)).fetchone()['branch_id']


def finalize_invoice(client, order_id):
    """Create and finalize the order's invoice; returns the document id."""
    res = client.post(f'/orders/{order_id}/documents', data={'document_type': 'invoice'},
                      follow_redirects=False)
    assert res.status_code == 302, res.status_code
    document_id = int(res.headers['Location'].rstrip('/').split('/')[-1])
    client.post(f'/documents/{document_id}/finalize', follow_redirects=True)
    return document_id


def tick_return_checklist(client, order_id):
    client.post(f'/orders/{order_id}/return-checklist',
                data={'no_damages': '1', 'no_revision_required': '1'},
                follow_redirects=True)


def staff_client(app, client, name='Depot Clerk', branch_id='1'):
    """The main profile creates a staff account, then this client signs in as it."""
    client.post('/settings/users/add',
                data={'name': name, 'password': 'staff123', 'branch_id': branch_id},
                follow_redirects=True)
    with app.app_context():
        row = get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
        staff_id = row['id']
    client.post('/logout')
    client.post('/login', data={'user_id': str(staff_id), 'password': 'staff123'},
                follow_redirects=True)
    return staff_id


# --- the statuses the button is offered on -----------------------------------

def test_a_reserved_order_reverts_to_draft_and_releases_its_stock(client, app):
    login(client)
    customer_id, product_id = seed_customer_and_product(client, quantity='1')
    order_id = new_order(client, customer_id, product_id)
    client.post(f'/orders/{order_id}/reserve', follow_redirects=True)
    assert status_of(app, order_id) == 'reserved'

    # The only unit is now held by the reserved order.
    blocked = new_order(client, customer_id, product_id)
    res = client.post(f'/orders/{blocked}/reserve', follow_redirects=True)
    assert b'Only' in res.data
    assert status_of(app, blocked) == 'draft'

    res = client.post(f'/orders/{order_id}/revert-draft', follow_redirects=True)
    assert b'Order reverted to draft' in res.data
    assert status_of(app, order_id) == 'draft'

    # Stock is released: the second booking can take the unit now.
    res = client.post(f'/orders/{blocked}/reserve', follow_redirects=True)
    assert b'Only' not in res.data
    assert status_of(app, blocked) == 'reserved'


def test_a_picked_up_order_reverts_to_draft_and_keeps_its_pickup_history(client, app):
    login(client)
    customer_id, product_id = seed_customer_and_product(client)
    order_id = new_order(client, customer_id, product_id)
    client.post(f'/orders/{order_id}/reserve', follow_redirects=True)
    client.post(f'/orders/{order_id}/start', follow_redirects=True)
    picked_up_at = order_row(app, order_id, 'picked_up_at')['picked_up_at']
    assert picked_up_at

    client.post(f'/orders/{order_id}/revert-draft', follow_redirects=True)
    row = order_row(app, order_id, 'status', 'picked_up_at')
    assert row['status'] == 'draft'
    # The real collection moment stays on the record — releasing stock does not
    # rewrite history.
    assert row['picked_up_at'] == picked_up_at


def test_a_returned_one_way_order_goes_back_to_its_collect_branch(client, app):
    """The return leg moved the units to the return branch; reverting to draft
    must put them back where the booking was collected from."""
    login(client)
    customer_id, product_id = seed_customer_and_product(client)
    order_id = one_way_order(client, customer_id, product_id, collect='1', return_to='2')
    client.post(f'/orders/{order_id}/reserve', follow_redirects=True)
    client.post(f'/orders/{order_id}/start', follow_redirects=True)
    finalize_invoice(client, order_id)
    tick_return_checklist(client, order_id)
    res = client.post(f'/orders/{order_id}/return', follow_redirects=True)
    assert b'Order returned' in res.data
    assert status_of(app, order_id) == 'returned'
    assert product_branch(app, product_id) == 2, 'the return leg moved the unit'

    res = client.post(f'/orders/{order_id}/revert-draft', follow_redirects=True)
    assert b'Order reverted to draft' in res.data
    assert status_of(app, order_id) == 'draft'
    assert product_branch(app, product_id) == 1, 'the released unit is back at the collect branch'


def test_a_sales_repairs_order_reverts_to_draft(client, app):
    login(client)
    customer_id, _ = seed_customer_and_product(client)
    res = client.post('/orders/new', data={
        'customer_id': str(customer_id),
        'custom_name': 'Brake Adjustment',
        'custom_unit_price': '195',
        'custom_billing_mode': 'fixed',
        'quantity': '1',
        'start_date': START_DATE,
        'start_time': START_TIME,
        'end_date': END_DATE,
        'end_time': END_TIME,
        'order_action': 'sales_repairs',
    }, follow_redirects=True)
    assert b'Order saved as Sales/Repairs' in res.data
    with app.app_context():
        order_id = get_db().execute(
            "SELECT id FROM orders ORDER BY id DESC LIMIT 1").fetchone()['id']
    assert status_of(app, order_id) == 'sales_repairs'

    res = client.post(f'/orders/{order_id}/revert-draft', follow_redirects=True)
    assert b'Order reverted to draft' in res.data
    assert status_of(app, order_id) == 'draft'


def test_a_cancelled_or_archived_order_is_never_resurrected(client, app):
    login(client)
    customer_id, product_id = seed_customer_and_product(client)
    order_id = new_order(client, customer_id, product_id)
    client.post(f'/orders/{order_id}/cancel', follow_redirects=True)
    assert status_of(app, order_id) == 'canceled'

    res = client.post(f'/orders/{order_id}/revert-draft', follow_redirects=True)
    assert b'Cannot revert an order with status Canceled' in res.data
    assert status_of(app, order_id) == 'canceled'

    archived = new_order(client, customer_id, product_id)
    client.post(f'/orders/{archived}/reserve', follow_redirects=True)
    client.post(f'/orders/{archived}/start', follow_redirects=True)
    finalize_invoice(client, archived)
    tick_return_checklist(client, archived)
    client.post(f'/orders/{archived}/return', follow_redirects=True)
    client.post(f'/orders/{archived}/archive', follow_redirects=True)
    assert status_of(app, archived) == 'archived'

    res = client.post(f'/orders/{archived}/revert-draft', follow_redirects=True)
    assert b'Cannot revert an order with status Archived' in res.data
    assert status_of(app, archived) == 'archived'


# --- payments and documents are left alone ----------------------------------

def test_payments_and_the_finalized_invoice_survive_the_revert(client, app):
    login(client)
    customer_id, product_id = seed_customer_and_product(client)
    order_id = new_order(client, customer_id, product_id)
    client.post(f'/orders/{order_id}/reserve', follow_redirects=True)
    client.post(f'/orders/{order_id}/start', follow_redirects=True)
    client.post(f'/orders/{order_id}/payments',
                data={'amount': '250', 'method': 'cash', 'payment_date': '2026-07-01'},
                follow_redirects=True)
    document_id = finalize_invoice(client, order_id)
    tick_return_checklist(client, order_id)
    client.post(f'/orders/{order_id}/return', follow_redirects=True)
    assert status_of(app, order_id) == 'returned'

    with app.app_context():
        db = get_db()
        before_payments = db.execute(
            "SELECT id, amount, method, status FROM payments WHERE order_id = ? ORDER BY id",
            (order_id,)).fetchall()
        before_document = dict(db.execute(
            "SELECT id, document_type, status, number FROM documents WHERE id = ?",
            (document_id,)).fetchone())
    assert before_payments and before_document['status'] == 'finalized'
    assert before_document['number']

    res = client.post(f'/orders/{order_id}/revert-draft', follow_redirects=True)
    assert b'Order reverted to draft' in res.data

    with app.app_context():
        db = get_db()
        after_payments = db.execute(
            "SELECT id, amount, method, status FROM payments WHERE order_id = ? ORDER BY id",
            (order_id,)).fetchall()
        after_document = dict(db.execute(
            "SELECT id, document_type, status, number FROM documents WHERE id = ?",
            (document_id,)).fetchone())
        paid_total = db.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM payments WHERE order_id = ?",
            (order_id,)).fetchone()['total']
    assert [dict(row) for row in after_payments] == [dict(row) for row in before_payments]
    assert after_document == before_document
    assert paid_total == 250


# --- main profile only -------------------------------------------------------

def test_the_button_is_offered_on_a_live_order_for_the_main_profile(client, app):
    login(client)
    customer_id, product_id = seed_customer_and_product(client)
    order_id = new_order(client, customer_id, product_id)
    client.post(f'/orders/{order_id}/reserve', follow_redirects=True)

    body = client.get(f'/orders/{order_id}').get_data(as_text=True)
    assert 'Revert to Draft' in body
    assert re.search(r'action="/orders/%d/revert-draft"' % order_id, body)


def test_staff_can_neither_see_nor_post_the_revert(client, app):
    login(client)
    customer_id, product_id = seed_customer_and_product(client)
    order_id = new_order(client, customer_id, product_id)
    client.post(f'/orders/{order_id}/reserve', follow_redirects=True)

    staff_client(app, client)

    body = client.get(f'/orders/{order_id}').get_data(as_text=True)
    assert 'Revert to Draft' not in body
    assert status_of(app, order_id) == 'reserved'

    # The dedicated endpoint is gated...
    assert client.post(f'/orders/{order_id}/revert-draft').status_code == 403
    # ...and so is the generic catch-all the same action would otherwise reach.
    assert client.post(f'/orders/{order_id}/revert_draft').status_code == 403
    assert status_of(app, order_id) == 'reserved'


# --- notifications stay once-per-order ---------------------------------------

def test_reverting_and_reserving_again_does_not_re_announce_the_order(client, app, sent):
    login(client)
    customer_id, product_id = seed_customer_and_product(client)
    order_id = new_order(client, customer_id, product_id)
    number = order_row(app, order_id, 'order_number')['order_number']

    client.post(f'/orders/{order_id}/reserve', follow_redirects=True)
    assert len(order_messages(sent)) == 1
    assert number in order_messages(sent)[0]
    assert order_row(app, order_id, 'new_order_notified_at')['new_order_notified_at']

    client.post(f'/orders/{order_id}/revert-draft', follow_redirects=True)
    client.post(f'/orders/{order_id}/reserve', follow_redirects=True)
    assert status_of(app, order_id) == 'reserved'
    assert len(order_messages(sent)) == 1, 'the reverted order was announced twice'


def test_an_order_that_already_left_draft_is_marked_as_announced(app):
    """The live backfill: an order that has already left draft has already been
    announced, so it can never announce again. A draft that has never moved still
    announces once when it finally does."""
    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO orders (order_number, booking_type, status, payment_status, created_at) "
            "VALUES ('LEGACY-RESERVED', 'return', 'reserved', 'paid', '2026-01-02T09:00:00')")
        db.execute(
            "INSERT INTO orders (order_number, booking_type, status, payment_status, created_at) "
            "VALUES ('LEGACY-DRAFT', 'return', 'draft', 'payment_due', '2026-01-02T09:00:00')")
        db.commit()
        run_migrations(db)
        rows = {row['order_number']: row['new_order_notified_at'] for row in db.execute(
            "SELECT order_number, new_order_notified_at FROM orders "
            "WHERE order_number IN ('LEGACY-RESERVED', 'LEGACY-DRAFT')").fetchall()}
    assert rows['LEGACY-RESERVED'] == '2026-01-02T09:00:00'
    assert rows['LEGACY-DRAFT'] is None
