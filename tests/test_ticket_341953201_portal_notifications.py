"""ABI-341953201: committed portal information sends one minimal admin alert."""
import sqlite3
from unittest.mock import Mock

import pytest

from app import create_app
from app.db import get_db
from app.services import portal_intake, telegram
from app.services.customers import create_customer
from test_client_portal_pending_registrations import submission


@pytest.fixture
def app(tmp_path, monkeypatch):
    app = create_app({
        "TESTING": True, "DATABASE": str(tmp_path / "ticket.sqlite"),
        "SECRET_KEY": "test", "ADMIN_EMAIL": "admin@abi.local",
        "ADMIN_PASSWORD": "admin123", "TELEGRAM_NOTIFICATIONS_ENABLED": "true",
        "TELEGRAM_BOT_TOKEN": "fixture-token", "TELEGRAM_CHAT_ID": "fixture-group",
        "PUBLIC_BASE_URL": "https://admin.example.test",
    })
    portal_intake.reset_rate_limits()
    monkeypatch.setattr(portal_intake, "registration_is_open", lambda: True)
    return app


@pytest.fixture
def sent(app, monkeypatch):
    messages = []
    def capture(text):
        # A separate connection proves the write and receipt are already committed.
        with sqlite3.connect(app.config["DATABASE"]) as db:
            assert db.execute("SELECT count(*) FROM portal_submission_claims WHERE status='done'").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM customers").fetchone()[0] == 1
        messages.append(text)
        return {"ok": True, "sent": True}
    monkeypatch.setattr(telegram, "_send_message", capture)
    return messages


@pytest.mark.parametrize("branch_portal", [False, True])
def test_success_and_replay_send_once(app, sent, branch_portal):
    with app.app_context():
        branch = get_db().execute("SELECT id, public_slug FROM branches ORDER BY id LIMIT 1").fetchone()
        get_db().execute("UPDATE branches SET portal_enabled=1 WHERE id=?", (branch['id'],))
        get_db().commit()
        slug = branch['public_slug']
    url = f"/portal/{slug}/register" if branch_portal else "/portal/register"
    client = app.test_client()
    assert client.get(url).status_code == 200
    assert sent == []
    data = submission(name='<Admin & customer>', request_key='synthetic-request-key-341953201')
    assert client.post(url, data=data).status_code == 200
    assert client.post(url, data=data).status_code == 200
    assert len(sent) == 1
    message = sent[0]
    assert 'Customer portal information submitted' in message
    assert '&lt;Admin &amp; customer&gt;' in message
    assert 'Submitted:' in message and '+02:00' in message
    assert 'https://admin.example.test/customers/1' in message
    for excluded in ('New customer created', data['email'].lower(), data['phone'], data['address_line1'], data['alternative_contact_name'], data['request_key'], '/portal/'):
        assert excluded not in message
    assert client.get('/customers/1').status_code == 302  # review remains login-protected
    with app.app_context():
        row = get_db().execute('SELECT name, address_line1 FROM customers').fetchone()
        assert row['name'] == data['name'] and row['address_line1'] == data['address_line1']


def test_permitted_existing_customer_link_notifies(app, sent):
    with app.app_context():
        customer_id = create_customer({'name': 'Existing Person', 'phone': '0835551234'}, notify=False)
        result = portal_intake.create_or_link_customer(submission(), 1, f'link:{customer_id}', slug='staff-link')
        assert result['linked']
        assert get_db().execute('SELECT address_line1 FROM customers WHERE id=?', (customer_id,)).fetchone()['address_line1'] == '12 Kerk Street'
        portal_intake.create_or_link_customer(submission(), 1, f'link:{customer_id}', slug='staff-link')
    assert len(sent) == 1


@pytest.mark.parametrize('overrides', [{'name': ''}, {'popia_consent': ''}, {'company_website': 'bot'}])
def test_invalid_or_honeypot_does_not_notify(app, sent, overrides):
    app.test_client().post('/portal/register', data=submission(**overrides))
    assert not sent


def test_closed_gate_does_not_notify(app, sent, monkeypatch):
    monkeypatch.setattr(portal_intake, 'registration_is_open', lambda: False)
    assert app.test_client().post('/portal/register', data=submission()).status_code == 200
    assert not sent


def test_public_existing_customer_refused_not_modified(app, monkeypatch):
    send = Mock()
    monkeypatch.setattr(telegram, '_send_message', send)
    with app.app_context():
        create_customer({'name': 'Existing Person', 'phone': '0835551234'}, notify=False)
    assert app.test_client().post('/portal/register', data=submission()).status_code == 200
    send.assert_not_called()
    with app.app_context():
        assert not get_db().execute('SELECT address_line1 FROM customers').fetchone()['address_line1']


def test_failed_save_does_not_notify(app, sent, monkeypatch):
    from app.services import portal_registrations
    def fail(*args, **kwargs):
        raise ValueError('Synthetic save failure')
    monkeypatch.setattr(portal_registrations, 'record_pending_registration', fail)
    assert app.test_client().post('/portal/register', data=submission()).status_code == 400
    assert not sent
    with app.app_context():
        assert get_db().execute('SELECT count(*) FROM customers').fetchone()[0] == 0


def test_disabled_telegram_preserves_save(app, monkeypatch):
    app.config['TELEGRAM_NOTIFICATIONS_ENABLED'] = 'false'
    network = Mock()
    monkeypatch.setattr(telegram.urllib.request, 'urlopen', network)
    assert app.test_client().post('/portal/register', data=submission()).status_code == 200
    network.assert_not_called()
    with app.app_context():
        assert get_db().execute('SELECT count(*) FROM customers').fetchone()[0] == 1


@pytest.mark.parametrize('failure', ['network', 'unexpected'])
def test_delivery_failure_logged_without_breaking_save(app, monkeypatch, caplog, failure):
    if failure == 'network':
        monkeypatch.setattr(telegram.urllib.request, 'urlopen', Mock(side_effect=OSError('synthetic offline')))
    else:
        monkeypatch.setattr(telegram, 'send_portal_submission_notification', Mock(side_effect=RuntimeError('synthetic failure')))
    client = app.test_client()
    assert client.post('/portal/register', data=submission()).status_code == 200
    assert client.post('/portal/register', data=submission()).status_code == 200
    assert 'failed' in caplog.text.lower()
    with app.app_context():
        assert get_db().execute('SELECT count(*) FROM customers').fetchone()[0] == 1


def test_transport_uses_existing_group(app, monkeypatch):
    from urllib.parse import parse_qs
    response = Mock()
    response.status = 200
    response.read.return_value = b'{"ok":true}'
    context = Mock()
    context.__enter__ = Mock(return_value=response)
    context.__exit__ = Mock(return_value=False)
    network = Mock(return_value=context)
    monkeypatch.setattr(telegram.urllib.request, 'urlopen', network)
    with app.app_context():
        customer_id = create_customer({'name': 'Fixture'}, notify=False)
        assert telegram.send_portal_submission_notification(customer_id)['sent']
    payload = parse_qs(network.call_args.args[0].data.decode())
    assert payload['chat_id'] == ['fixture-group']
    assert payload['parse_mode'] == ['HTML']
