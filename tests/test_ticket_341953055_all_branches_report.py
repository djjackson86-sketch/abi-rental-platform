"""Ticket ABI-341953055 - the combined day report follows the last depot's submission.

Requested edit: *"after all branches have submitted the pdf report for the day,
send the main user pdf dashboard report to the telegram"*.

What is pinned here:

* pressing **Submit day report** records that depot's submission for the business
  day (idempotently - a re-submit or a retry counts once);
* the combined ``All branches_Dashboard Report_<day>.pdf`` is posted **once**, and
  only when *every active depot* has submitted: two-of-three sends nothing;
* that combined report is the **whole business's** report even though the sign-in
  that triggers it is usually the last depot's own, depot-scoped staff member -
  combined cash figures, company-wide dashboard cards, the owner's name in
  "Prepared by", never that staff member's;
* it is idempotent: a re-submit, a retry and a repeat sweep can never post a
  second copy for the same business day (``day_report_sends``);
* a failed combined send writes no marker, so the cron-secret gated retry
  endpoint (the sweep) is allowed to deliver it - and does exactly once;
* quiet paths are quiet and safe: kill switch off, Telegram unconfigured, a
  partial day and an inactive depot all send nothing, and none of them can break
  the staff member's own submit (which keeps its own flash and redirect);
* the depot scope is never widened: ``access.all_depots_scope()`` is in-process
  only and leaves the session scope exactly as it found it.
"""

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
from app.db import get_db, run_migrations  # noqa: E402
from app.routes import cash as cash_routes  # noqa: E402
from app.services import access, cash, telegram  # noqa: E402
from app.services.access import MODULE_KEYS, create_additional_user  # noqa: E402

TODAY = cash.today_iso()
PAST = (date.fromisoformat(TODAY) - timedelta(days=2)).isoformat()

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
        # Telegram is fully switched ON and configured, so anything that sends
        # really would send: the recorder below is what proves what does.
        'TELEGRAM_NOTIFICATIONS_ENABLED': 'true',
        'TELEGRAM_BOT_TOKEN': 'token',
        'TELEGRAM_CHAT_ID': 'chat',
        'TELEGRAM_CRON_SECRET': 'secret',
    })
    yield application
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def sends(monkeypatch):
    """Every document that would have gone to Telegram, in order.

    Both call sites are recorded: the route's own per-depot send and the
    service's combined one. The recorder honours the same enable/config gates the
    real ``_send_document`` does (and never touches the network), so a test can
    flip a switch and see the real outcome.
    """
    recorded = []

    def fake(document_bytes, filename, caption='', chat_id=None):
        if not telegram.notifications_enabled():
            return {'ok': True, 'sent': False, 'skipped': 'disabled'}
        if not telegram.telegram_configured():
            return {'ok': True, 'sent': False, 'skipped': 'not_configured'}
        recorded.append({
            'filename': filename,
            'caption': caption,
            'chat_id': chat_id,
            'bytes': document_bytes,
        })
        return {'ok': True, 'sent': True, 'status': 200}

    monkeypatch.setattr(telegram, '_send_document', fake)
    monkeypatch.setattr(cash_routes, '_send_document', fake)
    return recorded


def combined(sends):
    return [send for send in sends if send['filename'].startswith('All branches_')]


def depot_sends(sends):
    return [send for send in sends if not send['filename'].startswith('All branches_')]


def card_value(drawn, label):
    """The value the report draws immediately after a card's label."""
    index = drawn.index(label)
    for value in drawn[index + 1:index + 3]:
        if value.strip():
            return value.strip()
    raise AssertionError(f'no value drawn after {label!r}')


def login(client, name=None, password='admin123'):
    with client.application.app_context():
        if name is None:
            row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
        else:
            row = get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
        user_id = row['id'] if row else None
    return client.post('/login', data={'user_id': str(user_id), 'password': password},
                       follow_redirects=True)


def add_depot_staff(client, app, name, branch_id, password='staff123'):
    """A single-depot staff account with every module granted."""
    login(client)
    client.post('/settings/users/permissions', data={'module': list(MODULE_KEYS)}, follow_redirects=True)
    with app.app_context():
        user_id, error = create_additional_user(name, password, branch_id=branch_id)
        assert error is None, error
    return user_id


def cash_up(app, day, branch_id, counted):
    with app.app_context():
        cash.save_cash_up(day, counted, branch_id=branch_id)


def submit(client, day, branch_id):
    """Press the depot's own Submit day report button."""
    return client.post('/cash-up/report/telegram',
                       data={'day': day, 'branch': str(branch_id)}, follow_redirects=True)


