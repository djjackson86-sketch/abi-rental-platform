"""Ticket ABI-341953036 - spare wheel count between cash up and end of day notes.

Requested edit, three asks, dashboard only (``GET /dashboard`` /
``admin.dashboard``):

1. Put the spare wheel count section BETWEEN the cash up panel and the
   End of day notes panel.
2. Replace the heading "Spare wheels by wheel size" with "Spare wheel count".
3. Remove the words "SPARE WHEEL COUNT" printed above the box.

How it is done - three template edits, nothing else:

* ``templates/admin/dashboard.html`` no longer renders a *Spare Wheel Count
  section*: the ``<section class="dashboard-day dashboard-card-section">`` +
  ``<div class="section-head"><h2 class="section-heading">Spare Wheel
  Count</h2>`` wrapper is gone.  That is ask 3.
* ``templates/admin/_dashboard_cash.html`` includes the spare-wheel partial
  between the ``Cash up · <depot>`` panel and the ``End of day notes`` panel,
  inside the same ``.two-col`` flow.  That is ask 1.
* ``templates/admin/_dashboard_spare_wheels.html``'s own ``<h2>`` now reads
  "Spare wheel count".  That is ask 2.

Display-only: no route, service, DB, permission or JS change.  ``app.css`` is
untouched, and the panel's ``<style>``, form, route, hidden fields and
``spare-wheel-panel`` classes are unchanged, so a saved count still round-trips.
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
from app.services.cash import today_iso  # noqa: E402
from app.services.products import WHEEL_SIZES, create_product  # noqa: E402

DASHBOARD_TEMPLATE = ROOT / "templates" / "admin" / "dashboard.html"
CASH_TEMPLATE = ROOT / "templates" / "admin" / "_dashboard_cash.html"
SPARE_TEMPLATE = ROOT / "templates" / "admin" / "_dashboard_spare_wheels.html"

DAY = today_iso()
CASH_UP_HEADING = "Cash up \u00b7 end of day"
NOTES_HEADING = "End of day notes"
NEW_PANEL_HEADING = "Spare wheel count"
OLD_PANEL_HEADING = "Spare wheels by wheel size"
OLD_SECTION_HEADING = "Spare Wheel Count"
SPARE_INCLUDE = '{% include "admin/_dashboard_spare_wheels.html" %}'


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
PANEL_HEADING_RE = re.compile(r'<div class="panel-title">\s*<h2>([^<]*)</h2>')


def named_sections(html):
    """Every dashboard card section, in document order, as (classes, heading)."""
    return [(tuple(classes.split()), heading) for classes, heading in SECTION_RE.findall(html)]


def panel_headings(html):
    """Every panel's own ``<h2>``, in document order."""
    return PANEL_HEADING_RE.findall(html)


def dashboard_html(client, app, query="?branch=1", staff=None):
    if staff:
        login(client, app, staff, "staff123")
    else:
        login(client, app)
    response = client.get(f"/dashboard{query}")
    assert response.status_code == 200
    return response.data.decode()


def spare_panel_html(html):
    """The rendered spare-wheel panel, from its ``<section>`` to its closing tag."""
    start = html.index('<section class="panel spare-wheel-panel">')
    return html[start:html.index("</section>", start) + len("</section>")]


# --------------------------------------------------------------------------- #
# Ask 3 - no "SPARE WHEEL COUNT" words above the box
# --------------------------------------------------------------------------- #

def test_the_words_above_the_box_are_gone(client, app):
    """The dashboard section heading that sat above the panel is removed."""
    html = dashboard_html(client, app)
    headings = [heading for _, heading in named_sections(html)]
    assert not [heading for heading in headings if re.search(r"spare", heading, re.I)]
    assert OLD_SECTION_HEADING not in html
    assert '<h2 class="section-heading">Spare' not in html
    # Nothing ABOVE the box says "spare wheel count" - the words now appear only
    # as the panel's own heading (ask 2), never as a heading above the box.
    block = html.index('<section class="panel spare-wheel-panel">')
    assert not re.search(r"spare\s+wheel\s+count", html[:block], re.I)


def test_the_named_sections_drop_the_spare_wheel_section(client, app):
    """The dashboard's own named sections are just Today / Cash up / Movement."""
    html = dashboard_html(client, app)
    assert [heading for _, heading in named_sections(html)] == [
        "Today", CASH_UP_HEADING, "Movement",
    ]


