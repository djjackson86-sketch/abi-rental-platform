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
import re
import tempfile

import pytest
from flask import session as flask_session

from app import create_app
from app.db import get_db
from app.services import cash, pdf_documents
from app.services.access import create_additional_user

TODAY = cash.today_iso()
YESTERDAY = '2026-09-12'
FUTURE = '2030-01-01'

# Every text run the card report actually draws: (font, size, x, y, text).
# Parentheses inside a string are escaped in the PDF literal, so the pattern
# accepts an escaped pair rather than stopping at the first ")".
_TEXT_RUN = re.compile(r'/(F\d) ([\d.]+) Tf 1 0 0 1 ([\d.]+) ([\d.]+) Tm \(((?:[^()\\]|\\.)*)\) Tj')


def _unescape(value):
    return value.replace('\\(', '(').replace('\\)', ')').replace('\\\\', '\\')


def _runs(pdf_bytes):
    """(x, y, size, text) for every run drawn on the page, in draw order."""
    return [(float(x), float(y), float(size), _unescape(text))
            for _font, size, x, y, text in _TEXT_RUN.findall(pdf_bytes.decode('latin-1'))]


def _drawn_text(pdf_bytes):
    """The text actually drawn, so a card's label and value are read separately."""
    return [run[3] for run in _runs(pdf_bytes)]


def _card_values(pdf_bytes):
    """{label: value} as the cards draw them — a card's value sits 15pt below it.

    Positional on purpose: it reads what the renderer put on the page instead of
    assuming a label and its figure were concatenated into one string.
    """
    runs = _runs(pdf_bytes)
    values = {}
    for index, (x, y, _size, text) in enumerate(runs[:-1]):
        next_x, next_y, _next_size, next_text = runs[index + 1]
        if abs(next_x - x) < 0.01 and abs((y - next_y) - 15) < 0.01:
            values[text] = next_text
    return values


def _baselines(pdf_bytes):
    return [y for _x, y, _size, _text in _runs(pdf_bytes)]


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


def test_dashboard_places_cash_up_below_cash_drop_off(client):
    login(client)
    body = client.get('/dashboard').get_data(as_text=True)
    drop_panel_start = body.index('<h2>Cash drop off (to bank)</h2>')
    cash_up_panel_start = body.index('<h2>Cash up · Branch 1</h2>')
    assert drop_panel_start < cash_up_panel_start
    assert body.count('<h2>Cash up · Branch 1</h2>') == 1


def test_dashboard_explains_cash_up_plainly(client):
    login(client)
    body = client.get('/dashboard').get_data(as_text=True)
    assert 'Cash up is tracked separately for each depot.' in body
    assert 'Opening cash is the previous recorded closing cash for Branch 1.' in body
    assert 'Expected cash = opening cash + cash received − cash used − cash dropped off at the bank.' in body
    assert 'Variance = counted cash − expected cash.' in body


def test_end_of_day_notes_have_their_own_panel(client):
    login(client)
    body = client.get('/dashboard').get_data(as_text=True)
    cash_up_start = body.index('<h2>Cash up · Branch 1</h2>')
    notes_start = body.index('<h2>End of day notes</h2>')
    next_section_after_cash_up = body.index('</section>', cash_up_start)
    assert cash_up_start < notes_start
    assert next_section_after_cash_up < notes_start
    notes_panel = body[notes_start:body.index('</section>', notes_start)]
    assert 'action="/cash-up/notes"' in notes_panel
    assert 'name="notes"' in notes_panel
    assert 'Anything the next shift should know' in notes_panel


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


def test_new_client_interactions_render_defaults_save_and_reach_reports(client, app):
    login(client)
    body = client.get('/dashboard').get_data(as_text=True)
    assert 'New client interactions' in body
    assert body.count('<h2>New client interactions</h2>') == 1
    for expected in [
        'name="interaction_calls" value="" placeholder="0"',
        'name="interaction_whatsapp" value="" placeholder="0"',
        'name="interaction_emails" value="" placeholder="0"',
        'name="interaction_walk_in" value="" placeholder="0"',
        'Notes for new client interactions',
    ]:
        assert expected in body

    body = client.post('/cash-up/interactions', data={
        'day': '',
        'branch': '1',
        'interaction_calls': '4',
        'interaction_whatsapp': '3',
        'interaction_emails': '2',
        'interaction_walk_in': '1',
        'interaction_notes': 'Two quote follow-ups needed.',
    }, follow_redirects=True).get_data(as_text=True)
    assert 'New client interactions saved' in body
    assert 'name="interaction_calls" value="" placeholder="0"' in body
    assert 'name="interaction_whatsapp" value="" placeholder="0"' in body
    assert 'name="interaction_emails" value="" placeholder="0"' in body
    assert 'name="interaction_walk_in" value="" placeholder="0"' in body
    assert 'Two quote follow-ups needed.' in body

    summary = _summary(app)
    assert summary['interactions'] == {
        'calls': 4,
        'whatsapp': 3,
        'emails': 2,
        'walk_in': 1,
        'notes': 'Two quote follow-ups needed.',
    }
    csv_body = client.get('/cash-up/export.csv').get_data(as_text=True)
    for expected in [
        'New client interactions,Calls,4',
        'New client interactions,WhatsApp,3',
        'New client interactions,Emails,2',
        'New client interactions,Walk-in,1',
        'New client interactions,Notes,Two quote follow-ups needed.',
    ]:
        assert expected in csv_body
    drawn = _drawn_text(client.get('/cash-up/report.pdf').data)
    for expected in ['NEW CLIENT INTERACTIONS', 'Calls', '4', 'WhatsApp', '3',
                     'Emails', '2', 'Walk-in', '1', 'Notes', 'Two quote follow-ups needed.']:
        assert expected in drawn


