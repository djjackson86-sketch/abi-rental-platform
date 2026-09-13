"""Per-account multi-branch access (ticket ABI-341952942, edit 2).

Ticket ask: "allow an option for additional user to manage more than one branch
but not all branches".

Model: ``users.branch_id`` + ``users.can_view_all_branches`` still describe the
primary branch and the "every branch" flag; ``user_branch_access`` holds the extra
depots. No rows + a primary branch + can_view_all=0 is exactly the old
single-branch behaviour, so nothing changes for an existing account.

These tests pin the two things that must not break:
- the single-branch path stays byte-identical (same SQL, same parameters);
- an account limited to several depots sees exactly those depots and a crafted
  POST or ``?branch=`` can never widen it or book against another depot.
"""
import os
import tempfile

import pytest
from flask import session

import app.services.access as access_module
from app import create_app
from app.db import get_db
from app.services.access import (
    MODULE_KEYS,
    order_branch_clause,
    product_branch_clause,
    resolve_branch_filter,
    session_branch_scope,
    session_branch_scope_ids,
    update_user_branch,
    update_user_branches,
    user_branch_ids,
    user_branch_map,
    user_can_access_order,
)


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


def login(client, name=None, password='admin123'):
    with client.application.app_context():
        if name is None:
            row = get_db().execute(
                "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
            ).fetchone()
        else:
            row = get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
        user_id = row['id'] if row else None
    return client.post('/login', data={'user_id': str(user_id), 'password': password},
                       follow_redirects=True)


def add_staff(client, app, name, branch_ids=None, branch_id='', password='staff123'):
    """Create an additional account that may reach every staff module.

    The owner signs in first; the shared default is widened to all staff modules
    so the branch-scoping assertions below exercise the access rules rather than
    the module gate.
    """
    login(client)
    client.post('/settings/users/permissions', data={'module': list(MODULE_KEYS)},
                follow_redirects=True)
    data = {'name': name, 'password': password}
    if branch_ids:
        data['branch_ids'] = [str(value) for value in branch_ids]
    else:
        data['branch_id'] = branch_id
    client.post('/settings/users/add', data=data, follow_redirects=True)
    with app.app_context():
        row = get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
        return row['id'] if row else None


def user_row(app, user_id):
    with app.app_context():
        return get_db().execute(
            "SELECT branch_id, can_view_all_branches FROM users WHERE id = ?", (user_id,)
        ).fetchone()


def scope_ids(app, **session_values):
    """Run session_branch_scope_ids() against a synthetic session."""
    with app.test_request_context():
        session.clear()
        session.update(session_values)
        return session_branch_scope_ids()


def single_scope(app, **session_values):
    with app.test_request_context():
        session.clear()
        session.update(session_values)
        return session_branch_scope()


def clause(app, fn, **kwargs):
    with app.test_request_context():
        session.clear()
        session.update(kwargs.pop('session_values'))
        return fn(**kwargs)


STAFF = {'user_id': 2, 'user_role': 'staff'}
BRANCH_MAP = {}


def make_orders(app, branch_ids):
    """One minimal order per branch id; returns {branch_id: order_number}."""
    numbers = {}
    with app.app_context():
        db = get_db()
        for index, branch_id in enumerate(branch_ids):
            number = f'ORD-{10145 + index}'
            db.execute(
                """INSERT INTO orders (order_number, booking_type, collect_branch_id, return_branch_id,
                status, payment_status, total, due_total, notes, created_at)
                VALUES (?, 'return', ?, ?, 'draft', 'payment_due', 100, 100, '', '2026-07-01T09:00:00')""",
                (number, branch_id, branch_id),
            )
            numbers[branch_id] = number
        db.commit()
    return numbers


def make_product(client, app, sku, branch_id='', name=None):
    res = client.post('/inventory/new', data={
        'name': name or f'Trailer {sku}',
        'sku': sku,
        'description': 'Multi-branch test.',
        'product_type': 'rental',
        'tracking_method': 'bulk',
        'price_amount': '200',
        'price_unit': 'day',
        'security_deposit': '0',
        'active': '1',
        'public_visible': '1',
        'branch_id': str(branch_id) if branch_id else '',
    }, follow_redirects=False)
    assert res.status_code == 302, res.data[:400]
    return int(res.headers['Location'].rstrip('/').split('/')[-2])


