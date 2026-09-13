"""Day-end cash up, cash used, end of day notes and the day report.

Ticket ABI-341952951. Five asks: an opening cash card taken from the previous
day's closing cash; an option for users to cash the drawer up; a cash used
section with a reason per line; end of day notes that show on the dashboard and
in the day report; and a downloadable day report.

The tests pin the *definitions* (opening = previous recorded closing for the same
depot, expected = opening + cash received - cash used, variance = counted -
expected) and the access rule (a branch-limited account sees and writes only its
own depot's drawer), because those are the parts that would quietly misreport
money if they drifted.

All fixtures use fixed dates so nothing depends on the wall clock.
"""
import os
import tempfile

import pytest
from flask import session as flask_session

from app import create_app
from app.db import get_db
from app.services import cash
from app.services.access import create_additional_user

TODAY = cash.today_iso()
YESTERDAY = '2026-09-12'
FUTURE = '2030-01-01'


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
        user_id = row['id'] if row else None
    return client.post('/login', data={'user_id': str(user_id), 'password': password},
                       follow_redirects=True)


def _seed_payment(app, amount, method='cash', day=TODAY, branch_id=1, number='ORD-90001'):
    """One started order on ``branch_id`` with a single payment dated ``day``."""
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
            deleted_at, created_at) VALUES (?, ?, ?, '', 'paid', ?, '', ?)""",
            (order_id, amount, method, day, f'{day} 09:00'),
        )
        db.commit()
    return order_id


def _cash_rows(app):
    with app.app_context():
        return [dict(row) for row in get_db().execute('SELECT * FROM cash_ups').fetchall()]


def _used_rows(app):
    with app.app_context():
        return [dict(row) for row in get_db().execute('SELECT * FROM cash_used').fetchall()]


def _summary(app, day=TODAY, branch_id=1):
    with app.test_request_context('/dashboard'):
        flask_session['user_id'] = 1
        flask_session['user_role'] = 'owner'
        return cash.day_summary(day=day, branch_id=branch_id)


def test_dashboard_shows_the_opening_cash_card_and_the_day_end_panel(client):
    login(client)
    body = client.get('/dashboard').get_data(as_text=True)
    assert 'Opening cash' in body
    assert 'Cash up · end of day' in body
    assert 'Cash up · Branch 1' in body
    # Every part the ticket asked for is on the dashboard.
    assert 'End of day notes' in body
    assert 'What the cash was used for' in body
    assert 'Download day report (PDF)' in body
    # First-ever day: nothing has been cashed up yet.
    assert 'No earlier closing' in body
    assert 'Not cashed up yet' in body


def test_opening_cash_is_the_previous_days_closing_cash(client, app):
    login(client)
    client.post('/cash-up', data={'day': YESTERDAY, 'counted_cash': '1000.00'}, follow_redirects=True)
    summary = _summary(app, day=TODAY, branch_id=1)
    assert summary['opening'] == 1000.0
    assert summary['opening_from'] == YESTERDAY
    # A day before any closing still opens at zero, and another depot is separate.
    assert _summary(app, day=YESTERDAY, branch_id=1)['opening'] == 0.0
    assert _summary(app, day=TODAY, branch_id=2)['opening'] == 0.0
    # Yesterday's own count is stored, today's is still open.
    assert _summary(app, day=YESTERDAY, branch_id=1)['counted'] == 1000.0
    assert _summary(app, day=TODAY, branch_id=1)['cashed_up'] is False
    # The card itself follows the previous closing.
    body = client.get('/dashboard').get_data(as_text=True)
    assert 'Prev. closing 2026-09-12' in body
    assert 'R1000.00' in body


def test_expected_cash_and_variance_arithmetic(client, app):
    _seed_payment(app, 500.0, method='cash', day=TODAY, branch_id=1)
    _seed_payment(app, 900.0, method='eft', day=TODAY, branch_id=1, number='ORD-90002')
    login(client)
    client.post('/cash-up/used', data={'day': '', 'amount': '150.50',
                                       'description': 'Diesel for the delivery bakkie'},
                follow_redirects=True)
    client.post('/cash-up', data={'day': '', 'counted_cash': '400.00'}, follow_redirects=True)
    summary = _summary(app)
    # opening 0 + cash 500 - used 150.50 = 349.50 expected; counted 400 -> +50.50
    assert summary['cash_received'] == 500.0, 'only cash payments count, not EFT'
    assert summary['used_total'] == 150.50
    assert summary['expected'] == 349.50
    assert summary['counted'] == 400.0
    assert summary['variance'] == 50.50
    assert summary['variance_display'] == '+R50.50'
    assert summary['variance_label'] == 'Over by R50.50'
    body = client.get('/dashboard').get_data(as_text=True)
    assert '+R50.50' in body and 'Over by R50.50' in body
    assert 'R349.50' in body and 'R500.00' in body and 'R150.50' in body


def test_a_short_drawer_is_reported_as_short(client, app):
    _seed_payment(app, 100.0, method='cash', day=TODAY, branch_id=1)
    login(client)
    client.post('/cash-up', data={'day': '', 'counted_cash': '90.00'}, follow_redirects=True)
    summary = _summary(app)
    assert summary['expected'] == 100.0
    assert summary['variance'] == -10.0
    assert summary['variance_display'] == '-R10.00'
    assert summary['variance_label'] == 'Short by R10.00'
    assert 'Short by R10.00' in client.get('/dashboard').get_data(as_text=True)


def test_cash_up_is_one_row_per_depot_per_day(client, app):
    login(client)
    client.post('/cash-up', data={'day': '', 'counted_cash': '100.00'}, follow_redirects=True)
    client.post('/cash-up', data={'day': '', 'counted_cash': '125.50'}, follow_redirects=True)
    rows = _cash_rows(app)
    assert len(rows) == 1, 're-saving the same day must update, not duplicate'
    assert rows[0]['counted_cash'] == 125.5
    assert rows[0]['branch_id'] == 1


def test_cash_up_rejects_junk_and_negative_counts(client, app):
    login(client)
    body = client.post('/cash-up', data={'day': '', 'counted_cash': 'not money'},
                       follow_redirects=True).get_data(as_text=True)
    assert 'Enter the cash counted for the day' in body
    body = client.post('/cash-up', data={'day': '', 'counted_cash': '-5'},
                       follow_redirects=True).get_data(as_text=True)
    assert 'Counted cash cannot be negative' in body
    assert _cash_rows(app) == []


def test_a_future_day_cannot_be_cashed_up(client, app):
    login(client)
    body = client.post('/cash-up', data={'day': FUTURE, 'counted_cash': '10'},
                       follow_redirects=True).get_data(as_text=True)
    assert 'Cash up today or a past day only' in body
    assert _cash_rows(app) == []


def test_a_junk_day_falls_back_to_today_instead_of_erroring(client):
    login(client)
    page = client.get('/cash-up/report.pdf?day=abc')
    assert page.status_code == 200
    assert page.data.startswith(b'%PDF-')
    assert cash.parse_business_day('abc') == TODAY
    assert cash.parse_business_day('') == TODAY
    assert cash.parse_business_day(YESTERDAY) == YESTERDAY


def test_cash_used_needs_a_reason_and_a_positive_amount(client, app):
    login(client)
    body = client.post('/cash-up/used', data={'day': '', 'amount': '50', 'description': '  '},
                       follow_redirects=True).get_data(as_text=True)
    assert 'Describe what the cash was used for' in body
    body = client.post('/cash-up/used', data={'day': '', 'amount': '0', 'description': 'Diesel'},
                       follow_redirects=True).get_data(as_text=True)
    assert 'The amount of cash used must be more than zero' in body
    body = client.post('/cash-up/used', data={'day': '', 'amount': 'x', 'description': 'Diesel'},
                       follow_redirects=True).get_data(as_text=True)
    assert 'Enter the amount of cash used' in body
    assert _used_rows(app) == []


def test_cash_used_lines_can_be_added_and_removed(client, app):
    login(client)
    client.post('/cash-up/used', data={'day': '', 'amount': '150.50', 'description': 'Diesel'},
                follow_redirects=True)
    client.post('/cash-up/used', data={'day': '', 'amount': '49.50', 'description': 'Cleaning supplies'},
                follow_redirects=True)
    body = client.get('/dashboard').get_data(as_text=True)
    assert 'Diesel' in body and 'Cleaning supplies' in body
    assert 'R200.00' in body, 'the two lines total R200.00'
    entry_id = _used_rows(app)[0]['id']
    client.post(f'/cash-up/used/{entry_id}/delete', data={'day': ''}, follow_redirects=True)
    remaining = _used_rows(app)
    assert [row['description'] for row in remaining] == ['Cleaning supplies']
    body = client.get('/dashboard').get_data(as_text=True)
    assert '>Cleaning supplies<' in body
    assert '>Diesel<' not in body, 'the removed line is gone from the panel'


def test_end_of_day_notes_save_without_a_cash_up_and_reach_the_report(client, app):
    login(client)
    client.post('/cash-up/notes', data={'day': '', 'notes': 'Two tyres booked for Monday.'},
                follow_redirects=True)
    summary = _summary(app)
    assert summary['notes'] == 'Two tyres booked for Monday.'
    assert summary['cashed_up'] is False
    body = client.get('/dashboard').get_data(as_text=True)
    assert 'Two tyres booked for Monday.' in body
    assert 'Two tyres booked for Monday.' in client.get('/cash-up/export.csv').get_data(as_text=True)
    assert b'Two tyres booked for Monday.' in client.get('/cash-up/report.pdf').data


def test_another_depots_cash_used_line_cannot_be_deleted(client, app):
    login(client)  # owner (all depots) adds a line against depot 1
    client.post('/cash-up/used', data={'day': '', 'amount': '20', 'description': 'Depot 1 line'},
                follow_redirects=True)
    entry_id = _used_rows(app)[0]['id']
    with app.app_context():
        create_additional_user('Depot Two Clerk', 'staff123', branch_id=2)
    login(client, name='Depot Two Clerk', password='staff123')
    body = client.post(f'/cash-up/used/{entry_id}/delete', data={'day': ''},
                       follow_redirects=True).get_data(as_text=True)
    assert 'Cash used line not found' in body
    assert len(_used_rows(app)) == 1, "another depot's line must survive a crafted delete"


def test_a_branch_limited_account_cashes_its_own_drawer_only(client, app):
    login(client)
    client.post('/cash-up', data={'day': YESTERDAY, 'counted_cash': '1000.00'}, follow_redirects=True)
    client.post('/cash-up/notes', data={'day': YESTERDAY, 'notes': 'Depot one overnight note.'},
                follow_redirects=True)
    _seed_payment(app, 500.0, method='cash', day=TODAY, branch_id=1)
    _seed_payment(app, 700.0, method='cash', day=TODAY, branch_id=2, number='ORD-90003')
    with app.app_context():
        create_additional_user('Depot Two Clerk', 'staff123', branch_id=2)

    login(client, name='Depot Two Clerk', password='staff123')
    body = client.get('/dashboard').get_data(as_text=True)
    assert 'Cash up · Branch 2' in body
    # Depot 1's closing, notes and cash payments are invisible to depot 2.
    assert 'Depot one overnight note.' not in body
    assert 'Prev. closing 2026-09-12' not in body
    assert 'R500.00' not in body
    assert 'R700.00' in body, 'their own cash payment is the one counted'
    summary = _summary(app, branch_id=2)
    with app.test_request_context('/dashboard'):
        flask_session['user_id'] = 4
        flask_session['user_role'] = 'staff'
        flask_session['branch_id'] = 2
        flask_session['can_view_all_branches'] = 0
        flask_session['branch_ids'] = []
        assert cash.acting_branch_id() == 2

    # A staff cash up lands on their own depot and never on depot 1.
    client.post('/cash-up', data={'day': '', 'counted_cash': '699.00'}, follow_redirects=True)
    rows = {row['branch_id']: row for row in _cash_rows(app)}
    assert rows[2]['counted_cash'] == 699.0
    assert rows[1]['counted_cash'] == 1000.0, 'depot 1 keeps its own count'
    assert summary['branch_name'] == 'Branch 2'


def test_the_csv_report_carries_the_dashboard_and_the_cash_figures(client, app):
    _seed_payment(app, 500.0, method='cash', day=TODAY, branch_id=1)
    login(client)
    client.post('/cash-up/used', data={'day': '', 'amount': '150.50', 'description': 'Diesel'},
                follow_redirects=True)
    client.post('/cash-up', data={'day': '', 'counted_cash': '349.50', 'notes': 'All receipts filed.'},
                follow_redirects=True)
    res = client.get('/cash-up/export.csv')
    assert res.status_code == 200
    assert 'text/csv' in res.headers['Content-Type']
    assert f'dashboard-report-{TODAY}.csv' in res.headers['Content-Disposition']
    lines = res.get_data(as_text=True).splitlines()
    joined = '\n'.join(lines)
    for expected in ['Total cash payments,R500.00', 'Total EFT payments,R0.00',
                     'Opening cash (previous day closing),R0.00',
                     'Expected cash in the drawer,R349.50',
                     'Closing cash (counted),R349.50',
                     'Variance (counted - expected),R0.00 (Balanced)',
                     'Diesel,R150.50', 'All receipts filed.']:
        assert expected in joined, expected


def test_the_pdf_report_renders_the_day_figures(client, app):
    _seed_payment(app, 500.0, method='cash', day=TODAY, branch_id=1)
    login(client)
    client.post('/cash-up/used', data={'day': '', 'amount': '150.50', 'description': 'Diesel'},
                follow_redirects=True)
    client.post('/cash-up', data={'day': '', 'counted_cash': '400.00', 'notes': 'Drawer counted twice.'},
                follow_redirects=True)
    res = client.get('/cash-up/report.pdf')
    assert res.status_code == 200
    assert res.data.startswith(b'%PDF-')
    assert res.data.rstrip().endswith(b'%%EOF')
    assert f'dashboard-report-{TODAY}.pdf' in res.headers['Content-Disposition']
    text = res.data.decode('latin-1')
    for expected in ['daily dashboard report', 'DASHBOARD', 'Total cash payments: R500.00',
                     'CASH UP', 'Expected cash in the drawer: R349.50',
                     'CASH USED', 'Diesel: R150.50', 'END OF DAY NOTES', 'Drawer counted twice.']:
        assert expected in text, expected
    # Em dashes and other non-ASCII characters from the notes must not arrive as "?".
    assert '?' not in text.split('END OF DAY NOTES')[1][:200]


def test_the_pdf_stays_on_one_page_and_says_what_it_left_out(client, app):
    login(client)
    for index in range(12):
        client.post('/cash-up/used', data={'day': '', 'amount': '10', 'description': f'Line {index + 1}'},
                    follow_redirects=True)
    text = client.get('/cash-up/report.pdf').data.decode('latin-1')
    assert 'Line 9: R10.00' in text
    assert 'Line 10: R10.00' not in text
    # Parentheses are escaped in the PDF stream, so match the plain wording.
    assert '... and 3 more cash used line' in text
    assert 'see the CSV export' in text
    # The CSV export is uncapped, so nothing is actually lost.
    csv_body = client.get('/cash-up/export.csv').get_data(as_text=True)
    assert 'Line 12,R10.00' in csv_body


def test_the_cash_panel_renders_with_libsql_shaped_rows(client, app, monkeypatch):
    """Production rows are libsql tuples, where ``row.count`` is a method.

    Nothing in the cash service may hand a raw row to a template: the panel has
    to render finished scalars, exactly like the day-metric cards do.
    """
    class Row(tuple):
        def __new__(cls, names, items):
            row = super().__new__(cls, items)
            row._fields = tuple(names)
            row._values = tuple(items)
            return row

        def asdict(self):
            return dict(zip(self._fields, self._values))

        def keys(self):
            return list(self._fields)

        def __getitem__(self, key):
            if isinstance(key, str):
                return self._values[self._fields.index(key)]
            return tuple.__getitem__(self, key)

    class Cursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return list(self._rows)

        def fetchone(self):
            return self._rows[0] if self._rows else None

    class FakeDB:
        def execute(self, sql, params=None):
            statement = ' '.join(sql.split()).lower()
            if 'from branches' in statement:
                return Cursor([Row(('name',), ('Midrand',))])
            if 'business_day <' in statement:
                return Cursor([Row(('business_day', 'counted_cash'), ('2026-09-12', 1000.0))])
            if 'from cash_ups' in statement:
                return Cursor([Row(('id', 'opening_cash', 'counted_cash', 'notes'),
                                   (7, 1000.0, 349.5, 'All good'))])
            if 'from cash_used' in statement:
                return Cursor([
                    Row(('id', 'amount', 'description', 'created_at'),
                        (1, 150.5, 'Diesel', '2026-09-13T09:00:00')),
                ])
            if 'sum(pay.amount)' in statement:
                return Cursor([Row(('s',), (500.0,))])
            return Cursor([Row(('c',), (0,))])

    login(client)
    monkeypatch.setattr(cash, 'get_db', lambda: FakeDB())
    with app.test_request_context('/dashboard'):
        flask_session['user_id'] = 1
        flask_session['user_role'] = 'owner'
        summary = cash.day_summary(day=TODAY, branch_id=1)
    assert summary['branch_name'] == 'Midrand'
    assert summary['expected'] == 1349.5  # 1000 opening + 500 cash - 150.50 used
    assert summary['variance'] == -1000.0  # counted 349.50 - expected 1349.50
    assert summary['used_lines'] == [{'id': 1, 'amount': 150.5, 'description': 'Diesel',
                                      'created_at': '2026-09-13T09:00:00'}]
    for value in (summary['opening'], summary['expected'], summary['counted'], summary['variance']):
        assert isinstance(value, float), value
    assert isinstance(summary['used_count'], int)
    body = client.get('/dashboard').get_data(as_text=True)
    assert 'built-in method' not in body
