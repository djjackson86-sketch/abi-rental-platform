"""Ticket ABI-341953042 - day report + the dashboard's business day.

Three asks:

1. the **spare wheel count** must be part of the dashboard day report (PDF and
   the CSV it shares its rows with);
2. on a new day staff must still see the **previous day's notes**;
3. the **main profile dashboard** must read every day-scoped panel for a chosen
   date - new client interactions, trailer service and maintenance, cash used,
   bank drop offs, cash up, spare wheel count and end of day notes - and offer
   the day report download for that day.

What is pinned here:

* ``cash.day_report_rows`` (the one source of truth for the PDF *and* the CSV)
  carries the five wheel sizes with Expected / Counted / difference, and the
  PDF draws that section between the cash panels and the end of day notes.
* ``cash.previous_day_notes`` is depot-scoped, skips days with nothing written,
  and is rendered read-only, only while the day being viewed has no notes.
* ``admin.dashboard`` follows ``?day=`` for the main profile (junk falls back to
  today), day-scoped panels go read-only for a past day, and a non-main sign-in
  stays pinned to today.
* ``trailer_service.services_for_day`` only returns that depot's that day's
  lines.
"""

import csv
import io
import os
import re
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.db import get_db  # noqa: E402
from app.services import cash, spare_wheels, trailer_service  # noqa: E402
from app.services.access import MODULE_KEYS  # noqa: E402
from app.services.products import WHEEL_SIZES, create_product  # noqa: E402

TODAY = cash.today_iso()
PAST = (date.fromisoformat(TODAY) - timedelta(days=2)).isoformat()
YESTERDAY = (date.fromisoformat(TODAY) - timedelta(days=1)).isoformat()
OLDER = (date.fromisoformat(TODAY) - timedelta(days=5)).isoformat()
FUTURE = (date.fromisoformat(TODAY) + timedelta(days=1)).isoformat()

SIZE_A, SIZE_B = WHEEL_SIZES[0], WHEEL_SIZES[1]
NOTE = 'Yard swept, two straps replaced.'
INTERACTION_NOTE = 'Two quote follow-ups needed.'

_TEXT_RUN = re.compile(r'/(F\d) ([\d.]+) Tf 1 0 0 1 ([\d.]+) ([\d.]+) Tm \(((?:[^()\\]|\\.)*)\) Tj')


def _drawn(pdf_bytes):
    """The text runs the report actually draws, in draw order."""
    return [text.replace('\\(', '(').replace('\\)', ')')
            for _font, _size, _x, _y, text in _TEXT_RUN.findall(pdf_bytes.decode('latin-1'))]


def _pages(pdf_bytes):
    return pdf_bytes.decode('latin-1').count('/Type /Page ')


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


def login(client, app, name=None, password='admin123'):
    with client.application.app_context():
        if name is None:
            row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
        else:
            row = get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
    return client.post('/login', data={'user_id': str(row['id']), 'password': password},
                       follow_redirects=True)


def add_staff(client, app, name, branch_id, password='staff123'):
    login(client, app)
    client.post('/settings/users/permissions', data={'module': list(MODULE_KEYS)}, follow_redirects=True)
    client.post('/settings/users/add', data={'name': name, 'password': password,
                                            'branch_id': str(branch_id)}, follow_redirects=True)
    with app.app_context():
        return get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()['id']


def seed_trailer(app, name, quantity=3, wheel_size=SIZE_A, branch_id=1):
    with app.app_context():
        product = create_product({
            'name': name, 'product_type': 'rental', 'tracking_method': 'bulk',
            'quantity': str(quantity), 'wheel_size': wheel_size,
            'branch_id': str(branch_id), 'active': '1',
        })
        row = get_db().execute('SELECT id FROM products WHERE name = ?', (name,)).fetchone()
    return int(row['id'])


