"""Ticket ABI-341953031 - one uniform button shape for the phone navigation chips.

Requested edit: fix the Customers / Dashboard / Orders / Inventory / Branch button
shapes on the phone view for users.

Cause: ``static/css/app.css`` ``@media(max-width:860px)`` laid the phone nav out as
``display:flex;gap:8px;overflow:auto`` with content-sized pills, so the six chips
had six different widths, the row scrolled sideways (measured 585px of content in a
284-324px box), the depot chip wrapped onto several lines and stretched every chip to
that height, and the last chips sat half-cut off the right edge.

The fix is presentational only: the chips are a fixed-shape grid (uniform height,
radius, border, background, weight, centred + ellipsised single-line labels) that
wraps instead of scrolling, and the depot chip - the only chip with a variable-length
label - is placed last and given the full row so a long depot name stays readable
(ellipsised, never able to widen the row). No href, permission gate, route or desktop
rule changed, which these tests pin from both sides.
"""
import os
import re
import tempfile

import pytest

from app import create_app
from app.db import get_db


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
    """Sign in the way the UI does: pick a name from the dropdown, then the password."""
    with client.application.app_context():
        if name is None:
            row = get_db().execute(
                "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
        else:
            row = get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
    return client.post('/login', data={'user_id': str(row['id']), 'password': password},
                       follow_redirects=True)


def _mobile_nav_markup(html):
    """Return just the phone nav block, so a link cannot be confused with the sidebar."""
    match = re.search(rb'<nav class="mobile-nav">(.*?)</nav>', html, re.S)
    assert match, 'the phone navigation was not rendered'
    return match.group(1)


def _css():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'static', 'css', 'app.css'), encoding='utf-8') as handle:
        return handle.read()


def _rule(css, selector):
    """The declaration block for ``selector``, wherever it is written."""
    match = re.search(re.escape(selector) + r'\{([^}]*)\}', css)
    assert match, f'{selector} has no rule in app.css'
    return match.group(1)


def _phone_nav_rule(css):
    """The ``.mobile-nav a`` chip rule, asserted to be the phone-media one."""
    phone = css[css.index('@media(max-width:860px)'):]
    phone = phone[:phone.index('@media(max-width:520px)')]
    match = re.search(r'\.mobile-nav a\{([^}]*)\}', phone)
    assert match, 'the phone chips have no rule inside @media(max-width:860px)'
    return match.group(1)


def _chips(markup):
    return [(m.group(1).decode(), m.group(2).decode())
            for m in re.finditer(rb'<a([^>]*)>([^<]*)</a>', markup)]


def _href(attrs):
    match = re.search(r'href="([^"]+)"', attrs)
    return match.group(1) if match else ''


def _branch_name(app, branch_id=1):
    """The depot name the chip must print, read from the database (never hard-coded)."""
    with app.app_context():
        return get_db().execute("SELECT name FROM branches WHERE id = ?",
                                (branch_id,)).fetchone()['name']


def _with_branch_session(client, branch_id=1):
    with client.session_transaction() as session:
        session['active_branch_id'] = branch_id


# --------------------------------------------------------------------------- markup


def test_every_phone_chip_renders_with_one_shape(client, app):
    """All six chips are offered on a phone, in one predictable order."""
    login(client)
    _with_branch_session(client)
    dashboard = client.get('/dashboard')
    assert dashboard.status_code == 200

    chips = _chips(_mobile_nav_markup(dashboard.data))
    labels = [label for _, label in chips]
    assert labels == ['Dashboard', 'Orders', 'Customers', 'Inventory', 'Settings',
                      'Branch: %s \u00b7 Change' % _branch_name(app)], labels
    assert [_href(attrs) for attrs, _ in chips] == [
        '/dashboard', '/orders', '/customers', '/inventory', '/settings/general',
        '/select-branch',
    ]

    # Every chip is one and the same element type/label style: no chip carries an
    # inline size, and the depot chip is the only one wearing the context class.
    assert all('class=' not in attrs or 'branch-context' in attrs for attrs, _ in chips)


def test_the_depot_chip_is_last_and_keeps_its_label(client, app):
    """The depot chip moves to the end of the row; its label and target are unchanged."""
    login(client)
    _with_branch_session(client)
    nav = _mobile_nav_markup(client.get('/dashboard').data).decode('utf-8')

    assert nav.rstrip().endswith(
        '<a class="branch-context" href="/select-branch">Branch: %s \u00b7 Change</a>'
        % _branch_name(app))
    # The sidebar control is untouched - same class, same label, desktop wording intact.
    sidebar = client.get('/dashboard').data.split(b'<nav class="mobile-nav">')[0].decode('utf-8')
    assert '<a class="branch-context" href="/select-branch">' in sidebar
    assert 'Branch: %s \u00b7 Change</span>' % _branch_name(app) in sidebar