# --- the single-branch path must stay byte-identical -------------------------

def test_a_single_branch_account_keeps_the_original_clauses(app):
    scope_session = {**STAFF, 'can_view_all_branches': False, 'branch_id': 2, 'branch_ids': []}
    assert scope_ids(app, **scope_session) == [2]
    assert single_scope(app, **scope_session) == 2

    order_sql, order_params = clause(app, order_branch_clause, session_values=scope_session)
    assert order_sql == " AND (o.collect_branch_id = ? OR o.return_branch_id = ?)"
    assert order_params == [2, 2]

    product_sql, product_params = clause(
        app, product_branch_clause, include_unassigned=True, session_values=scope_session
    )
    assert product_sql == " AND (p.branch_id = ? OR p.branch_id IS NULL)"
    assert product_params == [2]

    picked_sql, picked_params = clause(
        app, product_branch_clause, include_unassigned=True, branch_id=1,
        session_values={**scope_session, 'can_view_all_branches': True},
    )
    assert picked_sql == " AND p.branch_id = ?"
    assert picked_params == [1]


def test_the_unrestricted_paths_produce_no_clause(app):
    owner_session = {'user_id': 1, 'user_role': 'owner', 'branch_id': 1, 'branch_ids': []}
    assert scope_ids(app, **owner_session) is None
    assert clause(app, order_branch_clause, session_values=owner_session) == ("", [])

    all_branches = {**STAFF, 'can_view_all_branches': True, 'branch_id': None, 'branch_ids': []}
    assert scope_ids(app, **all_branches) is None
    assert clause(app, order_branch_clause, session_values=all_branches) == ("", [])

    # Historic rule: no rows, no primary branch, not "all" -> unrestricted.
    blank = {**STAFF, 'can_view_all_branches': False, 'branch_id': None, 'branch_ids': []}
    assert scope_ids(app, **blank) is None
    assert clause(app, order_branch_clause, session_values=blank) == ("", [])


def test_two_depots_match_either_depot(app):
    scope_session = {**STAFF, 'can_view_all_branches': False, 'branch_id': 1, 'branch_ids': [1, 2]}
    assert scope_ids(app, **scope_session) == [1, 2]
    assert single_scope(app, **scope_session) == 1

    order_sql, order_params = clause(app, order_branch_clause, session_values=scope_session)
    assert order_sql == (
        " AND (o.collect_branch_id IN (?,?) OR o.return_branch_id IN (?,?))"
    )
    assert order_params == [1, 2, 1, 2]

    product_sql, product_params = clause(
        app, product_branch_clause, include_unassigned=True, session_values=scope_session
    )
    assert product_sql == " AND (p.branch_id IN (?,?) OR p.branch_id IS NULL)"
    assert product_params == [1, 2]

    # A UI filter can narrow within their scope (and lands on the byte-identical
    # single-branch clause), but a filter outside the scope is ignored.
    narrowed_sql, narrowed_params = clause(
        app, order_branch_clause, branch_id=2, session_values=scope_session
    )
    assert narrowed_sql == " AND (o.collect_branch_id = ? OR o.return_branch_id = ?)"
    assert narrowed_params == [2, 2]

    widened_sql, widened_params = clause(
        app, order_branch_clause, branch_id=3, session_values=scope_session
    )
    assert widened_params == [1, 2, 1, 2]
    assert ' IN (?,?)' in widened_sql


def test_a_three_depot_scope_matches_all_three(app):
    scope_session = {**STAFF, 'can_view_all_branches': False, 'branch_id': 3, 'branch_ids': [1, 2, 3]}
    assert scope_ids(app, **scope_session) == [1, 2, 3]
    assert single_scope(app, **scope_session) == 3
    sql, params = clause(app, order_branch_clause, session_values=scope_session)
    assert sql == " AND (o.collect_branch_id IN (?,?,?) OR o.return_branch_id IN (?,?,?))"
    assert params == [1, 2, 3, 1, 2, 3]


