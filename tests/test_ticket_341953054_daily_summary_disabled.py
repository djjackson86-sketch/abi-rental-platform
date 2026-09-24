"""The "Sano Trailers daily summary" Telegram text report is no longer sent.

Ticket ABI-341953054: *"don't send the telegram text reports 'Sano Trailers daily
summary'"*.

That report is produced in exactly one place - ``telegram.send_daily_summary()``
(``app/services/telegram.py``), reached from
``POST /api/internal/telegram/daily-summary`` (the external 15:00 cron). It is now
suppressed at that one point: the call still succeeds (``ok`` True, ``sent``
False, ``skipped`` ``daily_summary_disabled``) and still carries the ``date`` /
``sections`` metadata, so the existing cron keeps getting a healthy 200 while
nothing is posted.

Every test here drives the real route or the real service function and records
what actually reaches ``_send_message`` / ``_send_document``, so a regression that
starts posting the report again fails loudly. Nothing else on Telegram may change:
the test message, the new customer notification and the manual dashboard day
report PDF are pinned as still sending.
"""
import os
import tempfile

import pytest

from app import create_app
from app.db import get_db
from app.services import telegram
from app.services.cash import today_iso

DAY = '2026-07-02'


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
        # really would send. The recorder below is what proves nothing does.
        'TELEGRAM_NOTIFICATIONS_ENABLED': 'true',
        'TELEGRAM_BOT_TOKEN': 'token',
        'TELEGRAM_CHAT_ID': 'chat',
        'TELEGRAM_CRON_SECRET': 'secret',
        'PUBLIC_BASE_URL': 'https://abi-rental-platform.onrender.com',
    })
    yield application
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def sent(monkeypatch):
    """Every text message that would have gone out, in order."""
    messages = []
    monkeypatch.setattr(telegram, '_send_message',
                        lambda text: messages.append(text) or {'ok': True, 'sent': True})
    return messages


def login(client):
    with client.application.app_context():
        row = get_db().execute(
            "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
        ).fetchone()
        user_id = row['id'] if row else None
    return client.post('/login', data={'user_id': str(user_id), 'password': 'admin123'},
                       follow_redirects=True)


def _seed_day(app, day=DAY):
    """One order going out that day plus one payment-due order, so the summary has
    something to report - otherwise 'nothing was sent' would be trivially true."""
    with app.app_context():
        db = get_db()
        db.execute(
            """INSERT INTO orders (order_number, booking_type, collect_branch_id, return_branch_id,
            status, payment_status, start_at, end_at, subtotal, tax_total, deposit_total, total,
            due_total, notes, created_at)
            VALUES ('ORD-54101', 'return', 1, 1, 'reserved', 'paid', ?, ?, 100, 15, 0, 115, 0, '', ?)""",
            (f'{day} 08:00', f'{day} 18:00', f'{day} 08:00'),
        )
        db.execute(
            """INSERT INTO orders (order_number, booking_type, collect_branch_id, return_branch_id,
            status, payment_status, start_at, end_at, subtotal, tax_total, deposit_total, total,
            due_total, notes, created_at)
            VALUES ('ORD-54102', 'return', 1, 1, 'started', 'payment_due', ?, ?, 100, 15, 0, 115, 115.0, '', ?)""",
            (f'{day} 09:00', f'{day} 19:00', f'{day} 09:00'),
        )
        db.commit()


def _call_route(client, date=DAY, secret='secret'):
    headers = {'x-cron-secret': secret} if secret is not None else {}
    return client.post(f'/api/internal/telegram/daily-summary?date={date}', headers=headers)


def test_the_daily_summary_route_posts_nothing(client, app, sent):
    _seed_day(app)
    res = _call_route(client)
    assert res.status_code == 200
    data = res.get_json()
    # Still a healthy, well-shaped response: the cron must not start failing loudly.
    assert data['ok'] is True
    assert data['sent'] is False
    assert data['skipped'] == 'daily_summary_disabled'
    assert data['date'] == DAY
    # The report really had content to report, and every send path stayed silent.
    assert data['sections'] == {'going_out': 2, 'coming_back': 2, 'payment_due': 1}
    assert sent == []


