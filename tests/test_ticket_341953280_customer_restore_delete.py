"""Deleted customer recovery and guarded irreversible erasure."""
import sqlite3
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

from app.db import get_db
from app.services import portal_registrations as registrations
from app.services.customers import (
    create_customer, delete_customer, restore_customer, permanently_delete_customer,
    get_customer, customer_filtered_total, list_customers,
)
from test_ticket_341953272_inactive_customer_exclusion import app, client, login


def seed(app, verified=None, registration=None, approved_at=None, blocked=False, legacy=False):
    with app.app_context():
        cid = create_customer({'name': 'Recovery Test Customer', 'client_verified': verified})
        db = get_db()
        db.execute("UPDATE customers SET branch_id=1, is_blocked=?, blocked_reason=?, custom_fields_json=? WHERE id=?",
                   (int(blocked), 'Retain fraud block' if blocked else '', '{"suspect_fraud":true,"customer_id_number":"test-id"}', cid))
        if registration:
            rid = registrations.record_pending_registration(cid, 1, f'TEST-{cid}')
            db.execute('UPDATE portal_registrations SET status=?, approved_at=? WHERE id=?', (registration, approved_at, rid))
        db.execute("INSERT INTO vehicles(customer_id,registration,created_at,updated_at) VALUES (?,'TEST-280','','')", (cid,))
        db.commit()
        if not legacy:
            delete_customer(cid)
        return cid


def post_action(client, cid, action, **extra):
    client.get(f'/customers/{cid}')
    scope = 'customer_restore' if action == 'restore' else 'customer_permanent_delete'
    with client.session_transaction() as session:
        token = session['_csrf_tokens'][scope]
    return client.post(f'/customers/{cid}/{action}', data={'csrf_token': token, **extra}, follow_redirects=True)


@pytest.mark.parametrize('verified,registration,approved_at,destination', [
    ('1', None, None, 'Active'),
    ('0', None, None, 'Awaiting Verification'),
    (None, None, None, 'Awaiting Verification'),
    ('1', 'approved', '2026-01-01', 'Active'),
    ('1', 'deleted', '2026-01-01', 'Active'),
    ('1', 'deleted', None, 'Awaiting Verification'),
    ('0', 'approved', '2026-01-01', 'Awaiting Verification'),
    ('1', 'pending', None, 'Awaiting Verification'),
])
def test_restore_destination_identity_evidence_and_counts(app, client, verified, registration, approved_at, destination):
    login(client)
    cid = seed(app, verified, registration, approved_at)
    with app.app_context():
        before = dict(get_customer(cid))
        assert customer_filtered_total(folder='retained') == 1
    response = post_action(client, cid, 'restore')
    assert f'Customer restored to {destination}'.encode() in response.data
    with app.app_context():
        customer = get_customer(cid)
        for field in ('id', 'name', 'client_verified', 'is_blocked', 'created_at'):
            assert customer[field] == before[field]
        fields = json.loads(customer['custom_fields_json'])
        assert fields['customer_id_number'] == 'test-id'
        assert fields['suspect_fraud'] is True
        assert fields['lifecycle_history'][-2]['lifecycle_changed_at'] == before['lifecycle_changed_at']
        assert fields['lifecycle_history'][-1]['action'] == 'restore'
        assert customer['lifecycle_status'] == 'active'
        assert customer_filtered_total(folder='retained') == 0
        assert registrations.is_pending(cid) == (destination != 'Active')
        assert (cid in [c['id'] for c in list_customers()]) == (destination == 'Active')
        assert len(get_db().execute('SELECT * FROM vehicles WHERE customer_id=?', (cid,)).fetchall()) == 1
        if approved_at:
            assert get_db().execute('SELECT approved_at FROM portal_registrations WHERE customer_id=?', (cid,)).fetchone()[0] == approved_at
        with pytest.raises(ValueError, match='Only a deleted'):
            restore_customer(cid)


def test_legacy_duplicate_tombstones_and_block_survive(app, client):
    login(client)
    cid = seed(app, '1', 'deleted', '2026-01-01', blocked=True, legacy=True)
    with app.app_context():
        db = get_db()
        registrations.record_pending_registration(cid, 1, 'SECOND')
        db.execute("UPDATE portal_registrations SET status='deleted' WHERE customer_id=?", (cid,))
        db.commit()
    assert b'Restore customer' in client.get(f'/customers/{cid}').data
    response = post_action(client, cid, 'restore')
    assert b'Customer restored to Active' in response.data
    with app.app_context():
        customer = get_customer(cid)
        assert customer['is_blocked'] == 1 and customer['blocked_reason'] == 'Retain fraud block'
        assert not registrations.is_pending(cid)
        assert not registrations.list_pending_registrations()
        assert registrations.approval_blockers(customer)
        assert customer_filtered_total(folder='retained') == 0