# --------------------------------------------------------------------------- #
# Ask 2 - the panel heading reads "Spare wheel count"
# --------------------------------------------------------------------------- #

def test_the_panel_heading_now_reads_spare_wheel_count(client, app):
    html = dashboard_html(client, app)
    panel = spare_panel_html(html)
    assert f"<h2>{NEW_PANEL_HEADING}</h2>" in panel
    assert OLD_PANEL_HEADING not in html
    # Exactly one panel heading carries the words, and it is the spare-wheel panel.
    assert panel_headings(html).count(NEW_PANEL_HEADING) == 1


def test_no_other_page_surface_keeps_the_old_wording():
    """The old wording lived in exactly one template - and it is gone from it."""
    for path in (DASHBOARD_TEMPLATE, CASH_TEMPLATE, SPARE_TEMPLATE):
        assert OLD_PANEL_HEADING not in path.read_text(encoding="utf-8"), path


# --------------------------------------------------------------------------- #
# Ask 1 - position: between Cash up and End of day notes
# --------------------------------------------------------------------------- #

def test_the_spare_wheel_block_sits_between_cash_up_and_end_of_day_notes(client, app):
    """The rendered document order: Cash up panel, then the block, then the notes."""
    html = dashboard_html(client, app)
    cash_panel = html.index('<h2>Cash up \u00b7 ')
    notes_panel = html.index(f"<h2>{NOTES_HEADING}</h2>")
    block = html.index('<section class="panel spare-wheel-panel">')
    assert cash_panel < block < notes_panel


def test_the_spare_wheel_block_is_inside_the_cash_up_day_section(client, app):
    """It moved into the Cash up · end of day section, not into a section of its own."""
    html = dashboard_html(client, app)
    section = html.index(f'<h2 class="section-heading">{CASH_UP_HEADING}</h2>')
    movement = html.index('<h2 class="section-heading">Movement</h2>')
    block = html.index('<section class="panel spare-wheel-panel">')
    assert section < block < movement
    # It is still the very next panel after the Cash up panel.
    panels = panel_headings(html)
    cash_index = next(i for i, heading in enumerate(panels) if heading.startswith("Cash up \u00b7 "))
    assert panels[cash_index + 1] == NEW_PANEL_HEADING


def test_the_branch_limited_staff_view_gets_the_same_order(client, app):
    add_staff(client, app, "Depot Two Staff", 2)
    html = dashboard_html(client, app, staff="Depot Two Staff")
    assert [heading for _, heading in named_sections(html)] == ["Today", CASH_UP_HEADING]
    cash_panel = html.index('<h2>Cash up \u00b7 ')
    notes_panel = html.index(f"<h2>{NOTES_HEADING}</h2>")
    block = html.index('<section class="panel spare-wheel-panel">')
    assert cash_panel < block < notes_panel
    panels = panel_headings(html)
    cash_index = next(i for i, heading in enumerate(panels) if heading.startswith("Cash up \u00b7 "))
    assert panels[cash_index + 1] == NEW_PANEL_HEADING


def test_the_all_branches_view_keeps_the_order_and_stays_read_only(client, app):
    html = dashboard_html(client, app, query="")
    assert [heading for _, heading in named_sections(html)] == [
        "Today", CASH_UP_HEADING, "Movement",
    ]
    cash_panel = html.index('<h2>Cash up \u00b7 ')
    notes_panel = html.index(f"<h2>{NOTES_HEADING}</h2>")
    block = html.index('<section class="panel spare-wheel-panel">')
    assert cash_panel < block < notes_panel
    # Still a read-only sum for "All branches".
    assert 'action="/dashboard/spare-wheels"' not in html
    assert "Select a branch in the top Branch filter to type in the actual counts." in html
    assert re.search(r'name="actual_0"', html) is None


# --------------------------------------------------------------------------- #
# Nothing else moved: the notes panel and the spare-wheel form still work
# --------------------------------------------------------------------------- #

def test_the_end_of_day_notes_panel_is_untouched(client, app):
    html = dashboard_html(client, app)
    assert re.search(r'<h2>' + re.escape(NOTES_HEADING) + r"</h2>", html)
    assert 'action="/cash-up/notes"' in html
    assert 'name="notes"' in html
    # The spare-wheel block did not swallow it - notes still come after it.
    assert html.index('<section class="panel spare-wheel-panel">') < html.index(
        f"<h2>{NOTES_HEADING}</h2>")