def test_a_scope_of_nothing_matches_no_rows(app, monkeypatch):
    """Defensive: an empty scope is a restriction to nothing, never "everything"."""
    monkeypatch.setattr(access_module, 'session_branch_scope_ids', lambda: [])
    with app.test_request_context():
        assert access_module.order_branch_clause('o') == (" AND 0=1", [])
        assert access_module.product_branch_clause('p') == (" AND 0=1", [])
        assert access_module.user_can_access_order(
            {'collect_branch_id': 1, 'return_branch_id': 1}
        ) is False


def test_scope_ids_come_from_the_session_not_the_filter(app):
    scope_session = {**STAFF, 'can_view_all_branches': False, 'branch_id': 1, 'branch_ids': [1, 2]}
    with app.test_request_context():
        session.clear()
        session.update(scope_session)
        # A crafted ?branch= outside the scope is ignored, and the chooser only
        # ever lists the account's own depots.
        selected, branch_id, label, branches, scope = resolve_branch_filter('3')
        assert (selected, branch_id, label, scope) == ('', None, '', None)
        assert [branch['id'] for branch in branches] == [1, 2]

        selected, branch_id, label, branches, scope = resolve_branch_filter('2')
        assert (selected, branch_id) == ('2', 2)
        assert label == branches[-1]['name']


# --- the whole flow -----------------------------------------------------------

def test_two_depot_staff_sees_only_their_own_orders(client, app):
    numbers = make_orders(app, [1, 2, 3])
    add_staff(client, app, 'Two Depot Staff', branch_ids=[1, 2])
    client.post('/logout')
    login(client, 'Two Depot Staff', 'staff123')

    body = client.get('/orders').data
    assert numbers[1].encode() in body and numbers[2].encode() in body
    assert numbers[3].encode() not in body
    # A chooser narrowed to their depots, never the third branch.
    assert b'name="branch"' in body
    assert b'<option value="3"' not in body

    # Neither "all branches" nor the third depot can widen the view.
    for attempt in ['', '3', '999', 'abc']:
        page = client.get('/orders?branch=' + attempt).data
        assert numbers[1].encode() in page, attempt
        assert numbers[2].encode() in page, attempt
        assert numbers[3].encode() not in page, attempt

    # Filtering to one of their own depots narrows rather than leaks.
    narrowed = client.get('/orders?branch=2').data
    assert numbers[2].encode() in narrowed
    assert numbers[1].encode() not in narrowed


def test_a_crafted_order_post_cannot_book_another_depots_branch(client, app):
    login(client)
    first_depot = make_product(client, app, 'MULTI-A', branch_id=1)
    second_depot = make_product(client, app, 'MULTI-B', branch_id=2)
    add_staff(client, app, 'Order Craft Staff', branch_ids=[1, 2])
    client.post('/logout')
    login(client, 'Order Craft Staff', 'staff123')

    def post(branch_id, product_id):
        res = client.post('/orders/new', data={
            'customer_type': 'individual',
            'name': f'Client {branch_id}',
            'email': f'client{branch_id}@example.test',
            'product_id': str(product_id),
            'quantity': '1',
            'start_date': '2026-07-01',
            'start_time': '09:00',
            'end_date': '2026-07-02',
            'end_time': '09:00',
            'collect_branch_id': str(branch_id),
            'return_branch_id': str(branch_id),
        }, follow_redirects=False)
        assert res.status_code == 302, res.data[:400]
        return int(res.headers['Location'].rstrip('/').split('/')[-1])

    # Depot 3 is not theirs: the order is kept inside their own depots.
    crafted = post(3, first_depot)
    # A depot they do have is honoured as posted.
    allowed = post(2, second_depot)
    with app.app_context():
        db = get_db()
        row = db.execute("SELECT collect_branch_id, return_branch_id FROM orders WHERE id = ?",
                         (crafted,)).fetchone()
        assert (row['collect_branch_id'], row['return_branch_id']) == (1, 1)
        row = db.execute("SELECT collect_branch_id, return_branch_id FROM orders WHERE id = ?",
                         (allowed,)).fetchone()
        assert (row['collect_branch_id'], row['return_branch_id']) == (2, 2)


