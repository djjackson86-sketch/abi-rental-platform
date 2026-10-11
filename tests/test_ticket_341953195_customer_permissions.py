"""Ticket ABI-341953195 - Customers import/export are main-profile only.

Requested edit: "customers page: only allow main user for import and export
buttons".

Treating "main user" as the existing main profile (``users.role = 'owner'``, the
account ``app/services/access.py`` calls the main profile) - no new role and no
hard-coded email.

The rules these tests pin:
- the main profile still sees Import and Export on the Customers page, and Import
  is still the disabled placeholder it always was;
- an additional (staff) account that HAS the Customers module sees neither
  control, and the page itself still works for them;
- the export endpoint carries the guard itself, so a direct URL - filtered or not
  - is a 403 that returns no customer data, not a download;
- login protection is unchanged (anonymous hits are redirected to sign in);
- staff customer browsing, viewing and editing is untouched.
"""
import importlib.util
from pathlib import Path

import pytest
from werkzeug.exceptions import Forbidden

spec = importlib.util.spec_from_file_location(
    'customer_permission_helpers',
    Path(__file__).with_name('test_ticket_341953028_customer_blocking.py'),
)
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)

# Reuse the blocking ticket's fixtures/helpers so every customer-permission test
# drives the same app and the same customer records.
app = helpers.app
client = helpers.client

STAFF_MODULES = ['customers', 'orders', 'documents']


def login_as_staff(client, modules=STAFF_MODULES):
    """Sign in as the main profile, then act as an additional account.

    ``user_role`` is what every permission check reads (``is_main_session`` /
    ``main_required``), so an additional account with the Customers module is the
    session a staff member's browser really carries.
    """
    helpers.login(client)
    with client.session_transaction() as sess:
        sess['user_role'] = 'staff'
        sess['staff_modules'] = list(modules)
        sess['user_name'] = 'Depot Staff'
    return client


def page(client, url):
    response = client.get(url)
    return response, response.get_data(as_text=True)


# --------------------------------------------------------------- main profile

def test_the_main_profile_still_sees_import_and_export(client, app):
    helpers.login(client)
    response, body = page(client, '/customers')
    assert response.status_code == 200
    assert 'import-btn' in body
    assert 'export-btn' in body
    assert '/customers/export.csv' in body
    # Import is untouched: still the disabled placeholder, not a working importer.
    assert 'disabled title="CSV import is planned for a later pass"' in body


def test_the_main_profile_can_still_download_the_customer_csv(client, app):
    helpers.login(client)
    helpers.create_customer(client, name='Export Visible Client', email='export@example.test')

    response = client.get('/customers/export.csv')
    assert response.status_code == 200
    assert response.mimetype == 'text/csv'
    assert response.headers['Content-Disposition'] == 'attachment; filename=customers.csv'
    body = response.get_data(as_text=True)
    assert body.splitlines()[0].startswith('name,customer_type,email')
    assert 'Export Visible Client' in body


# --------------------------------------------------------------------- staff

def test_a_staff_account_with_customers_access_sees_neither_control(client, app):
    helpers.login(client)
    helpers.create_customer(client, name='Hidden Button Client', email='hidden@example.test')
    login_as_staff(client)

    response, body = page(client, '/customers')
    assert response.status_code == 200
    # The Customers page itself is unchanged for staff ...
    assert 'Hidden Button Client' in body
    # ... but the archive controls are not rendered at all.
    assert 'import-btn' not in body
    assert 'export-btn' not in body
    assert 'customers/export.csv' not in body
    assert 'CSV import is planned' not in body


def test_staff_direct_export_is_a_403_that_returns_no_customer_data(client, app):
    helpers.login(client)
    helpers.create_customer(client, name='Secret Client', email='secret@example.test')
    login_as_staff(client)

    response = client.get('/customers/export.csv')
    assert response.status_code == 403
    body = response.get_data(as_text=True)
    assert 'Secret Client' not in body
    assert 'secret@example.test' not in body
    assert 'text/csv' not in response.content_type


@pytest.mark.parametrize('url', [
    '/customers/export.csv',
    '/customers/export.csv?query=Secret',
    '/customers/export.csv?customer_type=individual',
    '/customers/export.csv?customer_type=individual&marketing=subscribed',
    '/customers/export.csv?limit=5000',
])
def test_no_staff_export_url_works_including_filtered_ones(client, app, url):
    helpers.login(client)
    helpers.create_customer(client, name='Filtered Secret Client', email='filtered@example.test')
    login_as_staff(client)

    response = client.get(url)
    assert response.status_code == 403, url
    assert 'Filtered Secret Client' not in response.get_data(as_text=True)


def test_the_export_view_itself_carries_the_guard_not_just_the_button(client, app):
    """A crafted request that never renders the page is still refused."""
    from flask import session

    with app.test_request_context('/customers/export.csv'):
        session['user_id'] = 1
        session['user_role'] = 'staff'
        session['staff_modules'] = list(STAFF_MODULES)
        with pytest.raises(Forbidden):
            app.view_functions['customers.export_csv']()


# ----------------------------------------------------------------- anonymous

def test_anonymous_requests_are_still_login_protected(client, app):
    listing = client.get('/customers', follow_redirects=False)
    assert listing.status_code in (301, 302)
    assert '/login' in listing.headers['Location']

    export = client.get('/customers/export.csv', follow_redirects=False)
    assert export.status_code in (301, 302)
    assert '/login' in export.headers['Location']


# ------------------------------------------------- staff Customers unchanged

def test_staff_customer_browsing_and_editing_is_unchanged(client, app):
    helpers.login(client)
    customer_id = helpers.create_customer(client, name='Editable Client', email='editable@example.test')
    login_as_staff(client)

    listing = client.get('/customers')
    assert listing.status_code == 200

    detail = client.get(f'/customers/{customer_id}')
    assert detail.status_code == 200
    assert 'Editable Client' in detail.get_data(as_text=True)

    form = client.get(f'/customers/{customer_id}/edit')
    assert form.status_code == 200

    saved = helpers.save_customer_form(client, app, customer_id, name='Renamed By Staff')
    assert b'Customer saved' in saved.data
    assert helpers.customer_row(app, customer_id)['name'] == 'Renamed By Staff'


def test_staff_without_the_customers_module_is_still_gated_by_the_module(client, app):
    """The coarser module gate is unchanged by this ticket."""
    helpers.login(client)
    login_as_staff(client, modules=['orders'])

    assert client.get('/customers').status_code == 403
    assert client.get('/customers/export.csv').status_code == 403
