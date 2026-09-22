"""Ticket ABI-341953035 - spare wheel count after cash up, heading like cash up.

Requested edit: "spare wheel count should be after cash up section and make the
heading similar to cash up".

Two halves, and only one of them needed code:

1. **Position** - already correct.  ``templates/admin/dashboard.html`` renders
   Today -> Cash up / end of day -> Spare Wheel Count -> Movement, so the
   spare-wheel section is already the very next section after the cash-up
   section.  These tests pin that order so a later edit cannot quietly move it.
2. **Heading** - the real defect.  The cash-up section is
   ``<section class="dashboard-day dashboard-card-section">`` and
   ``static/css/app.css`` styles ``.dashboard-day .section-heading`` at 15px,
   uppercase, muted.  The spare-wheel wrapper was only
   ``dashboard-card-section``, so its heading fell back to a default 21px dark
   ``h2`` - measured live: 21px ``rgb(18,23,34)`` against the cash-up heading's
   15px ``rgb(101,115,134)`` uppercase.  Giving the spare-wheel wrapper the
   exact same class list as the cash-up section is the whole fix; the visible
   text needs no edit because the CSS uppercases both.

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

CASH_UP_HEADING = "Cash up \u00b7 end of day"
SPARE_HEADING = "Spare Wheel Count"
CASH_UP_SECTION_CLASSES = ("dashboard-day", "dashboard-card-section")


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
# 1. The heading renders like the cash-up heading
# --------------------------------------------------------------------------- #

def test_the_spare_wheel_section_carries_the_cash_up_wrapper(client, app):
    """The whole fix: the spare-wheel wrapper is the cash-up wrapper, class for class."""
    html = dashboard_html(client, app)
    spare = section_for(html, SPARE_HEADING)
    cash = section_for(html, CASH_UP_HEADING)
    assert spare == cash == CASH_UP_SECTION_CLASSES
    assert "dashboard-day" in spare


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

    # `.dashboard-day` is a bare class with exactly one rule, so adding it to
    # the spare-wheel section cannot shift anything else on the dashboard.
    assert rules["dashboard_day_selectors"] == [".dashboard-day .section-heading"], (
        "'.dashboard-day' now has more than the .section-heading rule - "
        "re-check what else the spare-wheel section inherits"
    )


def test_the_spare_wheel_heading_text_is_unchanged(client, app):
    """The text stays as it was committed for ABI-341953033 (the CSS uppercases it)."""
    html = dashboard_html(client, app)
    assert f">{SPARE_HEADING}</h2>" in html
    assert html.count(SPARE_HEADING) == 1
    # Still the panel's own content, not a re-labelled cash-up panel.
    assert 'action="/dashboard/spare-wheels"' in html


# --------------------------------------------------------------------------- #
# 2. The position - already right, pinned so it stays right
# --------------------------------------------------------------------------- #

def test_the_spare_wheel_section_comes_straight_after_the_cash_up_section(client, app):
    html = dashboard_html(client, app)
    headings = [heading for _, heading in named_sections(html)]
    assert headings == ["Today", CASH_UP_HEADING, SPARE_HEADING, "Movement"]
    assert headings.index(SPARE_HEADING) == headings.index(CASH_UP_HEADING) + 1


def test_the_section_order_is_unchanged_for_a_branch_limited_staff_account(client, app):
    add_staff(client, app, "Depot Two Staff", 2)
    html = dashboard_html(client, app, staff="Depot Two Staff")
    headings = [heading for _, heading in named_sections(html)]
    # No "Movement" block for a non-main account; everything else keeps its order.
    assert headings == ["Today", CASH_UP_HEADING, SPARE_HEADING]
    assert section_for(html, SPARE_HEADING) == CASH_UP_SECTION_CLASSES


def test_the_panel_stays_inside_the_spare_wheel_section(client, app):
    """The include must not be split away from its heading by the class change."""
    html = dashboard_html(client, app)
    section = html.split(f'<h2 class="section-heading">{SPARE_HEADING}</h2>', 1)[1]
    section = section.split("</section>", 1)[0]
    assert 'action="/dashboard/spare-wheels"' in section


def test_the_dashboard_template_carries_the_wrapper_attribute():
    """A cheap source-level guard beside the rendered assertions above."""
    template = DASHBOARD_TEMPLATE.read_text(encoding="utf-8")
    block = template.split(f'<h2 class="section-heading">{SPARE_HEADING}</h2>', 1)[0]
    wrapper, head_open = block.rstrip().splitlines()[-2:]
    assert head_open == '  <div class="section-head">'
    assert wrapper == '<section class="dashboard-day dashboard-card-section">'

    # Today first, then Cash up, then Spare Wheel (all three now carry the same
    # "for the day" wrapper); Movement keeps its own plain wrapper - the ticket
    # did not ask for it and it must not be dragged along by accident.
    section_lines = [line for line in template.splitlines()
                     if "dashboard-card-section" in line]
    assert section_lines == [
        '<section class="dashboard-day dashboard-card-section">',
        '<section class="dashboard-day dashboard-card-section">',
        '<section class="dashboard-day dashboard-card-section">',
        '<section class="dashboard-card-section">',
    ]
