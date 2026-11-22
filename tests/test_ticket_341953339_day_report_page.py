"""Ticket ABI-341953339 - the day-end block has its own Day report page.

Requested edit: *create a new page called "Day Report" at a level of Dashboard*
and move New client interactions, Trailer service and maintenance, Cash used,
Cash drop off (to bank), Cash up, POS Cashup, Spare wheel count, End of day
notes and Day report actions onto it.

What is pinned here:

* ``GET /day-report`` renders all nine panels and the dashboard no longer does.
* The page keeps the dashboard's own ``?branch=`` / ``?day=`` behaviour: the
  session scope always wins, a past day is read-only, and a non-main sign-in is
  pinned to today.
* The page carries the ``dashboard`` module gate (an account without that module
  gets 403, while the read-only dashboard itself stays open).
* Every write form still posts to the untouched ``/cash-up`` endpoints, and a
  submit now comes back to the page it was posted from.
* A confirmed day report submitted from this page still offers the log-out
  prompt (ticket ABI-341953300).
"""
import os
import tempfile

import pytest

from app import create_app
from app.db import get_db
from app.services import cash, spare_wheels
from app.services.access import MODULE_KEYS, create_additional_user

TODAY = cash.today_iso()
PAST = '2026-09-12'

PANEL_HEADINGS = [
    '<h2>New client interactions</h2>',
    '<h2>Trailer service and maintenance</h2>',
    '<h2>Cash used</h2>',
    '<h2>Cash drop off (to bank)</h2>',
    '<h2>Cash up \u00b7 Branch 1</h2>',
    '<h2>POS Cashup \u00b7 Branch 1</h2>',
    '<h2>Spare wheel count</h2>',
    '<h2>End of day notes</h2>',
    '<h2>Day report actions</h2>',
]


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
        assert row is not None, f'no account named {name!r}'
        user_id = row['id']
    return client.post('/login', data={'user_id': str(user_id), 'password': password},
                       follow_redirects=True)


def add_staff(client, app, name, branch_id=1, password='staff123', modules=None):
    """Owner sets the shared module default, then creates a depot account."""
    login(client)
    client.post('/settings/users/permissions', data={'module': list(modules or MODULE_KEYS)},
                follow_redirects=True)
    with app.app_context():
        user_id, error = create_additional_user(name, password, branch_id=branch_id)
    assert user_id, error
    return user_id


def seed_payment(app, amount, method='cash', day=TODAY, branch_id=1, number='ORD-341953339'):
    with app.app_context():
        db = get_db()
        db.execute(
            """INSERT INTO orders (order_number, booking_type, collect_branch_id, return_branch_id,
            status, payment_status, start_at, end_at, subtotal, tax_total, deposit_total, total,
            due_total, notes, created_at)
            VALUES (?, 'return', ?, ?, 'started', 'paid', ?, ?, 100, 15, 0, 115, 0, '', ?)""",
            (number, branch_id, branch_id, f'{day} 08:00', f'{day} 18:00', f'{day} 08:00'),
        )
        order_id = db.execute("SELECT id FROM orders WHERE order_number = ?",
                              (number,)).fetchone()['id']
        db.execute(
            """INSERT INTO payments (order_id, amount, method, reference, status, payment_date,
            deleted_at, created_at) VALUES (?, ?, ?, '', 'paid', ?, '', ?)""",
            (order_id, amount, method, day, f'{day} 09:00'),
        )
        db.commit()


def used_rows(app):
    with app.app_context():
        return [dict(row) for row in get_db().execute('SELECT * FROM cash_used').fetchall()]


def summary(app, day=TODAY, branch_id=1):
    with app.test_request_context('/day-report'):
        from flask import session as flask_session
        flask_session['user_id'] = 1
        flask_session['user_role'] = 'owner'
        return cash.day_summary(day=day, branch_id=branch_id)


# --------------------------------------------------------------------------- #
# The page exists, carries the panels, and the dashboard no longer does
# --------------------------------------------------------------------------- #

def test_day_report_page_carries_every_moved_panel(client):
    login(client)
    response = client.get('/day-report?branch=1')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    for heading in PANEL_HEADINGS:
        assert heading in body, heading
    # Same hero treatment as the dashboard: business day + branch filters.
    assert '<header class="dashboard-hero">' in body
    assert 'Business day' in body
    assert 'name="branch"' in body
    assert '← Dashboard' in body


