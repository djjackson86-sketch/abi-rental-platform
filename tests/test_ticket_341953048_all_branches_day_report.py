"""Ticket ABI-341953048 - the All branches dashboard offers the combined report.

Requested edit: "main profile dashboard: enable Day report actions even when all
branches is selected, this should allow access to dashboard reports with combined
performance numbers".

What is pinned here:

* the dashboard's Day report actions panel no longer reads "Select a branch ..."
  when the main profile is on **All branches** - it offers the same PDF and CSV
  downloads, pointing at the combined report (``?branch=all``);
* ``GET /cash-up/report.pdf`` and ``GET /cash-up/export.csv`` build that report
  from ``cash.aggregate_day_summary`` - **the combined figures for every depot the
  session may already see** - instead of silently falling back to the depot the
  sign-in happens to be acting as. A missing ``branch`` is the combined report
  too, which is exactly what the all-branches dashboard link produces;
* the combined download is named for All branches (``All branches_Dashboard
  Report_<day>.pdf``, CSV ``Depot`` row "All branches");
* the view stays read-only - no submit-to-Telegram action, no cash-up form - and
  a single-depot account can never be handed a wider report than its own depot
  (``?branch=all`` is resolved through the shared branch-filter resolver, which
  the session scope always wins);
* a per-depot download is untouched: one depot is still one depot.
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
from app.services.access import MODULE_KEYS, create_additional_user  # noqa: E402
from app.services.products import WHEEL_SIZES, create_product  # noqa: E402

TODAY = cash.today_iso()
PAST = (date.fromisoformat(TODAY) - timedelta(days=2)).isoformat()

SIZE_A, SIZE_B = WHEEL_SIZES[0], WHEEL_SIZES[1]

_TEXT_RUN = re.compile(r'/(F\d) ([\d.]+) Tf 1 0 0 1 ([\d.]+) ([\d.]+) Tm \(((?:[^()\\]|\\.)*)\) Tj')


def _drawn(pdf_bytes):
    """The text runs the report actually draws, in draw order."""
    return [text.replace('\\(', '(').replace('\\)', ')')
            for _font, _size, _x, _y, text in _TEXT_RUN.findall(pdf_bytes.decode('latin-1'))]


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
    """A single-depot staff account with every module granted."""
    login(client, app)
    client.post('/settings/users/permissions', data={'module': list(MODULE_KEYS)}, follow_redirects=True)
    with app.app_context():
        user_id, error = create_additional_user(name, password, branch_id=branch_id)
        assert error is None, error
    return user_id


def add_multi_branch_staff(client, app, name, branch_ids, password='staff123'):
    """A staff account that manages several - but not all - depots."""
    login(client, app)
    client.post('/settings/users/permissions', data={'module': list(MODULE_KEYS)}, follow_redirects=True)
    with app.app_context():
        user_id, error = create_additional_user(name, password, branch_ids=[str(b) for b in branch_ids])
        assert error is None, error
    return user_id


def seed_day(app, day, branch_id, counted, used=(), drops=(), notes=''):
    """Write one depot's business day through the cash service.

    The cash up goes in first on purpose: ``save_cash_up`` owns the row's
    ``notes`` column, so notes written before it would be blanked.
    """
    with app.app_context():
        if counted is not None:
            cash.save_cash_up(day, counted, branch_id=branch_id)
        for amount, description in used:
            cash.add_cash_used(day, amount, description, branch_id=branch_id)
        for amount in drops:
            cash.add_bank_drop(day, amount, branch_id=branch_id)
        if notes:
            cash.save_notes(day, notes, branch_id=branch_id)


def seed_payment(app, amount, day, branch_id, number):
    """One paid cash payment for the day, so the money cards have figures."""
    with app.app_context():
        db = get_db()
        db.execute(
            """INSERT INTO orders (order_number, booking_type, collect_branch_id, return_branch_id,
            status, payment_status, start_at, end_at, subtotal, tax_total, deposit_total, total,
            due_total, notes, created_at)
            VALUES (?, 'return', ?, ?, 'started', 'paid', ?, ?, 100, 15, 0, 115, 0, '', ?)""",
            (number, branch_id, branch_id, f'{day} 08:00', f'{day} 18:00', f'{day} 08:00'),
        )
        order_id = db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()['id']
        db.execute(
            """INSERT INTO payments (order_id, amount, method, reference, status, payment_date,
            deleted_at, created_at) VALUES (?, ?, 'cash', '', 'paid', ?, '', ?)""",
            (order_id, amount, day, f'{day} 09:00'),
        )
        db.commit()


def seed_trailer(app, name, quantity, wheel_size, branch_id):
    with app.app_context():
        create_product({
            'name': name, 'product_type': 'rental', 'tracking_method': 'bulk',
            'quantity': str(quantity), 'wheel_size': wheel_size,
            'branch_id': str(branch_id), 'active': '1',
        })
        row = get_db().execute('SELECT id FROM products WHERE name = ?', (name,)).fetchone()
    return int(row['id'])


def csv_rows(body):
    return [row for row in csv.reader(io.StringIO(body)) if row]


def seed_two_depots(app):
    """Depot 1 and depot 2 both trading on PAST, plus a third depot to exclude."""
    seed_payment(app, 500.0, PAST, 1, 'ORD-95348-A')
    seed_payment(app, 700.0, PAST, 2, 'ORD-95348-B')
    seed_payment(app, 9000.0, PAST, 3, 'ORD-95348-C')
    seed_day(app, PAST, 1, '840.00', used=[('150.50', 'Depot one diesel')], drops=['200.00'],
             notes='Depot one closed the yard.')
    seed_day(app, PAST, 2, '900.00', used=[('25.00', 'Depot two toll')],
             notes='Depot two handed over.')


def dashboard(client, app, query='', staff=None, password='staff123'):
    if staff:
        login(client, app, staff, password)
    else:
        login(client, app)
    response = client.get(f'/dashboard{query}')
    assert response.status_code == 200
    return response.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# The dashboard offers the downloads on All branches (main profile)
# --------------------------------------------------------------------------- #

def test_the_all_branches_dashboard_offers_the_combined_report_downloads(client, app):
    html = dashboard(client, app)
    assert 'Cash up · All branches' in html, 'the default main-profile view is All branches'
    assert 'Day report actions' in html
    assert f'/cash-up/report.pdf?day={TODAY}&amp;branch=all' in html
    assert f'/cash-up/export.csv?day={TODAY}&amp;branch=all' in html
    assert 'Download day report (PDF)' in html
    assert 'Download CSV' in html
    assert 'Combined figures for all' in html
    # The old dead-end wording is gone.
    assert 'Select a branch in the top Branch filter to submit or download a branch day report.' not in html


def test_the_all_branches_report_panel_stays_read_only(client, app):
    """A combined view must never grow a write or submit action."""
    html = dashboard(client, app)
    panel = html[html.index('Day report actions'):html.index('</section>', html.index('Day report actions'))]
    assert '<form' not in panel, 'no form on the combined report actions'
    assert 'Submit day report' not in panel
    assert 'action="/cash-up/report/telegram"' not in html
    assert 'action="/cash-up"' not in html
    assert 'name="cash_branch"' not in html


def test_the_all_branches_dashboard_picks_up_a_past_day_too(client, app):
    html = dashboard(client, app, query=f'?day={PAST}')
    assert f'/cash-up/report.pdf?day={PAST}&amp;branch=all' in html
    assert f'/cash-up/export.csv?day={PAST}&amp;branch=all' in html
    assert f'Combined figures for all' in html and PAST in html


def test_the_downloads_stay_hidden_from_a_multi_depot_staff_sign_in(client, app):
    """The combined report is a main-profile action (the ticket's wording)."""
    add_multi_branch_staff(client, app, 'Two Depot Staff', [1, 2])
    html = dashboard(client, app, staff='Two Depot Staff')
    assert 'Cash up · All branches' in html, 'their own depots, still a combined panel'
    assert 'Download day report (PDF)' not in html
    assert 'Download CSV' not in html
    assert 'Submit day report' not in html


# --------------------------------------------------------------------------- #
# The downloads themselves carry combined figures
# --------------------------------------------------------------------------- #

def test_the_combined_csv_says_all_branches_and_sums_every_depot(client, app):
    seed_two_depots(app)
    login(client, app)
    body = client.get(f'/cash-up/export.csv?branch=all&day={PAST}').get_data(as_text=True)
    rows = csv_rows(body)
    assert ['Report', 'Depot', 'All branches'] in rows
    assert ['Report', 'Business day', PAST] in rows
    # The main profile may reach every depot, so all three are in: 500 + 700 + 9000.
    assert ['Cash up', 'Cash received for the day', 'R10200.00'] in rows
    assert ['Cash up', 'Cash used for the day', 'R175.50'] in rows
    assert ['Cash up', 'Total dropped at the bank', 'R200.00'] in rows
    assert ['Cash up', 'Closing cash (counted)', 'R1740.00'] in rows
    assert ['Cash up', 'Variance (counted - expected)',
            '-R8084.50 (Partial: 2 of 3 depots cashed up)'] in rows
    # Every depot's own lines are there, attributed - the "combined" part.
    assert ['Cash used', 'Branch 1: Depot one diesel', 'R150.50'] in rows
    assert ['Cash used', 'Branch 2: Depot two toll', 'R25.00'] in rows
    assert ['Dashboard', 'Revenue for the day', 'R10200.00'] in rows, 'combined performance numbers'
    assert ['End of day notes', 'Notes', 'Branch 1: Depot one closed the yard.\nBranch 2: Depot two handed over.'] in rows


def test_a_download_with_no_branch_parameter_is_the_combined_report(client, app):
    """The old fallback silently reported whichever depot the sign-in acted as."""
    seed_two_depots(app)
    login(client, app)
    body = client.get(f'/cash-up/export.csv?day={PAST}').get_data(as_text=True)
    rows = csv_rows(body)
    assert ['Report', 'Depot', 'All branches'] in rows
    assert ['Report', 'Depot', 'Branch 1'] not in rows
    assert ['Cash up', 'Cash received for the day', 'R10200.00'] in rows
    assert ['Cash used', 'Branch 2: Depot two toll', 'R25.00'] in rows, 'depot 2 is not dropped'


def test_the_combined_pdf_is_named_and_headed_for_all_branches(client, app):
    seed_two_depots(app)
    login(client, app)
    response = client.get(f'/cash-up/report.pdf?branch=all&day={PAST}')
    assert response.status_code == 200
    assert response.data.startswith(b'%PDF-')
    expected = f'All branches_Dashboard Report_{PAST}.pdf'
    assert expected in response.headers['Content-Disposition']
    drawn = _drawn(response.data)
    assert 'Depot: All branches' in drawn
    assert f'Business day: {PAST}' in drawn
    assert 'Branch 1: Depot one diesel' in drawn
    assert 'Branch 2: Depot two toll' in drawn
    assert 'R10200.00' in drawn, 'combined cash received'
    assert 'Branch 1: Depot one closed the yard.' in drawn
    assert 'Branch 2: Depot two handed over.' in drawn


def test_the_combined_report_combines_the_wheel_and_service_sections(client, app):
    """The sections ticket ABI-341953042 added read the same combined scope."""
    seed_trailer(app, 'Depot one trailer', 3, SIZE_A, 1)
    seed_trailer(app, 'Depot two trailer', 5, SIZE_A, 2)
    service_id = seed_trailer(app, 'Depot two service trailer', 1, SIZE_B, 2)
    with app.app_context():
        spare_wheels.save_actual_counts(PAST, 1, {SIZE_A: 2})
        trailer_service.create_service_history(service_id, 'Tyre change', branch_id=2, service_date=PAST)
        report = cash.day_report(day=PAST, aggregate=True)
        rows = cash.day_report_rows(report)
    assert report['cash']['aggregate'] is True
    assert report['cash']['branch_name'] == 'All branches'
    assert report['cash']['writable'] is False
    wheels = [row for row in rows if row[0] == 'Spare wheel count']
    assert wheels[0][1] == SIZE_A
    assert wheels[0][2] == 'Expected 8 · Counted 2 · 6 short', 'both depots counted'
    service = [row for row in rows if row[0] == 'Trailer service and maintenance']
    assert [row[1] for row in service] == ['Depot two service trailer']


# --------------------------------------------------------------------------- #
# One depot is still one depot - and a scoped account never widens
# --------------------------------------------------------------------------- #

def test_a_depot_download_is_still_that_depot_alone(client, app):
    seed_two_depots(app)
    login(client, app)
    response = client.get(f'/cash-up/report.pdf?branch=2&day={PAST}')
    assert response.status_code == 200
    assert f'Branch 2_Dashboard Report_{PAST}.pdf' in response.headers['Content-Disposition']
    drawn = _drawn(response.data)
    assert 'Depot: Branch 2' in drawn
    assert 'Depot two toll' in drawn
    assert 'Depot one diesel' not in drawn
    assert 'Branch 1: Depot two toll' not in drawn, 'no depot prefix on a single depot report'
    assert 'R700.00' in drawn
    body = client.get(f'/cash-up/export.csv?branch=2&day={PAST}').get_data(as_text=True)
    rows = csv_rows(body)
    assert ['Report', 'Depot', 'Branch 2'] in rows
    assert ['Cash up', 'Cash received for the day', 'R700.00'] in rows
    assert ['Cash used', 'Depot one diesel', 'R150.50'] not in rows


def test_a_single_depot_account_cannot_download_a_wider_report(client, app):
    """``?branch=all`` is resolved by the session scope: it can only narrow."""
    seed_two_depots(app)
    add_staff(client, app, 'Depot One Staff', 1)
    login(client, app, 'Depot One Staff', 'staff123')
    body = client.get(f'/cash-up/export.csv?branch=all&day={PAST}').get_data(as_text=True)
    rows = csv_rows(body)
    assert ['Report', 'Depot', 'Branch 1'] in rows
    assert ['Report', 'Depot', 'All branches'] not in rows
    assert ['Cash up', 'Cash received for the day', 'R500.00'] in rows
    assert ['Cash used', 'Depot two toll', 'R25.00'] not in rows
    assert 'Depot two handed over.' not in body
    response = client.get(f'/cash-up/report.pdf?branch=all&day={PAST}')
    assert f'Branch 1_Dashboard Report_{PAST}.pdf' in response.headers['Content-Disposition']
    assert 'Branch 2: Depot two toll' not in _drawn(response.data)


def test_a_multi_depot_account_gets_only_its_own_depots_combined(client, app):
    seed_two_depots(app)
    add_multi_branch_staff(client, app, 'Two Depot Staff', [1, 2])
    login(client, app, 'Two Depot Staff', 'staff123')
    body = client.get(f'/cash-up/export.csv?branch=all&day={PAST}').get_data(as_text=True)
    rows = csv_rows(body)
    assert ['Report', 'Depot', 'All branches'] in rows
    assert ['Cash up', 'Cash received for the day', 'R1200.00'] in rows, 'depots 1 and 2, not depot 3'
    assert 'R9000.00' not in body, 'depot 3 is outside their grant'
    assert ['Dashboard', 'Revenue for the day', 'R1200.00'] in rows


def test_a_pinned_account_bare_download_is_still_its_own_depot(client, app):
    """A single-depot sign-in resolves to its own depot with or without a filter."""
    seed_two_depots(app)
    add_staff(client, app, 'Depot One Staff', 1)
    login(client, app, 'Depot One Staff', 'staff123')
    response = client.get(f'/cash-up/report.pdf?day={PAST}')
    assert f'Branch 1_Dashboard Report_{PAST}.pdf' in response.headers['Content-Disposition']
    drawn = _drawn(response.data)
    assert 'Depot: Branch 1' in drawn
    assert 'Depot one diesel' in drawn
    assert 'Depot two toll' not in drawn


def test_the_dashboard_links_match_what_the_routes_return(client, app):
    """The link the panel renders is the report it promises (no drift)."""
    seed_two_depots(app)
    html = dashboard(client, app)
    href = re.search(r'href="(/cash-up/report\.pdf[^"]*)"', html).group(1).replace('&amp;', '&')
    response = client.get(href)
    assert response.status_code == 200
    assert f'All branches_Dashboard Report_{TODAY}.pdf' in response.headers['Content-Disposition']
