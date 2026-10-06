"""Ticket ABI-341953127 — Notes on the Cash up and POS Cashup sections.

Requested edit: "on the cash up and POS cash up sections, add Notes".

Both panels get their own Notes box, saved in a dedicated column
(``cash_ups.cash_up_notes`` / ``cash_ups.pos_cash_up_notes``) so a cash-up note
never overwrites the day's End of day notes (``cash_ups.notes``) — those keep
their own panel and their own ``/cash-up/notes`` route, and the tests below pin
that separation as well as the save/reload and the read-only views.
"""

import os
import tempfile

import pytest
from flask import session as flask_session

from app import create_app
from app.db import get_db, run_migrations
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
    return client.post('/login', data={'user_id': str(row['id']), 'password': password},
                       follow_redirects=True)


def seed_card_payment(app, amount=400.0, day=TODAY, branch_id=1, number='ORD-NOTE-1'):
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
            deleted_at, created_at) VALUES (?, ?, 'card', '', 'paid', ?, '', ?)""",
            (order_id, amount, day, f'{day} 09:00'),
        )
        db.commit()


def summary(app, day=TODAY, branch_id=1):
    with app.test_request_context('/dashboard'):
        flask_session['user_id'] = 1
        flask_session['user_role'] = 'owner'
        return cash.day_summary(day=day, branch_id=branch_id)


def row(app, day=TODAY, branch_id=1):
    with app.app_context():
        found = get_db().execute(
            'SELECT * FROM cash_ups WHERE branch_id = ? AND business_day = ?', (branch_id, day)
        ).fetchone()
        return dict(found) if found is not None else None


def panel(body, heading, next_heading=None):
    """The slice of the dashboard for one panel, so a box is proven in place."""
    start = body.index(heading)
    end = body.index(next_heading, start) if next_heading else body.index('</section>', start)
    return body[start:end]


def test_the_two_panels_have_their_own_notes_box(client):
    login(client)
    body = client.get('/dashboard?branch=1').get_data(as_text=True)

    cash_panel = panel(body, '<h2>Cash up · Branch 1</h2>', '<h2>POS Cashup')
    assert '<textarea name="cash_up_notes" rows="3"></textarea>' in cash_panel
    assert 'Notes' in cash_panel
    assert 'action="/cash-up"' in cash_panel
    assert 'Save cash up' in cash_panel

    pos_panel = panel(body, '<h2>POS Cashup · Branch 1</h2>', '<h2>Spare wheel count</h2>')
    assert '<textarea name="pos_cash_up_notes" rows="3"></textarea>' in pos_panel
    assert 'Notes' in pos_panel
    assert 'action="/cash-up/pos"' in pos_panel
    assert 'Save POS cashup' in pos_panel

    # The End of day notes panel keeps its own box and route untouched.
    notes_panel = panel(body, '<h2>End of day notes</h2>')
    assert '<textarea name="notes" rows="3">' in notes_panel
    assert 'action="/cash-up/notes"' in notes_panel


def test_a_cash_up_note_saves_reloads_and_does_not_touch_the_end_of_day_notes(client, app):
    login(client)
    body = client.post('/cash-up', data={
        'day': TODAY, 'branch': '1', 'counted_cash': '350.00',
        'cash_up_notes': 'Drawer short by R50, safe key replaced.',
    }, follow_redirects=True).get_data(as_text=True)
    assert 'Cash up saved' in body

    data = summary(app)
    assert data['cash_up_notes'] == 'Drawer short by R50, safe key replaced.'
    assert data['counted'] == 350.0
    assert data['cashed_up'] is True
    # The dedicated column means the end of day notes and the POS notes stay clear.
    assert data['notes'] == ''
    assert data['pos_cash_up_notes'] == ''

    stored = row(app)
    assert stored['cash_up_notes'] == 'Drawer short by R50, safe key replaced.'
    assert stored['notes'] == '' and stored['pos_cash_up_notes'] == ''

    # Re-opening the panel prefills the box with what was saved.
    body = client.get('/dashboard?branch=1').get_data(as_text=True)
    cash_panel = panel(body, '<h2>Cash up · Branch 1</h2>', '<h2>POS Cashup')
    assert ('<textarea name="cash_up_notes" rows="3">Drawer short by R50, safe key replaced.'
            '</textarea>') in cash_panel

    # Saving a new note replaces the old one rather than duplicating it.
    client.post('/cash-up', data={'day': TODAY, 'branch': '1', 'counted_cash': '350.00',
                                  'cash_up_notes': 'Second count balances.'},
                follow_redirects=True)
    assert summary(app)['cash_up_notes'] == 'Second count balances.'


def test_a_pos_note_saves_without_touching_the_counts_or_the_other_notes(client, app):
    seed_card_payment(app, 400.0)
    login(client)
    body = client.post('/cash-up/pos', data={
        'day': TODAY, 'branch': '1', 'counted_card': '390.50',
        'pos_cash_up_notes': 'Two card slips not yet settled at the bank.',
    }, follow_redirects=True).get_data(as_text=True)
    assert 'POS cashup saved' in body

    data = summary(app)
    assert data['pos_cash_up_notes'] == 'Two card slips not yet settled at the bank.'
    assert data['counted_card'] == 390.5
    assert data['pos_cashed_up'] is True
    # POS notes are their own column: the cash count, the cash-up notes and the
    # end of day notes are all untouched by a POS save.
    assert data['cashed_up'] is False
    assert data['cash_up_notes'] == ''
    assert data['notes'] == ''
    stored = row(app)
    assert stored['counted_cash'] is None
    assert stored['cash_up_notes'] == '' and stored['notes'] == ''

    body = client.get('/dashboard?branch=1').get_data(as_text=True)
    pos_panel = panel(body, '<h2>POS Cashup · Branch 1</h2>', '<h2>Spare wheel count</h2>')
    assert ('<textarea name="pos_cash_up_notes" rows="3">Two card slips not yet settled at the '
            'bank.</textarea>') in pos_panel


def test_both_notes_are_kept_side_by_side(client, app):
    seed_card_payment(app, 400.0)
    login(client)
    client.post('/cash-up', data={'day': TODAY, 'branch': '1', 'counted_cash': '350.00',
                                  'cash_up_notes': 'Cash note.'}, follow_redirects=True)
    client.post('/cash-up/pos', data={'day': TODAY, 'branch': '1', 'counted_card': '390.50',
                                      'pos_cash_up_notes': 'POS note.'}, follow_redirects=True)
    stored = row(app)
    assert stored['cash_up_notes'] == 'Cash note.'
    assert stored['pos_cash_up_notes'] == 'POS note.'
    assert stored['counted_cash'] == 350.0 and stored['counted_card'] == 390.5


def test_a_cash_up_never_blanks_the_end_of_day_notes(client, app):
    """The old end of day notes stay put when the drawer is cashed up."""
    login(client)
    client.post('/cash-up/notes', data={'day': TODAY, 'branch': '1',
                                        'notes': 'Two tyres booked for Monday.'},
                follow_redirects=True)
    client.post('/cash-up', data={'day': TODAY, 'branch': '1', 'counted_cash': '100.00',
                                  'cash_up_notes': 'Counted twice.'}, follow_redirects=True)
    client.post('/cash-up/pos', data={'day': TODAY, 'branch': '1', 'counted_card': '50.00',
                                      'pos_cash_up_notes': 'Card batch checked.'},
                follow_redirects=True)
    data = summary(app)
    assert data['notes'] == 'Two tyres booked for Monday.'
    assert data['cash_up_notes'] == 'Counted twice.'
    assert data['pos_cash_up_notes'] == 'Card batch checked.'


def test_a_legacy_notes_post_still_writes_the_end_of_day_notes(client, app):
    """A caller that still posts ``notes`` to /cash-up keeps the old behaviour."""
    login(client)
    client.post('/cash-up', data={'day': TODAY, 'branch': '1', 'counted_cash': '100.00',
                                  'notes': 'Legacy note.'}, follow_redirects=True)
    data = summary(app)
    assert data['notes'] == 'Legacy note.'
    assert data['cash_up_notes'] == ''


def test_the_notes_are_read_only_on_a_past_day(client, app):
    login(client)
    with app.app_context():
        cash.save_cash_up(YESTERDAY, '500.00', branch_id=1, cash_up_notes='Past day cash note.')
        cash.save_pos_cash_up(YESTERDAY, '250.00', branch_id=1, pos_cash_up_notes='Past day POS note.')
    body = client.get(f'/dashboard?branch=1&day={YESTERDAY}').get_data(as_text=True)

    assert 'Past day cash note.' in body
    assert 'Past day POS note.' in body
    assert 'name="cash_up_notes"' not in body, 'a filed day must not offer the box again'
    assert 'name="pos_cash_up_notes"' not in body
    cash_panel = panel(body, '<h2>Cash up · Branch 1</h2>', '<h2>POS Cashup')
    assert 'Past day cash note.' in cash_panel
    pos_panel = panel(body, '<h2>POS Cashup · Branch 1</h2>', '<h2>Spare wheel count</h2>')
    assert 'Past day POS note.' in pos_panel


def test_the_all_branches_view_shows_each_depots_notes(client, app):
    login(client)
    with app.app_context():
        cash.save_cash_up(TODAY, '100.00', branch_id=1, cash_up_notes='Depot one cash note.')
        cash.save_pos_cash_up(TODAY, '60.00', branch_id=1, pos_cash_up_notes='Depot one POS note.')
        cash.save_cash_up(TODAY, '200.00', branch_id=2, cash_up_notes='Depot two cash note.')
    body = client.get('/dashboard').get_data(as_text=True)

    assert 'Branch 1: Depot one cash note.' in body
    assert 'Branch 2: Depot two cash note.' in body
    assert 'Branch 1: Depot one POS note.' in body
    with app.test_request_context('/dashboard'):
        flask_session['user_id'] = 1
        flask_session['user_role'] = 'owner'
        data = cash.aggregate_day_summary(day=TODAY)
    assert data['cash_up_notes'].splitlines() == ['Branch 1: Depot one cash note.',
                                                  'Branch 2: Depot two cash note.']
    assert data['pos_cash_up_notes'] == 'Branch 1: Depot one POS note.'


def test_a_staff_account_cannot_write_another_depots_notes(client, app):
    with app.app_context():
        create_additional_user('Depot Two Clerk', 'staff123', branch_id=2)
    login(client, name='Depot Two Clerk', password='staff123')
    client.post('/cash-up', data={'day': TODAY, 'branch': '1', 'counted_cash': '10.00',
                                  'cash_up_notes': 'Forged depot one note.'},
                follow_redirects=True)
    client.post('/cash-up/pos', data={'day': TODAY, 'branch': '1', 'counted_card': '5.00',
                                      'pos_cash_up_notes': 'Forged depot one POS note.'},
                follow_redirects=True)
    assert row(app, branch_id=1) is None, 'depot 1 must stay untouched'
    stored = row(app, branch_id=2)
    assert stored['cash_up_notes'] == 'Forged depot one note.'
    assert stored['pos_cash_up_notes'] == 'Forged depot one POS note.'


def test_a_future_day_note_is_refused(client, app):
    login(client)
    body = client.post('/cash-up', data={'day': FUTURE, 'branch': '1', 'counted_cash': '10',
                                        'cash_up_notes': 'Tomorrow.'},
                       follow_redirects=True).get_data(as_text=True)
    assert 'Cash up today or a past day only' in body
    assert row(app, day=FUTURE) is None


def test_the_note_columns_are_in_schema_and_migration(app):
    with app.app_context():
        db = get_db()
        columns = {r['name'] for r in db.execute('PRAGMA table_info(cash_ups)').fetchall()}
        assert {'cash_up_notes', 'pos_cash_up_notes'} <= columns
        db.execute('ALTER TABLE cash_ups DROP COLUMN cash_up_notes')
        db.execute('ALTER TABLE cash_ups DROP COLUMN pos_cash_up_notes')
        run_migrations(db)
        columns = {r['name'] for r in db.execute('PRAGMA table_info(cash_ups)').fetchall()}
        assert {'cash_up_notes', 'pos_cash_up_notes'} <= columns
        # A row that predates the columns reads back as empty text, never NULL.
        db.execute(
            """INSERT INTO cash_ups (branch_id, business_day, opening_cash, counted_cash,
            created_at, updated_at) VALUES (1, '2026-01-05', 0, 0, '', '')"""
        )
        db.commit()
        migrated = db.execute(
            "SELECT cash_up_notes, pos_cash_up_notes FROM cash_ups WHERE business_day = '2026-01-05'"
        ).fetchone()
        assert (migrated['cash_up_notes'], migrated['pos_cash_up_notes']) == ('', '')