def test_new_client_interactions_reject_junk_and_negative_counts(client, app):
    login(client)
    body = client.post('/cash-up/interactions', data={
        'day': '', 'interaction_calls': '-1', 'interaction_whatsapp': '0',
        'interaction_emails': '0', 'interaction_walk_in': '0',
    }, follow_redirects=True).get_data(as_text=True)
    assert 'Calls cannot be negative' in body
    body = client.post('/cash-up/interactions', data={
        'day': '', 'interaction_calls': '1', 'interaction_whatsapp': 'many',
        'interaction_emails': '0', 'interaction_walk_in': '0',
    }, follow_redirects=True).get_data(as_text=True)
    assert 'WhatsApp must be a whole number' in body
    assert _cash_rows(app) == [], 'a refused interaction form must not open the day row'


def test_new_client_interactions_are_scoped_to_the_acting_depot(client, app):
    login(client)
    client.post('/cash-up/interactions', data={
        'day': '', 'branch': '1', 'interaction_calls': '9',
        'interaction_whatsapp': '0', 'interaction_emails': '0', 'interaction_walk_in': '0',
    }, follow_redirects=True)
    with app.app_context():
        create_additional_user('Depot Two Clerk', 'staff123', branch_id=2)
    login(client, name='Depot Two Clerk', password='staff123')
    body = client.post('/cash-up/interactions', data={
        'day': '', 'branch': '1', 'interaction_calls': '2',
        'interaction_whatsapp': '1', 'interaction_emails': '1', 'interaction_walk_in': '1',
        'interaction_notes': 'Depot two own walk-in.',
    }, follow_redirects=True).get_data(as_text=True)
    assert 'cash_branch=2' in body or 'Cash up · Branch 2' in body
    rows = {row['branch_id']: row for row in _cash_rows(app)}
    assert rows[1]['interaction_calls'] == 9
    assert rows[2]['interaction_calls'] == 2
    assert rows[2]['interaction_whatsapp'] == 1
    assert rows[2]['interaction_emails'] == 1
    assert rows[2]['interaction_walk_in'] == 1
    assert rows[2]['interaction_notes'] == 'Depot two own walk-in.'


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


def _drop_rows(app):
    with app.app_context():
        return [dict(row) for row in get_db().execute('SELECT * FROM cash_bank_drops').fetchall()]


def test_a_bank_drop_reduces_the_cash_expected_in_the_drawer(client, app):
    """Ticket ABI-341952952 item 1: cash going to the bank comes off the drawer."""
    _seed_payment(app, 500.0, method='cash', day=TODAY, branch_id=1)
    login(client)
    body = client.post('/cash-up/bank', data={'day': '', 'amount': '200'}, follow_redirects=True).get_data(as_text=True)
    assert 'Bank drop off added' in body
    summary = _summary(app)
    # opening 0 + cash 500 - used 0 - banked 200 = 300 expected.
    assert summary['drop_total'] == 200.0
    assert summary['drop_count'] == 1
    assert summary['expected'] == 300.0
    # The panel shows the section, the line and its total.
    assert 'Cash drop off (to bank)' in body
    assert 'Dropped off at the bank' in body
    assert 'Total dropped at the bank' in body

    # Cash the drawer up at the reduced figure: it balances, which is the point.
    client.post('/cash-up', data={'day': '', 'counted_cash': '300.00'}, follow_redirects=True)
    summary = _summary(app)
    assert summary['variance'] == 0.0
    assert summary['variance_label'] == 'Balanced'

    # Both downloads state what went to the bank, and the arithmetic on screen does too.
    csv_body = client.get('/cash-up/export.csv').get_data(as_text=True)
    assert 'Total dropped at the bank,R200.00' in csv_body
    assert 'Expected cash in the drawer,R300.00' in csv_body
    assert 'Cash drop off (to bank),Dropped at the bank' in csv_body
    blob = client.get('/cash-up/report.pdf').data
    assert 'CASH DROP OFF \\(TO BANK\\)' in blob.decode('latin-1')
    # The cards draw the label and its figure separately, so read them as drawn.
    cards = _card_values(blob)
    assert cards['Expected cash in the drawer'] == 'R300.00'
    assert cards['Total dropped at the bank'] == 'R200.00'


