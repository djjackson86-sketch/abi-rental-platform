"""Unarchive an archived order, main profile only (ticket ABI-341953038, item 1).

Client ask: "allow archived orders to be reverted to unarchived orders. This must
done only by the main user".
Clarification: the order comes back to "status held before archived".

So the rules these tests pin are:

* archiving REMEMBERS the status the order held, and unarchiving restores exactly
  that (Returned for a hire order, Sales/Repairs for a sale or repair);
* a row archived before this ticket existed has no remembered status, and falls
  back to Returned rather than guessing;
* payments, quotes and invoices are untouched by the unarchive (exactly like
  Revert to Draft) and the order becomes editable again;
* posting the action is main-profile only — the dedicated endpoint AND the
  generic status catch-all a crafted URL would reach;
* the button is never rendered for a staff account, and a second unarchive is
  refused because the order is no longer archived.
"""
import os
import sqlite3
import tempfile

import pytest
from werkzeug.exceptions import Forbidden

from app import create_app
from app.db import get_db, run_migrations
from app.routes.orders import change_status
from app.services.orders import unarchive_target_status

START_DATE = '2026-07-01'
START_TIME = '09:00'
END_DATE = '2026-07-03'
END_TIME = '15:00'


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


def login(client, password='admin123'):
    with client.application.app_context():
        row = get_db().execute(
            "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
        ).fetchone()
        user_id = row['id'] if row else None
    return client.post('/login', data={'user_id': str(user_id), 'password': password},
                       follow_redirects=True)


def staff_client(app, client, name='Depot Clerk', branch_id='1'):
    """The main profile creates a staff account, then this client signs in as it."""
    client.post('/settings/users/add',
                data={'name': name, 'password': 'staff123', 'branch_id': branch_id},
                follow_redirects=True)
    with app.app_context():
        staff_id = get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()['id']
    client.post('/logout')
    client.post('/login', data={'user_id': str(staff_id), 'password': 'staff123'},
                follow_redirects=True)
    return staff_id


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


def order_row(app, order_id, *columns):
    with app.app_context():
        row = get_db().execute(
            f"SELECT {', '.join(columns)} FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        return dict(row) if row else None


def status_of(app, order_id):
    return order_row(app, order_id, 'status')['status']


def stored_prior_status(app, order_id):
    return order_row(app, order_id, 'status_before_archive')['status_before_archive']


def product_branch(app, product_id):
    with app.app_context():
        return get_db().execute("SELECT branch_id FROM products WHERE id = ?",
                                (product_id,)).fetchone()['branch_id']


def finalize_invoice(client, order_id):
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


def take_payment(client, order_id, amount='100.00'):
    client.post(f'/orders/{order_id}/payments', data={
        'amount': amount, 'method': 'cash', 'payment_date': f'{START_DATE}T10:00',
        'reference': 'unarchive test',
    }, follow_redirects=True)


def a_returned_order(client, customer_id, product_id):
    """Draft → reserved → picked up → returned, with a finalized invoice."""
    order_id = new_order(client, customer_id, product_id)
    client.post(f'/orders/{order_id}/reserve', follow_redirects=True)
    client.post(f'/orders/{order_id}/start', follow_redirects=True)
    tick_return_checklist(client, order_id)
    finalize_invoice(client, order_id)
    client.post(f'/orders/{order_id}/return', follow_redirects=True)
    return order_id


def money_rows(app, order_id):
    """(payments, documents) still attached to the order."""
    with app.app_context():
        db = get_db()
        payments = db.execute("SELECT COUNT(*) c, COALESCE(SUM(amount), 0) s FROM payments "
                              "WHERE order_id = ?", (order_id,)).fetchone()
        documents = db.execute("SELECT COUNT(*) c, GROUP_CONCAT(status) s FROM documents "
                               "WHERE order_id = ?", (order_id,)).fetchone()
        return (payments['c'], round(float(payments['s'] or 0), 2),
                documents['c'], (documents['s'] or ''))


# ------------------------------------------------------- remembering + restoring

def test_archiving_remembers_the_status_and_the_main_profile_restores_it(client, app):
    login(client)
    customer_id, product_id = seed_customer_and_product(client)
    order_id = a_returned_order(client, customer_id, product_id)
    assert status_of(app, order_id) == 'returned'

    client.post(f'/orders/{order_id}/archive', follow_redirects=True)
    assert status_of(app, order_id) == 'archived'
    assert stored_prior_status(app, order_id) == 'returned'

    res = client.post(f'/orders/{order_id}/unarchive', follow_redirects=True)
    assert b'Order unarchived' in res.data
    assert status_of(app, order_id) == 'returned'
    # The remembered status is consumed, so a second unarchive cannot repeat it.
    assert stored_prior_status(app, order_id) == ''


def test_a_sales_repairs_order_comes_back_as_sales_repairs(client, app):
    login(client)
    seed_customer_and_product(client)
    # A draft that hires nothing out: the Sales/Repairs status is only offered on
    # an order without rental items (ticket ABI-341952962).
    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO orders (order_number, customer_id, booking_type, status, payment_status, "
            "collect_branch_id, start_at, end_at, created_at) "
            "VALUES ('ORD-SALE-1', 1, 'return', 'draft', 'payment_due', 1, "
            "'2026-07-01T09:00', '2026-07-03T15:00', '2026-07-01T09:00:00')")
        db.commit()
        order_id = db.execute("SELECT id FROM orders WHERE order_number = 'ORD-SALE-1'"
                              ).fetchone()['id']

    client.post(f'/orders/{order_id}/sales_repairs', follow_redirects=True)
    assert status_of(app, order_id) == 'sales_repairs'

    client.post(f'/orders/{order_id}/archive', follow_redirects=True)
    assert status_of(app, order_id) == 'archived'
    assert stored_prior_status(app, order_id) == 'sales_repairs'

    client.post(f'/orders/{order_id}/unarchive', follow_redirects=True)
    assert status_of(app, order_id) == 'sales_repairs'


def test_a_legacy_archived_row_without_a_remembered_status_falls_back_to_returned(client, app):
    """An order archived before this ticket existed has nothing stored."""
    login(client)
    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO orders (order_number, booking_type, status, payment_status, created_at) "
            "VALUES ('LEGACY-ARCHIVED', 'return', 'archived', 'paid', '2026-01-02T09:00:00')")
        db.commit()
        order_id = db.execute("SELECT id FROM orders WHERE order_number = 'LEGACY-ARCHIVED'"
                              ).fetchone()['id']
    assert stored_prior_status(app, order_id) == ''

    res = client.post(f'/orders/{order_id}/unarchive', follow_redirects=True)
    assert b'Order unarchived' in res.data
    assert status_of(app, order_id) == 'returned'


