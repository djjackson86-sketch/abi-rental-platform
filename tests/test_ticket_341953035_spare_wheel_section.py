"""Ticket ABI-341953035 - spare wheel heading reads like the cash-up heading.

Requested edit (that ticket): "spare wheel count should be after cash up section
and make the heading similar to cash up".  The fix was one attribute: giving the
spare-wheel wrapper the same ``dashboard-day dashboard-card-section`` class list
as the cash-up wrapper, so the day sections share one heading treatment.

**Partly superseded by ticket ABI-341953036**, which moved the spare-wheel panel
*inside* the ``Cash up · end of day`` section (between the Cash up panel and the
End of day notes panel) and renamed its heading to ``Spare wheel count``.  The
spare-wheel block is no longer a named dashboard section of its own, so the two
tests that pinned it as one - its own ``<section>`` wrapper and its own entry in
the heading order - are superseded and now live, against the new contract, in
``tests/test_ticket_341953036_spare_wheel_placement.py``.

What survives from ABI-341953035 and is still pinned here:

1. The day sections (Today, Cash up · end of day) still wear the
   ``dashboard-day dashboard-card-section`` wrapper, and ``static/css/app.css``
   still styles ``.dashboard-day .section-heading`` at 15px / uppercase - the
   heading treatment this ticket bought.  ``app.css`` is untouched by both
   tickets; the CSS contract test below keeps it that way.
2. With the spare-wheel block no longer being a named section, the rendered
   heading order is Today -> Cash up · end of day -> Movement (main profile).
3. The panel itself still renders - including for a branch-limited staff
   account - and it still lives inside the same section as the Cash up panel.
4. The source-level guard on ``dashboard.html``: the two day wrappers plus the
   plain wrapper around Movement, and no spare-wheel include left behind.

Display-only: no route, service, DB, permission or JS change, and ``app.css``
is not touched.  ``GET /dashboard`` (``admin.dashboard``) only.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.db import get_db  # noqa: E402
from app.services.access import MODULE_KEYS  # noqa: E402

CSS_PATH = ROOT / "static" / "css" / "app.css"
DASHBOARD_TEMPLATE = ROOT / "templates" / "admin" / "dashboard.html"
SPARE_TEMPLATE = ROOT / "templates" / "admin" / "_dashboard_spare_wheels.html"

CASH_UP_HEADING = "Cash up \u00b7 end of day"
SPARE_OLD_WORDING = "Spare wheels by wheel size"
DAY_WRAPPER = "dashboard-day dashboard-card-section"
PLAIN_WRAPPER = "dashboard-card-section"


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    application = create_app({
        "TESTING": True,
        "DATABASE": path,
        "SECRET_KEY": "test",
        "ADMIN_EMAIL": "admin@abi.local",
        "ADMIN_PASSWORD": "admin123",
    })
    yield application
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def login(client, app, name=None, password="admin123"):
    """Sign in the way the UI does: pick a name, then the password."""
    with client.application.app_context():
        if name is None:
            row = get_db().execute(
                "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
            ).fetchone()
        else:
            row = get_db().execute(
                "SELECT id FROM users WHERE name = ?", (name,)).fetchone()
    return client.post("/login", data={"user_id": str(row["id"]), "password": password},
                       follow_redirects=True)


def add_staff(client, app, name, branch_id, password="staff123"):
    """Owner creates an additional (non-main) account tied to one depot."""
    login(client, app)
    client.post("/settings/users/permissions", data={"module": list(MODULE_KEYS)},
                follow_redirects=True)
    client.post("/settings/users/add", data={"name": name, "password": password,
                                            "branch_id": str(branch_id)},
                follow_redirects=True)
    with app.app_context():
        return get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()["id"]


SECTION_RE = re.compile(
    r'<section class="([^"]*)">\s*'
    r'<div class="section-head"><h2 class="section-heading">([^<]*)</h2>'
)


def named_sections(html):
    """Every dashboard card section, in document order, as (classes, heading).

    Only sections built from the ``section-head`` / ``section-heading`` pair are
    picked up, so the metrics bars and the panels inside an include cannot be
    mistaken for one of the named sections.
    """
    return [(tuple(classes.split()), heading) for classes, heading in SECTION_RE.findall(html)]


def section_for(html, heading):
    for classes, text in named_sections(html):
        if text == heading:
            return classes
    raise AssertionError(f"no dashboard section headed {heading!r} in the rendered page")


def css_rules():
    """The ``app.css`` rules that style a dashboard section heading."""
    css = re.sub(r"/\*.*?\*/", "", CSS_PATH.read_text(encoding="utf-8"), flags=re.S)
    return {
        "dashboard_day_heading": re.search(
            r"\.dashboard-day \.section-heading\{([^}]*)\}", css),
        "card_section_heading": re.search(
            r"\.dashboard-card-section \.section-heading\{([^}]*)\}", css),
        "dashboard_day_selectors": re.findall(r"\.dashboard-day(?![\w-])[^{,]*", css),
    }


def dashboard_html(client, app, staff=None):
    if staff:
        login(client, app, staff, "staff123")
    else:
        login(client, app)
    response = client.get("/dashboard?branch=1")
    assert response.status_code == 200
    return response.data.decode()


# --------------------------------------------------------------------------- #
# 1. The heading treatment this ticket bought is still in force
# --------------------------------------------------------------------------- #

def test_the_day_sections_still_wear_the_one_shared_wrapper(client, app):
    """Today and Cash up · end of day still share the cash-up wrapper, class for class."""
    html = dashboard_html(client, app)
    today = section_for(html, "Today")
    cash = section_for(html, CASH_UP_HEADING)
    assert today == cash == ("dashboard-day", "dashboard-card-section")


def test_the_heading_style_the_cash_up_section_uses_is_still_15px_uppercase():
    """The CSS contract the wrapper class buys - pinned so it cannot drift."""
    rules = css_rules()
    day = rules["dashboard_day_heading"]
    assert day, "'.dashboard-day .section-heading' rule is missing from app.css"
    body = day.group(1)
    assert "font-size:15px" in body.replace(" ", "")
    assert "text-transform:uppercase" in body.replace(" ", "")

    # The card-section rule that both sections share only zeroes the margin, so
    # the type treatment can only come from `.dashboard-day`.
    card = rules["card_section_heading"]
    assert card, "'.dashboard-card-section .section-heading' rule is missing"
    assert "margin:0" in card.group(1).replace(" ", "")
    assert "font-size" not in card.group(1)
    assert "text-transform" not in card.group(1)

    # `.dashboard-day` is a bare class with exactly one rule, so adding it to a
    # day section cannot shift anything else on the dashboard.
    assert rules["dashboard_day_selectors"] == [".dashboard-day .section-heading"], (
        "'.dashboard-day' now has more than the .section-heading rule - "
        "re-check what else the day sections inherit"
    )


# --------------------------------------------------------------------------- #
# 2. The heading order, with the spare-wheel block no longer a named section
# --------------------------------------------------------------------------- #

def test_the_spare_wheel_block_is_no_longer_a_named_dashboard_section(client, app):
    html = dashboard_html(client, app)
    headings = [heading for _, heading in named_sections(html)]
    assert headings == ["Today", CASH_UP_HEADING, "Movement"]
    assert not [heading for heading in headings if re.search(r"spare", heading, re.I)]


def test_the_branch_limited_staff_order_drops_the_spare_wheel_section(client, app):
    add_staff(client, app, "Depot Two Staff", 2)
    html = dashboard_html(client, app, staff="Depot Two Staff")
    headings = [heading for _, heading in named_sections(html)]
    # No "Movement" block for a non-main account.
    assert headings == ["Today", CASH_UP_HEADING]


# --------------------------------------------------------------------------- #
# 3. The panel itself is still there, now riding inside the cash-up section
# --------------------------------------------------------------------------- #

def test_the_panel_still_renders_inside_the_cash_up_section(client, app):
    html = dashboard_html(client, app)
    cash = html.index(f'<h2 class="section-heading">{CASH_UP_HEADING}</h2>')
    movement = html.index('<h2 class="section-heading">Movement</h2>')
    panel = html.index('<section class="panel spare-wheel-panel">')
    assert cash < panel < movement, (
        "the spare-wheel panel should render inside the Cash up · end of day section"
    )
    assert 'action="/dashboard/spare-wheels"' in html


def test_a_branch_limited_staff_account_still_gets_the_panel(client, app):
    add_staff(client, app, "Depot Three Staff", 3)
    html = dashboard_html(client, app, staff="Depot Three Staff")
    assert '<section class="panel spare-wheel-panel">' in html
    assert 'action="/dashboard/spare-wheels"' in html


# --------------------------------------------------------------------------- #
# 4. Source guard
# --------------------------------------------------------------------------- #

def test_the_dashboard_template_now_carries_two_day_wrappers():
    """A cheap source-level guard beside the rendered assertions above."""
    template = DASHBOARD_TEMPLATE.read_text(encoding="utf-8")
    section_lines = [line for line in template.splitlines()
                     if "dashboard-card-section" in line]
    assert section_lines == [
        f'<section class="{DAY_WRAPPER}">',   # Today
        f'<section class="{DAY_WRAPPER}">',   # Cash up · end of day
        f'<section class="{PLAIN_WRAPPER}">',  # Movement keeps its plain wrapper
    ]
    # The spare-wheel block is no longer a section of its own here.
    assert "_dashboard_spare_wheels.html" not in template
    assert not re.search(r"spare[- ]wheel", template, re.I)


def test_the_panel_heading_is_the_only_wording_this_include_owns():
    """The include keeps its heading slot; ABI-341953036 changed only the words."""
    template = SPARE_TEMPLATE.read_text(encoding="utf-8")
    assert SPARE_OLD_WORDING not in template
    assert '<section class="panel spare-wheel-panel">' in template
    assert 'action="{{ url_for(\'admin.dashboard_spare_wheels\') }}"' in template