def test_a_bank_drop_needs_a_positive_amount(client, app):
    login(client)
    body = client.post('/cash-up/bank', data={'day': '', 'amount': 'x'},
                       follow_redirects=True).get_data(as_text=True)
    assert 'Enter the amount dropped off at the bank' in body
    for value in ('0', '-20'):
        body = client.post('/cash-up/bank', data={'day': '', 'amount': value},
                           follow_redirects=True).get_data(as_text=True)
        assert 'The amount dropped off at the bank must be more than zero' in body
    assert _drop_rows(app) == []
    assert _cash_rows(app) == [], 'a refused amount must not even open the day'


def test_bank_drop_lines_can_be_added_and_removed(client, app):
    login(client)
    client.post('/cash-up/bank', data={'day': '', 'amount': '300'}, follow_redirects=True)
    body = client.post('/cash-up/bank', data={'day': '', 'amount': '150.50'},
                       follow_redirects=True).get_data(as_text=True)
    assert '2 lines' in body
    assert 'R450.50' in body, 'the two lines total R450.50'
    assert _summary(app)['expected'] == -450.5, 'nothing received, so the drawer is short by what left it'

    entry_id = _drop_rows(app)[0]['id']
    client.post(f'/cash-up/bank/{entry_id}/delete', data={'day': ''}, follow_redirects=True)
    remaining = _drop_rows(app)
    assert [row['amount'] for row in remaining] == [150.5]
    body = client.get('/dashboard').get_data(as_text=True)
    assert 'R150.50' in body
    assert '1 line' in body
    assert _summary(app)['drop_total'] == 150.5


def test_another_depots_bank_drop_line_cannot_be_deleted(client, app):
    login(client)  # owner (all depots) banks cash against depot 1
    client.post('/cash-up/bank', data={'day': '', 'amount': '20', 'branch': '1'}, follow_redirects=True)
    entry_id = _drop_rows(app)[0]['id']
    with app.app_context():
        create_additional_user('Depot Two Clerk', 'staff123', branch_id=2)
    login(client, name='Depot Two Clerk', password='staff123')
    body = client.post(f'/cash-up/bank/{entry_id}/delete', data={'day': ''},
                       follow_redirects=True).get_data(as_text=True)
    assert 'Bank drop off line not found' in body
    assert len(_drop_rows(app)) == 1, "another depot's line must survive a crafted delete"


def test_a_depot_can_only_bank_its_own_drawer(client, app):
    login(client)
    client.post('/cash-up/bank', data={'day': '', 'amount': '50', 'branch': '2'}, follow_redirects=True)
    with app.app_context():
        create_additional_user('Depot Two Clerk', 'staff123', branch_id=2)
    login(client, name='Depot Two Clerk', password='staff123')
    # A forged depot 1 post lands on their own drawer instead (never depot 1).
    client.post('/cash-up/bank', data={'day': '', 'amount': '10', 'branch': '1'}, follow_redirects=True)
    with app.app_context():
        rows = [dict(row) for row in get_db().execute(
            """SELECT d.amount AS amount, c.branch_id AS branch_id FROM cash_bank_drops d
            JOIN cash_ups c ON c.id = d.cash_up_id ORDER BY d.id""").fetchall()]
    assert [(row['branch_id'], row['amount']) for row in rows] == [(2, 50.0), (2, 10.0)]


def test_the_pdf_stays_on_one_page_with_bank_drop_offs_too(client, app):
    """A dozen drop offs still fit the one-page report without losing a line."""
    login(client)
    for index in range(12):
        client.post('/cash-up/bank', data={'day': '', 'amount': '10'}, follow_redirects=True)
    blob = client.get('/cash-up/report.pdf').data
    text = blob.decode('latin-1')
    assert 'CASH DROP OFF \\(TO BANK\\)' in text
    assert text.count('/Type /Page ') == 1, 'the report renders exactly one page'
    # The card layout has room for far more than the old 42-line page: every one
    # of the twelve fits, so nothing needs to be declared as left out.
    assert text.count('Dropped at the bank') == 12, text.count('Dropped at the bank')
    assert 'see the CSV export' not in text
    # Nothing is drawn outside the printable band (the page footer is the only
    # run below the content floor, and it is a fixed part of the layout).
    baselines = _baselines(blob)
    assert baselines, 'the report must draw its lines'
    assert min(baselines) >= pdf_documents.REPORT_FOOTER_Y
    assert min(y for y in baselines if y != pdf_documents.REPORT_FOOTER_Y) >= pdf_documents.REPORT_BOTTOM
    # The CSV export is uncapped, so all twelve drop offs survive there too.
    csv_body = client.get('/cash-up/export.csv').get_data(as_text=True)
    assert csv_body.count('Cash drop off (to bank),Dropped at the bank') == 12


