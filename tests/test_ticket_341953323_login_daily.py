"""Ticket ABI-341953323: one sign-in alert per account per South African day.

The session flag alone only deduplicated one sign-in: signing out and back in, a
second device, a restarted process or two requests arriving together all opened a
fresh session and announced the same person again. The day is now claimed in the
database (``login_notification_days``), so exactly one Telegram alert goes out per
account per South African day, while everything ABI-341953300 and ABI-341953309
settled stays true:

* the head-office admin's own sign-in is still silent (role 'owner');
* a multi-depot account still waits for the branch chooser before its alert;
* the alert is still best effort — it can never break a sign-in.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest

from app import create_app
from app.db import get_db, run_migrations
from app.routes import auth as auth_module
from app.services import telegram
from app.services.timezone import SA_TIMEZONE
from test_branch_login_choice import add_staff, sign_in

CONFIG = {
    'TESTING': True,
    'SECRET_KEY': 'test',
    'ADMIN_EMAIL': 'admin@abi.local',
    'ADMIN_PASSWORD': 'admin123',
    'TELEGRAM_NOTIFICATIONS_ENABLED': '1',
    'TELEGRAM_BOT_TOKEN': 'token',
    'TELEGRAM_CHAT_ID': 'chat',
}


@pytest.fixture()
def app(tmp_path):
    return create_app({**CONFIG, 'DATABASE': str(tmp_path / 'login_daily.db')})


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def messages(monkeypatch):
    sent = []
    monkeypatch.setattr(
        telegram, '_send_message',
        lambda text: sent.append(text) or {'ok': True, 'sent': True},
    )
    return sent


def _claims(app, user_id=None):
    with app.app_context():
        sql = "SELECT * FROM login_notification_days"
        params = ()
        if user_id is not None:
            sql += " WHERE user_id = ?"
            params = (user_id,)
        return [dict(row) for row in get_db().execute(sql + " ORDER BY id", params).fetchall()]


def _user_id(app, name):
    with app.app_context():
        return get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()['id']


# --- once per account per day ----------------------------------------------

def test_a_second_sign_in_on_the_same_day_is_silent(client, app, messages):
    add_staff(client, app, 'Pat Daily', [1])
    messages.clear()

    sign_in(client, 'Pat Daily', 'staff123', follow=False)
    assert len(messages) == 1 and 'Name: Pat Daily' in messages[0]

    client.post('/logout')
    sign_in(client, 'Pat Daily', 'staff123', follow=False)
    client.get('/dashboard')
    assert len(messages) == 1, 'the same account was announced twice in one day'

    claims = _claims(app, _user_id(app, 'Pat Daily'))
    assert len(claims) == 1
    assert claims[0]['business_day'] == auth_module._south_african_day()


def test_the_day_survives_a_process_restart(client, app, tmp_path, messages):
    """A new app instance on the same database must not re-announce the day.

    This is the part a session flag cannot do: the claim is on disk, not in the
    browser's cookie.
    """
    add_staff(client, app, 'Restart Pat', [1])
    messages.clear()
    sign_in(client, 'Restart Pat', 'staff123', follow=False)
    assert len(messages) == 1

    restarted = create_app({**CONFIG, 'DATABASE': app.config['DATABASE']})
    second_client = restarted.test_client()
    sign_in(second_client, 'Restart Pat', 'staff123', follow=False)
    assert len(messages) == 1, 'a restart re-announced the same account on the same day'


def test_each_account_keeps_its_own_daily_alert(client, app, messages):
    add_staff(client, app, 'Pat One', [1])
    add_staff(client, app, 'Pat Two', [1])
    messages.clear()

    sign_in(client, 'Pat One', 'staff123', follow=False)
    client.post('/logout')
    sign_in(client, 'Pat Two', 'staff123', follow=False)

    assert len(messages) == 2
    assert 'Name: Pat One' in messages[0] and 'Name: Pat Two' in messages[1]
    assert len(_claims(app)) == 2


def test_the_next_south_african_day_alerts_again(client, app, messages, monkeypatch):
    add_staff(client, app, 'Pat Tomorrow', [1])
    messages.clear()

    sign_in(client, 'Pat Tomorrow', 'staff123', follow=False)
    assert len(messages) == 1
    today = auth_module._south_african_day()

    tomorrow = datetime.now(SA_TIMEZONE).replace(tzinfo=None) + timedelta(days=1)
    monkeypatch.setattr(auth_module, 'local_now', lambda: tomorrow)
    client.post('/logout')
    sign_in(client, 'Pat Tomorrow', 'staff123', follow=False)

    assert len(messages) == 2
    days = [row['business_day'] for row in _claims(app)]
    assert days == [today, tomorrow.date().isoformat()]


def test_the_day_is_the_south_african_wall_clock_not_utc(monkeypatch):
    """Stamped from the SA wall clock, so the day matches the depot's working day."""
    # 00:30 SA time is still the previous day in UTC; the claim must follow SA.
    monkeypatch.setattr(auth_module, 'local_now',
                        lambda: datetime(2026, 11, 17, 0, 30))
    assert auth_module._south_african_day() == '2026-11-17'