def test_the_dashboard_no_longer_renders_them(client):
    login(client)
    body = client.get('/dashboard?branch=1').get_data(as_text=True)
    for heading in PANEL_HEADINGS:
        assert heading not in body, heading
    assert '<h2 class="section-heading">Cash up' not in body
    # The link to the new page sits in the hero actions.
    assert 'href="/day-report' in body
    # And the dashboard keeps its own day metrics and Movement cards.
    assert '<h2 class="section-heading">Today</h2>' in body
    assert '<h2 class="section-heading">Movement</h2>' in body


def test_the_sidebar_and_phone_nav_link_to_the_page(client):
    login(client)
    body = client.get('/dashboard').get_data(as_text=True)
    assert body.count('href="/day-report"') >= 1
    assert '>Day report<' in body


# --------------------------------------------------------------------------- #
# Filters behave exactly as they did on the dashboard
# --------------------------------------------------------------------------- #

def test_a_past_day_is_read_only_on_the_new_page(client):
    login(client)
    with client.application.app_context():
        cash.save_cash_up(PAST, '500.00', branch_id=1, cash_up_notes='Filed day note.')
    body = client.get(f'/day-report?branch=1&day={PAST}').get_data(as_text=True)
    assert 'Filed day note.' in body
    assert 'action="/cash-up"' not in body, 'no cash-up form on a filed day'
    assert 'name="counted_cash"' not in body
    assert 'name="interaction_calls"' not in body
    assert 'name="actual_0"' not in body
    assert 'Submit day report' not in body
    # The downloads stay available for a past day.
    assert 'Download day report (PDF)' in body


def test_a_non_main_sign_in_is_pinned_to_today(client, app):
    seed_payment(app, 300.0, method='cash', day=TODAY, branch_id=1)
    add_staff(client, app, 'Day Report Clerk', branch_id=1)
    login(client, name='Day Report Clerk', password='staff123')
    with app.app_context():
        cash.save_cash_up(PAST, '111.00', branch_id=1)
    body = client.get(f'/day-report?branch=1&day={PAST}').get_data(as_text=True)
    # ``?day=`` is ignored: the page still reports today (the past date can only
    # appear as the opening-cash reference from the previous closing figure).
    assert '<h1>Today \u00b7 Branch 1</h1>' in body
    assert 'This business day is in the past' not in body
    # Today's forms and the submit action are available; the filing is untouched
    # (the past figure only shows up as the previous closing reference).
    assert 'name="counted_cash"' in body
    assert 'Submit day report' in body
    # Every form posts today's business day, never the filed one.
    assert 'action="/cash-up"' in body
    assert 'name="day" value="' + TODAY + '"' in body
    assert 'name="day" value="' + PAST + '"' not in body


def test_a_branch_scoped_account_cannot_widen_the_page(client, app):
    seed_payment(app, 500.0, method='cash', day=TODAY, branch_id=1, number='ORD-339-A')
    seed_payment(app, 700.0, method='cash', day=TODAY, branch_id=2, number='ORD-339-B')
    add_staff(client, app, 'Depot Two Clerk 339', branch_id=2)
    login(client, name='Depot Two Clerk 339', password='staff123')

    body = client.get('/day-report').get_data(as_text=True)
    assert 'Cash up \u00b7 Branch 2' in body
    assert 'R700.00' in body
    assert 'R500.00' not in body, 'the other depot stays invisible'

    body = client.get('/day-report?branch=1').get_data(as_text=True)
    assert 'Cash up \u00b7 Branch 2' in body, 'a crafted ?branch= cannot widen the session'


# --------------------------------------------------------------------------- #
# Access gate follows the endpoint
# --------------------------------------------------------------------------- #

def test_anonymous_is_sent_to_the_sign_in(client):
    response = client.get('/day-report')
    assert response.status_code == 302
    assert '/login' in response.headers['Location']


def test_the_page_needs_the_dashboard_module(client, app):
    add_staff(client, app, 'Orders Only Clerk', branch_id=1, modules=['orders'])
    login(client, name='Orders Only Clerk', password='staff123')
    assert client.get('/day-report').status_code == 403
    assert client.post('/day-report').status_code == 405
    # The read-only dashboard itself stays reachable (ticket ABI-341953320).
    assert client.get('/dashboard').status_code == 200


# --------------------------------------------------------------------------- #
# Writes still work, and come back to the page they were posted from
# --------------------------------------------------------------------------- #

