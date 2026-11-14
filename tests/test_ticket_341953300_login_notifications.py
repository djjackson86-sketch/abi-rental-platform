"""Sign-in alerts: real authentication, mocked Telegram transport only."""
import pytest
from app import create_app
from app.db import get_db
from app.services import telegram
from test_branch_login_choice import add_staff, sign_in


@pytest.fixture
def app(tmp_path):
    return create_app({'TESTING': True, 'DATABASE': str(tmp_path / 'login.db'),
                       'SECRET_KEY': 'test', 'ADMIN_EMAIL': 'admin@abi.local',
                       'ADMIN_PASSWORD': 'admin123', 'TELEGRAM_NOTIFICATIONS_ENABLED': '1',
                       'TELEGRAM_BOT_TOKEN': '', 'TELEGRAM_CHAT_ID': ''})


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def messages(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram, '_send_message', lambda text: sent.append(text) or {'sent': True})
    return sent


def test_owner_once_per_signin(client, messages):
    assert sign_in(client, follow=False).status_code == 302
    assert len(messages) == 1
    assert 'Name: Head office admin' in messages[0]
    assert 'Branch: All branches' in messages[0]
    client.get('/dashboard')
    client.get('/dashboard')
    client.get('/select-branch')
    assert len(messages) == 1
    client.post('/logout')
    sign_in(client, follow=False)
    assert len(messages) == 2


def test_failed_login_silent(client, messages):
    sign_in(client, password='incorrect', follow=False)
    assert messages == []
    with client.session_transaction() as state:
        assert 'user_id' not in state


def test_single_branch_name_and_escape(client, app, messages):
    add_staff(client, app, 'Pat <A&B>', [2])
    with app.app_context():
        get_db().execute('UPDATE branches SET name=? WHERE id=2', ('Depot <North & West>',))
        get_db().commit()
    messages.clear()
    sign_in(client, 'Pat <A&B>', 'staff123', follow=False)
    assert messages == ['<b>User logged in</b>\nName: Pat &lt;A&amp;B&gt;\nBranch: Depot &lt;North &amp; West&gt;']


def test_multi_waits_for_valid_selection_and_deduplicates(client, app, messages):
    add_staff(client, app, 'Two Depot', [1, 2])
    messages.clear()
    sign_in(client, 'Two Depot', 'staff123')
    client.get('/select-branch')
    client.post('/select-branch', data={'branch_id': '3'})
    assert messages == []
    client.post('/select-branch', data={'branch_id': '2'})
    assert len(messages) == 1 and 'Branch: Branch 2' in messages[0]
    client.get('/dashboard')
    client.post('/select-branch', data={'branch_id': '1'})
    assert len(messages) == 1


def test_skipped_choice_reports_actual_scope(client, app, messages):
    add_staff(client, app, 'Two Depot', [1, 3])
    messages.clear()
    sign_in(client, 'Two Depot', 'staff123')
    client.get('/static/css/app.css')
    client.get('/manifest.webmanifest')
    client.get('/sw.js')
    client.get('/portal/midrand')
    assert messages == []
    client.get('/dashboard')
    client.get('/dashboard')
    assert len(messages) == 1
    assert 'Branch: All authorised branches: Branch 1, Branch 3' in messages[0]
    assert 'Branch 2' not in messages[0]


def test_provider_exception_cannot_break_login(client, monkeypatch):
    def fail(*args):
        raise RuntimeError('transport unavailable')
    monkeypatch.setattr(telegram, '_send_message', fail)
    assert sign_in(client, follow=False).status_code == 302
    with client.session_transaction() as state:
        assert state['user_id'] and 'login_notification_pending' not in state
    assert client.get('/dashboard').status_code == 200


@pytest.mark.parametrize('enabled,configured,reason', [('0', True, 'disabled'), ('1', False, 'not_configured')])
def test_existing_global_switch_and_configuration(app, monkeypatch, enabled, configured, reason):
    app.config.update(TELEGRAM_NOTIFICATIONS_ENABLED=enabled,
                      TELEGRAM_BOT_TOKEN='test' if configured else '',
                      TELEGRAM_CHAT_ID='test' if configured else '')
    monkeypatch.setattr(telegram.urllib.request, 'urlopen', lambda *a, **k: pytest.fail('unexpected network call'))
    with app.app_context():
        result = telegram.send_login_notification('Pat', 'Branch 1')
    assert result['skipped'] == reason and not result['sent']