# --- failures must not become duplicates ------------------------------------

def test_a_failed_send_is_not_retried_later_the_same_day(client, app, monkeypatch):
    add_staff(client, app, 'Unlucky Pat', [1])
    attempts = []

    def flaky(text):
        attempts.append(text)
        if len(attempts) == 1:
            raise RuntimeError('transport unavailable')
        return {'ok': True, 'sent': True}

    monkeypatch.setattr(telegram, '_send_message', flaky)

    assert sign_in(client, 'Unlucky Pat', 'staff123', follow=False).status_code == 302
    assert len(attempts) == 1, 'the alert was never attempted'
    # Sign-in itself is unaffected by the provider failure.
    assert client.get('/dashboard').status_code == 200

    client.post('/logout')
    assert sign_in(client, 'Unlucky Pat', 'staff123', follow=False).status_code == 302
    assert len(attempts) == 1, 'a failed send was retried into a duplicate alert'
    assert len(_claims(app, _user_id(app, 'Unlucky Pat'))) == 1


def test_an_unavailable_claim_store_never_breaks_sign_in(client, app, monkeypatch):
    add_staff(client, app, 'Brave Pat', [1])
    messages = []
    monkeypatch.setattr(
        telegram, '_send_message',
        lambda text: messages.append(text) or {'sent': True},
    )

    def broken(*args, **kwargs):
        raise RuntimeError('database unavailable')

    monkeypatch.setattr(auth_module, '_claim_login_notification_day', broken)
    messages.clear()
    assert sign_in(client, 'Brave Pat', 'staff123', follow=False).status_code == 302
    assert client.get('/dashboard').status_code == 200
    # No claim could be made, so nothing was announced: skipping is the safe side
    # of "a failure must not become a duplicate".
    assert messages == []


# --- concurrency ------------------------------------------------------------

def test_the_claim_is_won_by_exactly_one_concurrent_request(app, client, monkeypatch):
    add_staff(client, app, 'Two Tabs', [1])
    user_id = _user_id(app, 'Two Tabs')
    day = auth_module._south_african_day()

    def attempt(_):
        # Each thread gets its own app context, so its own database connection —
        # the same situation as two web workers claiming at once.
        with app.app_context():
            try:
                return auth_module._claim_login_notification_day(user_id, day)
            except Exception:
                return False  # a lock-lost writer has also not won the day

    with ThreadPoolExecutor(max_workers=6) as pool:
        wins = list(pool.map(attempt, range(6)))

    assert sum(1 for won in wins if won) == 1, f'expected one winner, got {wins}'
    assert len(_claims(app, user_id)) == 1


