"""Nav retirement regression - App store and Scan a barcode are gone from the chrome.

This is the pinned proof for the nav/removal task:

* the ``/app-store`` and ``/scan-barcode`` **routes** are retired (404 on GET *and*
  POST, for a full-permission sign-in and for staff), and with them the inline
  product-by-SKU lookup that lived on ``/scan-barcode``;
* the **permission modules** ``app_store`` and ``scan_barcode`` no longer exist, so
  the grant UI on ``/settings/users`` can no longer offer them;
* their **page templates** are deleted from disk, while the ``app_store_items`` DB
  table and its rows are deliberately preserved (DB records kept);
* the surviving scanner screens - the vehicle-disk scan and scan-to-return - keep
  their nav links and their routes;
* the desktop sidebar and the mobile menu now run **Reports -> Online store ->
  Customer portal**, the Customer portal link points at ``settings.portal_index``
  (``/settings/portal``) and is gated on the ``settings`` module, and the settings
  tab rail carries the same Customer portal link;
* ``requirements.txt`` pins ``qrcode==7.4.2`` for the portal QR codes.
"""
import os
import re
import tempfile

import pytest

from app import create_app
from app.db import get_db
from app.services.access import MODULES, MODULE_KEYS

RETIRED_PATHS = ["/app-store", "/scan-barcode"]


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
    return client.post('/login', data={'user_id': str(row['id']), 'password': password},
                       follow_redirects=True)


def _mobile_nav(html):
    match = re.search(rb'<nav class="mobile-nav">(.*?)</nav>', html, re.S)
    assert match, 'the phone navigation was not rendered'
    return match.group(1)


def _sidebar(html):
    head, _, _ = html.partition(b'<nav class="mobile-nav">')
    assert head, 'the sidebar was not rendered'
    return head


def _ordered(haystack, needles):
    """Positions of each needle; assert they appear left to right and all present."""
    positions = []
    for needle in needles:
        index = haystack.find(needle)
        assert index != -1, f'{needle!r} not found'
        positions.append(index)
    assert positions == sorted(positions), f'{needles!r} are out of order'
    return positions


# ----------------------------------------------------------------- routes retired


def test_retired_routes_answer_404_for_main(client):
    """App store and Scan a barcode are gone for a full-permission sign-in."""
    login(client)
    for path in RETIRED_PATHS:
        assert client.get(path).status_code == 404, path
        assert client.post(path, data={}).status_code == 404, path


def test_retired_routes_are_not_reachable_via_url_for(client, app):
    """The endpoints no longer exist, so the URL map refuses to build them."""
    with app.test_request_context():
        from flask import url_for
        from werkzeug.routing import BuildError
        for endpoint in ('admin.app_store', 'admin.scan_barcode'):
            with pytest.raises(BuildError):
                url_for(endpoint)


def test_surviving_scanner_routes_still_work(client):
    """The vehicle-disk scan and scan-to-return screens are untouched."""
    login(client)
    assert client.get('/scan-vehicle').status_code == 200
    assert client.get('/scan-return').status_code == 200


# ------------------------------------------------------------- templates removed


def test_retired_page_templates_are_deleted():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert not os.path.exists(os.path.join(root, 'templates', 'admin', 'app_store.html'))
    assert not os.path.exists(os.path.join(root, 'templates', 'admin', 'scan_barcode.html'))


def test_app_store_db_records_are_preserved(client, app):
    """The table survives the UI removal; its rows are never deleted."""
    with app.app_context():
        row = get_db().execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='app_store_items'"
        ).fetchone()
        assert row is not None, 'app_store_items table was dropped'
        assert get_db().execute("SELECT COUNT(*) AS c FROM app_store_items").fetchone()['c'] >= 0


# ---------------------------------------------------------------- modules retired


def test_retired_module_keys_are_gone():
    assert 'app_store' not in MODULE_KEYS
    assert 'scan_barcode' not in MODULE_KEYS
    # The surviving scanner modules and the settings module stay.
    assert 'scan_vehicle' in MODULE_KEYS
    assert 'scan_return' in MODULE_KEYS
    assert 'settings' in MODULE_KEYS
    labels = dict(MODULES)
    assert 'App store' not in labels.values()
    assert 'Scan a barcode' not in labels.values()


def test_grant_ui_no_longer_offers_the_retired_modules(client):
    login(client)
    body = client.get('/settings/users').data
    assert b'value="app_store"' not in body
    assert b'value="scan_barcode"' not in body
    # A surviving module the page still offers, so the assertion is not vacuous.
    assert b'value="scan_vehicle"' in body


# ------------------------------------------------------------------- nav order


def test_desktop_sidebar_runs_reports_online_store_customer_portal(client):
    login(client)
    body = client.get('/dashboard').data
    sidebar = _sidebar(body)
    _ordered(sidebar, [b'href="/reports"', b'href="/online-store"', b'href="/settings/portal"'])
    # The retired links are absent; the scanner links survive.
    assert b'/app-store' not in sidebar
    assert b'/scan-barcode' not in sidebar
    assert b'href="/scan-vehicle"' in sidebar
    assert b'href="/scan-return"' in sidebar
    assert b'App store' not in sidebar
    assert b'Scan a barcode' not in sidebar


def test_mobile_menu_runs_reports_online_store_customer_portal(client):
    login(client)
    nav = _mobile_nav(client.get('/dashboard').data)
    _ordered(nav, [b'href="/reports"', b'href="/online-store"', b'href="/settings/portal"'])
    assert b'/app-store' not in nav
    assert b'/scan-barcode' not in nav
    assert b'href="/scan-vehicle"' in nav
    assert b'href="/scan-return"' in nav


def test_customer_portal_link_is_gated_on_the_settings_module(client, app):
    """Main always sees it; a settings-less staff account never does."""
    login(client)
    assert b'href="/settings/portal"' in _sidebar(client.get('/dashboard').data)

    client.post('/settings/users/permissions', data={
        'module': ['new_order', 'dashboard', 'orders'],
    }, follow_redirects=True)
    client.post('/settings/users/add', data={
        'name': 'No Settings Staff', 'password': 'staff123',
    }, follow_redirects=True)
    client.post('/logout')
    login(client, 'No Settings Staff', 'staff123')

    body = client.get('/dashboard').data
    assert b'href="/settings/portal"' not in _sidebar(body)
    assert b'href="/settings/portal"' not in _mobile_nav(body)
    # Without the settings module the portal page itself is refused.
    assert client.get('/settings/portal').status_code == 403


def test_settings_tab_rail_carries_the_customer_portal_link(client):
    login(client)
    general = client.get('/settings/general')
    assert general.status_code == 200
    assert b'href="/settings/portal">Customer portal</a>' in general.data


# --------------------------------------------------------------- requirements


def test_requirements_pin_qrcode():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'requirements.txt'), encoding='utf-8') as handle:
        text = handle.read()
    assert re.search(r'^qrcode==7\.4\.2$', text, re.M), 'qrcode==7.4.2 is not pinned'