def submit_all(client, day, branches=(1, 2, 3)):
    responses = [submit(client, day, branch_id) for branch_id in branches]
    return responses[-1]


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


def seed_depot_notes(app, day, branch_id, note, counted):
    """One depot's day, with its own end of day note (so the combined report has
    depot-prefixed lines only an aggregate report can carry)."""
    cash_up(app, day, branch_id, counted)
    with app.app_context():
        cash.save_notes(day, note, branch_id=branch_id)


def sweep(client, day=None, secret='secret'):
    headers = {'x-cron-secret': secret} if secret is not None else {}
    url = '/api/internal/telegram/all-branches-report'
    if day:
        url += f'?date={day}'
    return client.post(url, headers=headers)


# --------------------------------------------------------------------------- #
# The record and the tables
# --------------------------------------------------------------------------- #

def test_the_app_ships_the_submission_tables_and_migrates_them_on_boot(app):
    with app.app_context():
        db = get_db()
        names = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {'day_report_submissions', 'day_report_sends'} <= names
        # A boot on a database that lost them (or predates the ticket) re-adds them.
        db.execute('DROP TABLE day_report_submissions')
        db.execute('DROP TABLE day_report_sends')
        run_migrations(db)
        names = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {'day_report_submissions', 'day_report_sends'} <= names
    with app.test_request_context('/'):
        assert telegram.all_branches_report_enabled() is True, 'the feature is on by default'


def test_a_depot_submitting_twice_records_one_submission(app):
    with app.app_context():
        assert cash.record_day_report_submission(TODAY, 2, None) is True
        assert cash.record_day_report_submission(TODAY, 2, None) is False, 'idempotent'
        assert cash.submitted_depot_ids(TODAY) == {2}
        assert cash.missing_day_report_depots(TODAY) == [1, 3]
        assert cash.all_branches_submitted(TODAY) is False


# --------------------------------------------------------------------------- #
# Two of three sends nothing; the last one sends the combined report exactly once
# --------------------------------------------------------------------------- #

def test_two_of_three_depots_send_only_their_own_reports(client, app, sends):
    for branch_id in (1, 2, 3):
        cash_up(app, TODAY, branch_id, '500')
    login(client)
    submit(client, TODAY, 1)
    assert [send['filename'] for send in combined(sends)] == [], 'not complete yet'
    submit(client, TODAY, 2)
    assert combined(sends) == [], 'still one depot missing'
    assert [send['filename'] for send in depot_sends(sends)] == [
        f'Branch 1_Dashboard Report_{TODAY}.pdf',
        f'Branch 2_Dashboard Report_{TODAY}.pdf',
    ], 'each depot still gets its own report'
    with app.app_context():
        assert cash.submitted_depot_ids(TODAY) == {1, 2}
        assert cash.missing_day_report_depots(TODAY) == [3]


def test_the_last_depot_triggers_exactly_one_combined_report(client, app, sends):
    for branch_id in (1, 2, 3):
        cash_up(app, TODAY, branch_id, '500')
    login(client)
    body = submit_all(client, TODAY).get_data(as_text=True)

    assert 'Day report sent on Telegram for Branch 3' in body, 'the staff flash is untouched'
    assert len(depot_sends(sends)) == 3
    assert len(combined(sends)) == 1, 'exactly one combined report'
    sent = combined(sends)[0]
    assert sent['filename'] == f'All branches_Dashboard Report_{TODAY}.pdf'
    assert sent['caption'] == f'All branches day report — {TODAY}'
    assert sent['chat_id'] == 'chat', 'the group the bot already posts to'
    assert sent['bytes'].startswith(b'%PDF-')
    assert sent['bytes'].rstrip().endswith(b'%%EOF')
    with app.app_context():
        assert cash.all_branches_submitted(TODAY) is True
        marker = cash.day_report_sent(TODAY)
        assert marker is not None and marker['chat_id'] == 'chat'


