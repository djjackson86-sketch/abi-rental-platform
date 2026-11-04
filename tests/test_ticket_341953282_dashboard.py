"""Ticket ABI-341953282 - the dashboard's Owing Customers section.

The client asked the dashboard to show every customer who still owes money,
not just the orders raised today or inside the quick-range window. This file
pins the four things that make the list trustworthy:

* **all dates** - a balance from months ago is still listed;
* **current orders only** - positive collectable due, drafts/cancelled/archived
  and Sales/Repairs without an accepted quote are out (the Orders page's own
  "Payment due" definition, reused rather than re-invented);
* **branch scope** - the top Branch filter narrows the list and a depot-limited
  sign-in can only see its own depots;
* **both renders** - the full dashboard and the lazy
  ``/dashboard/partials/owing`` panel show the same rows, links and the same
  centred ``.empty-results`` empty message.
"""
import html as html_module
import os
import re
import tempfile
from urllib.parse import urlparse

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from app.db import get_db

OLD_DAY = '2025-01-15T09:00:00'
RECENT_DAY = '2026-11-01T09:00:00'
TODAY = '2026-11-04T09:00:00'


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


def add_depot_user(app, name, branch_id, password='staff123'):
    """A single-depot staff account, so the session itself carries the scope."""
    with app.app_context():
        db = get_db()
        db.execute(
            """INSERT INTO users (email, password_hash, name, initials, role, branch_id,
            can_view_all_branches, active, modules_json, created_at)
            VALUES (?, ?, ?, 'ST', 'staff', ?, 0, 1, NULL, ?)""",
            (f'{name.lower().replace(" ", ".")}@abi.local', generate_password_hash(password),
             name, branch_id, TODAY),
        )
        db.commit()


def add_customer(db, name, branch_id=1):
    db.execute(
        "INSERT INTO customers (name, branch_id, created_at) VALUES (?, ?, ?)",
        (name, branch_id, TODAY),
    )
    return db.execute("SELECT id FROM customers WHERE name = ?", (name,)).fetchone()['id']


def add_order(db, number, customer_id, status, due, branch_id=1, return_branch_id=None,
              payment_status='payment_due', created_at=RECENT_DAY):
    db.execute(
        """INSERT INTO orders (order_number, customer_id, booking_type, collect_branch_id,
        return_branch_id, status, payment_status, start_at, end_at, subtotal, tax_total,
        deposit_total, total, due_total, notes, created_at)
        VALUES (?, ?, 'return', ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, '', ?)""",
        (number, customer_id, branch_id, return_branch_id if return_branch_id is not None else branch_id,
         status, payment_status, created_at, created_at, due, due, due, created_at),
    )
    return db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()['id']


def seed_owing_book(app):
    """A book with owing orders at several dates plus every row that must not show."""
    ids = {}
    with app.app_context():
        db = get_db()
        alice = add_customer(db, 'Alice Owing', branch_id=1)
        bob = add_customer(db, 'Bob Debtor', branch_id=2)
        ids['alice'] = alice
        ids['bob'] = bob

        # Three real debts: old, recent, and a deposit/return branch overlap.
        ids['old'] = add_order(db, 'OWE-OLD', alice, 'started', 450.50, branch_id=1,
                               created_at=OLD_DAY)
        ids['recent'] = add_order(db, 'OWE-RECENT', bob, 'reserved', 120.00, branch_id=2,
                                  created_at=RECENT_DAY)
        # Collected at depot 1, returning to depot 2: either depot's view may see it.
        ids['shared'] = add_order(db, 'OWE-SHARED', alice, 'returned', 300.00, branch_id=1,
                                  return_branch_id=2, created_at=RECENT_DAY)
        # Partially paid is still an outstanding balance.
        ids['partial'] = add_order(db, 'OWE-PARTIAL', bob, 'started', 75.00, branch_id=2,
                                   payment_status='partially_paid', created_at=RECENT_DAY)

        # Rows that look like money but are not current debts.
        ids['draft'] = add_order(db, 'SKIP-DRAFT', alice, 'draft', 999.00, created_at=RECENT_DAY)
        ids['archived'] = add_order(db, 'SKIP-ARCHIVED', alice, 'archived', 888.00,
                                    created_at=RECENT_DAY)
        ids['canceled'] = add_order(db, 'SKIP-CANCELED', alice, 'canceled', 777.00,
                                    created_at=RECENT_DAY)
        ids['zero'] = add_order(db, 'SKIP-ZERO', alice, 'started', 0.00, created_at=RECENT_DAY)
        # Sales/Repairs without an accepted quote is still a quote, not a debt.
        ids['sales'] = add_order(db, 'SKIP-SALES', alice, 'sales_repairs', 500.00,
                                 created_at=RECENT_DAY)
        db.commit()
    return ids


def owing_block(page):
    """The rendered Owing Customers section, from its heading to the next card."""
    start = page.index('id="owing-customers"')
    start = page.rindex('<section', 0, start)
    end = page.index('</section>', start)
    return page[start:end]