def seed_day(app, day, branch_id=1, counted='840.00', notes='', used=None, drops=(), interactions=None):
    """Write a complete business day straight through the cash service.

    The cash up is written first on purpose: ``save_cash_up`` owns the row's
    ``notes`` column (exactly as the route does), so notes come after it.
    """
    with app.app_context():
        if counted is not None:
            cash.save_cash_up(day, counted, branch_id=branch_id)
        for amount, description in (used or []):
            cash.add_cash_used(day, amount, description, branch_id=branch_id)
        for amount in drops:
            cash.add_bank_drop(day, amount, branch_id=branch_id)
        if interactions:
            cash.save_interactions(day, *interactions, branch_id=branch_id)
        if notes:
            cash.save_notes(day, notes, branch_id=branch_id)


def dashboard(client, app, query='', staff=None):
    if staff:
        login(client, app, staff, 'staff123')
    else:
        login(client, app)
    response = client.get(f'/dashboard{query}')
    assert response.status_code == 200
    return response.get_data(as_text=True)


def sections(html):
    """The dashboard's named card sections, in document order."""
    return re.findall(r'<div class="section-head"><h2 class="section-heading">([^<]*)</h2>', html)


def previous_notes_block(html):
    start = html.index('Previous day')
    return html[start:html.index('</section>', start)]


# --------------------------------------------------------------------------- #
# Ask 1 - spare wheel count is part of the day report
# --------------------------------------------------------------------------- #

def test_the_day_report_rows_carry_the_spare_wheel_count(app):
    seed_trailer(app, 'Yard trailer', quantity=3, wheel_size=SIZE_A)
    seed_trailer(app, 'Second trailer', quantity=2, wheel_size=SIZE_B)
    with app.app_context():
        spare_wheels.save_actual_counts(PAST, 1, {SIZE_A: 4, SIZE_B: 2})
        seed_day(app, PAST, notes=NOTE)
        report = cash.day_report(day=PAST, branch_id=1)
        rows = cash.day_report_rows(report)

    spare = [row for row in rows if row[0] == 'Spare wheel count']
    assert [row[1] for row in spare] == list(WHEEL_SIZES)
    by_size = {row[1]: row[2] for row in spare}
    assert by_size[SIZE_A] == 'Expected 3 · Counted 4 · +1 over'
    assert by_size[SIZE_B] == 'Expected 2 · Counted 2 · matches'
    # A size nobody counted says so instead of pretending a zero was counted.
    assert by_size[WHEEL_SIZES[2]] == 'Expected 0 · Not counted yet'


def test_the_csv_export_carries_the_spare_wheel_count(client, app):
    seed_trailer(app, 'Yard trailer', quantity=3, wheel_size=SIZE_A)
    with app.app_context():
        spare_wheels.save_actual_counts(PAST, 1, {SIZE_A: 1})
    login(client, app)
    body = client.get(f'/cash-up/export.csv?branch=1&day={PAST}').get_data(as_text=True)
    # Parsed, not substring-matched: the wheel size carries an inch mark, so the
    # CSV field is quoted (a raw substring assert would false-fail).
    rows = [row for row in csv.reader(io.StringIO(body)) if row and row[0] == 'Spare wheel count']
    assert rows == [
        ['Spare wheel count', SIZE_A, 'Expected 3 · Counted 1 · 2 short'],
        ['Spare wheel count', SIZE_B, 'Expected 0 · Not counted yet'],
        ['Spare wheel count', WHEEL_SIZES[2], 'Expected 0 · Not counted yet'],
        ['Spare wheel count', WHEEL_SIZES[3], 'Expected 0 · Not counted yet'],
        ['Spare wheel count', WHEEL_SIZES[4], 'Expected 0 · Not counted yet'],
    ]


def test_the_pdf_draws_the_spare_wheel_section_between_cash_and_the_notes(client, app):
    seed_trailer(app, 'Yard trailer', quantity=3, wheel_size=SIZE_A)
    with app.app_context():
        spare_wheels.save_actual_counts(PAST, 1, {SIZE_A: 4})
        seed_day(app, PAST, notes=NOTE)
    login(client, app)
    response = client.get(f'/cash-up/report.pdf?branch=1&day={PAST}')
    assert response.status_code == 200
    assert response.data.startswith(b'%PDF-')
    drawn = _drawn(response.data)
    assert 'SPARE WHEEL COUNT' in drawn
    assert drawn.index('CASH DROP OFF (TO BANK)') < drawn.index('SPARE WHEEL COUNT')
    assert drawn.index('SPARE WHEEL COUNT') < drawn.index('END OF DAY NOTES')
    # Every wheel size is printed with its own reading, and the note is not cut.
    for size in WHEEL_SIZES:
        assert size in drawn, size
    assert 'Expected 3 · Counted 4 · +1 over' in drawn
    assert 'Expected 0 · Not counted yet' in drawn
    assert NOTE in drawn


