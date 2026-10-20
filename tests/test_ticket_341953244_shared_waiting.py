"""Head-office availability and stale allocation regression (synthetic records only)."""
import pytest
from flask import session
from app.db import get_db
from app.services import vehicles
from test_ticket_341953235_waiting_per_branch import (
    app, client, branch_ids, owner_id, sign_in, save, scan_form, make_pending,
)


def staff_scope(branch):
    session.update(user_role='staff', branch_id=branch, branch_ids=[branch],
                   can_view_all_branches=False, staff_modules=['customers'])


@pytest.mark.parametrize('branch_name', ['Branch 1', 'Branch 2', 'Branch 3'])
def test_head_office_available_in_every_branch_and_customer_dialog(app, client, branch_name):
    ids = branch_ids(app)
    shared = save(app, 'HQ 244')
    save(app, 'PRIVATE 244', ids['Branch 2'])
    branch = ids[branch_name]
    customer = make_pending(app, branch)
    sign_in(client, app, role='staff', branch_id=branch, branch_ids=[branch],
            can_view_all=False, modules=['customers'])
    html = client.get('/customers').get_data(as_text=True)
    assert 'HQ 244' in html and 'Head office' in html
    html = client.get(f'/customers/{customer}/edit').get_data(as_text=True)
    assert f'value="{shared}"' in html
    if branch_name != 'Branch 2':
        assert 'PRIVATE 244' not in html


def test_owner_scan_is_shared_even_with_selected_branch(app):
    with app.test_request_context():
        session.update(user_id=owner_id(app), user_role='owner', branch_id=1,
                       active_branch_id=1, can_view_all_branches=True)
        waiting = vehicles.save_waiting_vehicle(scan_form('HQ SELECTED'))
        row = vehicles.get_waiting_vehicle(waiting)
        assert row['branch_id'] is None and row['shared_pool'] == 1


@pytest.mark.parametrize('mode', ['additional', 'replace_main'])
def test_roodepoort_claim_removes_shared_scan_everywhere_and_preserves_choice(app, mode):
    ids = branch_ids(app)
    target = make_pending(app, ids['Branch 3'])
    # Ordinary existing client: no pending-portal restrictions.
    with app.app_context():
        get_db().execute('DELETE FROM portal_registrations WHERE customer_id=?', (target,))
        get_db().commit()
        old = vehicles.create_vehicle(scan_form('OLD 244'), customer_id=target)
        vehicles.promote_main_vehicle(target, old)
    waiting = save(app, 'HQ CLAIM')
    with app.test_request_context():
        staff_scope(ids['Branch 3'])
        new = vehicles.allocate_waiting_vehicle(waiting, target, mode)
        assert vehicles.get_main_vehicle_id(target) == (old if mode == 'additional' else new)
        assert len(vehicles.list_vehicles(target)) == 2
        with pytest.raises(ValueError, match='no longer waiting'):
            vehicles.allocate_waiting_vehicle(waiting, target, mode)
    for branch in ids.values():
        with app.test_request_context():
            staff_scope(branch)
            assert vehicles.list_waiting_vehicles() == []
            assert vehicles.waiting_vehicle_groups() == []


def test_direct_allocation_hides_stale_queue_and_releases_capacity(app):
    ids = branch_ids(app)
    target = make_pending(app, ids['Branch 3'])
    waiting = save(app, 'STALE 244')
    save(app, 'HQ TWO')
    save(app, 'HQ THREE')
    with app.app_context():
        vehicle = vehicles.create_vehicle(scan_form('stale244'), customer_id=target)
        vehicles.promote_main_vehicle(target, vehicle)
        assert len(vehicles.list_waiting_vehicles()) == 2
        assert sum(g['count'] for g in vehicles.waiting_vehicle_groups()) == 2
    save(app, 'HQ FOUR')
    with app.app_context():
        assert vehicles.get_waiting_vehicle(waiting) is None
        assert len(vehicles.list_waiting_vehicles()) == 3
        assert vehicles.get_main_vehicle_id(target) == vehicle


def test_shared_vehicle_does_not_bypass_customer_branch_scope(app):
    ids = branch_ids(app)
    target = make_pending(app, ids['Branch 3'])
    waiting = save(app, 'HQ SCOPE')
    with app.test_request_context():
        staff_scope(ids['Branch 1'])
        with pytest.raises(ValueError, match='outside the branches'):
            vehicles.allocate_waiting_vehicle(waiting, target)
        assert vehicles.get_waiting_vehicle(waiting) is not None


def test_unknown_branchless_scan_not_shared_and_crafted_id_refused(app):
    ids = branch_ids(app)
    target = make_pending(app, ids['Branch 1'])
    waiting = save(app, 'UNKNOWN 244')
    with app.app_context():
        get_db().execute('UPDATE vehicle_waiting_list SET created_by_user_id=NULL WHERE id=?', (waiting,))
        get_db().commit()
    with app.test_request_context():
        staff_scope(ids['Branch 1'])
        assert vehicles.list_waiting_vehicles() == []
        with pytest.raises(ValueError, match='outside the branches'):
            vehicles.allocate_waiting_vehicle(waiting, target)


def test_shared_pool_has_three_slots_independently_of_branch_capacity(app):
    branch = branch_ids(app)['Branch 1']
    for i in range(3):
        save(app, f'HQ CAP {i}')
        save(app, f'LOCAL CAP {i}', branch)
    with pytest.raises(ValueError, match='full'):
        save(app, 'HQ FULL')
    with pytest.raises(ValueError, match='full'):
        save(app, 'LOCAL FULL', branch)
    with app.test_request_context():
        staff_scope(branch)
        assert sorted(g['count'] for g in vehicles.waiting_vehicle_groups()) == [3, 3]
