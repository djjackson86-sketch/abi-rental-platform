"""ABI-341953219: portal alerts use persisted customer branch, not session/link."""
import html
from unittest.mock import Mock
from urllib.parse import parse_qs

import pytest

from app.db import get_db
from app.services import branches, portal_intake, telegram
from app.services.customers import create_customer
from test_client_portal_pending_registrations import submission
from test_ticket_341953201_portal_notifications import app, sent


@pytest.mark.parametrize('branch_name', ['Midrand Depot', 'West <Depot> & Hire'])
def test_branch_portal_submission_and_replay(app, sent, branch_name):
    with app.app_context():
        branch_id = branches.create_branch({'name': branch_name, 'active': 1})
        db = get_db()
        db.execute('UPDATE branches SET portal_enabled=1 WHERE id=?', (branch_id,))
        db.commit()
        slug = db.execute('SELECT public_slug FROM branches WHERE id=?', (branch_id,)).fetchone()['public_slug']
    client = app.test_client()
    # An unrelated staff session must not supply notification attribution.
    with client.session_transaction() as session:
        session['active_branch_id'] = 1
    url = f'/portal/{slug}/register'
    data = submission(request_key='ticket-341953219-branch')
    assert client.get(url).status_code == 200
    assert client.post(url, data=data).status_code == 200
    assert client.post(url, data=data).status_code == 200
    assert len(sent) == 1
    assert f'Created at branch: {html.escape(branch_name, quote=False)}' in sent[0]
    with app.app_context():
        assert get_db().execute('SELECT branch_id FROM customers').fetchone()['branch_id'] == branch_id


def test_universal_portal_uses_saved_branch(app, sent):
    assert app.test_client().post('/portal/register', data=submission()).status_code == 200
    with app.app_context():
        branch = get_db().execute('SELECT b.name FROM customers c JOIN branches b ON b.id=c.branch_id').fetchone()
    assert len(sent) == 1
    assert f'Created at branch: {html.escape(branch["name"], quote=False)}' in sent[0]


def test_existing_customer_keeps_ownership_not_submission_branch(app, sent):
    with app.app_context():
        original = branches.create_branch({'name': 'Original & Owner', 'active': 1})
        other = branches.create_branch({'name': 'Other Depot', 'active': 1})
        customer_id = create_customer({'name': 'Existing Person', 'phone': '0835551234'}, branch_id=original, notify=False)
        result = portal_intake.create_or_link_customer(submission(), other, f'link:{customer_id}', slug='staff-link')
        assert result['linked']
        assert get_db().execute('SELECT branch_id FROM customers WHERE id=?', (customer_id,)).fetchone()['branch_id'] == original
    assert len(sent) == 1
    assert 'Created at branch: Original &amp; Owner' in sent[0]
    assert 'Other Depot' not in sent[0]


@pytest.mark.parametrize('missing_customer', [False, True])
def test_unassigned_and_missing_customer_fallback(app, monkeypatch, missing_customer):
    messages = []
    monkeypatch.setattr(telegram, '_send_message', lambda text: messages.append(text) or {'sent': True})
    with app.app_context():
        customer_id = 999999
        if not missing_customer:
            customer_id = create_customer({'name': 'Unassigned'}, notify=False)
            get_db().execute('UPDATE customers SET branch_id=NULL WHERE id=?', (customer_id,))
            get_db().commit()
        assert telegram.send_portal_submission_notification(customer_id)['sent']
    assert 'Created at branch: Unassigned / head office' in messages[0]


def test_outbound_html_payload_has_saved_branch(app, monkeypatch):
    response = Mock(status=200)
    response.read.return_value = b'{"ok":true}'
    context = Mock()
    context.__enter__ = Mock(return_value=response)
    context.__exit__ = Mock(return_value=False)
    network = Mock(return_value=context)
    monkeypatch.setattr(telegram.urllib.request, 'urlopen', network)
    with app.app_context():
        branch_id = branches.create_branch({'name': 'Depot <B> & Hire', 'active': 1})
        customer_id = create_customer({'name': 'Transport Fixture'}, branch_id=branch_id, notify=False)
        assert telegram.send_portal_submission_notification(customer_id)['sent']
    payload = parse_qs(network.call_args.args[0].data.decode())
    assert payload['chat_id'] == ['fixture-group']
    assert payload['parse_mode'] == ['HTML']
    assert 'Created at branch: Depot &lt;B&gt; &amp; Hire' in payload['text'][0]
    assert network.call_count == 1