def test_the_combined_report_is_the_whole_business_from_a_depot_scoped_sign_in(client, app, sends):
    """The trigger is depot 3's own staff member - the report is still the main
    user's, with every depot's figures and the owner's name on it."""
    seed_payment(app, 500.0, TODAY, 1, 'ORD-55001-A')
    seed_payment(app, 700.0, TODAY, 2, 'ORD-55001-B')
    seed_payment(app, 9000.0, TODAY, 3, 'ORD-55001-C')
    seed_depot_notes(app, TODAY, 1, 'Depot one closed the yard.', '840.00')
    seed_depot_notes(app, TODAY, 2, 'Depot two handed over.', '900.00')
    seed_depot_notes(app, TODAY, 3, 'Depot three banked late.', '700.00')
    add_depot_staff(client, app, 'Depot Three Clerk', 3)

    login(client)
    submit(client, TODAY, 1)
    submit(client, TODAY, 2)
    login(client, name='Depot Three Clerk', password='staff123')
    submit(client, TODAY, 3)

    assert len(combined(sends)) == 1
    drawn = _drawn(combined(sends)[0]['bytes'])
    assert 'Depot: All branches' in drawn
    assert f'Business day: {TODAY}' in drawn
    assert 'Prepared by: Head office admin (Main profile)' in drawn, 'signed by the owner'
    assert 'Depot Three Clerk' not in ' '.join(drawn), 'never by the last depot to submit'
    # Every depot is in the combined cash half, attributed - R10200 is a figure
    # only a genuinely combined report can carry (depot 3 alone has R9000).
    assert 'R10200.00' in drawn, 'combined cash received (500 + 700 + 9000)'
    assert card_value(drawn, 'Cash received for the day') == 'R10200.00', \
        'the per-depot cash figures add up, they do not repeat depot 3'
    assert 'R27000.00' not in drawn, 'no depot counted three times'
    assert card_value(drawn, 'New orders for the day') == '3', 'company-wide cards'
    assert 'Branch 1: Depot one closed the yard.' in drawn
    assert 'Branch 3: Depot three banked late.' in drawn

    # ...and its dashboard cards are company-wide, not depot 3's.
    with app.test_request_context('/'):
        from flask import session
        from app.services.reports import dashboard_day_metrics
        session['user_role'] = 'staff'
        session['can_view_all_branches'] = 0
        session['branch_id'] = 3
        assert dashboard_day_metrics(TODAY)['orders'] == 1, 'depot 3 sees only its own order'
        whole = cash.day_report(day=TODAY, aggregate=True, branch_ids=[1, 2, 3], all_depots=True)
        assert whole['metrics']['orders'] == 3, 'the combined report counts all three depots'
        assert whole['metrics']['revenue'] == 10200.0
        assert whole['cash']['branch_name'] == 'All branches'


def test_a_resubmission_never_posts_a_second_combined_report(client, app, sends):
    for branch_id in (1, 2, 3):
        cash_up(app, TODAY, branch_id, '500')
    login(client)
    submit_all(client, TODAY)
    assert len(combined(sends)) == 1

    submit(client, TODAY, 3)
    submit(client, TODAY, 1)
    assert len(combined(sends)) == 1, 'no second copy on a re-submit'
    with app.app_context():
        rows = get_db().execute(
            'SELECT COUNT(*) c FROM day_report_sends WHERE business_day = ?', (TODAY,)
        ).fetchone()['c']
        assert rows == 1


# --------------------------------------------------------------------------- #
# The retry / sweep endpoint
# --------------------------------------------------------------------------- #

def test_the_sweep_needs_the_cron_secret(client, app, sends):
    for branch_id in (1, 2, 3):
        cash_up(app, TODAY, branch_id, '500')
        submit(client, TODAY, branch_id)
    sends.clear()

    assert sweep(client, secret=None).status_code == 401
    assert sweep(client, secret='wrong').status_code == 401
    assert sends == [], 'an unauthorised sweep sends nothing'


def test_the_sweep_sends_nothing_for_a_partial_day(client, app, sends):
    cash_up(app, TODAY, 1, '500')
    login(client)
    submit(client, TODAY, 1)
    sends.clear()

    response = sweep(client)
    assert response.status_code == 200
    data = response.get_json()
    assert data['ok'] is True and data['sent'] is False
    assert data['skipped'] == 'awaiting_depots'
    assert data['missing'] == [2, 3] and data['depots'] == 3
    assert sends == []


def test_the_sweep_sends_the_day_once_it_is_complete(client, app, sends):
    """The retry path: submissions are in, the combined report is not out yet."""
    with app.app_context():
        for branch_id in (1, 2, 3):
            cash.record_day_report_submission(TODAY, branch_id, None)

    response = sweep(client)
    assert response.status_code == 200
    data = response.get_json()
    assert data['sent'] is True and data['ok'] is True
    assert data['filename'] == f'All branches_Dashboard Report_{TODAY}.pdf'

    again = sweep(client)
    assert again.status_code == 200
    assert again.get_json()['skipped'] == 'already_sent'
    assert len(combined(sends)) == 1, 'a repeat sweep never posts a second copy'