def test_a_status_that_could_not_have_been_archived_is_refused_by_the_fallback(client, app):
    """Only Returned and Sales/Repairs are ever archivable, so nothing else is
    restored — a corrupt/foreign stored value cannot resurrect a cancelled order."""
    login(client)
    customer_id, product_id = seed_customer_and_product(client)
    order_id = a_returned_order(client, customer_id, product_id)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE orders SET status = 'archived', status_before_archive = 'canceled' "
                   "WHERE id = ?", (order_id,))
        db.commit()
        row = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        assert unarchive_target_status(row) == 'returned'

    client.post(f'/orders/{order_id}/unarchive', follow_redirects=True)
    assert status_of(app, order_id) == 'returned'


# ------------------------------------------------- ---- nothing financial moves

def test_unarchiving_keeps_payments_quotes_and_the_finalized_invoice(client, app):
    login(client)
    customer_id, product_id = seed_customer_and_product(client)
    order_id = a_returned_order(client, customer_id, product_id)
    client.post(f'/orders/{order_id}/documents', data={'document_type': 'quote'},
                follow_redirects=True)
    take_payment(client, order_id, '100.00')
    before = money_rows(app, order_id)
    assert before[0] == 1 and before[2] == 2

    client.post(f'/orders/{order_id}/archive', follow_redirects=True)
    client.post(f'/orders/{order_id}/unarchive', follow_redirects=True)

    assert status_of(app, order_id) == 'returned'
    assert money_rows(app, order_id) == before


def test_an_unarchived_order_is_editable_again_and_listed_under_its_status(client, app):
    login(client)
    customer_id, product_id = seed_customer_and_product(client)
    order_id = a_returned_order(client, customer_id, product_id)
    number = order_row(app, order_id, 'order_number')['order_number']

    client.post(f'/orders/{order_id}/archive', follow_redirects=True)
    blocked = client.post(f'/orders/{order_id}/edit', data={
        'customer_id': str(customer_id), 'start_date': START_DATE, 'start_time': START_TIME,
        'end_date': END_DATE, 'end_time': END_TIME,
    }, follow_redirects=True)
    assert b'cannot be edited' in blocked.data

    client.post(f'/orders/{order_id}/unarchive', follow_redirects=True)
    with app.app_context():
        db = get_db()
        assert db.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()['status'] == 'returned'

    listing = client.get('/orders?status=returned').get_data(as_text=True)
    assert number in listing
    detail = client.get(f'/orders/{order_id}').get_data(as_text=True)
    assert 'Edit order' in detail


