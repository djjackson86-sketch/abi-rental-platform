"""The Branch filter on /inventory (ticket ABI-341952949, part 1).

Ticket ask: "under the inventory page add a filter for branches".

The filter follows the same contract as /calendar, /reports and /orders: it is
resolved by ``app.services.access.resolve_branch_filter`` so the session scope
always wins. A branch filter can therefore only ever NARROW what an account sees
— a crafted ``?branch=`` for a depot the account does not hold is ignored, and a
single-depot account is shown a fixed label instead of a chooser.

It also has to survive everywhere the other inventory filters do: the list, the
search form, the "All groups" / "All" links, the "Clear" links, the CSV export
and the active-filter line.
"""
import os
import tempfile

import pytest

from app import create_app
from app.db import get_db
from app.services.access import MODULE_KEYS

BRANCH_ONE = 'Branch 1'
BRANCH_TWO = 'Branch 2'


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
                "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
        else:
            row = get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
        user_id = row['id'] if row else None
    return client.post('/login', data={'user_id': str(user_id), 'password': password},
                       follow_redirects=True)


def add_staff(client, app, name, branch_ids, password='staff123'):
    login(client)
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


def make_product(client, sku, branch_id):
    res = client.post('/inventory/new', data={
        'name': f'Trailer {sku}', 'sku': sku, 'description': '',
        'product_type': 'rental', 'tracking_method': 'bulk', 'quantity': '1',
        'price_amount': '200', 'price_unit': 'day', 'security_deposit': '0',
        'active': '1', 'public_visible': '1',
        'branch_id': str(branch_id) if branch_id else '',
    }, follow_redirects=False)
    assert res.status_code == 302, res.data[:400]
    return int(res.headers['Location'].rstrip('/').split('/')[-2])


def seed_two_depots(client):
    """One trailer in each depot, plus one with no branch (unassigned)."""
    make_product(client, 'DEPOT-A', 1)
    make_product(client, 'DEPOT-B', 2)
    make_product(client, 'FLOAT-C', None)


def filter_form(page):
    """The filter rail's own form, so a name="branch" count means what we think."""
    marker = 'class="inventory-filter-form"'
    start = page.index(marker)
    return page[start:page.index('</form>', start)]


# --- the filter narrows the list --------------------------------------------

def test_the_branch_filter_narrows_the_inventory_list(client):
    login(client)
    seed_two_depots(client)

    unfiltered = client.get('/inventory').data.decode()
    assert 'DEPOT-A' in unfiltered and 'DEPOT-B' in unfiltered and 'FLOAT-C' in unfiltered
    # With no branch chosen the rail offers "All branches" and selects nothing.
    assert '<option value="">All branches</option>' in unfiltered
    assert '<option value="1" selected>' not in unfiltered

    filtered = client.get('/inventory?branch=2').data.decode()
    assert 'DEPOT-B' in filtered
    assert 'DEPOT-A' not in filtered
    assert 'FLOAT-C' not in filtered          # an explicit branch excludes unassigned
    assert f'<option value="2" selected>{BRANCH_TWO}</option>' in filtered
    # The active-filter line names the narrowing and offers the way out.
    assert 'Showing filtered products' in filtered
    assert 'Clear all filters' in filtered


def test_the_csv_export_matches_the_filtered_view(client):
    login(client)
    seed_two_depots(client)

    export = client.get('/inventory/export.csv?branch=2').data.decode()
    assert 'DEPOT-B' in export
    assert 'DEPOT-A' not in export
    assert 'FLOAT-C' not in export

    every = client.get('/inventory/export.csv').data.decode()
    assert 'DEPOT-A' in every and 'DEPOT-B' in every


def test_the_branch_survives_the_search_form_and_the_links(client):
    login(client)
    seed_two_depots(client)
    page = client.get('/inventory?branch=2').data.decode()

    # The header search box keeps the branch, and the filter rail has exactly ONE
    # branch control (a select). A second hidden copy would submit the value twice
    # — the trap that made typed dates silently ignored on /calendar and /reports.
    assert '<input type="hidden" name="branch" value="2">' in page
    assert filter_form(page).count('name="branch"') == 1

    # Every link that keeps (or clears) a filter keeps the branch too.
    assert 'visibility=&amp;branch=2">All groups' in page
    assert 'product_group_id=&amp;branch=2">All<' in page
    assert '/inventory/export.csv?' in page and 'branch=2' in page
    # No date-filter twin was reintroduced by this change.
    assert 'name="start_date"' not in page and 'name="end_date"' not in page


def test_clear_branch_keeps_the_other_filters(client):
    login(client)
    seed_two_depots(client)
    page = client.get('/inventory?branch=2&product_type=rental').data.decode()
    start = page.index('Clear branch')
    link = page[page.rindex('<a', 0, start):page.index('</a>', start)]
    assert 'branch=' not in link.split('href="')[1].split('"')[0]
    assert 'product_type=rental' in link
    assert 'Showing filtered products' in page


def test_clearing_everything_removes_the_branch(client):
    login(client)
    seed_two_depots(client)
    page = client.get('/inventory?branch=2').data.decode()
    start = page.index('Clear all filters')
    link = page[page.rindex('<a', 0, start):page.index('</a>', start)]
    assert 'href="/inventory"' in link.replace('&#34;', '"')


# --- the filter can only ever narrow ----------------------------------------

def test_a_branch_outside_the_session_scope_is_ignored(client, app):
    login(client)
    seed_two_depots(client)
    add_staff(client, app, 'Depot Two', [2])
    login(client, 'Depot Two', 'staff123')

    # Its own depot, with the other depot's stock gone. The unassigned float
    # rides along with a staff scope view — that is the pre-existing rule for
    # unallocated stock, not something this filter changed.
    own = client.get('/inventory').data.decode()
    assert 'DEPOT-B' in own
    assert 'DEPOT-A' not in own
    assert 'FLOAT-C' in own

    # A crafted ?branch= cannot widen the view, and there is no chooser at all:
    # the rail shows a fixed label instead.
    for crafted in ('1', '999', 'abc', '-1', '2 OR 1=1'):
        page = client.get(f'/inventory?branch={crafted}').data.decode()
        assert 'DEPOT-B' in page, crafted
        assert 'DEPOT-A' not in page, crafted
        assert 'name="branch"' not in filter_form(page), crafted
        assert f'<label class="filter-fixed"' in page, crafted
        assert BRANCH_TWO in page, crafted


def test_an_unknown_branch_is_ignored_for_an_all_branch_account(client):
    login(client)
    seed_two_depots(client)

    for crafted in ('999', 'abc', '-1', '2 OR 1=1'):
        page = client.get(f'/inventory?branch={crafted}').data.decode()
        assert 'DEPOT-A' in page and 'DEPOT-B' in page and 'FLOAT-C' in page, crafted
        # Nothing is echoed as selected, so the rail stays on "All branches".
        assert '<option value="1" selected>' not in page, crafted
        assert '<option value="2" selected>' not in page, crafted
        assert '<option value="">All branches</option>' in page, crafted


def test_the_module_gate_still_applies(client, app):
    login(client)
    seed_two_depots(client)
    login(client)
    client.post('/settings/users/permissions', data={'module': ['dashboard']},
                follow_redirects=True)
    client.post('/settings/users/add', data={'name': 'No Inventory', 'password': 'staff123'},
                follow_redirects=True)
    client.post('/logout')
    login(client, 'No Inventory', 'staff123')
    assert client.get('/inventory?branch=1').status_code == 403
    assert client.get('/inventory/export.csv?branch=1').status_code == 403