def test_a_failed_combined_send_is_delivered_once_by_the_sweep(client, app, monkeypatch):
    calls = []
    failed = []

    def fake(document_bytes, filename, caption='', chat_id=None):
        calls.append(filename)
        if filename.startswith('All branches_') and not failed:
            failed.append(filename)
            return {'ok': False, 'sent': False, 'error': 'boom'}
        return {'ok': True, 'sent': True, 'status': 200}

    monkeypatch.setattr(telegram, '_send_document', fake)
    monkeypatch.setattr(cash_routes, '_send_document', fake)

    for branch_id in (1, 2, 3):
        cash_up(app, TODAY, branch_id, '500')
    login(client)
    submit_all(client, TODAY)

    assert failed == [f'All branches_Dashboard Report_{TODAY}.pdf'], 'it was attempted once'
    with app.app_context():
        assert cash.day_report_sent(TODAY) is None, 'a failed send writes no marker'

    response = sweep(client)
    assert response.get_json()['sent'] is True, 'the sweep delivers what failed'
    assert [name for name in calls if name.startswith('All branches_')] == [
        f'All branches_Dashboard Report_{TODAY}.pdf',
        f'All branches_Dashboard Report_{TODAY}.pdf',
    ], 'exactly one attempt + one retry'
    assert sweep(client).get_json()['skipped'] == 'already_sent'
    with app.app_context():
        assert cash.day_report_sent(TODAY) is not None


# --------------------------------------------------------------------------- #
# Quiet paths: kill switch, unconfigured, inactive depot, past day
# --------------------------------------------------------------------------- #

def test_the_kill_switch_stops_only_the_combined_report(client, app, sends):
    app.config.update(TELEGRAM_ALL_BRANCHES_REPORT_ENABLED='0')
    for branch_id in (1, 2, 3):
        cash_up(app, TODAY, branch_id, '500')
    login(client)
    submit_all(client, TODAY)

    assert len(depot_sends(sends)) == 3, 'the depot reports themselves still go out'
    assert combined(sends) == []
    assert sweep(client).get_json()['skipped'] == 'all_branches_disabled'
    with app.app_context():
        assert cash.day_report_sent(TODAY) is None, 'nothing was sent, nothing is marked'

    # Flipping it back on delivers the day that was held back - no re-submitting.
    app.config.update(TELEGRAM_ALL_BRANCHES_REPORT_ENABLED='1')
    assert sweep(client).get_json()['sent'] is True
    assert len(combined(sends)) == 1


def test_an_unconfigured_telegram_skips_without_failing_the_submit(client, app, sends):
    app.config.update(TELEGRAM_BOT_TOKEN='', TELEGRAM_CHAT_ID='')
    for branch_id in (1, 2, 3):
        cash_up(app, TODAY, branch_id, '500')
    login(client)
    body = submit_all(client, TODAY)
    assert body.status_code == 200
    assert 'Telegram is not configured; day report was not sent' in body.get_data(as_text=True)
    assert sends == [] and sweep(client).get_json()['skipped'] == 'not_configured'

    # Once it is configured the sweep still delivers the day once.
    app.config.update(TELEGRAM_BOT_TOKEN='token', TELEGRAM_CHAT_ID='chat')
    assert sweep(client).get_json()['sent'] is True
    assert len(combined(sends)) == 1


def test_an_inactive_depot_never_blocks_the_day(client, app, sends):
    with app.app_context():
        db = get_db()
        db.execute('UPDATE branches SET active = 0 WHERE id = 3')
        db.commit()
        assert cash.active_depot_ids() == [1, 2]

    for branch_id in (1, 2):
        cash_up(app, TODAY, branch_id, '500')
    login(client)
    submit(client, TODAY, 1)
    submit(client, TODAY, 2)
    assert len(combined(sends)) == 1, 'the switched-off depot is not required'


def test_a_past_day_submission_does_not_complete_today(client, app, sends):
    with app.app_context():
        for branch_id in (1, 2, 3):
            cash.record_day_report_submission(PAST, branch_id, None)
            cash_up(app, PAST, branch_id, '500')
        assert cash.all_branches_submitted(PAST) is True
        assert cash.all_branches_submitted(TODAY) is False

    response = sweep(client)
    assert response.get_json()['skipped'] == 'awaiting_depots'
    assert response.get_json()['day'] == TODAY
    assert sends == [], 'nothing fires for today just because a past day is complete'

    past = sweep(client, day=PAST)
    assert past.get_json()['sent'] is True
    assert combined(sends)[0]['filename'] == f'All branches_Dashboard Report_{PAST}.pdf'
    with app.app_context():
        assert cash.day_report_sent(PAST) is not None
        assert cash.day_report_sent(TODAY) is None, 'today is still open'