def test_unarchiving_a_returned_one_way_order_leaves_the_unit_at_the_return_branch(client, app):
    """A returned one-way hire left its units at the return depot, and an
    unarchive restores Returned — so the units must NOT be moved back."""
    login(client)
    customer_id, product_id = seed_customer_and_product(client)
    order_id = new_order(client, customer_id, product_id, booking_type='oneway',
                         collect_branch_id='1', return_branch_id='2')
    client.post(f'/orders/{order_id}/reserve', follow_redirects=True)
    client.post(f'/orders/{order_id}/start', follow_redirects=True)
    tick_return_checklist(client, order_id)
    finalize_invoice(client, order_id)
    client.post(f'/orders/{order_id}/return', follow_redirects=True)
    assert product_branch(app, product_id) == 2

    client.post(f'/orders/{order_id}/archive', follow_redirects=True)
    client.post(f'/orders/{order_id}/unarchive', follow_redirects=True)
    assert status_of(app, order_id) == 'returned'
    assert product_branch(app, product_id) == 2


# -------------------------------------------------------------- main profile only

def test_staff_can_neither_see_nor_post_the_unarchive(client, app):
    login(client)
    customer_id, product_id = seed_customer_and_product(client)
    order_id = a_returned_order(client, customer_id, product_id)
    client.post(f'/orders/{order_id}/archive', follow_redirects=True)

    staff_client(app, client)

    body = client.get(f'/orders/{order_id}').get_data(as_text=True)
    assert 'Unarchive order' not in body
    # ...and the main profile's Revert to Draft stays hidden too.
    assert 'Revert to Draft' not in body
    assert status_of(app, order_id) == 'archived'

    assert client.post(f'/orders/{order_id}/unarchive').status_code == 403
    assert status_of(app, order_id) == 'archived'


def test_the_generic_status_catch_all_refuses_unarchive_for_staff(app, client):
    """A crafted URL must not be able to sidestep the main-profile rule: the
    catch-all carries the same guard the dedicated endpoint does."""
    login(client)
    customer_id, product_id = seed_customer_and_product(client)
    order_id = a_returned_order(client, customer_id, product_id)
    client.post(f'/orders/{order_id}/archive', follow_redirects=True)
    staff_id = staff_client(app, client)

    from flask import session as flask_session
    with app.test_request_context(f'/orders/{order_id}/unarchive'):
        flask_session['user_id'] = staff_id
        flask_session['user_role'] = 'staff'
        with pytest.raises(Forbidden):
            change_status(order_id, 'unarchive')
    assert status_of(app, order_id) == 'archived'


def test_the_main_profile_still_sees_the_button_on_an_archived_order_only(client, app):
    login(client)
    customer_id, product_id = seed_customer_and_product(client)
    order_id = a_returned_order(client, customer_id, product_id)

    # Returned: the archive button, no unarchive button.
    returned = client.get(f'/orders/{order_id}').get_data(as_text=True)
    assert 'Archive order' in returned
    assert 'Unarchive order' not in returned

    client.post(f'/orders/{order_id}/archive', follow_redirects=True)
    archived = client.get(f'/orders/{order_id}').get_data(as_text=True)
    assert 'Unarchive order' in archived
    assert 'Archive order' not in archived


def test_only_an_archived_order_can_be_unarchived(client, app):
    login(client)
    customer_id, product_id = seed_customer_and_product(client)
    order_id = a_returned_order(client, customer_id, product_id)

    res = client.post(f'/orders/{order_id}/unarchive', follow_redirects=True)
    assert b'Only an archived order can be unarchived' in res.data
    assert status_of(app, order_id) == 'returned'


# ------------------------------------------------------------------- the column

def test_the_migration_adds_the_column_to_an_orders_table_that_lacks_it(client, app):
    """The additive migration path, not just the fresh CREATE TABLE path."""
    if tuple(int(part) for part in sqlite3.sqlite_version.split('.')) < (3, 35):
        pytest.skip('ALTER TABLE ... DROP COLUMN needs SQLite 3.35+')
    login(client)
    customer_id, product_id = seed_customer_and_product(client)
    order_id = new_order(client, customer_id, product_id)

    with app.app_context():
        db = get_db()
        db.execute('ALTER TABLE orders DROP COLUMN status_before_archive')
        db.commit()
        columns = {row['name'] for row in db.execute('PRAGMA table_info(orders)').fetchall()}
        assert 'status_before_archive' not in columns
        run_migrations(db)
        db.commit()
        columns = {row['name'] for row in db.execute('PRAGMA table_info(orders)').fetchall()}
        assert 'status_before_archive' in columns

    row = order_row(app, order_id, 'status_before_archive')
    assert row['status_before_archive'] == ''
