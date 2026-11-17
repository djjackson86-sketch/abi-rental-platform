"""Ticket ABI-341953326 - legacy references must open the record they name.

Ticket ABI-341953323 made the dashboard's legacy list a company-wide, read-only
view. This ticket makes its two references useful without widening any grant:

* the customer name opens that client's existing page, at the legacy balance and
  payment section (``#legacy-balance``);
* an imported reference opens the real platform order with the SAME number on
  the SAME customer;
* an imported source with no such order keeps showing its own source fields -
  status, dates and totals - and never gets an invented order link;
* links follow the existing module and branch rules exactly: the customers
  module for a client page, the orders module plus ``user_can_access_order``
  for an order page. The routes still refuse crafted URLs themselves.
"""
import json

import pytest

from app.db import get_db
from app.services.legacy_balances import legacy_balance_summary, outstanding_legacy_balances
from test_ticket_341953282_dashboard import app, client, login, add_order, add_depot_user
from test_ticket_341953284_dashboard import seed_legacy


def seed_references(app):
    """The 284 legacy book plus real orders for only some of its references."""
    ids = seed_legacy(app)
    with app.app_context():
        db = get_db()
        # A real order for Alice's LEG-OPEN import.
        ids['open_order'] = add_order(db, 'LEG-OPEN', ids['alice'], 'started', 0, branch_id=1)
        # A real order for Bob's LEG-DEPOT-TWO import (branch 2).
        ids['depot_two_order'] = add_order(db, 'LEG-DEPOT-TWO', ids['bob'], 'started', 0, branch_id=2)
        # Same reference text, but recorded against another client: not Alice's order.
        ids['borrowed_number_order'] = add_order(db, 'LEG-PARTIAL', ids['bob'], 'started', 0, branch_id=2)
        db.commit()
    return ids


def set_modules(app, name, modules):
    with app.app_context():
        db = get_db()
        db.execute('UPDATE users SET modules_json=? WHERE name=?', (json.dumps(modules), name))
        db.commit()


def legacy_panel(client, query=''):
    return client.get('/dashboard/partials/legacy' + query).get_data(as_text=True)


def legacy_rows(html):
    body = html.split('id="owing-legacy-rows"', 1)[1].split('</tbody>', 1)[0]
    return body.split('<tr')[1:]


def row_for(html, reference):
    return next(row for row in legacy_rows(html) if reference in row)


@pytest.mark.parametrize('lazy', [False, True])
def test_owner_links_customer_and_only_real_matching_order(app, client, lazy):
    ids = seed_references(app)
    login(client)
    app.config['TESTING'] = not lazy
    if not lazy:
        shell = client.get('/dashboard').get_data(as_text=True)
        assert 'id="owing-legacy-balances"' in shell
    html = legacy_panel(client)

    # The customer name opens the existing client page's legacy section.
    assert f'href="/customers/{ids["alice"]}#legacy-balance"' in html
    assert f'href="/customers/{ids["bob"]}#legacy-balance"' in html

    # Only the reference with a real order on the same customer becomes a link.
    assert f'href="/orders/{ids["open_order"]}"' in row_for(html, 'LEG-OPEN')
    assert f'href="/orders/{ids["depot_two_order"]}"' in row_for(html, 'LEG-DEPOT-TWO')
    borrowed = row_for(html, 'LEG-PARTIAL')
    assert '/orders/' not in borrowed
    assert 'imported source' in borrowed.lower()
    unassigned = row_for(html, 'LEG-UNASSIGNED')
    assert '/orders/' not in unassigned
    assert 'imported source' in unassigned.lower()

    # The linked records really open, from the same session.
    detail = client.get(f'/orders/{ids["open_order"]}')
    assert detail.status_code == 200
    assert 'LEG-OPEN' in detail.get_data(as_text=True)
    customer_page = client.get(f'/customers/{ids["alice"]}').get_data(as_text=True)
    assert f'href="/orders/{ids["open_order"]}"' in customer_page
    assert 'Imported source only' in customer_page