def test_a_depot_can_be_marked_submitted_for_another_day(app):
    """A crafted past day cannot be recorded against today's business day."""
    with app.app_context():
        cash.record_day_report_submission(PAST, 1, None)
        assert cash.submitted_depot_ids(PAST) == {1}
        assert cash.submitted_depot_ids(TODAY) == set()


# --------------------------------------------------------------------------- #
# The combined chat, and the promise that none of this can break a submit
# --------------------------------------------------------------------------- #

def test_the_combined_report_can_target_its_own_chat(client, app, sends):
    with app.app_context():
        for branch_id in (1, 2, 3):
            cash.record_day_report_submission(TODAY, branch_id, None)
    with app.test_request_context('/'):
        assert telegram.all_branches_chat_id() == 'chat', 'the group by default'
    app.config.update(TELEGRAM_ALL_BRANCHES_CHAT_ID='managers')
    with app.test_request_context('/'):
        assert telegram.all_branches_chat_id() == 'managers'

    assert sweep(client).get_json()['sent'] is True
    assert combined(sends)[0]['chat_id'] == 'managers'

    # A blank override is the same as none at all.
    app.config.update(TELEGRAM_ALL_BRANCHES_CHAT_ID='')
    with app.test_request_context('/'):
        assert telegram.all_branches_chat_id() == 'chat'


def test_a_recording_or_sending_failure_never_breaks_the_staff_submit(client, app, sends, monkeypatch):
    """Both new side effects are best-effort: the depot's own submit keeps its
    success flash and its redirect whatever happens to them."""
    def boom(*_args, **_kwargs):
        raise RuntimeError('database is having a bad day')

    monkeypatch.setattr(cash_routes.cash, 'record_day_report_submission', boom)
    cash_up(app, TODAY, 1, '500')
    login(client)
    body = submit(client, TODAY, 1)
    assert body.status_code == 200
    assert 'Day report sent on Telegram for Branch 1' in body.get_data(as_text=True)
    assert len(depot_sends(sends)) == 1
    assert combined(sends) == [], 'nothing is recorded, so nothing is combined'

    monkeypatch.setattr(cash_routes, 'send_all_branches_day_report', boom)
    body = submit(client, TODAY, 1)
    assert body.status_code == 200
    assert 'Day report sent on Telegram for Branch 1' in body.get_data(as_text=True)
    assert len(depot_sends(sends)) == 2, 'the depot report still went out'


def test_the_combined_report_is_silent_when_telegram_as_a_whole_is_off(client, app, sends):
    app.config.update(TELEGRAM_NOTIFICATIONS_ENABLED='')
    for branch_id in (1, 2, 3):
        cash_up(app, TODAY, branch_id, '500')
    login(client)
    body = submit_all(client, TODAY).get_data(as_text=True)
    assert 'Telegram notifications are disabled; day report was not sent' in body
    assert sends == []
    assert sweep(client).get_json()['skipped'] == 'disabled'


# --------------------------------------------------------------------------- #
# The scope override itself: in-process only, and it never leaks
# --------------------------------------------------------------------------- #

def test_all_depots_scope_is_confined_to_its_block(client, app):
    """It lifts the session scope inside the block and restores it after, and it
    can never be reached from a request value."""
    with app.test_request_context('/'):
        from flask import session
        session['user_role'] = 'staff'
        session['can_view_all_branches'] = 0
        session['branch_id'] = 3

        assert access.session_branch_scope_ids() == [3]
        with access.all_depots_scope():
            assert access.session_branch_scope_ids() is None, 'whole business inside'
            # An explicit depot still means exactly that depot.
            assert access._branch_id_list(3) == [3]
        assert access.session_branch_scope_ids() == [3], 'restored afterwards'

        # The flag is not persisted anywhere and is not carried by the session.
        assert '_abi_all_depots_scope' not in session
    with app.app_context():
        assert access.session_branch_scope_ids() is None, 'non-request contexts stay company-wide'


def test_a_scoped_sign_in_still_cannot_widen_its_own_download(client, app):
    """The new scope is used by the combined report only - the ordinary
    dashboard download keeps obeying the session (regression guard)."""
    add_depot_staff(client, app, 'Depot One Clerk', 1)
    login(client, name='Depot One Clerk', password='staff123')
    response = client.get(f'/cash-up/export.csv?branch=all&day={TODAY}')
    assert response.status_code == 200
    assert 'Branch 1' in response.get_data(as_text=True)
    assert 'All branches' not in response.get_data(as_text=True).splitlines()[1]