def test_the_daily_summary_service_itself_sends_nothing(app, sent):
    _seed_day(app)
    with app.app_context():
        result = telegram.send_daily_summary(DAY)
    assert result['ok'] is True
    assert result['sent'] is False
    assert result['skipped'] == 'daily_summary_disabled'
    assert result['date'] == DAY
    assert result['sections']['going_out'] == 2
    assert sent == []


def test_the_suppressed_report_wording_is_still_the_sano_trailers_daily_summary(app, sent):
    """Pin the message the ticket named, so 'it is off' cannot drift into
    'the report was renamed/deleted and something else is off'."""
    _seed_day(app)
    with app.app_context():
        text = telegram.format_daily_summary(telegram.daily_summary_counts(
            telegram._parse_date(DAY)))
    assert 'Sano Trailers daily summary' in text
    assert 'ORD-54101' in text
    assert sent == []  # formatting on its own never sends


def test_the_report_is_off_by_default(app):
    with app.app_context():
        assert telegram.daily_summary_enabled() is False
        assert telegram.notifications_enabled() is True  # Telegram at large is live
        assert telegram.telegram_configured() is True


def test_switching_the_report_back_on_posts_it_again(client, app, sent):
    """The gate is a real switch, not a deletion: prove the same call sends when
    the feature is turned back on (which is how it would be restored)."""
    app.config['TELEGRAM_DAILY_SUMMARY_ENABLED'] = '1'
    res = _call_route(client)
    assert res.status_code == 200
    data = res.get_json()
    assert data['sent'] is True
    assert len(sent) == 1
    assert 'Sano Trailers daily summary' in sent[0]


def test_other_telegram_text_notifications_still_send(client, app, sent):
    # The internal test-message endpoint.
    res = client.post('/api/internal/telegram/test', headers={'x-cron-secret': 'secret'})
    assert res.status_code == 200
    assert res.get_json()['sent'] is True
    assert len(sent) == 1
    assert 'Telegram notifications are connected' in sent[0]

    # A new-customer notification (the other text path in this module).
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO customers (name, created_at) VALUES ('Smoke Customer', '2026-07-02 08:00')")
        db.commit()
        customer_id = db.execute("SELECT id FROM customers WHERE name = 'Smoke Customer'").fetchone()['id']
        telegram.send_new_customer_notification(customer_id)
    assert len(sent) == 2
    assert 'New customer created' in sent[1]

    # ...and the daily summary on the same configured app still adds nothing.
    _call_route(client)
    assert len(sent) == 2


def test_the_internal_route_still_requires_the_cron_secret(client, sent):
    assert _call_route(client, secret=None).status_code == 401
    assert _call_route(client, secret='wrong').status_code == 401
    assert _call_route(client).status_code == 200
    assert sent == []


def test_the_manual_day_report_pdf_still_goes_out(client, app, monkeypatch, sent):
    """The dashboard's own "submit day report" button sends a PDF document - it must
    keep working, and it must not be routed through the text path."""
    documents = {}

    def fake_document(document_bytes, filename, caption=''):
        documents['filename'] = filename
        documents['caption'] = caption
        documents['bytes'] = document_bytes
        return {'ok': True, 'sent': True, 'status': 200}

    from app.routes import cash as cash_routes
    monkeypatch.setattr(cash_routes, '_send_document', fake_document)
    day = today_iso()
    login(client)
    client.post('/cash-up', data={'day': day, 'branch': '1', 'counted_cash': '0'}, follow_redirects=True)
    body = client.post('/cash-up/report/telegram', data={'day': day, 'branch': '1'},
                       follow_redirects=True).get_data(as_text=True)
    assert 'Day report sent on Telegram for Branch 1' in body
    assert documents['filename'] == f'Branch 1_Dashboard Report_{day}.pdf'
    assert documents['bytes'].startswith(b'%PDF-')
    assert sent == []  # the PDF is a document send, never the suppressed text report