def test_the_report_spare_wheels_follow_the_dashboard_panel_exactly(client, app):
    """One definition: the PDF, the CSV and the panel read the same rows."""
    seed_trailer(app, 'Yard trailer', quantity=3, wheel_size=SIZE_A)
    with app.app_context():
        spare_wheels.save_actual_counts(PAST, 1, {SIZE_A: 4})
        panel = spare_wheels.spare_wheel_rows(PAST, branch_id=1, editable=True)
        report = cash.day_report(day=PAST, branch_id=1)
        rows = {row[1]: row[2] for row in cash.day_report_rows(report) if row[0] == 'Spare wheel count'}

    counted = [row for row in panel if row['counted']]
    assert counted, 'the seed must produce at least one counted size'
    assert rows[SIZE_A] == 'Expected 3 · Counted 4 · +1 over'
    # Uncounted sizes read the same way in both places.
    assert rows[SIZE_B] == 'Expected 0 · Not counted yet'


def test_the_report_spare_wheels_are_per_depot(client, app):
    seed_trailer(app, 'Depot one trailer', quantity=3, wheel_size=SIZE_A, branch_id=1)
    seed_trailer(app, 'Depot two trailer', quantity=5, wheel_size=SIZE_A, branch_id=2)
    with app.app_context():
        spare_wheels.save_actual_counts(PAST, 1, {SIZE_A: 4})
        spare_wheels.save_actual_counts(PAST, 2, {SIZE_A: 9})
        first = {row[1]: row[2] for row in cash.day_report_rows(cash.day_report(day=PAST, branch_id=1))
                 if row[0] == 'Spare wheel count'}
        second = {row[1]: row[2] for row in cash.day_report_rows(cash.day_report(day=PAST, branch_id=2))
                  if row[0] == 'Spare wheel count'}
    assert first[SIZE_A] == 'Expected 3 · Counted 4 · +1 over'
    assert second[SIZE_A] == 'Expected 5 · Counted 9 · +4 over'


def test_the_day_report_carries_the_days_trailer_service_lines(client, app):
    """Ask 3's service list reaches the same report as the rest of the day."""
    product_id = seed_trailer(app, 'Service trailer')
    with app.app_context():
        trailer_service.create_service_history(product_id, 'Tyre change', branch_id=1, service_date=PAST)
        report = cash.day_report(day=PAST, branch_id=1)
        rows = [row for row in cash.day_report_rows(report) if row[0] == 'Trailer service and maintenance']
    assert rows == [('Trailer service and maintenance', 'Service trailer', 'Tyre change')]


# --------------------------------------------------------------------------- #
# Ask 2 - previous day notes on a new day
# --------------------------------------------------------------------------- #

def test_previous_day_notes_returns_the_last_day_that_has_notes(app):
    seed_day(app, OLDER, notes='Older note')
    seed_day(app, PAST, notes=NOTE)
    seed_day(app, YESTERDAY, notes='')  # a day with nothing written is skipped
    with app.app_context():
        previous = cash.previous_day_notes(TODAY, 1)
    assert previous['day'] == PAST
    assert previous['notes'] == NOTE
    assert previous['branch_id'] == 1


def test_previous_day_notes_are_depot_scoped(app):
    seed_day(app, PAST, notes='Depot one note', branch_id=1)
    seed_day(app, PAST, notes='Depot two note', branch_id=2)
    with app.app_context():
        assert cash.previous_day_notes(TODAY, 1)['notes'] == 'Depot one note'
        assert cash.previous_day_notes(TODAY, 2)['notes'] == 'Depot two note'
        assert cash.previous_day_notes(TODAY, 3) is None
        assert cash.previous_day_notes(TODAY, None) is None
        # Nothing earlier at all -> nothing to show.
        assert cash.previous_day_notes(OLDER, 1) is None