def test_customer_page_shows_imported_source_when_no_order_exists(app, client):
    ids = seed_references(app)
    login(client)
    page = client.get(f'/customers/{ids["alice"]}').get_data(as_text=True)
    section = page.split('id="legacy-balance"', 1)[1].split('</article>', 1)[0]
    assert f'href="/orders/{ids["open_order"]}"' in section
    # Alice's LEG-PARTIAL import has no order of her own (the number was reused
    # on Bob's order), so it keeps its own source fields and gets no link.
    assert 'Old order #LEG-PARTIAL' in section
    assert f'href="/orders/{ids["borrowed_number_order"]}"' not in section
    assert 'Imported source only' in section
    assert 'LEG-UNASSIGNED' not in section


def test_branch_limited_staff_only_gets_links_they_can_open(app, client):
    ids = seed_references(app)
    add_depot_user(app, 'Depot One', 1)
    set_modules(app, 'Depot One', ['dashboard', 'orders', 'customers'])
    login(client, 'Depot One', password='staff123')
    html = legacy_panel(client)
    assert f'href="/orders/{ids["open_order"]}"' in html
    assert f'href="/orders/{ids["depot_two_order"]}"' not in html
    # The client page stays shared for customer-enabled staff (existing rule).
    assert f'href="/customers/{ids["bob"]}#legacy-balance"' in html
    # The order route enforces the same depot restriction server-side.
    assert client.get(f'/orders/{ids["open_order"]}').status_code == 200
    assert client.get(f'/orders/{ids["depot_two_order"]}').status_code == 404

    with client.application.test_request_context('/dashboard/partials/legacy'):
        from flask import session
        with client.session_transaction() as state:
            session.update(state)
        rows = {row['source_order_number']: row for row in outstanding_legacy_balances(company_wide=True)}
    assert rows['LEG-OPEN']['order_id'] == ids['open_order']
    assert rows['LEG-DEPOT-TWO']['order_id'] is None
    assert rows['LEG-UNASSIGNED']['order_id'] is None


def test_dashboard_only_account_gets_no_links_and_routes_still_refuse(app, client):
    ids = seed_references(app)
    add_depot_user(app, 'Just Dashboard', 1)
    set_modules(app, 'Just Dashboard', ['dashboard'])
    login(client, 'Just Dashboard', password='staff123')
    html = legacy_panel(client)
    assert 'LEG-OPEN' in html and 'LEG-UNASSIGNED' in html
    assert 'href=' not in html
    # Hiding a link is convenience; the server-side gates do the real work.
    assert client.get(f'/customers/{ids["alice"]}').status_code == 403
    assert client.get(f'/orders/{ids["open_order"]}').status_code == 403


def test_legacy_reads_remain_write_free(app, client):
    ids = seed_references(app)
    login(client)
    with app.app_context():
        db = get_db()
        before = db.total_changes
        outstanding_legacy_balances(company_wide=True)
        legacy_balance_summary(ids['alice'])
        assert db.total_changes == before
    before = None
    with app.app_context():
        before = get_db().total_changes
    client.get('/dashboard/partials/legacy')
    client.get(f'/customers/{ids["alice"]}')
    with app.app_context():
        assert get_db().total_changes == before


@pytest.mark.parametrize('modules,allowed', [(['dashboard','customers','orders'],True),(['dashboard','customers'],False),(['dashboard','orders'],False),(['dashboard'],False)])
def test_payment_action_reuses_permissions_and_posts(app,client,modules,allowed):
    ids=seed_references(app)
    add_depot_user(app,'Legacy payment staff',1)
    set_modules(app,'Legacy payment staff',modules)
    login(client,'Legacy payment staff','staff123')
    path=f'/customers/{ids["alice"]}/legacy-payments'
    page=client.get(f'/customers/{ids["alice"]}').get_data(as_text=True)
    assert ('class="legacy-payment-form"' in page)==allowed
    with app.app_context():
        before=legacy_balance_summary(ids['alice'])['total']
        receipts=get_db().execute('SELECT COUNT(*) FROM legacy_balance_payments').fetchone()[0]
    response=client.post(path,data={'amount':'10','method':'cash','reference':'LOCAL-326'},follow_redirects=False)
    assert response.status_code==(302 if allowed else 403)
    with app.app_context():
        assert legacy_balance_summary(ids['alice'])['total']==before-(10 if allowed else 0)
        assert get_db().execute('SELECT COUNT(*) FROM legacy_balance_payments').fetchone()[0]==receipts+(1 if allowed else 0)

