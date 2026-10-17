"""ABI-341953232: reasoned soft archiving, not customer deletion."""
import pytest
from app.db import get_db
from app.services import portal_registrations
from app.services.customers import customer_counts, list_customers, search_customers
from test_client_portal_pending_registrations import (
    app, client, branches, register, rows, sign_in, csrf_token, registration_for,
    customer_by_email, owner_id,
)
@pytest.fixture()
def pending(client, app, branches):
    from app.services.customers import create_customer
    with app.app_context():
        cid = create_customer({'name': 'Archive Test', 'email': 'archive@example.test',
                               'phone': '0835551234', 'branch_id': branches['midrand']})
        portal_registrations.record_pending_registration(cid, branches['midrand'], 'ARCHIVE-TEST', name='Archive Test')
        customer = rows(app, 'SELECT * FROM customers WHERE id=?', (cid,))[0]
        registration = registration_for(app, cid)
    sign_in(client, app)
    assert client.get(f'/customers/{cid}').status_code == 200
    return customer, registration


def archive(client, registration, reason='  Registration withdrawn  ', **kwargs):
    return client.post(f"/customers/portal/{registration['id']}/archive", data={
        'csrf_token': csrf_token(client, 'portal_archive'), 'reason': reason,
    }, **kwargs)


def test_archive_keeps_history_and_audit_on_retry(client, app, pending):
    customer, registration = pending
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO orders (order_number, customer_id, created_at) VALUES ('ARCHIVE-1', ?, '')", (customer['id'],))
        oid = db.execute("SELECT id FROM orders WHERE order_number='ARCHIVE-1'").fetchone()['id']
        db.execute("INSERT INTO payments (order_id, amount, method, created_at) VALUES (?, 100, 'cash', '')", (oid,))
        db.execute("INSERT INTO customer_credits (customer_id, amount, created_at) VALUES (?, 75, '')", (customer['id'],))
        db.execute("INSERT INTO documents (order_id, document_type, number, created_at) VALUES (?, 'invoice', 'ARCHIVE-INV', '')", (oid,))
        db.commit()
        assert len(portal_registrations.list_pending_registrations()) == 1
    tables = ['customers', 'orders', 'payments', 'customer_credits', 'documents', 'consent_records']
    before = {t: rows(app, f'SELECT * FROM {t}') for t in tables}
    assert archive(client, registration).status_code == 302
    saved = registration_for(app, customer['id'])
    assert saved['status'] == 'deleted'
    assert saved['archive_reason'] == 'Registration withdrawn'
    assert saved['archived_at'] and saved['archived_by_user_id'] == owner_id(app)
    assert {t: rows(app, f'SELECT * FROM {t}') for t in tables} == before
    with app.app_context():
        assert portal_registrations.list_pending_registrations() == []
        assert list_customers() == []
        assert customer_counts()['total'] == 0
        assert search_customers('Pieter', allow_pending=True) == []
    assert archive(client, registration, 'Replacement reason').status_code == 302
    assert registration_for(app, customer['id']) == saved
    for suffix in ['', '/edit']:
        page = client.get(f"/customers/{customer['id']}{suffix}").data
        assert b'Customer archived' in page and b'Registration withdrawn' in page
        assert b'id="archive-customer-button"' not in page


@pytest.mark.parametrize('reason', ['', '  \n\t '])
def test_requires_reason(client, app, pending, reason):
    customer, registration = pending
    response = archive(client, registration, reason, follow_redirects=True)
    assert b'Enter a reason for deleting and archiving' in response.data
    assert registration_for(app, customer['id'])['status'] == 'pending'


def test_guards(client, app, pending, branches):
    customer, registration = pending
    url = f"/customers/portal/{registration['id']}/archive"
    assert client.get(url).status_code == 405
    assert client.post(url, data={'reason': 'test'}).status_code == 400
    assert client.post('/customers/portal/999999/archive', data={
        'reason': 'test', 'csrf_token': csrf_token(client, 'portal_archive'),
    }).status_code == 404
    sign_in(client, app, role='staff', branch_id=branches['roodepoort'], branch_ids=[branches['roodepoort']], modules=['customers'], can_view_all=False)
    assert archive(client, registration).status_code == 403
    sign_in(client, app, role='staff', modules=['orders'])
    assert archive(client, registration).status_code == 403
    assert registration_for(app, customer['id'])['status'] == 'pending'
    with client.session_transaction() as session:
        session.clear()
    assert client.post(url, data={'reason': 'test'}).status_code == 302


def test_approved_customer_refused(client, app, pending):
    customer, registration = pending
    with app.app_context():
        db = get_db()
        db.execute("UPDATE portal_registrations SET status='approved' WHERE id=?", (registration['id'],))
        db.commit()
    response = archive(client, registration, follow_redirects=True)
    assert b'Only a customer awaiting verification' in response.data
    assert registration_for(app, customer['id'])['status'] == 'approved'


def test_branch_staff_can_archive_and_reason_is_escaped(client, app, pending, branches):
    customer, registration = pending
    sign_in(client, app, role='staff', branch_id=branches['midrand'], branch_ids=[branches['midrand']], modules=['customers'], can_view_all=False)
    assert archive(client, registration, '<script>private</script>').status_code == 302
    page = client.get(f"/customers/{customer['id']}").data
    assert b'&lt;script&gt;private&lt;/script&gt;' in page
    assert b'<script>private</script>' not in page


def test_archive_already_disapproved_customer(client, app, pending):
    customer, registration = pending
    with app.app_context():
        portal_registrations.disapprove_registration(registration['id'], 'Existing block')
    assert archive(client, registration).status_code == 302
    with app.app_context():
        assert list_customers() == []
        assert customer_counts()['total'] == 0
    assert rows(app, 'SELECT blocked_reason FROM customers')[0]['blocked_reason'] == 'Existing block'


def test_existing_disapprove_and_approve_still_work(client, app, pending):
    customer, registration = pending
    response = client.post(f"/customers/portal/{registration['id']}/disapprove", data={
        'csrf_token': csrf_token(client, 'portal_pending_delete'), 'reason': 'Existing disapproval',
    })
    assert response.status_code == 302
    assert registration_for(app, customer['id'])['status'] == 'pending'
    assert rows(app, 'SELECT blocked_reason FROM customers')[0]['blocked_reason'] == 'Existing disapproval'
    with app.app_context():
        from app.services import vehicles
        db = get_db()
        db.execute('UPDATE customers SET is_blocked=0, client_verified=1 WHERE id=?', (customer['id'],))
        db.commit()
        vid = vehicles.create_vehicle({'registration': 'TEST001GP', 'make': 'TEST'}, customer_id=customer['id'])
        vehicles.promote_main_vehicle(customer['id'], vid)
    response = client.post(f"/customers/portal/{registration['id']}/approve", data={
        'csrf_token': csrf_token(client, 'portal_approval'),
    })
    assert response.status_code == 302
    assert registration_for(app, customer['id'])['status'] == 'approved'


def test_button_below_disapprove_on_both_views(client, pending):
    customer, _ = pending
    for suffix in ['', '/edit']:
        html = client.get(f"/customers/{customer['id']}{suffix}").get_data(as_text=True)
        assert html.index('>Disapprove client</button>') < html.index('id="archive-customer-button"')
        assert 'Delete &amp; Archive' in html and 'background: #ffddb3' in html
        assert 'id="archive-reason" name="reason" rows="4" required' in html
        assert 'type="button" onclick="this.closest(\'dialog\').close()">Cancel' in html