def test_previous_day_notes_can_come_from_the_interaction_notes(app):
    seed_day(app, PAST, notes='', interactions=(1, 2, 3, 4, INTERACTION_NOTE))
    with app.app_context():
        previous = cash.previous_day_notes(TODAY, 1)
    assert previous['notes'] == ''
    assert previous['interaction_notes'] == INTERACTION_NOTE


def test_the_dashboard_shows_the_previous_days_notes_to_staff(client, app):
    """A new day: nothing written yet, so the last recorded notes are shown."""
    seed_day(app, PAST, notes=NOTE, interactions=(1, 2, 3, 4, INTERACTION_NOTE))
    add_staff(client, app, 'Depot One Staff', 1)
    html = dashboard(client, app, staff='Depot One Staff')

    assert f'Previous day’s notes ({PAST})' in html
    block = previous_notes_block(html)
    assert NOTE in block
    assert INTERACTION_NOTE in block
    # Read-only: the earlier day cannot be edited from here.
    assert '<form' not in block
    # The panel says why it is there: the day being viewed is still empty.
    assert f'Nothing has been recorded for {TODAY} yet' in block
    assert 'End of day notes' in html
    # Today is an open day: the notes panel offers its own save form.
    assert 'action="/cash-up/notes"' in html


def test_the_previous_day_notes_panel_goes_away_once_today_has_notes(client, app):
    seed_day(app, PAST, notes=NOTE)
    add_staff(client, app, 'Depot One Staff', 1)
    html = dashboard(client, app, staff='Depot One Staff')
    assert 'Previous day’s notes' in html

    seed_day(app, TODAY, notes='Today is written up')
    html = dashboard(client, app, staff='Depot One Staff')
    assert 'Previous day’s notes' not in html
    assert 'Today is written up' in html


def test_another_depots_notes_are_never_shown_as_previous_day_notes(client, app):
    seed_day(app, PAST, notes='Depot one note', branch_id=1)
    add_staff(client, app, 'Depot Two Staff', 2)
    html = dashboard(client, app, staff='Depot Two Staff')
    assert 'Previous day’s notes' not in html
    assert 'Depot one note' not in html


def test_the_all_branches_view_does_not_guess_at_previous_day_notes(client, app):
    seed_day(app, PAST, notes=NOTE)
    html = dashboard(client, app)
    assert 'Previous day’s notes' not in html


# --------------------------------------------------------------------------- #
# Ask 3 - the main profile dashboard follows the selected business day
# --------------------------------------------------------------------------- #

def test_the_main_profile_sees_the_selected_days_panels(client, app):
    seed_trailer(app, 'Yard trailer', quantity=3, wheel_size=SIZE_A)
    seed_day(app, PAST, notes=NOTE, used=[('120.50', 'Diesel for the bakkie')], drops=['300.00'],
             interactions=(7, 3, 1, 2, INTERACTION_NOTE))
    seed_day(app, YESTERDAY, notes='Yesterday note', used=[('11.00', 'Yesterday spend')])
    with app.app_context():
        spare_wheels.save_actual_counts(PAST, 1, {SIZE_A: 4})
    product_id = seed_trailer(app, 'Serviced trailer')

    login(client, app)
    client.post('/cash-up/trailer-service', data={
        'day': PAST, 'branch': '1', 'product_id': str(product_id), 'service_type': 'Tyre change',
    }, follow_redirects=True)
    # Today's own, different figures must not leak into the past day's view.
    client.post('/cash-up/used', data={'day': '', 'branch': '1', 'amount': '9.99',
                                       'description': 'Today spend'}, follow_redirects=True)

    html = dashboard(client, app, query=f'?branch=1&day={PAST}')
    assert sections(html)[0] == f'Business day {PAST}'
    # Cash up / cash used / bank drop off / notes for the selected day.
    assert 'Diesel for the bakkie' in html
    assert 'R120.50' in html
    assert 'R300.00' in html
    assert NOTE in html
    assert 'Today spend' not in html
    assert 'Yesterday note' not in html
    # New client interactions for the selected day.
    assert INTERACTION_NOTE in html
    assert 'name="interaction_calls"' not in html
    # Trailer service and maintenance for the selected day.
    assert 'Serviced trailer' in html
    assert 'Tyre change' in html
    # Spare wheel count for the selected day.
    assert 'name="actual_0" value="4"' not in html  # read-only view
    assert 'Not counted yet' in html