def test_a_busy_day_keeps_both_capped_sections_on_the_page(client, app):
    """A day full of banked cash must not blank the described cash-used lines."""
    login(client)
    for index in range(12):
        client.post('/cash-up/used', data={'day': '', 'amount': '10', 'description': f'Line {index + 1}'},
                    follow_redirects=True)
        client.post('/cash-up/bank', data={'day': '', 'amount': '10'}, follow_redirects=True)
    client.post('/cash-up/notes', data={'day': '', 'notes': 'One.\nTwo.\nThree.'},
                follow_redirects=True)
    blob = client.get('/cash-up/report.pdf').data
    text = blob.decode('latin-1')
    drawn = _drawn_text(blob)
    # 24 lines cannot all fit: both capped sections share the page evenly and
    # each states how many of its own lines went to the CSV instead.
    assert 'Line 1' in drawn and 'Line 6' in drawn and 'Line 7' not in drawn
    assert text.count('Dropped at the bank') == 6, text.count('Dropped at the bank')
    assert 'more cash used line' in text and 'more bank drop off line' in text
    # The end of day notes keep their slot in the same round-robin share.
    assert 'One.' in drawn and 'Three.' in drawn
    assert text.count('/Type /Page ') == 1
    baselines = _baselines(blob)
    assert min(y for y in baselines if y != pdf_documents.REPORT_FOOTER_Y) >= pdf_documents.REPORT_BOTTOM
    # The CSV export still carries every one of the 24 lines.
    csv_body = client.get('/cash-up/export.csv').get_data(as_text=True)
    assert csv_body.count('Cash drop off (to bank),Dropped at the bank') == 12
    assert csv_body.count('Cash used,Line ') == 12


def test_deleting_a_depot_takes_its_bank_drop_lines_with_it(client, app):
    login(client)
    client.post('/cash-up/bank', data={'day': '', 'amount': '40', 'branch': '1'}, follow_redirects=True)
    client.post('/cash-up/bank', data={'day': '', 'amount': '60', 'branch': '2'}, follow_redirects=True)
    assert len(_drop_rows(app)) == 2
    client.post('/branches/2/delete', data={}, follow_redirects=True)
    with app.app_context():
        remaining = [dict(row) for row in get_db().execute(
            """SELECT d.amount AS amount, c.branch_id AS branch_id FROM cash_bank_drops d
            JOIN cash_ups c ON c.id = d.cash_up_id""").fetchall()]
    assert [(row['branch_id'], row['amount']) for row in remaining] == [(1, 40.0)]


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
    drawn = _drawn_text(res.data)
    for expected in ['Daily dashboard report', 'DASHBOARD', 'CASH UP', 'CASH USED',
                     'CASH DROP OFF (TO BANK)', 'END OF DAY NOTES', 'Diesel', 'R150.50',
                     'Drawer counted twice.']:
        assert expected in drawn, expected
    cards = _card_values(res.data)
    assert cards['Total cash payments'] == 'R500.00'
    assert cards['Expected cash in the drawer'] == 'R349.50'
    assert cards['Closing cash (counted)'] == 'R400.00'
    # Em dashes and other non-ASCII characters from the notes must not arrive as "?".
    notes_index = drawn.index('END OF DAY NOTES')
    assert '?' not in ''.join(drawn[notes_index:])


def test_the_pdf_stays_on_one_page_and_says_what_it_left_out(client, app):
    login(client)
    for index in range(16):
        client.post('/cash-up/used', data={'day': '', 'amount': '10', 'description': f'Line {index + 1}'},
                    follow_redirects=True)
    blob = client.get('/cash-up/report.pdf').data
    drawn = _drawn_text(blob)
    assert 'Line 14' in drawn and 'Line 15' not in drawn
    # Parentheses are escaped in the PDF stream, so match the plain wording.
    assert '... and 2 more cash used line' in blob.decode('latin-1')
    assert 'see the CSV export' in blob.decode('latin-1')
    assert blob.decode('latin-1').count('/Type /Page ') == 1
    # The CSV export is uncapped, so nothing is actually lost.
    csv_body = client.get('/cash-up/export.csv').get_data(as_text=True)
    assert 'Line 16,R10.00' in csv_body