def test_cash_used_can_be_added_and_deleted_from_the_new_page(client, app):
    login(client)
    response = client.post('/cash-up/used', data={
        'day': TODAY, 'branch': '1', 'amount': '150.50', 'description': 'Diesel',
        'next': '/day-report?branch=1',
    })
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/day-report?branch=1')
    rows = used_rows(app)
    assert [row['description'] for row in rows] == ['Diesel']

    body = client.get('/day-report?branch=1').get_data(as_text=True)
    assert '>Diesel<' in body
    assert 'R150.50' in body

    response = client.post(f"/cash-up/used/{rows[0]['id']}/delete",
                           data={'day': TODAY, 'branch': '1', 'next': '/day-report?branch=1'})
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/day-report?branch=1')
    assert used_rows(app) == []
    assert '>Diesel<' not in client.get('/day-report?branch=1').get_data(as_text=True)


def test_a_referrer_also_returns_staff_to_the_page(client, app):
    """The browser sends Referer for a same-origin form post; that is enough."""
    login(client)
    response = client.post('/cash-up/used',
                           data={'day': TODAY, 'branch': '1', 'amount': '20',
                                 'description': 'Cleaning'},
                           headers={'Referer': 'http://localhost/day-report?branch=1'})
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/day-report?branch=1')


def test_a_crafted_next_cannot_leave_the_site(client):
    login(client)
    response = client.post('/cash-up/used',
                           data={'day': TODAY, 'branch': '1', 'amount': '20',
                                 'description': 'Cleaning', 'next': '//evil.example/x'})
    assert response.status_code == 302
    assert 'evil.example' not in response.headers['Location']
    assert response.headers['Location'].startswith('/dashboard')


def test_spare_wheels_and_interactions_save_from_the_new_page(client, app):
    login(client)
    values = {f'actual_{index}': '' for index in range(len(spare_wheels.WHEEL_SIZES))}
    values.update({'actual_0': '2'})
    response = client.post('/dashboard/spare-wheels',
                           data={'day': TODAY, 'branch': '1', 'next': '/day-report?branch=1',
                                 **values})
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/day-report?branch=1')

    response = client.post('/cash-up/interactions', data={
        'day': TODAY, 'branch': '1', 'next': '/day-report?branch=1',
        'interaction_calls': '4', 'interaction_whatsapp': '3',
        'interaction_emails': '2', 'interaction_walk_in': '1',
        'interaction_notes': 'Follow-ups booked.',
    })
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/day-report?branch=1')

    data = summary(app)
    assert data['interactions']['calls'] == 4
    assert data['interactions']['notes'] == 'Follow-ups booked.'
    body = client.get('/day-report?branch=1').get_data(as_text=True)
    assert 'Follow-ups booked.' in body


def test_submitting_the_day_report_from_the_new_page_prompts_to_log_out(client, app, monkeypatch):
    sent = {}

    def fake_send(document_bytes, filename, caption=''):
        sent['filename'] = filename
        return {'ok': True, 'sent': True, 'status': 200}

    from app.routes import cash as cash_routes
    monkeypatch.setattr(cash_routes, '_send_document', fake_send)
    seed_payment(app, 500.0, method='cash', day=TODAY, branch_id=1)
    login(client)
    # The day report submits only when the required sections are filled.
    client.post('/cash-up/interactions', data={
        'day': TODAY, 'branch': '1', 'interaction_calls': '0',
        'interaction_whatsapp': '0', 'interaction_emails': '0',
        'interaction_walk_in': '0', 'interaction_notes': '',
    }, follow_redirects=True)
    for path, data in (
        ('/cash-up', {'counted_cash': '500'}),
        ('/cash-up/pos', {'counted_card': '0'}),
    ):
        client.post(path, data={'day': TODAY, 'branch': '1', **data}, follow_redirects=True)
    values = {f'actual_{index}': '0' for index in range(len(spare_wheels.WHEEL_SIZES))}
    client.post('/dashboard/spare-wheels',
                data={'day': TODAY, 'branch': '1', **values}, follow_redirects=True)

    response = client.post('/cash-up/report/telegram',
                           data={'day': TODAY, 'branch': '1', 'next': '/day-report?branch=1'},
                           follow_redirects=True)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'Day report sent on Telegram for Branch 1' in body
    assert sent['filename'].endswith(f'{TODAY}.pdf')
    # Ticket ABI-341953300: the one-time log-out prompt shows on the page that
    # rendered the send.
    assert 'You have submitted your day report, LOGOUT?' in body
    assert 'You have submitted your day report, LOGOUT?' not in client.get(
        '/day-report?branch=1').get_data(as_text=True)