def test_a_selected_past_day_is_read_only(client, app):
    seed_day(app, PAST, notes=NOTE, used=[('120.50', 'Diesel')], drops=['300.00'])
    login(client, app)
    html = dashboard(client, app, query=f'?branch=1&day={PAST}')
    # No write path is offered for a day that is not today.
    for action in ('action="/cash-up"', 'action="/cash-up/notes"', 'action="/cash-up/used"',
                   'action="/cash-up/bank"', 'action="/cash-up/interactions"',
                   'action="/cash-up/trailer-service"', 'action="/dashboard/spare-wheels"',
                   'Submit day report'):
        assert action not in html, action
    assert 'This business day is in the past' in html
    # The downloads are still there, and they point at the selected day.
    assert f'day={PAST}' in html
    assert 'Download day report (PDF)' in html
    assert 'Download CSV' in html


def test_today_still_renders_its_write_forms(client, app):
    """The read-only rule is scoped to other days: today is untouched."""
    login(client, app)
    html = dashboard(client, app, query='?branch=1')
    assert sections(html)[0] == 'Today'
    for action in ('action="/cash-up"', 'action="/cash-up/notes"', 'action="/cash-up/used"',
                   'action="/cash-up/bank"', 'action="/cash-up/interactions"',
                   'action="/cash-up/trailer-service"', 'action="/dashboard/spare-wheels"'):
        assert action in html, action
    assert 'Submit day report' in html


def test_a_junk_or_missing_day_falls_back_to_today(client, app):
    login(client, app)
    for query in ('?branch=1', '?branch=1&day=', '?branch=1&day=junk', '?branch=1&day=2026-13-45'):
        html = dashboard(client, app, query=query)
        assert sections(html)[0] == 'Today', query
        assert 'action="/cash-up"' in html, query


def test_a_future_day_is_read_only_and_never_offered_as_editable(client, app):
    login(client, app)
    html = dashboard(client, app, query=f'?branch=1&day={FUTURE}')
    assert sections(html)[0] == f'Business day {FUTURE}'
    assert 'action="/cash-up"' not in html
    assert 'action="/dashboard/spare-wheels"' not in html


def test_only_the_main_profile_gets_the_business_day_picker(client, app):
    add_staff(client, app, 'Depot One Staff', 1)
    main_html = dashboard(client, app, query='?branch=1')
    staff_html = dashboard(client, app, staff='Depot One Staff')
    assert '<input type="date" name="day"' in main_html and 'Business day' in main_html
    assert '<input type="date" name="day"' not in staff_html
    assert 'Business day' not in staff_html
    # Staff still carry the day in the panels' own hidden fields (unchanged).
    assert 'name="day"' in staff_html


def test_a_staff_sign_in_stays_pinned_to_today(client, app):
    seed_day(app, PAST, notes=NOTE, used=[('120.50', 'Diesel for the bakkie')])
    add_staff(client, app, 'Depot One Staff', 1)
    html = dashboard(client, app, query=f'?day={PAST}', staff='Depot One Staff')
    assert sections(html)[0] == 'Today'
    assert 'Diesel for the bakkie' not in html
    assert 'Previous day’s notes' in html
    # Their own depot's day still writes normally.
    assert 'action="/cash-up"' in html


def test_the_selected_days_report_download_is_named_for_that_day(client, app):
    seed_day(app, PAST, notes=NOTE, used=[('120.50', 'Diesel for the bakkie')])
    login(client, app)
    pdf = client.get(f'/cash-up/report.pdf?branch=1&day={PAST}')
    assert pdf.status_code == 200
    expected = f'Branch 1_Dashboard Report_{PAST}.pdf'
    assert expected in pdf.headers['Content-Disposition']
    drawn = _drawn(pdf.data)
    assert f'Business day: {PAST}' in drawn
    assert 'Diesel for the bakkie' in drawn
    csv_body = client.get(f'/cash-up/export.csv?branch=1&day={PAST}').get_data(as_text=True)
    assert f'Report,Business day,{PAST}' in csv_body
    assert 'Diesel for the bakkie,R120.50' in csv_body