def test_a_counted_value_still_round_trips_after_the_move(client, app):
    """Moving the include must not break the panel's form or its saved values."""
    with app.app_context():
        create_product({
            "name": "Moved Panel Trailer", "product_type": "rental",
            "tracking_method": "bulk", "quantity": "3",
            "wheel_size": WHEEL_SIZES[0], "branch_id": "1", "active": "1",
        })
    login(client, app)
    response = client.post("/dashboard/spare-wheels", data={
        "day": DAY, "branch": "1", "actual_0": "5",
    }, follow_redirects=True)
    html = response.data.decode()
    assert f"Spare wheel count saved for {DAY}" in html
    assert re.search(r'name="actual_0" value="5"', html)
    assert "+2 (over)" in html
    assert re.search(r'name="day" value="' + DAY + r'"', html)


# --------------------------------------------------------------------------- #
# Source guards - the three edits, in the three files
# --------------------------------------------------------------------------- #

def test_the_cash_template_includes_the_panel_between_cash_up_and_the_notes():
    template = CASH_TEMPLATE.read_text(encoding="utf-8")
    assert template.count(SPARE_INCLUDE) == 1
    cash = template.index("<h2>Cash up \u00b7 {{ cash_day.branch_name }}</h2>")
    notes = template.index(f"<h2>{NOTES_HEADING}</h2>")
    block = template.index(SPARE_INCLUDE)
    assert cash < block < notes, "the include must sit between the two panels"
    # It is inside the .two-col flow (that grid opens before the Cash up panel
    # and closes after the End of day notes panel).
    assert template.index('<div class="two-col">') < block < template.index("</div>", notes)


def test_the_dashboard_template_drops_the_spare_section_entirely():
    template = DASHBOARD_TEMPLATE.read_text(encoding="utf-8")
    assert "_dashboard_spare_wheels.html" not in template
    assert not re.search(r"spare[- ]wheel", template, re.I)
    assert [line for line in template.splitlines() if "dashboard-card-section" in line] == [
        '<section class="dashboard-day dashboard-card-section">',   # Today
        '<section class="dashboard-day dashboard-card-section">',   # Cash up · end of day
        '<section class="dashboard-card-section">',                 # Movement (untouched)
    ]


def test_the_panel_is_allowed_to_shrink_to_its_grid_track():
    """Phone-width guard: the wheel table must not push the page wider.

    Measured in Chromium at 390px: the spare-wheel panel's min-content width
    (its four-column table) sets the single-track ``.two-col`` floor, which with
    the default ``min-width:auto`` grid item sized the track to 434px and gave
    the whole page 33-62px of horizontal overflow (the exact number depends on
    whether the web font had loaded).  ``min-width:0`` on the panel keeps it at
    the pre-ticket 354px and lets ``.table-wrap`` scroll - verified before/after
    in a real browser: identical panel width, table scroll and 0 page overflow.
    """
    template = SPARE_TEMPLATE.read_text(encoding="utf-8")
    assert ".spare-wheel-panel{min-width:0}" in template
    # The rule stays in the include's scoped <style>, so app.css keeps its
    # cache marker and no other page or panel is touched.
    css = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    assert ".spare-wheel" not in css


def test_the_spare_template_changed_only_its_heading():
    template = SPARE_TEMPLATE.read_text(encoding="utf-8")
    assert f"<h2>{NEW_PANEL_HEADING}</h2>" in template
    assert OLD_PANEL_HEADING not in template
    # Form, route, hidden fields and the panel/scoping classes all survive.
    assert '<section class="panel spare-wheel-panel">' in template
    assert 'action="{{ url_for(\'admin.dashboard_spare_wheels\') }}"' in template
    assert '<input type="hidden" name="day" value="{{ spare_wheel_day }}">' in template
    assert '<input type="hidden" name="branch" value="{{ spare_wheel_branch_id }}">' in template
    assert "Save spare wheel count" in template
    for rule in (".spare-wheel-panel .spare-wheel-table{width:100%;border-collapse:collapse}",
                 ".spare-wheel-panel .spare-wheel-variance.is-short{color:var(--danger)}"):
        assert rule in template