def test_a_chip_can_only_appear_where_the_module_gate_allows_it(client, app):
    """Permissions behaviour is untouched: the phone chip follows the sidebar gate."""
    login(client)
    saved = client.post('/settings/users/permissions', data={
        'module': ['new_order', 'dashboard', 'orders', 'inventory', 'settings'],
    }, follow_redirects=True)
    assert b'Additional account permissions saved' in saved.data
    added = client.post('/settings/users/add', data={
        'name': 'Phone Nav Staff', 'password': 'staff123',
    }, follow_redirects=True)
    assert b'Additional account created' in added.data
    client.post('/logout')
    login(client, 'Phone Nav Staff', 'staff123')
    _with_branch_session(client)

    nav = _mobile_nav_markup(client.get('/dashboard').data)
    labels = [label for _, label in _chips(nav)]
    assert labels == ['Dashboard', 'Orders', 'Inventory', 'Settings',
                      'Branch: %s \u00b7 Change' % _branch_name(app)]
    assert b'/customers' not in nav
    assert client.get('/customers').status_code == 403
    # The depot chip still points at the picker, and both halves still agree.
    assert client.get('/select-branch').status_code in (200, 302)


def test_no_chip_is_rendered_outside_the_phone_nav(client, app):
    """Guards the markup extraction: the sidebar block never leaks into the assertions."""
    login(client)
    _with_branch_session(client)
    body = client.get('/dashboard').data
    nav = _mobile_nav_markup(body)
    # The sidebar hides at <=860px, so the phone nav is the only way in; the module
    # links exist twice in the document (sidebar + phone nav) and never more.
    assert body.count(b'href="/customers"') == 2
    assert nav.count(b'href="/customers"') == 1


# ------------------------------------------------------------------------------ css


def test_the_phone_chips_share_one_uniform_button_shape():
    """One shape for every chip: same height, radius, border, fill, weight, label."""
    css = _css()
    phone = css[css.index('@media(max-width:860px)'):]
    phone = phone[:phone.index('@media(max-width:520px)')]

    nav_rule = re.search(r'\.mobile-nav\{([^}]*)\}', phone).group(1)
    assert 'display:grid' in nav_rule
    assert 'repeat(auto-fit,minmax(96px,1fr))' in nav_rule
    # The row no longer scrolls sideways: the chips wrap inside the viewport.
    assert 'overflow:auto' not in nav_rule
    assert 'display:flex' not in nav_rule

    chip = _phone_nav_rule(_css())
    assert 'height:44px' in chip, 'touch target must be at least 44px'
    assert 'line-height:42px' in chip, 'labels must be vertically centred in the 44px pill'
    assert 'border-radius:999px' in chip
    assert 'background:#fff' in chip
    assert 'border:1px solid var(--line)' in chip
    assert 'font-weight:750' in chip
    assert 'text-align:center' in chip
    # Long labels ellipsise instead of widening or clipping the pill.
    assert 'white-space:nowrap' in chip
    assert 'overflow:hidden' in chip
    assert 'text-overflow:ellipsis' in chip


def test_the_depot_chip_takes_the_full_row_and_never_widens_the_grid():
    """The only variable-length label gets the whole row and cannot stretch anything."""
    css = _css()
    phone = css[css.index('@media(max-width:860px)'):]
    phone = phone[:phone.index('@media(max-width:520px)')]

    branch_rule = re.search(r'\.mobile-nav a\.branch-context\{([^}]*)\}', phone).group(1)
    assert 'grid-column:1/-1' in branch_rule
    # Same shape as the others: the rule only overrides placement and text colour.
    for declaration in ('height', 'border-radius', 'background:#fff', 'border:1px solid'):
        assert declaration in branch_rule or declaration in _phone_nav_rule(css)
    assert 'color:var(--brand)' in branch_rule


def test_the_desktop_chrome_is_untouched():
    """The phone rules live in the <=860px media query and the desktop nav still hides."""
    css = _css()
    phone_start = css.index('@media(max-width:860px)')
    phone_end = css.index('@media(max-width:520px)')

    # Nothing about the phone chips is written outside the phone media query...
    assert 'minmax(96px,1fr)' not in css[:phone_start]
    assert 'minmax(96px,1fr)' not in css[phone_end:]
    # ...and the desktop rule that hides the phone nav is still the only .mobile-nav rule.
    assert '.mobile-nav{display:none}' in css[:phone_start]
    assert css.count('.mobile-nav{') == 2