def test_the_database_itself_refuses_a_second_claim_for_the_day(client, app):
    """The UNIQUE pair is the backstop under a lost race, not just the SELECT."""
    add_staff(client, app, 'Unique Pat', [1])
    user_id = _user_id(app, 'Unique Pat')
    day = auth_module._south_african_day()

    with app.app_context():
        db = get_db()
        assert auth_module._claim_login_notification_day(user_id, day) is True
        with pytest.raises(Exception) as refused:
            db.execute(
                "INSERT INTO login_notification_days (user_id, business_day, notified_at) "
                "VALUES (?, ?, ?)",
                (user_id, day, '2026-11-16T08:00:00'),
            )
        assert 'UNIQUE' in str(refused.value).upper()
        assert auth_module._is_unique_violation(refused.value)

    # A second session for the same day is simply not the winner.
    with app.app_context():
        assert auth_module._claim_login_notification_day(user_id, day) is False
    assert len(_claims(app, user_id)) == 1


# --- prior behaviour that must not regress ----------------------------------

def test_the_head_office_admin_stays_silent_and_claims_nothing(client, app, messages):
    """Ticket ABI-341953309: the main profile's sign-in is never announced."""
    sign_in(client, follow=False)
    client.get('/dashboard')
    client.post('/logout')
    sign_in(client, follow=False)
    assert messages == []
    assert _claims(app) == []


def test_the_branch_chooser_still_decides_the_alert_and_alerts_once(client, app, messages):
    add_staff(client, app, 'Two Depot Daily', [1, 2])
    messages.clear()

    sign_in(client, 'Two Depot Daily', 'staff123', follow=False)
    assert messages == [], 'the alert must wait for the branch chooser'
    client.post('/select-branch', data={'branch_id': '3'})
    assert messages == [], 'an invalid choice must not announce anything'
    client.post('/select-branch', data={'branch_id': '2'})
    assert len(messages) == 1 and 'Branch: Branch 2' in messages[0]

    # Choosing another depot in the same session, and signing in again that day,
    # are both silent now that the day is claimed.
    client.post('/select-branch', data={'branch_id': '1'})
    client.get('/dashboard')
    client.post('/logout')
    sign_in(client, 'Two Depot Daily', 'staff123', follow=False)
    client.post('/select-branch', data={'branch_id': '2'})
    assert len(messages) == 1


def test_a_bookmarked_page_still_alerts_once_with_the_real_scope(client, app, messages):
    add_staff(client, app, 'Scope Pat', [1, 3])
    messages.clear()

    sign_in(client, 'Scope Pat', 'staff123', follow=False)
    client.get('/dashboard')
    client.get('/dashboard')
    assert len(messages) == 1
    assert 'All authorised branches: Branch 1, Branch 3' in messages[0]
    assert 'Branch 2' not in messages[0]


# --- additive, secret-free storage ------------------------------------------

def test_the_claim_table_adds_no_personal_or_credential_data(client, app):
    add_staff(client, app, 'Private Pat', [1])
    sign_in(client, 'Private Pat', 'staff123', follow=False)

    assert set(_claims(app)[0]) == {'id', 'user_id', 'business_day', 'notified_at'}
    with app.app_context():
        columns = {row[1] for row in get_db().execute(
            "PRAGMA table_info(login_notification_days)").fetchall()}
    assert columns == {'id', 'user_id', 'business_day', 'notified_at'}


def test_an_existing_database_gains_the_table_and_keeps_working(client, app, messages):
    """Additive migration: a database without the table gains it on the next start."""
    add_staff(client, app, 'Migrated Pat', [1])
    messages.clear()
    with app.app_context():
        db = get_db()
        db.execute("DROP TABLE login_notification_days")
        db.commit()
        with pytest.raises(Exception):
            db.execute("SELECT 1 FROM login_notification_days").fetchall()
        run_migrations(db)   # what a restart does
        run_migrations(db)   # and again: idempotent
        columns = {row[1] for row in db.execute(
            "PRAGMA table_info(login_notification_days)").fetchall()}
        assert columns == {'id', 'user_id', 'business_day', 'notified_at'}

    # The restored table works exactly as on a fresh install.
    sign_in(client, 'Migrated Pat', 'staff123', follow=False)
    assert len(messages) == 1
    client.post('/logout')
    sign_in(client, 'Migrated Pat', 'staff123', follow=False)
    assert len(messages) == 1