def test_the_pdf_report_is_branded_and_names_the_user_who_ran_it(client, app):
    """Ticket ABI-341952956: the day report carries the logo, cards and the user."""
    login(client)
    client.post('/cash-up', data={'day': '', 'counted_cash': '100.00'}, follow_redirects=True)
    blob = client.get('/cash-up/report.pdf').data
    text = blob.decode('latin-1')
    # The SANO wordmark is embedded as a JPEG XObject and actually drawn.
    assert '/Subtype /Image' in text and '/Filter /DCTDecode' in text
    assert '/XObject << /Im1 7 0 R >>' in text
    assert 'cm /Im1 Do Q' in text
    drawn = _drawn_text(blob)
    assert 'Sano Trailers' in drawn
    assert 'Daily dashboard report' in drawn
    assert f'Business day: {TODAY}' in drawn
    assert 'Prepared by: Head office admin (Main profile)' in drawn
    # The figures are cards with their own panel, not a plain list of lines.
    assert text.count(' re B Q') >= 12, 'the report draws card panels'
    cards = _card_values(blob)
    assert cards['New orders for the day'] == '0'
    assert cards['Closing cash (counted)'] == 'R100.00'
    # A depot user's own name is what prints, not a fixed label.
    with app.app_context():
        create_additional_user('Depot Two Clerk', 'staff123', branch_id=2)
    login(client, name='Depot Two Clerk', password='staff123')
    text = client.get('/cash-up/report.pdf').data.decode('latin-1')
    assert 'Prepared by: Depot Two Clerk \\(Staff\\)' in text


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
            if 'from cash_bank_drops' in statement:
                return Cursor([
                    Row(('id', 'amount', 'created_at'), (1, 200.0, '2026-09-13T14:30:00')),
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
    assert summary['expected'] == 1149.5  # 1000 opening + 500 cash - 150.50 used - 200 banked
    assert summary['variance'] == -800.0  # counted 349.50 - expected 1149.50
    assert summary['used_lines'] == [{'id': 1, 'amount': 150.5, 'description': 'Diesel',
                                      'created_at': '2026-09-13T09:00:00'}]
    assert summary['drop_lines'] == [{'id': 1, 'amount': 200.0,
                                      'created_at': '2026-09-13T14:30:00'}]
    assert summary['drop_total'] == 200.0 and summary['drop_count'] == 1
    for value in (summary['opening'], summary['expected'], summary['counted'], summary['variance']):
        assert isinstance(value, float), value
    assert isinstance(summary['used_count'], int)
    body = client.get('/dashboard').get_data(as_text=True)
    assert 'built-in method' not in body


def test_the_depot_chooser_appears_only_for_a_multi_depot_sign_in(client, app):
    login(client)
    body = client.get('/dashboard').get_data(as_text=True)
    assert 'name="cash_branch"' in body, 'an all-branch viewer picks which drawer to cash up'
    assert body.count('name="cash_branch"') == 1, 'a hidden duplicate would win over the picker'
    assert 'name="branch"' in body, 'each write form must carry the chosen depot'

    with app.app_context():
        create_additional_user('Depot Two Clerk', 'staff123', branch_id=2)
    login(client, name='Depot Two Clerk', password='staff123')
    body = client.get('/dashboard').get_data(as_text=True)
    assert 'name="cash_branch"' not in body, 'a single-depot account gets a fixed label, not a chooser'
    assert 'Cash up · Branch 2' in body


def test_a_chosen_depot_is_the_one_cashed_up(client, app):
    _seed_payment(app, 500.0, method='cash', day=TODAY, branch_id=1, number='ORD-90001')
    _seed_payment(app, 700.0, method='cash', day=TODAY, branch_id=2, number='ORD-90002')
    login(client)
    body = client.get('/dashboard?cash_branch=2').get_data(as_text=True)
    assert 'Cash up · Branch 2' in body
    assert re.search(r'Cash received</small>\s*<b>R700\.00</b>', body), 'depot 2 cash only'
    # The chosen depot is what the form writes to, and the reply keeps showing it.
    res = client.post('/cash-up', data={'day': '', 'branch': '2', 'counted_cash': '700.00'})
    assert res.status_code == 302
    assert 'cash_branch=2' in res.headers['Location'], res.headers['Location']
    rows = {row['branch_id']: row for row in _cash_rows(app)}
    assert rows[2]['counted_cash'] == 700.0
    assert 1 not in rows, 'nothing was written for depot 1'
    # Downloads follow the chosen depot, and the CSV figures match the panel.
    csv_body = client.get('/cash-up/export.csv?branch=2').get_data(as_text=True)
    assert 'Depot,Branch 2' in csv_body
    assert 'Cash received for the day,R700.00' in csv_body


def test_dashboard_branch_filter_defaults_the_cash_panel_to_the_same_depot(client, app):
    _seed_payment(app, 500.0, method='cash', day=TODAY, branch_id=1, number='ORD-90001')
    _seed_payment(app, 700.0, method='cash', day=TODAY, branch_id=2, number='ORD-90002')
    login(client)
    body = client.get('/dashboard?branch=2').get_data(as_text=True)
    assert 'Cash up · Branch 2' in body
    assert re.search(r'Cash received</small>\s*<b>R700\.00</b>', body), 'top branch 2 drives cash panel'
    assert 'name="branch" value="2"' in body, 'cash write forms carry the same depot'


def test_explicit_cash_branch_still_overrides_the_top_dashboard_branch(client, app):
    _seed_payment(app, 500.0, method='cash', day=TODAY, branch_id=1, number='ORD-90001')
    _seed_payment(app, 700.0, method='cash', day=TODAY, branch_id=2, number='ORD-90002')
    login(client)
    body = client.get('/dashboard?branch=2&cash_branch=1').get_data(as_text=True)
    assert 'Cash up · Branch 1' in body
    assert re.search(r'Cash received</small>\s*<b>R500\.00</b>', body), 'explicit cash_branch wins'
    assert 'name="branch" value="1"' in body, 'cash write forms carry the explicit depot'


def test_a_crafted_depot_can_never_widen_a_cash_up(client, app):
    login(client)
    client.post('/cash-up', data={'day': '', 'branch': '2', 'counted_cash': '300.00'},
                follow_redirects=True)
    with app.app_context():
        create_additional_user('Depot Two Clerk', 'staff123', branch_id=2)
    login(client, name='Depot Two Clerk', password='staff123')
    # Their own depot renders even when the query string asks for another one,
    # and a forged depot 1 post is ignored rather than honoured.
    body = client.get('/dashboard?cash_branch=1').get_data(as_text=True)
    assert 'Cash up · Branch 2' in body
    assert 'name="cash_branch"' not in body, 'no depot chooser for a pinned account'
    client.post('/cash-up', data={'day': '', 'branch': '1', 'counted_cash': '999.00'},
                follow_redirects=True)
    client.post('/cash-up', data={'day': '', 'branch': '999', 'counted_cash': '888.00'},
                follow_redirects=True)
    rows = {row['branch_id']: row for row in _cash_rows(app)}
    assert 1 not in rows, 'depot 1 must be untouched by another depot'
    assert rows[2]['counted_cash'] == 888.0, 'a junk id falls back to their own depot'
    assert 999 not in rows


def test_branch_for_request_resolves_inside_the_session_scope(app):
    with app.test_request_context('/dashboard'):
        flask_session['user_id'] = 1
        flask_session['user_role'] = 'staff'
        flask_session['branch_id'] = 2
        flask_session['can_view_all_branches'] = 0
        flask_session['branch_ids'] = []
        assert [row['id'] for row in cash.cash_branches()] == [2]
        assert cash.branch_for_request('1') == 2, 'another depot is refused, not honoured'
        assert cash.branch_for_request('2') == 2
        assert cash.branch_for_request('junk') == 2
        assert cash.panel_state('1')['branch_name'] == 'Branch 2'
    with app.test_request_context('/dashboard'):
        flask_session['user_id'] = 1
        flask_session['user_role'] = 'owner'
        assert len(cash.cash_branches()) == 3
        assert cash.branch_for_request('3') == 3
        assert cash.branch_for_request('999') == 1, 'junk falls back to the acting depot'


def test_dashboard_has_submit_day_report_button(client):
    login(client)
    body = client.get('/dashboard').get_data(as_text=True)
    assert 'Submit day report' in body
    assert 'id="submit-day-report-form" method="post" action="/cash-up/report/telegram"' in body
    assert '<button class="btn warning" type="submit">Submit day report</button>' in body
    assert body.index('Day report actions') > body.index('Cash drop off (to bank)')


def test_dashboard_download_report_buttons_are_main_profile_only(client, app):
    login(client)
    body = client.get('/dashboard').get_data(as_text=True)
    assert 'Download day report (PDF)' in body
    assert 'Download CSV' in body
    assert 'Submit day report' in body
    assert '<button class="btn warning" type="submit">Submit day report</button>' in body

    with app.app_context():
        create_additional_user('Depot Two Clerk', 'staff123', branch_id=2)
    login(client, name='Depot Two Clerk', password='staff123')
    body = client.get('/dashboard').get_data(as_text=True)
    assert 'Submit day report' in body
    assert '<button class="btn warning" type="submit">Submit day report</button>' in body
    assert 'Download day report (PDF)' not in body
    assert 'Download CSV' not in body


def test_submit_day_report_requires_cash_up_amount_before_sending(client, app, monkeypatch):
    sent = {'called': False}

    def fake_send(*_args, **_kwargs):
        sent['called'] = True
        return {'ok': True, 'sent': True, 'status': 200}

    from app.routes import cash as cash_routes
    monkeypatch.setattr(cash_routes, '_send_document', fake_send)
    login(client)
    body = client.post('/cash-up/report/telegram', data={'day': TODAY, 'branch': '1'}, follow_redirects=True).get_data(as_text=True)
    assert 'Cash up amount must be filled before submitting the day report' in body
    assert sent['called'] is False


def test_submit_day_report_sends_the_existing_pdf_to_telegram(client, app, monkeypatch):
    sent = {}

    def fake_send(document_bytes, filename, caption=''):
        sent['document_bytes'] = document_bytes
        sent['filename'] = filename
        sent['caption'] = caption
        return {'ok': True, 'sent': True, 'status': 200}

    from app.routes import cash as cash_routes
    monkeypatch.setattr(cash_routes, '_send_document', fake_send)
    login(client)
    client.post('/cash-up', data={'day': TODAY, 'branch': '1', 'counted_cash': '0'}, follow_redirects=True)
    res = client.post('/cash-up/report/telegram', data={'day': TODAY, 'branch': '1'}, follow_redirects=True)
    body = res.get_data(as_text=True)
    assert res.status_code == 200
    assert 'Day report sent on Telegram for Branch 1' in body
    assert sent['filename'] == f'dashboard-report-{TODAY}.pdf'
    assert sent['document_bytes'].startswith(b'%PDF-')
    assert sent['document_bytes'].rstrip().endswith(b'%%EOF')
    assert sent['caption'] == f'Day report — Branch 1 — {TODAY}'


def test_submit_day_report_cannot_widen_a_branch_limited_account(client, app, monkeypatch):
    sent = {}

    def fake_send(_document_bytes, _filename, caption=''):
        sent['caption'] = caption
        return {'ok': True, 'sent': True, 'status': 200}

    from app.routes import cash as cash_routes
    monkeypatch.setattr(cash_routes, '_send_document', fake_send)
    with app.app_context():
        create_additional_user('Depot Two Clerk', 'staff123', branch_id=2)
    login(client, name='Depot Two Clerk', password='staff123')
    client.post('/cash-up', data={'day': TODAY, 'branch': '2', 'counted_cash': '0'}, follow_redirects=True)
    res = client.post('/cash-up/report/telegram', data={'day': TODAY, 'branch': '1'}, follow_redirects=False)
    assert res.status_code == 302
    assert 'cash_branch=2' in res.headers['Location']
    assert sent['caption'] == f'Day report — Branch 2 — {TODAY}'


def test_submit_day_report_flashes_when_telegram_is_disabled_or_not_configured(client, app):
    login(client)
    client.post('/cash-up', data={'day': TODAY, 'branch': '1', 'counted_cash': '0'}, follow_redirects=True)
    app.config.update(TELEGRAM_NOTIFICATIONS_ENABLED='')
    body = client.post('/cash-up/report/telegram', data={'day': TODAY, 'branch': '1'}, follow_redirects=True).get_data(as_text=True)
    assert 'Telegram notifications are disabled; day report was not sent' in body

    app.config.update(TELEGRAM_NOTIFICATIONS_ENABLED='true', TELEGRAM_BOT_TOKEN='', TELEGRAM_CHAT_ID='')
    body = client.post('/cash-up/report/telegram', data={'day': TODAY, 'branch': '1'}, follow_redirects=True).get_data(as_text=True)
    assert 'Telegram is not configured; day report was not sent' in body


def test_submit_day_report_failure_does_not_crash_dashboard(client, app, monkeypatch):
    from app.routes import cash as cash_routes
    monkeypatch.setattr(cash_routes, '_send_document', lambda *_args, **_kwargs: {'ok': False, 'sent': False, 'error': 'boom'})
    login(client)
    client.post('/cash-up', data={'day': TODAY, 'branch': '1', 'counted_cash': '0'}, follow_redirects=True)
    res = client.post('/cash-up/report/telegram', data={'day': TODAY, 'branch': '1'}, follow_redirects=True)
    body = res.get_data(as_text=True)
    assert res.status_code == 200
    assert 'Day report could not be sent on Telegram' in body
    assert 'Cash up · Branch 1' in body



def _seed_service_products(app):
    with app.app_context():
        db = get_db()
        ts = '2026-09-21 08:00'
        db.execute("INSERT INTO product_groups (name, active, sort_order, created_at, updated_at) VALUES ('Trailers', 1, 1, ?, ?)", (ts, ts))
        db.execute("INSERT INTO product_groups (name, active, sort_order, created_at, updated_at) VALUES ('Other rental products', 1, 2, ?, ?)", (ts, ts))
        trailers = db.execute("SELECT id FROM product_groups WHERE name = 'Trailers'").fetchone()['id']
        other = db.execute("SELECT id FROM product_groups WHERE name = 'Other rental products'").fetchone()['id']
        rows = [
            ('Road trailer', 'rental', trailers, 1, 1),
            ('Branch two trailer', 'rental', trailers, 1, 2),
            ('Hidden old trailer', 'rental', trailers, 0, 1),
            ('Generator rental', 'rental', other, 1, 1),
            ('Brake service', 'service', None, 1, 1),
            ('Ratchet sale', 'sale', None, 1, 1),
        ]
        ids = {}
        for name, product_type, group_id, active, branch_id in rows:
            db.execute(
                """INSERT INTO products (name, product_type, tracking_method, description, sku, active, public_visible,
                price_amount, price_unit, security_deposit, hourly_extra_rate, product_group_id, quantity, branch_id, created_at)
                VALUES (?, ?, 'bulk', '', ?, ?, 1, 100, 'day', 0, 0, ?, 1, ?, ?)""",
                (name, product_type, name.upper().replace(' ', '-'), active, group_id, branch_id, ts),
            )
            ids[name] = db.execute("SELECT id FROM products WHERE name = ?", (name,)).fetchone()['id']
        db.commit()
        return ids


def test_trailer_service_history_table_exists(app):
    with app.app_context():
        cols = {row['name'] for row in get_db().execute('PRAGMA table_info(trailer_service_history)').fetchall()}
    assert {'id', 'product_id', 'service_type', 'custom_description', 'service_date', 'branch_id', 'created_by_user_id', 'created_at'} <= cols


def test_dashboard_trailer_service_panel_filters_eligible_products(client, app):
    ids = _seed_service_products(app)
    login(client)
    body = client.get('/dashboard').get_data(as_text=True)
    assert body.index('<h2>New client interactions</h2>') < body.index('<h2>Trailer service and maintenance</h2>') < body.index('<h2>Cash used</h2>')
    assert 'action="/cash-up/trailer-service"' in body
    assert 'Add trailer' in body
    assert 'Road trailer' in body
    assert 'Bearing service' in body and 'Custom' in body
    assert 'Generator rental' not in body
    assert 'Brake service' not in body
    assert 'Ratchet sale' not in body
    assert 'Hidden old trailer' not in body


def test_add_trailer_service_saves_and_shows_on_product(client, app):
    ids = _seed_service_products(app)
    login(client)
    body = client.post('/cash-up/trailer-service', data={
        'day': TODAY,
        'branch': '1',
        'product_id': str(ids['Road trailer']),
        'service_type': 'Bearing service',
    }, follow_redirects=True).get_data(as_text=True)
    assert 'Trailer service and maintenance saved' in body
    with app.app_context():
        row = get_db().execute('SELECT * FROM trailer_service_history WHERE product_id = ?', (ids['Road trailer'],)).fetchone()
        assert row['service_type'] == 'Bearing service'
        assert row['service_date'] == TODAY
        assert row['branch_id'] == 1
    product_page = client.get(f"/inventory/{ids['Road trailer']}/edit").get_data(as_text=True)
    assert 'Trailer service and maintenance history' in product_page
    assert 'Bearing service' in product_page
    assert TODAY in product_page


def test_custom_trailer_service_requires_manual_description(client, app):
    ids = _seed_service_products(app)
    login(client)
    body = client.post('/cash-up/trailer-service', data={
        'day': TODAY, 'branch': '1', 'product_id': str(ids['Road trailer']), 'service_type': 'Custom', 'custom_description': '  '
    }, follow_redirects=True).get_data(as_text=True)
    assert 'Enter the custom service or maintenance done' in body
    body = client.post('/cash-up/trailer-service', data={
        'day': TODAY, 'branch': '1', 'product_id': str(ids['Road trailer']), 'service_type': 'Custom', 'custom_description': 'Welded jockey wheel bracket'
    }, follow_redirects=True).get_data(as_text=True)
    assert 'Trailer service and maintenance saved' in body
    product_page = client.get(f"/inventory/{ids['Road trailer']}/edit").get_data(as_text=True)
    assert 'Custom — Welded jockey wheel bracket' in product_page


def test_trailer_service_rejects_other_groups_and_branch_widening(client, app):
    ids = _seed_service_products(app)
    login(client)
    body = client.post('/cash-up/trailer-service', data={
        'day': TODAY, 'branch': '1', 'product_id': str(ids['Generator rental']), 'service_type': 'Tyre change'
    }, follow_redirects=True).get_data(as_text=True)
    assert 'Choose an active rental trailer from inventory' in body
    with app.app_context():
        create_additional_user('Depot Two Service', 'staff123', branch_id=2)
    login(client, name='Depot Two Service', password='staff123')
    body = client.post('/cash-up/trailer-service', data={
        'day': TODAY, 'branch': '1', 'product_id': str(ids['Road trailer']), 'service_type': 'Tyre change'
    }, follow_redirects=True).get_data(as_text=True)
    assert 'Choose an active rental trailer from inventory' in body
    body = client.post('/cash-up/trailer-service', data={
        'day': TODAY, 'branch': '1', 'product_id': str(ids['Branch two trailer']), 'service_type': 'Tyre change'
    }, follow_redirects=True).get_data(as_text=True)
    assert 'Trailer service and maintenance saved' in body
    with app.app_context():
        rows = get_db().execute('SELECT product_id, branch_id FROM trailer_service_history').fetchall()
        assert [(row['product_id'], row['branch_id']) for row in rows] == [(ids['Branch two trailer'], 2)]
