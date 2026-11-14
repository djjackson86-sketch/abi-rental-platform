"""Ticket ABI-341953300 - "You have submitted your day report, LOGOUT?"

Requested edit: *"when the day report has been successfully sent, show a one-time
pop-up after the redirect: You have submitted your day report, LOGOUT? YES logs
out, NO dismisses and the session stays."*

What is pinned here:

* a **confirmed** send (Telegram answered 2xx, i.e. ``result['sent']``) is the only
  thing that arms the prompt: a failed send, a disabled kill switch, an
  unconfigured bot and the missing-sections gate all leave the dashboard exactly
  as it was, with their own existing flash and no modal;
* the modal appears on the dashboard the submit redirects to, once: the refresh
  after it is silent, because the flag is consumed by the render that shows it;
* **YES** posts the existing ``/logout`` form - the same endpoint, the same hidden
  ``push_endpoint`` field push.js fills in - so the session ends and this
  browser's notifications are still stopped on the way out;
* **NO** is a ``form method="dialog"`` close: nothing is posted, the session is
  untouched and the staff member stays on the dashboard;
* accessibility: the dialog is labelled and described by its own heading/copy,
  the safe choice (NO) takes focus, and the fallback opens without ``showModal``
  support rather than dropping the prompt;
* the dashboard's own inline logout form in the sidebar is untouched.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.db import get_db  # noqa: E402
from app.routes import cash as cash_routes  # noqa: E402
from app.services import cash  # noqa: E402
from app.services.access import create_additional_user  # noqa: E402

TODAY = cash.today_iso()
DIALOG_ID = 'id="day-report-logout-dialog"'
TITLE = 'You have submitted your day report, LOGOUT?'


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


def fill_day_report_sections(client, day=TODAY, branch='1'):
    """Every section the day-report gate requires, so the submit is allowed."""
    client.post('/cash-up/interactions', data={
        'day': day, 'branch': branch,
        'interaction_calls': '0', 'interaction_whatsapp': '0',
        'interaction_emails': '0', 'interaction_walk_in': '0',
        'interaction_notes': '',
    }, follow_redirects=True)
    client.post('/cash-up', data={'day': day, 'branch': branch, 'counted_cash': '0'},
                follow_redirects=True)
    client.post('/cash-up/pos', data={'day': day, 'branch': branch, 'counted_card': '0'},
                follow_redirects=True)
    data = {'day': day, 'branch': branch}
    data.update({f'actual_{index}': '0' for index in range(5)})
    client.post('/dashboard/spare-wheels', data=data, follow_redirects=True)


def submit_day_report(client, day=TODAY, branch='1'):
    return client.post('/cash-up/report/telegram',
                       data={'day': day, 'branch': branch}, follow_redirects=True)


def assert_prompt(body):
    assert TITLE in body
    assert DIALOG_ID in body
    assert 'aria-labelledby="day-report-logout-title"' in body
    assert 'aria-describedby="day-report-logout-copy"' in body
    assert 'id="day-report-logout-title"' in body
    assert 'id="day-report-logout-copy"' in body


def assert_no_prompt(body):
    assert DIALOG_ID not in body
    assert TITLE not in body


@pytest.fixture()
def confirmed_send(monkeypatch):
    """Telegram confirms the send, as the real service does on a 2xx."""
    calls = []

    def fake_send(document_bytes, filename, caption=''):
        calls.append(filename)
        return {'ok': True, 'sent': True, 'status': 200}

    monkeypatch.setattr(cash_routes, '_send_document', fake_send)
    return calls


# --------------------------------------------------------------------------- #
# The prompt itself.
# --------------------------------------------------------------------------- #

def test_confirmed_send_prompts_once_on_the_dashboard(client, app, confirmed_send):
    login(client)
    fill_day_report_sections(client)

    body = submit_day_report(client).get_data(as_text=True)
    assert confirmed_send, 'the day report PDF was not sent at all'
    # The existing success flash and the prompt are both on the dashboard.
    assert 'Day report sent on Telegram for Branch 1' in body
    assert_prompt(body)

    # The refresh (the redirect target fetched again) cannot ask twice.
    again = client.get(f'/dashboard?branch=1').get_data(as_text=True)
    assert_no_prompt(again)
    assert 'Day report sent on Telegram for Branch 1' not in again


def test_yes_posts_the_existing_logout_form_with_the_push_endpoint(client, app, confirmed_send):
    login(client)
    fill_day_report_sections(client)
    body = submit_day_report(client).get_data(as_text=True)
    assert_prompt(body)

    # Same endpoint the sidebar form posts to, and the hidden field push.js fills
    # in so logout still stops notifications for this browser only.
    assert '<form method="post" action="/logout" class="day-report-logout-yes">' in body
    assert body.count('name="push_endpoint"') == 2  # sidebar form + this prompt
    assert '<button class="btn primary" type="submit">YES</button>' in body
    # 1) the sidebar's own logout form is still there, unchanged in behaviour
    assert 'class="logout"' in body

    # 2) YES really logs out through that endpoint and clears the session.
    res = client.post('/logout', data={'push_endpoint': 'https://push.example/abc'})
    assert res.status_code == 302
    assert res.headers['Location'].endswith('/login')
    after = client.get('/dashboard')
    assert after.status_code == 302
    assert '/login' in after.headers['Location']


def test_no_dismisses_without_posting_and_leaves_the_session(client, app, confirmed_send):
    login(client)
    fill_day_report_sections(client)
    body = submit_day_report(client).get_data(as_text=True)
    assert_prompt(body)

    # NO is a dialog-closing form: no action, no post, nothing cleared.
    assert '<form method="dialog" class="day-report-logout-no">' in body
    assert '<button class="btn ghost" type="submit" autofocus>NO</button>' in body

    # The session survived the send: the dashboard still renders for this user.
    still_in = client.get('/dashboard')
    assert still_in.status_code == 200
    assert 'Welcome back' in still_in.get_data(as_text=True)


def test_dialog_shows_itself_and_has_an_accessible_fallback(client, app, confirmed_send):
    login(client)
    fill_day_report_sections(client)
    body = submit_day_report(client).get_data(as_text=True)
    assert_prompt(body)
    assert "typeof dialog.showModal === 'function'" in body
    assert "dialog.setAttribute('open', '')" in body


def test_a_plain_dashboard_visit_never_prompts(client, app, confirmed_send):
    login(client)
    body = client.get('/dashboard').get_data(as_text=True)
    assert 'Welcome back' in body
    assert_no_prompt(body)


def test_a_branch_limited_clerk_is_prompted_for_their_own_depot(client, app, monkeypatch):
    sent = []

    def fake_send(document_bytes, filename, caption=''):
        sent.append(caption)
        return {'ok': True, 'sent': True, 'status': 200}

    monkeypatch.setattr(cash_routes, '_send_document', fake_send)
    with app.app_context():
        create_additional_user('Depot Three Clerk', 'staff123', branch_id=3)
    login(client, name='Depot Three Clerk', password='staff123')
    fill_day_report_sections(client, branch='3')

    body = submit_day_report(client, branch='3').get_data(as_text=True)
    assert sent and 'Branch 3' in sent[0]
    assert_prompt(body)
    assert_no_prompt(client.get('/dashboard?branch=3').get_data(as_text=True))


# --------------------------------------------------------------------------- #
# Only a confirmed send arms the prompt.
# --------------------------------------------------------------------------- #

def test_failed_send_flashes_and_never_prompts(client, app, monkeypatch):
    monkeypatch.setattr(cash_routes, '_send_document',
                        lambda *_args, **_kwargs: {'ok': False, 'sent': False, 'status': 500})
    login(client)
    fill_day_report_sections(client)

    body = submit_day_report(client).get_data(as_text=True)
    assert 'Day report could not be sent on Telegram' in body
    assert_no_prompt(body)
    assert_no_prompt(client.get('/dashboard').get_data(as_text=True))


def test_disabled_kill_switch_flashes_and_never_prompts(client, app):
    login(client)
    fill_day_report_sections(client)
    app.config.update(TELEGRAM_NOTIFICATIONS_ENABLED='')

    first = submit_day_report(client).get_data(as_text=True)
    assert 'Telegram notifications are disabled; day report was not sent' in first
    assert_no_prompt(first)
    assert_no_prompt(client.get('/dashboard').get_data(as_text=True))


def test_unconfigured_bot_flashes_and_never_prompts(client, app):
    login(client)
    fill_day_report_sections(client)
    app.config.update(TELEGRAM_NOTIFICATIONS_ENABLED='true',
                      TELEGRAM_BOT_TOKEN='', TELEGRAM_CHAT_ID='')

    body = submit_day_report(client).get_data(as_text=True)
    assert 'Telegram is not configured; day report was not sent' in body
    assert_no_prompt(body)
    assert_no_prompt(client.get('/dashboard').get_data(as_text=True))


def test_a_skipped_send_never_prompts(client, app, monkeypatch):
    """``ok`` without ``sent`` is a quiet skip, not a confirmation."""
    monkeypatch.setattr(cash_routes, '_send_document',
                        lambda *_args, **_kwargs: {'ok': True, 'sent': False,
                                                   'skipped': 'disabled'})
    login(client)
    fill_day_report_sections(client)
    body = submit_day_report(client).get_data(as_text=True)
    assert_no_prompt(body)
    assert_no_prompt(client.get('/dashboard').get_data(as_text=True))


def test_the_missing_sections_gate_never_prompts(client, app, monkeypatch):
    called = []
    monkeypatch.setattr(cash_routes, '_send_document',
                        lambda *_args, **_kwargs: called.append(True) or {'ok': True, 'sent': True})
    login(client)
    # Nothing filled in: the gate refuses before anything is sent.
    body = submit_day_report(client).get_data(as_text=True)
    assert 'Complete these sections before submitting the day report' in body
    assert called == []
    assert_no_prompt(body)
    assert_no_prompt(client.get('/dashboard').get_data(as_text=True))


def test_prompt_flag_is_cleared_by_the_render_that_shows_it(client, app, confirmed_send):
    """The flag cannot leak into a later render, so the prompt is genuinely once."""
    login(client)
    fill_day_report_sections(client)
    # No redirect followed: the flag is parked, waiting for the dashboard.
    res = client.post('/cash-up/report/telegram', data={'day': TODAY, 'branch': '1'},
                      follow_redirects=False)
    assert res.status_code == 302

    with client.session_transaction() as flask_session:
        assert flask_session[cash_routes.DAY_REPORT_LOGOUT_PROMPT_KEY] == TODAY

    client.get('/dashboard?branch=1')
    with client.session_transaction() as flask_session:
        assert cash_routes.DAY_REPORT_LOGOUT_PROMPT_KEY not in flask_session


def test_the_prompt_survives_a_stop_on_another_page_then_shows_on_the_dashboard(
        client, app, confirmed_send):
    """A booked-in staff member who lands elsewhere still gets asked, once."""
    login(client)
    fill_day_report_sections(client)
    # Do not follow the redirect: the flag is still waiting.
    res = client.post('/cash-up/report/telegram', data={'day': TODAY, 'branch': '1'},
                      follow_redirects=False)
    assert res.status_code == 302
    assert '/dashboard' in res.headers['Location']

    other = client.get('/orders').get_data(as_text=True)
    assert_no_prompt(other)  # only the dashboard owns the prompt

    first = client.get('/dashboard?branch=1').get_data(as_text=True)
    assert_prompt(first)
    assert_no_prompt(client.get('/dashboard?branch=1').get_data(as_text=True))
