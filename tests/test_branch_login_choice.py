"""Choosing the branch a multi-depot sign-in is managing (ticket ABI-341952946, item 9).

Ticket ask: "for additional users who manage multiple branches: as soon as they
login, give them an option to indicate which branch are they managing for that
login period and there after only show dashboard, products etc. for that
particular branch".

The whole point is that the choice can only ever NARROW: it is validated against
the depots the account may already reach, the main profile and single-depot
accounts are never asked, and skipping the screen leaves the account with all of
its own depots exactly as before.
"""
import os
import tempfile

import pytest
from flask import session

from app import create_app
from app.db import get_db
from app.services.access import MODULE_KEYS, session_branch_scope_ids


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


def sign_in(client, name=None, password='admin123', follow=True):
    with client.application.app_context():
        if name is None:
            row = get_db().execute(
                "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
        else:
            row = get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
        user_id = row['id'] if row else None
    return client.post('/login', data={'user_id': str(user_id), 'password': password},
                       follow_redirects=follow)


def add_staff(client, app, name, branch_ids, password='staff123'):
    sign_in(client)
    client.post('/settings/users/permissions', data={'module': list(MODULE_KEYS)},
                follow_redirects=True)
    client.post('/settings/users/add', data={
        'name': name, 'password': password,
        'branch_ids': [str(value) for value in branch_ids],
    }, follow_redirects=True)
    client.post('/logout')
    with app.app_context():
        row = get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
        return row['id'] if row else None


def make_product(client, app, sku, branch_id):
    res = client.post('/inventory/new', data={
        'name': f'Trailer {sku}', 'sku': sku, 'description': '',
        'product_type': 'rental', 'tracking_method': 'bulk',
        'price_amount': '200', 'price_unit': 'day', 'security_deposit': '0',
        'active': '1', 'public_visible': '1',
        'branch_id': str(branch_id) if branch_id else '',
    }, follow_redirects=False)
    assert res.status_code == 302, res.data[:400]
    return int(res.headers['Location'].rstrip('/').split('/')[-2])


def make_order(app, number, branch_id):
    with app.app_context():
        db = get_db()
        db.execute(
            """INSERT INTO orders (order_number, booking_type, collect_branch_id, return_branch_id,
            status, payment_status, total, due_total, notes, created_at)
            VALUES (?, 'return', ?, ?, 'draft', 'payment_due', 100, 100, '', '2026-07-01T09:00:00')""",
            (number, branch_id, branch_id))
        db.commit()


# --- who gets asked ----------------------------------------------------------

def test_a_multi_depot_account_is_asked_before_it_lands_on_a_screen(client, app):
    sign_in(client)
    add_staff(client, app, 'Two Depot', [1, 2])

    res = sign_in(client, 'Two Depot', 'staff123', follow=False)
    assert res.status_code == 302
    assert res.headers['Location'].endswith('/select-branch')

    page = sign_in(client, 'Two Depot', 'staff123')
    assert b'Which branch are you managing?' in page.data


def test_a_single_depot_account_and_the_main_profile_are_not_asked(client, app):
    sign_in(client)
    add_staff(client, app, 'One Depot', [2])

    res = sign_in(client, 'One Depot', 'staff123', follow=False)
    assert res.headers['Location'].endswith('/dashboard')

    res = sign_in(client, None, follow=False)
    assert res.headers['Location'].endswith('/dashboard')

    # Nothing to choose on the screen either: it simply redirects on.
    sign_in(client, 'One Depot', 'staff123')
    assert client.get('/select-branch').status_code == 302


def test_an_all_branch_account_is_not_asked(client, app):
    sign_in(client)
    client.post('/settings/users/permissions', data={'module': list(MODULE_KEYS)},
                follow_redirects=True)
    client.post('/settings/users/add', data={'name': 'Everywhere', 'password': 'staff123',
                                             'branch_id': ''}, follow_redirects=True)
    client.post('/logout')
    res = sign_in(client, 'Everywhere', 'staff123', follow=False)
    assert res.headers['Location'].endswith('/dashboard')


# --- the choice only ever narrows -------------------------------------------

def test_the_screen_lists_only_the_depots_the_account_manages(client, app):
    sign_in(client)
    add_staff(client, app, 'Two Depot', [1, 3])
    sign_in(client, 'Two Depot', 'staff123')
    page = client.get('/select-branch').data
    assert b'Midrand' not in page  # placeholders are Branch 1/2/3 on a fresh DB
    assert b'Branch 1' in page and b'Branch 3' in page
    assert b'Branch 2' not in page


def test_choosing_a_depot_scopes_every_screen_to_it(client, app):
    sign_in(client)
    make_product(client, app, 'DEPOT-A', 1)
    make_product(client, app, 'DEPOT-B', 2)
    make_order(app, 'ORD-10145', 1)
    make_order(app, 'ORD-10146', 2)
    add_staff(client, app, 'Two Depot', [1, 2])

    sign_in(client, 'Two Depot', 'staff123')
    # Before choosing, the account still sees both of its own depots.
    both = client.get('/orders').data
    assert b'ORD-10145' in both and b'ORD-10146' in both

    res = client.post('/select-branch', data={'branch_id': '2'}, follow_redirects=False)
    assert res.headers['Location'].endswith('/dashboard')

    orders = client.get('/orders').data
    assert b'ORD-10146' in orders
    assert b'ORD-10145' not in orders

    inventory = client.get('/inventory').data
    assert b'DEPOT-B' in inventory
    assert b'DEPOT-A' not in inventory

    dashboard = client.get('/dashboard').data
    assert b'Total no. of trailers out' in dashboard
    # The chrome names the depot and offers the way back to the chooser.
    assert b'Branch: Branch 2' in dashboard
    assert b'/select-branch' in dashboard


def test_a_crafted_post_cannot_pick_a_depot_the_account_does_not_have(client, app):
    sign_in(client)
    make_order(app, 'ORD-10145', 1)
    make_order(app, 'ORD-10147', 3)
    add_staff(client, app, 'Two Depot', [1, 2])

    sign_in(client, 'Two Depot', 'staff123')
    res = client.post('/select-branch', data={'branch_id': '3'}, follow_redirects=True)
    assert b'Choose one of the branches you manage' in res.data
    with client.session_transaction() as sess:
        assert 'active_branch_id' not in sess

    # The account still sees both of its own depots and never the third.
    orders = client.get('/orders').data
    assert b'ORD-10145' in orders
    assert b'ORD-10147' not in orders
    assert client.get('/inventory').status_code == 200


def test_a_tampered_session_branch_outside_the_grant_is_ignored(app):
    with app.test_request_context():
        session.clear()
        session.update({'user_id': 2, 'user_role': 'staff', 'can_view_all_branches': False,
                        'branch_id': 1, 'branch_ids': [1, 2], 'active_branch_id': 3})
        assert session_branch_scope_ids() == [1, 2]
        session['active_branch_id'] = 2
        assert session_branch_scope_ids() == [2]
        # The main profile stays unrestricted whatever the session says.
        session.update({'user_role': 'owner', 'can_view_all_branches': True})
        assert session_branch_scope_ids() is None


def test_new_work_defaults_to_the_depot_the_session_is_managing(client, app):
    sign_in(client)
    # Unassigned stock is bookable from every depot, so the assertion below is
    # about the branch the session is managing rather than about availability.
    product_id = make_product(client, app, 'ANYWHERE', '')
    add_staff(client, app, 'Two Depot', [1, 2])

    sign_in(client, 'Two Depot', 'staff123')
    client.post('/select-branch', data={'branch_id': '2'}, follow_redirects=True)

    # A crafted collection branch from the other depot is refused server-side and
    # the order lands in the depot this sign-in is managing, so the account can
    # still see the order it just created.
    res = client.post('/orders/new', data={
        'customer_type': 'individual',
        'name': 'Active Branch Client',
        'email': 'active@example.test',
        'product_id': str(product_id),
        'quantity': '1',
        'start_date': '2026-07-01',
        'start_time': '09:00',
        'end_date': '2026-07-02',
        'end_time': '09:00',
        'collect_branch_id': '1',
        'return_branch_id': '1',
    }, follow_redirects=False)
    assert res.status_code == 302, res.data[:400]
    order_id = int(res.headers['Location'].rstrip('/').split('/')[-1])
    with app.app_context():
        row = get_db().execute(
            "SELECT collect_branch_id, return_branch_id FROM orders WHERE id = ?",
            (order_id,)).fetchone()
        assert (row['collect_branch_id'], row['return_branch_id']) == (2, 2)


def test_changing_the_depot_again_is_always_reachable(client, app):
    sign_in(client)
    add_staff(client, app, 'Two Depot', [1, 2])
    sign_in(client, 'Two Depot', 'staff123')
    client.post('/select-branch', data={'branch_id': '2'}, follow_redirects=True)

    page = client.get('/select-branch')
    assert page.status_code == 200
    # The depot already chosen is preselected, and the other one is offered.
    assert b'<option value="2" selected>' in page.data
    client.post('/select-branch', data={'branch_id': '1'}, follow_redirects=True)
    assert b'Branch: Branch 1' in client.get('/dashboard').data


def test_the_owner_never_sees_the_branch_control(client, app):
    sign_in(client)
    page = client.get('/dashboard').data
    assert b'Branch:' not in page
    assert b'/select-branch' not in page
    # A branch-limited session also keeps the chooser out of its chrome until a
    # choice is actually made.
    add_staff(client, app, 'One Depot', [2])
    sign_in(client, 'One Depot', 'staff123')
    assert b'Branch: Branch 2' not in client.get('/dashboard').data
