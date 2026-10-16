"""ABI-341953220: final verification save approves without a second question."""
import pytest

from app.db import get_db
from app.services import portal_registrations
from test_ticket_341953212_vehicle_approval import (
    app, client, branches, sign_in, make_pending, save_scan, manual_scan,
    registration_status, vehicle_rows, visible_customer_ids,
)


def verify_id(client, customer_id, value='1'):
    return client.post(f'/customers/{customer_id}/edit', data={
        'name': 'Synthetic Ticket Client', 'customer_type': 'individual',
        'phone': '0835551234', 'client_verified': value,
    })


def evidence(app, rid):
    with app.app_context():
        row = portal_registrations.registration_by_id(rid)
        return row['approved_at'], row['approved_by_user_id']


@pytest.mark.parametrize('id_first', [True, False])
def test_second_check_approves_and_persists_in_either_order(app, client, branches, id_first):
    cid, rid = make_pending(app, verified=False, branch_id=branches['midrand'])
    sign_in(client, app)
    if id_first:
        assert verify_id(client, cid).status_code == 302
        assert registration_status(app, rid) == 'pending'
        response = save_scan(client, cid, 'TICKET 220', follow=False)
    else:
        response = save_scan(client, cid, 'TICKET 220', follow=False)
        assert response.status_code == 302
        assert registration_status(app, rid) == 'pending'
        response = verify_id(client, cid)
    assert response.status_code == 302
    assert registration_status(app, rid) == 'approved'
    saved = evidence(app, rid)
    assert all(saved)
    for url in (f'/customers/{cid}/edit', f'/customers/{cid}'):
        html = client.get(url).get_data(as_text=True)
        assert 'Customer approved' in html
        assert '>Approved<' in html
        assert 'Pending approval' not in html
    assert cid in visible_customer_ids(app)
    assert 'PEND-1' not in client.get('/customers').get_data(as_text=True)
    assert verify_id(client, cid).status_code == 302
    assert save_scan(client, cid, 'TICKET 220', follow=False).status_code == 302
    assert evidence(app, rid) == saved
    assert len(vehicle_rows(app, 'TICKET 220')) == 1


@pytest.mark.parametrize('verified', [None, 0])
def test_missing_or_rejected_id_never_approves(app, client, verified):
    cid, rid = make_pending(app, verified=False)
    sign_in(client, app)
    assert verify_id(client, cid, '' if verified is None else '0').status_code == 302
    assert save_scan(client, cid, 'NO ID 220', follow=False).status_code == 302
    assert registration_status(app, rid) == 'pending'


def test_rejected_vehicle_and_missing_vehicle_never_approve(app, client):
    cid, rid = make_pending(app, verified=False)
    sign_in(client, app)
    verify_id(client, cid)
    assert registration_status(app, rid) == 'pending'
    assert save_scan(client, cid, 'REJECT 220', action='disapprove', follow=False).status_code == 302
    verify_id(client, cid)
    assert registration_status(app, rid) == 'pending'


def test_blocked_customer_never_auto_approves(app, client):
    cid, rid = make_pending(app)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE customers SET is_blocked=1, blocked_reason='Synthetic refusal' WHERE id=?", (cid,))
        db.commit()
    sign_in(client, app)
    save_scan(client, cid, 'BLOCK 220', follow=False)
    verify_id(client, cid)
    assert registration_status(app, rid) == 'pending'


def test_scanner_only_staff_cannot_auto_approve(app, client, branches):
    cid, rid = make_pending(app, branch_id=branches['midrand'])
    sign_in(client, app, role='staff', branch_id=branches['midrand'],
            branch_ids=[branches['midrand']], modules=['scan_vehicle'], can_view_all=False)
    assert save_scan(client, cid, 'STAFF 220', follow=False).status_code == 302
    assert registration_status(app, rid) == 'pending'
    assert verify_id(client, cid).status_code == 403


def test_wrong_branch_cannot_complete_verification(app, client, branches):
    cid, rid = make_pending(app, branch_id=branches['midrand'])
    sign_in(client, app, role='staff', branch_id=branches['roodepoort'],
            branch_ids=[branches['roodepoort']], modules=['scan_vehicle', 'customers'], can_view_all=False)
    save_scan(client, cid, 'SCOPE 220', follow=False)
    verify_id(client, cid)
    assert registration_status(app, rid) == 'pending'
    assert evidence(app, rid) == (None, None)


def test_no_popup_in_either_scanner_entry(app, client):
    cid, _ = make_pending(app)
    sign_in(client, app)
    for return_intent in (True, False):
        html = manual_scan(client, cid, return_to_verification=return_intent).get_data(as_text=True)
        assert 'approve-client-dialog' not in html
        assert 'Approve client?' not in html
        assert 'state.ask_approval' not in html
        assert 'scan-approve-save' in html
        assert 'scan-disapprove-vehicle' in html


def test_bad_csrf_rolls_back_vehicle_and_customer_approval(app, client):
    cid, rid = make_pending(app)
    sign_in(client, app)
    assert save_scan(client, cid, 'CSRF 220', csrf_token='bad', follow=False).status_code == 400
    assert registration_status(app, rid) == 'pending'
    assert not vehicle_rows(app, 'CSRF 220')


def test_approval_failure_rolls_back_second_check(app, client, monkeypatch):
    cid, rid = make_pending(app, verified=False)
    sign_in(client, app)
    save_scan(client, cid, 'ATOMIC 220', follow=False)
    def fail(_):
        raise ValueError('Synthetic approval failure')
    monkeypatch.setattr(portal_registrations, 'approve_registration', fail)
    verify_id(client, cid)
    with app.app_context():
        assert get_db().execute('SELECT client_verified FROM customers WHERE id=?', (cid,)).fetchone()[0] != 1
    assert registration_status(app, rid) == 'pending'


def test_get_does_not_approve_historical_ready_customer(app, client):
    cid, rid = make_pending(app, verified=False)
    sign_in(client, app)
    save_scan(client, cid, 'HISTORY 220', follow=False)
    with app.app_context():
        db = get_db()
        db.execute('UPDATE customers SET client_verified=1 WHERE id=?', (cid,))
        db.commit()
    assert client.get(f'/customers/{cid}/edit').status_code == 200
    assert client.get('/customers').status_code == 200
    assert registration_status(app, rid) == 'pending'
