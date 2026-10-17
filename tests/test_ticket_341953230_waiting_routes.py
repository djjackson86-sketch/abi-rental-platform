"""ABI-341953230 real route persistence and permission checks."""
import pytest
from app.db import get_db
from app.services import vehicles
from test_ticket_341953212_vehicle_approval import (
    app, client, branches, sign_in, make_pending, save_scan, manual_scan, registration_status,
)


def queued(app):
    with app.app_context():
        return [dict(v) for v in vehicles.list_waiting_vehicles()]


def allocate(client, cid, wid, token='synthetic-token'):
    return client.post(f'/customers/{cid}/allocate-waiting-vehicle', data={
        'waiting_id': wid, 'csrf_token': token,
    })


def test_save_without_customer_and_render_refresh(app, client):
    sign_in(client, app)
    html = manual_scan(client).get_data(as_text=True)
    assert html.index('Approve &amp; Save Vehicle') < html.index('Approve &amp; place on waiting list') < html.index('Start a new scan')
    assert 'formnovalidate' in html
    response = save_scan(client, None, 'QUEUE 230', action='waiting', make_main='', follow=False)
    assert response.status_code == 302
    assert len(queued(app)) == 1
    with app.app_context():
        assert get_db().execute('SELECT COUNT(*) FROM vehicles').fetchone()[0] == 0
    for _ in range(2):
        html = client.get('/customers').get_data(as_text=True)
        assert 'Vehicles awaiting allocation' in html and 'QUEUE 230' in html
        assert '1 / 3 vehicles' in html


@pytest.mark.parametrize('verified', [True, False])
def test_allocate_removes_queue_and_preserves_verification(app, client, verified):
    cid, rid = make_pending(app, verified=verified)
    sign_in(client, app)
    assert save_scan(client, None, 'ALLOC 230', action='waiting', follow=False).status_code == 302
    wid = queued(app)[0]['id']
    html = client.get(f'/customers/{cid}/edit').get_data(as_text=True)
    assert 'Allocate from list' in html and 'ALLOC 230' in html
    assert allocate(client, cid, wid).status_code == 302
    assert not queued(app)
    with app.app_context():
        main = vehicles.get_main_vehicle(cid)
        assert main['registration'] == 'ALLOC 230' and main['status'] == 'approved'
    assert registration_status(app, rid) == ('approved' if verified else 'pending')
    assert allocate(client, cid, wid).status_code == 302
    with app.app_context():
        assert len(vehicles.list_vehicles(cid)) == 1


def test_full_duplicate_and_csrf(app, client):
    sign_in(client, app)
    assert save_scan(client, None, 'BAD CSRF', action='waiting', csrf_token='bad', follow=False).status_code == 400
    assert not queued(app)
    for n in range(3):
        assert save_scan(client, None, f'CAP {n}', action='waiting', follow=False).status_code == 302
    html = save_scan(client, None, 'CAP 3', action='waiting').get_data(as_text=True)
    assert 'waiting list is full' in html
    html = save_scan(client, None, 'cap0', action='waiting').get_data(as_text=True)
    assert 'already waiting' in html
    assert len(queued(app)) == 3


def test_allocation_csrf_and_module_guards(app, client, branches):
    cid, _ = make_pending(app, branch_id=branches['midrand'])
    sign_in(client, app)
    save_scan(client, None, 'GUARD 230', action='waiting', follow=False)
    wid = queued(app)[0]['id']
    assert allocate(client, cid, wid, 'bad').status_code == 400
    sign_in(client, app, role='staff', modules=['customers'])
    assert allocate(client, cid, wid).status_code == 403
    assert save_scan(client, None, 'DENIED 230', action='waiting', follow=False).status_code == 403
    sign_in(client, app, role='staff', branch_id=branches['roodepoort'], branch_ids=[branches['roodepoort']], modules=['scan_vehicle', 'customers'], can_view_all=False)
    assert allocate(client, cid, wid).status_code == 302
    assert len(queued(app)) == 1


def test_existing_main_not_overwritten(app, client):
    cid, _ = make_pending(app, verified=False)
    sign_in(client, app)
    save_scan(client, cid, 'MAIN 230', follow=False)
    save_scan(client, None, 'OTHER 230', action='waiting', follow=False)
    assert allocate(client, cid, queued(app)[0]['id']).status_code == 302
    assert len(queued(app)) == 1
    with app.app_context():
        assert vehicles.get_main_vehicle(cid)['registration'] == 'MAIN 230'