def test_the_download_links_on_the_dashboard_point_at_the_selected_day(client, app):
    login(client, app)
    html = dashboard(client, app, query=f'?branch=1&day={PAST}')
    assert f'/cash-up/report.pdf?day={PAST}&amp;branch=1' in html
    assert f'/cash-up/export.csv?day={PAST}&amp;branch=1' in html


def test_a_past_day_spare_wheel_panel_is_that_depots_own_read_only_view(client, app):
    """Read-only must not silently become the all-branches sum.

    Found by looking at the rendered page: forcing the panel non-editable for a
    past day must not fall back to the aggregated view, or the depot would read
    another depot's yard (ticket ABI-341953042).
    """
    seed_trailer(app, 'Depot one trailer', quantity=3, wheel_size=SIZE_A, branch_id=1)
    seed_trailer(app, 'Depot two trailer', quantity=5, wheel_size=SIZE_B, branch_id=2)
    with app.app_context():
        spare_wheels.save_actual_counts(PAST, 1, {SIZE_A: 4})
        spare_wheels.save_actual_counts(PAST, 2, {SIZE_B: 9})
    login(client, app)
    html = dashboard(client, app, query=f'?branch=1&day={PAST}')
    start = html.index('spare-wheel-panel')
    panel = html[start:html.index('</section>', start)]
    assert 'data-expected="3"' in panel, "the depot's own yard is in the view"
    assert 'data-expected="5"' not in panel, "another depot's trailers must not leak in"
    assert re.search(r'class="spare-wheel-variance is-over"[^>]*>\s*\+1 \(over\)', panel)
    assert 'value="4"' not in panel, 'a past day offers no inputs'
    assert f'Branch 1 · {PAST}' in panel, 'the pill names the depot, not "All branches"'
    # One query covers it: the same rows the day report prints for that depot.
    with app.app_context():
        report = cash.day_report(day=PAST, branch_id=1)
        rows = {row[1]: row[2] for row in cash.day_report_rows(report) if row[0] == 'Spare wheel count'}
    assert rows[SIZE_A] == 'Expected 3 · Counted 4 · +1 over'
    assert rows[SIZE_B] == 'Expected 0 · Not counted yet'


# --------------------------------------------------------------------------- #
# Service-level: the day's trailer service lines
# --------------------------------------------------------------------------- #

def test_services_for_day_is_one_day_and_one_depot(client, app):
    first = seed_trailer(app, 'First trailer', branch_id=1)
    second = seed_trailer(app, 'Second trailer', branch_id=2)
    with app.app_context():
        trailer_service.create_service_history(first, 'Tyre change', branch_id=1, service_date=PAST)
        trailer_service.create_service_history(first, 'Re-wiring', branch_id=1, service_date=OLDER)
        trailer_service.create_service_history(second, 'Bearing service', branch_id=2, service_date=PAST)
        trailer_service.create_service_history(first, 'Custom', custom_description='Fitted a winch',
                                               branch_id=1, service_date=PAST)
    client.application  # keep the fixture used
    login(client, app)
    with app.test_request_context('/dashboard'):
        from flask import session as flask_session
        flask_session['user_role'] = 'owner'
        flask_session['user_id'] = 1
        rows = trailer_service.services_for_day(PAST, branch_id=1)
    assert [row['label'] for row in rows] == ['Tyre change', 'Custom — Fitted a winch']
    assert {row['product_name'] for row in rows} == {'First trailer'}
    with app.test_request_context('/dashboard'):
        from flask import session as flask_session
        flask_session['user_role'] = 'owner'
        flask_session['user_id'] = 1
        other = trailer_service.services_for_day(PAST, branch_id=2)
    assert [row['label'] for row in other] == ['Bearing service']
