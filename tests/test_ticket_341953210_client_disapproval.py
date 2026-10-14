"""ABI-341953210: retaining a pending client, required reasons and rental refusal."""
import pytest
from app.db import get_db
from app.services import portal_intake, portal_registrations
from test_client_portal_pending_registrations import (
    app, client, branches, register, rows, sign_in, csrf_token, registration_for,
    customer_by_email,
)
from test_ticket_341953028_customer_blocking import order_payload


@pytest.fixture()
def pending(client, app, branches):
    assert register(client, app, branches['midrand']).status_code == 200
    customer = customer_by_email(app, 'pieter@example.co.za')
    registration = registration_for(app, customer['id'])
    sign_in(client, app)
    assert client.get(f"/customers/{customer['id']}").status_code == 200
    return customer, registration


def disapprove(client, registration, reason='Unsafe towing history', path='disapprove', **kwargs):
    return client.post(f"/customers/portal/{registration['id']}/{path}", data={
        'csrf_token': csrf_token(client, 'portal_pending_delete'), 'reason': reason,
    }, **kwargs)


@pytest.mark.parametrize('path', ['disapprove', 'delete'])
def test_retains_customer_and_reason_excludes_queue_and_rental(client, app, pending, path):
    customer, registration = pending
    result = disapprove(client, registration, '  Unsafe towing history  ', path, follow_redirects=True)
    assert result.status_code == 200
    assert b'Client disapproved' in result.data
    assert b'Unsafe towing history' in result.data
    saved = rows(app, 'SELECT * FROM customers WHERE id=?', (customer['id'],))[0]
    assert saved['is_blocked'] == 1
    assert saved['blocked_reason'] == 'Unsafe towing history'
    assert saved['client_verified'] == customer['client_verified']
    assert registration_for(app, customer['id'])['status'] == 'pending'
    with app.app_context():
        assert portal_registrations.list_pending_registrations() == []
        assert portal_intake.blocked_match('Different name', '+27 83 555 1234', '')
        assert portal_intake.blocked_match('Different name', '', 'PIETER@EXAMPLE.CO.ZA')
    response = client.post('/orders/new', data=order_payload(customer['id']), follow_redirects=True)
    assert b'Customer blocked' in response.data
    assert rows(app, 'SELECT id FROM orders') == []
    disapprove(client, registration, 'Changed reason')
    assert rows(app, 'SELECT blocked_reason FROM customers')[0]['blocked_reason'] == 'Unsafe towing history'


@pytest.mark.parametrize('reason', ['', '  \n\t '])
@pytest.mark.parametrize('path', ['disapprove', 'delete'])
def test_blank_reason_cannot_write_even_from_old_delete_url(client, app, pending, reason, path):
    customer, registration = pending
    response = disapprove(client, registration, reason, path, follow_redirects=True)
    assert b'Enter a reason' in response.data
    assert rows(app, 'SELECT is_blocked FROM customers')[0]['is_blocked'] == 0
    assert registration_for(app, customer['id'])['status'] == 'pending'


def test_modal_and_cancel_are_non_submitting_and_reason_is_escaped(client, app, pending):
    customer, registration = pending
    page = client.get(f"/customers/{customer['id']}/edit").get_data(as_text=True)
    assert 'Delete pending client' not in page
    assert 'showModal()' in page and '<dialog ' in page
    assert 'name="reason"' in page and 'required autofocus' in page
    assert 'type="button" onclick="this.closest(\'dialog\').close()">Cancel' in page
    assert rows(app, 'SELECT is_blocked FROM customers')[0]['is_blocked'] == 0
    response = disapprove(client, registration, '<script>alert(1)</script>', follow_redirects=True)
    assert b'&lt;script&gt;alert(1)&lt;/script&gt;' in response.data
    assert b'<script>alert(1)</script>' not in response.data


def test_csrf_method_unknown_and_scope_guards(client, app, pending, branches):
    customer, registration = pending
    url = f"/customers/portal/{registration['id']}/disapprove"
    assert client.get(url).status_code == 405
    assert client.post(url, data={'reason': 'test'}).status_code == 400
    assert client.post('/customers/portal/999999/disapprove', data={
        'reason': 'test', 'csrf_token': csrf_token(client, 'portal_pending_delete'),
    }).status_code == 404
    sign_in(client, app, role='staff', branch_id=branches['roodepoort'],
            branch_ids=[branches['roodepoort']], modules=['customers'], can_view_all=False)
    assert disapprove(client, registration).status_code == 403
    sign_in(client, app, role='staff', modules=['orders'])
    assert disapprove(client, registration).status_code == 403
    assert rows(app, 'SELECT is_blocked FROM customers')[0]['is_blocked'] == 0


def test_cannot_approve_blocked_client_and_unblock_restores_pending(client, app, pending):
    customer, registration = pending
    disapprove(client, registration)
    with app.test_request_context():
        with pytest.raises(ValueError, match='blocked'):
            portal_registrations.approve_registration(registration['id'])
    with app.app_context():
        db = get_db()
        db.execute('UPDATE customers SET is_blocked=0 WHERE id=?', (customer['id'],))
        db.commit()
        assert len(portal_registrations.list_pending_registrations()) == 1
        assert registration_for(app, customer['id'])['status'] == 'pending'


@pytest.mark.parametrize('identity', [
    {'customer_email': 'PIETER@example.co.za', 'customer_phone': '0990000000'},
    {'customer_email': 'different@example.test', 'customer_phone': '+27 83 555 1234'},
])
def test_storefront_refuses_duplicate_identity_without_private_reason(client, app, pending, monkeypatch, identity):
    _, registration = pending
    disapprove(client, registration, 'PRIVATE REASON')
    monkeypatch.setattr('app.routes.public.get_company_settings', lambda: {'store_enabled': True})
    monkeypatch.setattr('app.routes.public._public_product', lambda _: {'id': 1})
    monkeypatch.setattr('app.routes.public.render_template', lambda *a, **kw: 'refused')
    response = client.post('/store/products/1/book', data={'customer_name': 'Another name', **identity})
    assert response.status_code == 400
    assert b'PRIVATE REASON' not in response.data
    assert len(rows(app, 'SELECT id FROM customers')) == 1
    assert rows(app, 'SELECT id FROM orders') == []