def test_delete_eligible_children_no_orphans_and_retries(app, client):
    login(client)
    cid = seed(app, '0', 'deleted')
    with app.app_context():
        db = get_db()
        rid = db.execute('SELECT id FROM portal_registrations WHERE customer_id=?', (cid,)).fetchone()[0]
        db.execute("INSERT INTO portal_submission_claims(scope,request_key,customer_id,registration_id,created_at) VALUES ('test','key',?,?, '')", (cid, rid))
        db.commit()
    response = post_action(client, cid, 'permanently-delete', confirmation=f'DELETE {cid}')
    assert b'Customer permanently deleted' in response.data
    with app.app_context():
        assert get_customer(cid) is None
        for table in ('vehicles', 'portal_registrations', 'portal_submission_claims'):
            assert not get_db().execute(f'SELECT 1 FROM {table} WHERE customer_id=?', (cid,)).fetchone()
        assert not get_db().execute('PRAGMA foreign_key_check').fetchall()
        with pytest.raises(ValueError, match='Only a deleted'):
            permanently_delete_customer(cid)


@pytest.mark.parametrize('status', ['approved', 'pending'])
def test_active_registration_with_old_tombstone_is_not_a_deleted_target(app, client, status):
    login(client)
    cid = seed(app, '1', 'deleted', legacy=True)
    with app.app_context():
        rid = registrations.record_pending_registration(cid, 1, 'CURRENT')
        db = get_db()
        db.execute('UPDATE portal_registrations SET status=? WHERE id=?', (status, rid))
        db.commit()
        for action in (restore_customer, permanently_delete_customer):
            with pytest.raises(ValueError, match='Only a deleted'):
                action(cid)
        assert get_customer(cid) is not None
    assert b'Permanently delete</button>' not in client.get(f'/customers/{cid}').data


@pytest.mark.parametrize('verified,blocked,allowed', [('1', False, True), ('0', False, True), ('1', True, False)])
def test_restored_booking_gate_keeps_existing_reservation_and_block_rules(app, client, verified, blocked, allowed):
    from test_ticket_341953272_inactive_customer_exclusion import order_payload
    login(client)
    cid = seed(app, verified, blocked=blocked)
    response = client.post(f'/orders/new', data=order_payload(cid))
    assert response.status_code == 200
    if not blocked:
        assert b'deleted and cannot take a new booking' in response.data
    with app.app_context():
        assert not get_db().execute('SELECT 1 FROM orders WHERE customer_id=?', (cid,)).fetchone()
    post_action(client, cid, 'restore')
    response = client.post('/orders/new', data=order_payload(cid))
    if allowed:
        assert response.status_code == 302
    else:
        assert response.status_code == 200
        with app.app_context():
            assert not get_db().execute('SELECT 1 FROM orders WHERE customer_id=?', (cid,)).fetchone()


@pytest.mark.parametrize('sql', [
    "INSERT INTO orders(customer_id,order_number,created_at) VALUES (?,'PROTECTED','')",
    "INSERT INTO customer_credits(customer_id,amount,created_at) VALUES (?,0,'')",
    "INSERT INTO legacy_customer_balances(customer_id,source_order_number,created_at,updated_at) VALUES (?,'legacy','','')",
    "INSERT INTO legacy_balance_payments(customer_id,amount,method,payment_date,created_at) VALUES (?,0,'cash','','')",
    "INSERT INTO prepaid_fundings(customer_id,amount,method,payment_date,created_at) VALUES (?,10,'cash','','')",
    "INSERT INTO consent_records(customer_id,accepted_at) VALUES (?,'')",
])
def test_business_and_audit_history_refused_without_mutation(app, client, sql):
    login(client)
    cid = seed(app, '1', 'deleted')
    with app.app_context():
        db = get_db()
        db.execute(sql, (cid,))
        db.commit()
        before = dict(get_customer(cid))
    response = post_action(client, cid, 'permanently-delete', confirmation=f'DELETE {cid}')
    assert b'Permanent deletion blocked' in response.data
    with app.app_context():
        assert dict(get_customer(cid)) == before
        assert get_db().execute('SELECT 1 FROM vehicles WHERE customer_id=?', (cid,)).fetchone()
        assert get_db().execute('SELECT 1 FROM portal_registrations WHERE customer_id=?', (cid,)).fetchone()