def test_a_crafted_product_post_cannot_move_stock_to_another_depot(client, app):
    login(client)
    owner_product = make_product(client, app, 'MULTI-OWNER', branch_id=1)
    # A product in depot 3, created by the owner before the staff account exists.
    other = make_product(client, app, 'MULTI-THREE', branch_id=3)
    add_staff(client, app, 'Product Craft Staff', branch_ids=[1, 2])
    client.post('/logout')
    login(client, 'Product Craft Staff', 'staff123')

    # A product in depot 3 is invisible (404), even by direct URL.
    assert client.get(f'/inventory/{other}/edit').status_code == 404
    assert client.post(f'/inventory/{other}/archive', follow_redirects=True).status_code == 404

    # Saving their own product with a crafted depot keeps it inside their scope.
    res = client.post(f'/inventory/{owner_product}/edit', data={
        'name': 'MULTI-OWNER', 'sku': 'MULTI-OWNER', 'description': '',
        'product_type': 'rental', 'tracking_method': 'bulk',
        'price_amount': '200', 'price_unit': 'day', 'security_deposit': '0',
        'active': '1', 'public_visible': '1', 'branch_id': '3',
    }, follow_redirects=True)
    assert res.status_code == 200
    with app.app_context():
        assert get_db().execute("SELECT branch_id FROM products WHERE id = ?",
                                (owner_product,)).fetchone()['branch_id'] == 1


def test_the_primary_branch_is_kept_while_it_is_still_selected(client, app):
    login(client)
    staff = add_staff(client, app, 'Primary Keeper', branch_id='2')
    with app.app_context():
        # 2 is the primary and stays the primary when the set grows.
        assert update_user_branches(staff, [1, 2, 3]) is True
        assert user_branch_ids(staff) == [1, 2, 3]
        assert user_row(app, staff)['branch_id'] == 2
        assert user_row(app, staff)['can_view_all_branches'] == 0

        # Once 2 is dropped, the FIRST depot in the submitted list takes over as
        # the default (the page submits them in the order it lists them).
        assert update_user_branches(staff, [3, 1]) is True
        assert sorted(user_branch_ids(staff)) == [1, 3]
        assert user_row(app, staff)['branch_id'] == 3

        # Blank selection restores all-branch access and clears the rows.
        assert update_user_branches(staff, []) is True
        assert user_branch_ids(staff) == []
        assert user_row(app, staff)['can_view_all_branches'] == 1
        assert user_row(app, staff)['branch_id'] is None

        # The historic single-branch helper writes the same rows.
        assert update_user_branch(staff, '3') is True
        assert user_branch_ids(staff) == [3]
        assert user_row(app, staff)['branch_id'] == 3

        # Unknown branch ids are ignored rather than trusted.
        assert update_user_branches(staff, ['9999', 'abc', 2]) is True
        assert user_branch_ids(staff) == [2]

        owner = get_db().execute("SELECT id FROM users WHERE role = 'owner'").fetchone()['id']
        assert update_user_branches(owner, [1]) is False
        assert user_row(app, owner)['can_view_all_branches'] == 1


def test_stock_rows_are_written_for_every_depot_in_scope(client, app):
    login(client)
    product_id = make_product(client, app, 'MULTI-STOCK', branch_id=1)
    # Pre-existing row for depot 3, written by the owner.
    client.post(f'/inventory/{product_id}/edit', data={
        'name': 'MULTI-STOCK', 'sku': 'MULTI-STOCK', 'description': '',
        'product_type': 'rental', 'tracking_method': 'bulk',
        'price_amount': '200', 'price_unit': 'day', 'security_deposit': '0',
        'active': '1', 'public_visible': '1', 'branch_id': '1',
        'qty_branch_3': '4',
    }, follow_redirects=True)
    add_staff(client, app, 'Stock Staff', branch_ids=[1, 2])

    login(client, 'Stock Staff', 'staff123')
    client.post(f'/inventory/{product_id}/edit', data={
        'name': 'MULTI-STOCK', 'sku': 'MULTI-STOCK', 'description': '',
        'product_type': 'rental', 'tracking_method': 'bulk',
        'price_amount': '200', 'price_unit': 'day', 'security_deposit': '0',
        'active': '1', 'public_visible': '1', 'branch_id': '2',
        'qty_branch_1': '5', 'qty_branch_2': '6', 'qty_branch_3': '9',
    }, follow_redirects=True)

    with app.app_context():
        rows = {int(row['branch_id']): int(row['quantity']) for row in get_db().execute(
            "SELECT branch_id, quantity FROM product_branch_stock WHERE product_id = ?", (product_id,)
        ).fetchall()}
        # Both of their depots were written; depot 3 was neither read nor changed.
        assert rows == {1: 5, 2: 6, 3: 4}


