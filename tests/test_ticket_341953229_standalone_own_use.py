"""ABI-341953229: independent, type-bound compulsory own-use acceptance."""
import pytest
from app.db import get_db
from app.services import consent, customers
from test_ticket_341953228_portal_own_use import app, endpoint, payload, assert_declaration


@pytest.mark.parametrize('branch', [False, True])
@pytest.mark.parametrize('customer_type', ['individual', 'company'])
@pytest.mark.parametrize('acceptance', [None, '', '0', 'false', '1', 'on', 'other_type'])
def test_direct_post_cannot_bypass_or_reuse_other_type(app, branch, customer_type, acceptance):
    data = payload(customer_type)
    if acceptance is None:
        data.pop('own_use_declaration')
    else:
        data['own_use_declaration'] = (
            ('individual' if customer_type == 'company' else 'company')
            if acceptance == 'other_type' else acceptance
        )
    response = app.test_client().post(endpoint(app, branch), data=data)
    assert response.status_code == 400
    assert b'Please tick the separate box' in response.data
    assert_declaration(response.get_data(as_text=True), customer_type)
    with app.app_context():
        for table in ['customers', 'consent_records', 'portal_submission_claims']:
            assert get_db().execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0


@pytest.mark.parametrize('branch', [False, True])
@pytest.mark.parametrize('customer_type', ['individual', 'company'])
def test_privacy_still_separate_and_required(app, branch, customer_type):
    data = payload(customer_type)
    data['popia_consent'] = '0'
    response = app.test_client().post(endpoint(app, branch), data=data)
    assert response.status_code == 400
    assert consent.consent_required_error().encode() in response.data
    with app.app_context():
        assert get_db().execute('SELECT COUNT(*) FROM customers').fetchone()[0] == 0


def test_no_backfill_existing_customer_and_replay_does_not_duplicate_evidence(app):
    with app.app_context():
        old_id = customers.create_customer({'name': 'Existing customer', 'phone': '0825550000'})
        consent.record_consent(old_id, consent.CHANNEL_COUNTER, True)
    client = app.test_client()
    data = payload('company')
    data['request_key'] = 'ticket-341953229-replay-key'
    assert client.post('/portal/register', data=data).status_code == 200
    assert client.post('/portal/register', data=data).status_code == 200
    with app.app_context():
        assert consent.consent_for(old_id) is not None
        assert consent.consent_for(old_id, 'trailer_own_use') is None
        assert get_db().execute("SELECT COUNT(*) FROM consent_records WHERE consent_type='trailer_own_use'").fetchone()[0] == 1