@pytest.mark.parametrize('action', ['restore', 'permanently-delete'])
def test_access_csrf_confirmation_and_scope(app, client, action):
    cid = seed(app, '1')
    assert client.post(f'/customers/{cid}/{action}').status_code == 302
    login(client)
    assert client.get(f'/customers/{cid}/{action}').status_code == 405
    assert client.post(f'/customers/{cid}/{action}').status_code == 400
    client.get(f'/customers/{cid}')
    if action == 'permanently-delete':
        with client.session_transaction() as session:
            token = session['_csrf_tokens']['customer_permanent_delete']
        assert client.post(f'/customers/{cid}/{action}', data={'csrf_token': token, 'confirmation': 'yes'}).status_code == 400
    with client.session_transaction() as session:
        session['user_role'] = 'staff'
        session['staff_modules'] = ['orders']
    assert client.post(f'/customers/{cid}/{action}').status_code == 403
    with client.session_transaction() as session:
        session['staff_modules'] = ['customers']
        session['can_view_all_branches'] = False
        session['branch_id'] = 999
        session['branch_ids'] = [999]
    response = post_action(client, cid, action, confirmation=f'DELETE {cid}')
    assert b'outside the branches' in response.data
    with app.app_context():
        assert get_customer(cid)['lifecycle_status'] == 'deleted'


@pytest.mark.parametrize('action', [restore_customer, permanently_delete_customer])
def test_archived_and_active_are_not_destructive_targets(app, client, action):
    login(client)
    cid = seed(app, '1')
    with app.app_context():
        db = get_db()
        for status in ('archived', 'active'):
            db.execute('UPDATE customers SET lifecycle_status=? WHERE id=?', (status, cid))
            db.commit()
            with pytest.raises(ValueError, match='Only a deleted'):
                action(cid)
            assert get_customer(cid)['lifecycle_status'] == status
    assert b'Permanently delete</button>' not in client.get(f'/customers/{cid}').data


@pytest.mark.parametrize('action,trigger', [
    (permanently_delete_customer, "BEFORE DELETE ON customers"),
    (restore_customer, "BEFORE UPDATE ON customers WHEN NEW.lifecycle_status='active' AND OLD.lifecycle_status='deleted'"),
])
def test_transaction_failure_rolls_back_all_children(app, client, action, trigger):
    login(client)
    cid = seed(app, '0', 'deleted')
    with app.app_context():
        db = get_db()
        before = dict(get_customer(cid))
        db.execute(f"CREATE TRIGGER fail_recovery {trigger} BEGIN SELECT RAISE(ABORT,'forced rollback'); END")
        db.commit()
        with pytest.raises(sqlite3.IntegrityError, match='forced rollback'):
            action(cid)
        assert dict(get_customer(cid)) == before
        assert db.execute('SELECT status FROM portal_registrations WHERE customer_id=?', (cid,)).fetchone()[0] == 'deleted'
        assert db.execute('SELECT 1 FROM vehicles WHERE customer_id=?', (cid,)).fetchone()


def test_desktop_mobile_restore_and_delete(app, client):
    login(client)
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = Path.home() / 'AppData/Local/hermes/cache/scratch/ABI-341953280'
    output.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome', headless=True)
            for width, height, label in [(1440, 1000, 'desktop'), (390, 844, 'mobile')]:
                cid = seed(app, '1')
                context = browser.new_context(viewport={'width': width, 'height': height})
                context.add_cookies([{'name': 'session', 'value': client.get_cookie('session').value, 'domain': '127.0.0.1', 'path': '/'}])
                page = context.new_page()
                page.goto(f'http://127.0.0.1:{server.server_port}/customers?folder=retained')
                page.locator(f'a.customer-name-cell[href="/customers/{cid}"]').click()
                expect(page.get_by_role('button', name='Restore customer', exact=True)).to_be_visible()
                expect(page.get_by_role('button', name='Permanently delete', exact=True)).to_be_visible()
                page.screenshot(path=str(output / f'{label}-actions.png'), full_page=True)
                page.get_by_role('button', name='Restore customer', exact=True).click()
                expect(page.get_by_text('Customer restored to Active.', exact=False)).to_be_visible()
                assert page.get_by_role('button', name='Permanently delete', exact=True).count() == 0
                with app.app_context():
                    delete_customer(cid)
                page.reload()
                page.get_by_label(f'Type DELETE {cid} to confirm').fill(f'DELETE {cid}')
                page.once('dialog', lambda dialog: dialog.dismiss())
                page.get_by_role('button', name='Permanently delete', exact=True).click()
                expect(page.locator('h1')).to_contain_text('Recovery Test Customer')
                page.once('dialog', lambda dialog: dialog.accept())
                page.get_by_role('button', name='Permanently delete', exact=True).click()
                expect(page.get_by_text('Customer permanently deleted. This cannot be undone.', exact=True)).to_be_visible()
                with app.app_context():
                    assert get_customer(cid) is None
                context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print('Desktop/mobile real Chrome restore, cancel and confirmed permanent delete passed; server closed:', output)
