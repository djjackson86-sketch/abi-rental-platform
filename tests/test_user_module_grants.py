"""Per-account module permission grants (ticket ABI-341952942, edit 1).

Ticket ask: "allow additional user modules to be edited after user is saved".

Before this, every additional account shared ONE set of module ticks held in
``company_settings.staff_permissions_json`` and it could only be changed for all
of them at once. An account may now carry its own set in ``users.modules_json``
(NULL = keep inheriting the shared default), which the Users page edits per row.

The rules these tests pin:
- an account's own set wins over the shared default, and only for that account;
- the ticks can be changed at any time (not just at creation);
- the main profile can never be restricted;
- "Use shared default" puts an account back on the shared set;
- unknown module keys are dropped, and an account with nothing ticked can open
  nothing (an explicit admin choice, not an accident).
"""
import json
import os
import tempfile

import pytest

from app import create_app
from app.db import get_db
from app.services.access import (
    DEFAULT_STAFF_MODULES,
    MODULE_KEYS,
    clear_user_modules,
    list_users,
    save_staff_modules,
    save_user_modules,
    user_module_assignments,
    user_module_keys,
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


def owner_id(app):
    with app.app_context():
        return get_db().execute(
            "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
        ).fetchone()['id']


def user_id_for(app, name):
    with app.app_context():
        row = get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
        return row['id'] if row else None


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


def add_staff(client, app, name, password='staff123', branch_id=''):
    client.post('/settings/users/add', data={
        'name': name, 'password': password, 'branch_id': branch_id,
    }, follow_redirects=True)
    return user_id_for(app, name)


def set_own_modules(client, staff_id, modules):
    return client.post(f'/settings/users/{staff_id}/modules', data={'module': modules},
                       follow_redirects=True)


def test_an_accounts_own_modules_override_the_shared_default(client, app):
    """Two additional accounts, different module sets, no cross-talk."""
    login(client)
    client.post('/settings/users/permissions', data={
        'module': ['dashboard', 'orders'],
    }, follow_redirects=True)
    alice = add_staff(client, app, 'Alice Modular')
    bob = add_staff(client, app, 'Bob Modular')

    assert b'Account modules saved' in set_own_modules(client, alice, ['dashboard']).data

    client.post('/logout')
    login(client, 'Alice Modular', 'staff123')
    assert client.get('/dashboard').status_code == 200
    assert client.get('/orders').status_code == 403

    client.post('/logout')
    login(client, 'Bob Modular', 'staff123')
    assert client.get('/orders').status_code == 200
    assert client.get('/dashboard').status_code == 200

    with app.app_context():
        assert user_module_keys(alice) == ['dashboard']
        # Bob never had his own set, so he still inherits the shared default.
        assert user_module_keys(bob) is None


def test_saving_modules_after_the_account_exists_changes_access(client, app):
    """The ticket's ask: the ticks stay editable once the account is saved."""
    login(client)
    client.post('/settings/users/permissions', data={
        'module': ['dashboard', 'orders'],
    }, follow_redirects=True)
    staff = add_staff(client, app, 'Editable Modules')

    client.post('/logout')
    login(client, 'Editable Modules', 'staff123')
    assert client.get('/orders').status_code == 200
    assert client.get('/inventory').status_code == 403

    client.post('/logout')
    login(client)
    saved = set_own_modules(client, staff, ['dashboard', 'inventory'])
    assert b'Account modules saved. They apply on the next sign-in.' in saved.data

    client.post('/logout')
    login(client, 'Editable Modules', 'staff123')
    assert client.get('/inventory').status_code == 200
    assert client.get('/orders').status_code == 403
    assert client.get('/customers').status_code == 403


def test_the_main_profile_cannot_be_restricted(client, app):
    """The owner is refused by the service AND by the route, and keeps everything."""
    login(client)
    owner = owner_id(app)

    refused = set_own_modules(client, owner, [])
    assert b'The main profile always has every function' in refused.data

    with app.app_context():
        assert user_module_keys(owner) is None
        assert save_user_modules(owner, ['dashboard']) == (False, 'The main profile always has every function')
        assert clear_user_modules(owner) is False

    # Still sees every screen.
    for path in ['/dashboard', '/orders', '/inventory', '/customers', '/settings/users']:
        assert client.get(path).status_code == 200, path


def test_unknown_module_keys_are_dropped(client, app):
    login(client)
    staff = add_staff(client, app, 'Junk Modules')
    with app.app_context():
        ok, saved = save_user_modules(staff, ['dashboard', 'not_a_module', '', 'orders', 'dashboard'])
        assert ok is True
        # De-duplicated, unknown keys gone, real keys kept.
        assert saved == ['dashboard', 'orders']
        assert user_module_keys(staff) == ['dashboard', 'orders']
        assert set(saved).issubset(set(MODULE_KEYS))
        raw = get_db().execute("SELECT modules_json FROM users WHERE id = ?", (staff,)).fetchone()['modules_json']
        assert json.loads(raw) == ['dashboard', 'orders']


def test_use_shared_default_clears_the_accounts_own_set(client, app):
    login(client)
    client.post('/settings/users/permissions', data={
        'module': ['dashboard', 'orders', 'calendar'],
    }, follow_redirects=True)
    staff = add_staff(client, app, 'Default Returner')
    set_own_modules(client, staff, ['dashboard'])

    reset = client.post(f'/settings/users/{staff}/modules/reset', follow_redirects=True)
    assert b'Account now uses the shared default modules' in reset.data

    with app.app_context():
        assert user_module_keys(staff) is None
    client.post('/logout')
    login(client, 'Default Returner', 'staff123')
    # Back on the shared default, so Orders and Calendar work again.
    assert client.get('/orders').status_code == 200
    assert client.get('/calendar').status_code == 200


def test_an_account_with_nothing_ticked_can_open_no_module(client, app):
    """An explicitly empty set is respected (it is never read as the default)."""
    login(client)
    staff = add_staff(client, app, 'No Modules')
    assert b'Account modules saved' in set_own_modules(client, staff, []).data

    with app.app_context():
        assert user_module_keys(staff) == []
    client.post('/logout')
    login(client, 'No Modules', 'staff123')
    for path in ['/dashboard', '/orders', '/inventory', '/calendar']:
        assert client.get(path).status_code == 403, path


def test_accounts_without_their_own_set_follow_the_shared_default(client, app):
    """The shared panel keeps working for everyone who has no per-account set."""
    login(client)
    staff = add_staff(client, app, 'Shared Default Staff')
    client.post('/settings/users/permissions', data={
        'module': ['dashboard', 'calendar'],
    }, follow_redirects=True)

    with app.app_context():
        assert user_module_keys(staff) is None
    client.post('/logout')
    login(client, 'Shared Default Staff', 'staff123')
    assert client.get('/calendar').status_code == 200
    assert client.get('/orders').status_code == 403

    client.post('/logout')
    login(client)
    assert b'Additional account permissions saved' in client.post(
        '/settings/users/permissions', data={'module': list(DEFAULT_STAFF_MODULES)},
        follow_redirects=True,
    ).data
    client.post('/logout')
    login(client, 'Shared Default Staff', 'staff123')
    assert client.get('/orders').status_code == 200


def test_users_page_renders_a_module_form_for_each_account(client, app):
    login(client)
    staff = add_staff(client, app, 'Page Modules')
    set_own_modules(client, staff, ['dashboard', 'inventory'])
    page = client.get('/settings/users')
    assert page.status_code == 200
    body = page.data
    # The shared default panel survives, plus a per-account form for the staff row.
    assert b'Shared default permissions' in body
    assert b'Save shared default' in body
    assert f'/settings/users/{staff}/modules'.encode() in body
    assert f'/settings/users/{staff}/modules/reset'.encode() in body
    assert b'Use shared default' in body
    # The account's own ticks are the ones rendered as checked.
    with app.app_context():
        assignments = user_module_assignments(list_users(), DEFAULT_STAFF_MODULES)
        assert assignments[staff] == {'own': True, 'keys': ['dashboard', 'inventory']}
    for only_own in ['dashboard', 'inventory']:
        assert f'value="{only_own}" checked'.encode() in body


def test_a_staff_account_still_cannot_open_the_users_page(client, app):
    login(client)
    add_staff(client, app, 'Nosy Staff')
    client.post('/logout')
    login(client, 'Nosy Staff', 'staff123')
    assert client.get('/settings/users').status_code == 403


def test_module_grants_helper_refuses_an_unknown_account(client, app):
    with app.app_context():
        assert save_user_modules(9999, ['dashboard']) == (False, 'The main profile always has every function')
        assert clear_user_modules(9999) is False
        assert user_module_keys(9999) is None
        assert save_staff_modules(['dashboard']) == ['dashboard']
