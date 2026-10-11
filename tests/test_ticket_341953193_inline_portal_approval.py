"""ABI-341953193: saved inline approval and customer-specific new-order entry."""
import re

import pytest

from app.db import get_db
from app.services import vehicles
from test_client_portal_frontend import (
    app, client, branch, pending_client, scoped_csrf, registration_for, sign_in,
)


def prepare(app, customer_id, *, verified=True, main_vehicle=True):
    with app.app_context():
        db = get_db()
        db.execute("UPDATE customers SET client_verified = ? WHERE id = ?", (int(verified), customer_id))
        db.commit()
        if main_vehicle:
            vehicle_id = vehicles.create_vehicle({"registration": f"TEST193{customer_id}GP", "make": "Toyota"}, customer_id=customer_id)
            vehicles.promote_main_vehicle(customer_id, vehicle_id)


def approve(client, registration, token, **kwargs):
    return client.post(f"/customers/portal/{registration['id']}/approve", data={"csrf_token": token}, **kwargs)


def test_inline_success_persists_and_prefills_existing_customer(client, app, pending_client):
    customer, registration = pending_client
    cid = customer['id']
    prepare(app, cid)
    client.get(f"/customers/{cid}")
    response = approve(client, registration, scoped_csrf(client), headers={"Accept": "application/json"})
    assert response.status_code == 200
    assert 'Location' not in response.headers
    result = response.get_json()
    assert result['ok'] is True
    assert 'disabled>Approved</button>' in result['html']
    assert 'Delete pending client' not in result['html']
    assert f'/orders/new?customer_id={cid}' in result['html']
    assert result['html'].index('>Create order</a>') < result['html'].index('>Create order for this customer</a>')
    assert registration_for(app, cid)['status'] == 'approved'
    for path in (f'/customers/{cid}', f'/customers/{cid}/edit'):
        html = client.get(path).get_data(as_text=True)
        assert 'disabled>Approved</button>' in html
        assert 'data-portal-approval>' not in html
    order = client.get(f'/orders/new?customer_id={cid}').get_data(as_text=True)
    assert re.search(rf'name="customer_id"[^>]*value="{cid}"', order)
    assert customer['name'] in order
    with app.app_context():
        assert get_db().execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 0
        assert get_db().execute('SELECT COUNT(*) FROM customers').fetchone()[0] == 1


@pytest.mark.parametrize('verified,main_vehicle', [(False, True), (True, False), (False, False)])
def test_failed_validation_keeps_pending_and_actionable(client, app, pending_client, verified, main_vehicle):
    customer, registration = pending_client
    prepare(app, customer['id'], verified=verified, main_vehicle=main_vehicle)
    client.get(f"/customers/{customer['id']}")
    response = approve(client, registration, scoped_csrf(client), headers={'Accept': 'application/json'})
    assert response.status_code == 400
    assert response.get_json()['ok'] is False
    assert response.get_json()['message']
    assert registration_for(app, customer['id'])['status'] == 'pending'
    html = client.get(f"/customers/{customer['id']}").get_data(as_text=True)
    assert 'data-portal-approval>' in html
    assert 'portal-approved' not in html
    assert 'Create order for this customer' not in html


def test_retry_is_idempotent_and_retains_approval_audit(client, app, pending_client):
    customer, registration = pending_client
    prepare(app, customer['id'])
    client.get(f"/customers/{customer['id']}")
    token = scoped_csrf(client)
    assert approve(client, registration, token, headers={'Accept': 'application/json'}).status_code == 200
    saved = registration_for(app, customer['id'])
    assert approve(client, registration, token, headers={'Accept': 'application/json'}).get_json()['ok']
    assert registration_for(app, customer['id']) == saved


def test_no_js_fallback_does_not_redirect_to_edit(client, app, pending_client):
    customer, registration = pending_client
    prepare(app, customer['id'])
    client.get(f"/customers/{customer['id']}")
    response = approve(client, registration, scoped_csrf(client))
    assert response.status_code == 302
    assert response.headers['Location'] == f"/customers/{customer['id']}"


def test_inline_csrf_still_required(client, app, pending_client):
    customer, registration = pending_client
    prepare(app, customer['id'])
    response = approve(client, registration, 'invalid', headers={'Accept': 'application/json'})
    assert response.status_code == 400
    assert registration_for(app, customer['id'])['status'] == 'pending'


def test_order_button_respects_module_permission(client, app, pending_client):
    customer, registration = pending_client
    prepare(app, customer['id'])
    client.get(f"/customers/{customer['id']}")
    approve(client, registration, scoped_csrf(client), headers={'Accept': 'application/json'})
    with client.session_transaction() as session:
        session['user_role'] = 'staff'
        session['staff_modules'] = ['customers']
    html = client.get(f"/customers/{customer['id']}").get_data(as_text=True)
    assert 'disabled>Approved</button>' in html
    assert 'Create order for this customer' not in html