def test_every_positive_outstanding_order_is_listed_whatever_its_date(client, app):
    ids = seed_owing_book(app)
    login(client)
    page = client.get('/dashboard').get_data(as_text=True)
    block = owing_block(page)

    assert 'Owing Customers' in page
    # The three-month-old debt is still owed, so it must be here on the default
    # "This month" dashboard.
    for number in ('OWE-OLD', 'OWE-RECENT', 'OWE-SHARED', 'OWE-PARTIAL'):
        assert number in block, number
    # Current = positive collectible due on a live order.
    for number in ('SKIP-DRAFT', 'SKIP-ARCHIVED', 'SKIP-CANCELED', 'SKIP-ZERO', 'SKIP-SALES'):
        assert number not in block, number

    # The amount is red, and both the customer and the order are links.
    assert 'class="balance-owing">R450.50<' in block
    assert f'href="/orders/{ids["old"]}"' in block
    assert f'href="/customers/{ids["alice"]}"' in block
    assert f'href="/orders/{ids["recent"]}"' in block
    assert f'href="/customers/{ids["bob"]}"' in block
    # The running total is red too, and counts every owing order.
    assert '4 orders · <strong class="balance-owing">R945.50</strong>' in block
    assert 'No outstanding orders right now.' not in block


def test_owing_list_ignores_the_dashboard_quick_range(client, app):
    """Choosing a one-day range must not hide debts raised outside that day."""
    seed_owing_book(app)
    login(client)
    page = client.get(f'/dashboard?start_date={TODAY[:10]}&end_date={TODAY[:10]}'
                      ).get_data(as_text=True)
    block = owing_block(page)

    assert 'OWE-OLD' in block
    assert 'OWE-RECENT' in block
    assert 'OWE-SHARED' in block
    assert 'OWE-PARTIAL' in block


def test_branch_filter_narrows_the_owing_list_on_collection_or_return(client, app):
    ids = seed_owing_book(app)
    login(client)

    depot_two = owing_block(client.get('/dashboard?branch=2').get_data(as_text=True))
    assert 'OWE-RECENT' in depot_two
    assert 'OWE-PARTIAL' in depot_two
    # Collected at depot 1 but returning to depot 2 is still depot 2's debt.
    assert 'OWE-SHARED' in depot_two
    assert 'OWE-OLD' not in depot_two

    depot_one = owing_block(client.get('/dashboard?branch=1').get_data(as_text=True))
    assert 'OWE-OLD' in depot_one
    assert 'OWE-SHARED' in depot_one
    assert 'OWE-RECENT' not in depot_one
    assert 'OWE-PARTIAL' not in depot_one

    # An all-branch viewer still sees the whole book.
    all_branches = owing_block(client.get('/dashboard').get_data(as_text=True))
    for number in ('OWE-OLD', 'OWE-RECENT', 'OWE-SHARED', 'OWE-PARTIAL'):
        assert number in all_branches, number


def test_a_depot_limited_sign_in_can_never_widen_its_own_owing_list(client, app):
    seed_owing_book(app)
    add_depot_user(app, 'Depot Two Staff', branch_id=2)
    login(client, 'Depot Two Staff', password='staff123')

    page = client.get('/dashboard?branch=1').get_data(as_text=True)
    block = owing_block(page)
    assert 'OWE-RECENT' in block
    assert 'OWE-PARTIAL' in block
    assert 'OWE-OLD' not in block, 'a crafted ?branch=1 must not widen depot 2'


def test_empty_owing_section_uses_the_centred_dashboard_empty_block(client, app):
    login(client)
    page = client.get('/dashboard').get_data(as_text=True)
    block = owing_block(page)

    assert ('<div class="empty-results"><strong>No results</strong>'
            '<span>No outstanding orders right now.</span></div>') in block


def _lazy_owing_src(page):
    match = re.search(r'data-dashboard-section="owing" data-src="([^"]+)"', page)
    assert match, 'the lazy dashboard must allocate an Owing Customers section'
    return html_module.unescape(match.group(1))


def test_lazy_render_has_a_placeholder_and_the_partial_returns_the_same_rows(client, app):
    ids = seed_owing_book(app)
    app.config['TESTING'] = False       # production path: the dashboard shell only
    login(client)

    shell = client.get('/dashboard?branch=1').get_data(as_text=True)
    # The heading is present immediately; only the rows are deferred.
    assert 'Owing Customers' in shell
    assert ('<div class="panel"><div class="empty-results"><strong>Loading…</strong>'
            '<span>Fetching outstanding orders…</span></div></div>') in shell
    src = _lazy_owing_src(shell)
    assert urlparse(src).path == '/dashboard/partials/owing'

    partial = client.get(src).get_data(as_text=True)
    assert 'OWE-OLD' in partial
    assert 'OWE-SHARED' in partial
    assert 'OWE-RECENT' not in partial          # branch=1 in the shell's data-src
    assert f'href="/orders/{ids["old"]}"' in partial
    assert f'href="/customers/{ids["alice"]}"' in partial
    assert 'class="balance-owing">R450.50<' in partial
    assert 'Skip' not in partial


def test_lazy_partial_empty_state_matches_the_normal_empty_state(client, app):
    app.config['TESTING'] = False
    login(client)

    shell = client.get('/dashboard').get_data(as_text=True)
    partial = client.get(_lazy_owing_src(shell)).get_data(as_text=True)
    assert ('<div class="empty-results"><strong>No results</strong>'
            '<span>No outstanding orders right now.</span></div>') in partial