def test_deleting_a_branch_removes_its_rows_without_widening_access(client, app):
    login(client)
    kept = add_staff(client, app, 'Branch Keeper', branch_ids=[2, 3])
    stranded = add_staff(client, app, 'Branch Stranded', branch_ids=[3])

    client.post('/branches/3/delete', follow_redirects=True)
    with app.app_context():
        # The account that still has a depot keeps it and stays restricted.
        assert user_branch_ids(kept) == [2]
        assert user_row(app, kept)['can_view_all_branches'] == 0
        # The account that only had the deleted depot is explicitly all-branches
        # rather than silently falling back to the historic blank-branch rule.
        assert user_branch_ids(stranded) == []
        assert user_row(app, stranded)['can_view_all_branches'] == 1

    login(client, 'Branch Keeper', 'staff123')
    body = client.get('/orders').data
    assert b'Branch 1' not in body


def test_users_page_edits_branches_per_account(client, app):
    login(client)
    staff = add_staff(client, app, 'Page Branch Staff', branch_id='1')

    page = client.get('/settings/users')
    assert page.status_code == 200
    assert f'/settings/users/{staff}/branch'.encode() in page.data

    multi = client.post(f'/settings/users/{staff}/branch', data={
        'branches_edited': '1', 'branch_ids': ['1', '3'],
    }, follow_redirects=True)
    assert b'Account branch access updated' in multi.data

    with app.app_context():
        assert user_branch_ids(staff) == [1, 3]
        assert user_row(app, staff)['branch_id'] == 1

    # Ticking nothing means all branches.
    client.post(f'/settings/users/{staff}/branch', data={
        'branches_edited': '1',
    }, follow_redirects=True)
    with app.app_context():
        assert user_branch_ids(staff) == []
        assert user_row(app, staff)['can_view_all_branches'] == 1

    # The legacy single-select post still works (no branches_edited marker).
    client.post(f'/settings/users/{staff}/branch', data={'branch_id': '2'}, follow_redirects=True)
    with app.app_context():
        assert user_branch_ids(staff) == [2]
        assert user_row(app, staff)['branch_id'] == 2

    page = client.get('/settings/users').data
    assert b'One depot' in page


def test_user_branch_map_reports_the_tick_state(client, app):
    login(client)
    multi = add_staff(client, app, 'Map Multi', branch_ids=[1, 3])
    single = add_staff(client, app, 'Map Single', branch_id='2')
    everyone = add_staff(client, app, 'Map Everyone', branch_id='')

    with app.app_context():
        from app.services.access import list_users
        mapping = user_branch_map(list_users())
        assert sorted(mapping[multi]['ids']) == [1, 3]
        assert mapping[multi]['all'] is False
        assert mapping[multi]['primary'] == 1
        assert mapping[single]['ids'] == [2]
        assert mapping[single]['all'] is False
        assert mapping[everyone]['ids'] == []
        assert mapping[everyone]['all'] is True


def test_user_can_access_order_respects_the_depot_list(app):
    scope_session = {**STAFF, 'can_view_all_branches': False, 'branch_id': 1, 'branch_ids': [1, 2]}
    with app.test_request_context():
        session.clear()
        session.update(scope_session)
        assert user_can_access_order({'collect_branch_id': 2, 'return_branch_id': 2}) is True
        assert user_can_access_order({'collect_branch_id': 3, 'return_branch_id': 1}) is True
        assert user_can_access_order({'collect_branch_id': 3, 'return_branch_id': 3}) is False
        assert user_can_access_order(None) is False
