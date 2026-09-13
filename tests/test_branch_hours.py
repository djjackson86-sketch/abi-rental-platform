"""Per-branch trading hours (ticket ABI-341952941).

A depot branch can hold one opening/closing time per weekday in
``branch_operating_hours``. The hours are **informational only** - they are shown
on the Branches page and deliberately never enforced on availability, the
calendar or public bookings, so no existing workflow changes. A branch with no
saved rows falls back to the global ``operating_hours`` defaults for display
(weekends closed), which is why nothing had to be written for the existing
live branches.
"""
import os
import tempfile

import pytest

from app import create_app
from app.db import get_db, init_db
from app.services.branches import (
    branch_hours_summaries,
    default_hours,
    hours_saved,
    list_branch_hours,
    save_branch_hours,
)


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    application = create_app({
        'TESTING': True,
        'DATABASE': path,
        'SECRET_KEY': 'test',
        'ADMIN_EMAIL': 'admin@abi.local',
        'ADMIN_PASSWORD': 'admin123',
    })
    yield application
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client, name=None, password='admin123'):
    with client.application.app_context():
        if name is None:
            row = get_db().execute(
                "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
            ).fetchone()
        else:
            row = get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
        user_id = row['id'] if row else None
    return client.post('/login', data={'user_id': str(user_id), 'password': password}, follow_redirects=True)


def branch_id_by_name(app, name):
    with app.app_context():
        row = get_db().execute("SELECT id FROM branches WHERE name = ?", (name,)).fetchone()
        return row['id'] if row else None


def hours_payload(open_time='08:30', close_time='16:30', closed_days=(0, 6)):
    """A full week of submitted hours; the listed days are marked closed.

    Day numbering is Sunday-first (0 = Sunday), matching the app's global
    operating_hours table - so the default closed days are the weekend.
    """
    data = {}
    for day in range(7):
        data[f'open_time_{day}'] = open_time
        data[f'close_time_{day}'] = close_time
        if day in closed_days:
            data[f'closed_{day}'] = '1'
    return data


def test_branch_with_no_saved_hours_shows_the_default_week(app):
    branch_id = branch_id_by_name(app, 'Branch 1')
    with app.app_context():
        hours = list_branch_hours(branch_id)
        assert len(hours) == 7
        assert [row['day_label'] for row in hours] == [
            'Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday',
        ]
        assert all(row['open_time'] == '09:00' and row['close_time'] == '17:00' for row in hours)
        # Weekends closed is the global seed; nothing has been saved for this branch.
        assert [row['closed'] for row in hours] == [1, 0, 0, 0, 0, 0, 1]
        assert all(row['saved'] == 0 for row in hours)
        assert hours_saved(branch_id) is False
        assert get_db().execute(
            "SELECT COUNT(*) AS c FROM branch_operating_hours WHERE branch_id = ?", (branch_id,)
        ).fetchone()['c'] == 0


def test_editing_a_branch_shows_the_trading_hours_section(client, app):
    login(client)
    branch_id = branch_id_by_name(app, 'Branch 1')

    page = client.get(f'/branches?edit={branch_id}')
    assert page.status_code == 200
    body = page.data.decode()
    assert 'Trading hours' in body
    assert 'Save trading hours' in body
    for day in ('Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'):
        assert day in body
    for day in range(7):
        assert f'name="open_time_{day}"' in body
        assert f'name="close_time_{day}"' in body
        assert f'name="closed_{day}"' in body
    assert f'action="/branches/{branch_id}/hours"' in body

    # Without ?edit= there is no branch to save hours for, so no hours form.
    plain = client.get('/branches')
    assert plain.status_code == 200
    assert 'Save trading hours' not in plain.data.decode()
    assert 'Use Edit on a branch to set its trading hours' in plain.data.decode()


def test_saving_hours_persists_and_is_isolated_per_branch(client, app):
    login(client)
    first = branch_id_by_name(app, 'Branch 1')
    second = branch_id_by_name(app, 'Branch 2')

    res = client.post(f'/branches/{first}/hours', data=hours_payload('08:30', '16:30'), follow_redirects=True)
    assert res.status_code == 200
    assert b'Trading hours saved' in res.data

    with app.app_context():
        saved = list_branch_hours(first)
        assert [row['open_time'] for row in saved] == ['08:30'] * 7
        assert [row['close_time'] for row in saved] == ['16:30'] * 7
        assert [row['closed'] for row in saved] == [1, 0, 0, 0, 0, 0, 1]
        assert all(row['saved'] == 1 for row in saved)
        assert hours_saved(first) is True
        assert get_db().execute(
            "SELECT COUNT(*) AS c FROM branch_operating_hours WHERE branch_id = ?", (first,)
        ).fetchone()['c'] == 7

        # The other branch is untouched and still shows the defaults.
        assert hours_saved(second) is False
        assert all(row['saved'] == 0 for row in list_branch_hours(second))
        assert get_db().execute(
            "SELECT COUNT(*) AS c FROM branch_operating_hours WHERE branch_id = ?", (second,)
        ).fetchone()['c'] == 0


def test_saving_hours_again_updates_instead_of_duplicating(client, app):
    login(client)
    branch_id = branch_id_by_name(app, 'Branch 1')

    client.post(f'/branches/{branch_id}/hours', data=hours_payload('08:00', '16:00'), follow_redirects=True)
    client.post(f'/branches/{branch_id}/hours', data=hours_payload('07:15', '15:45', closed_days=(6,)), follow_redirects=True)

    with app.app_context():
        assert get_db().execute(
            "SELECT COUNT(*) AS c FROM branch_operating_hours WHERE branch_id = ?", (branch_id,)
        ).fetchone()['c'] == 7
        hours = list_branch_hours(branch_id)
        assert [row['open_time'] for row in hours] == ['07:15'] * 7
        assert [row['close_time'] for row in hours] == ['15:45'] * 7
        assert [row['closed'] for row in hours] == [0, 0, 0, 0, 0, 0, 1]


def test_saving_hours_with_nothing_closed_opens_all_seven_days(client, app):
    login(client)
    branch_id = branch_id_by_name(app, 'Branch 1')

    client.post(f'/branches/{branch_id}/hours', data=hours_payload('09:00', '17:00', closed_days=()), follow_redirects=True)

    with app.app_context():
        assert [row['closed'] for row in list_branch_hours(branch_id)] == [0] * 7


def test_malformed_time_is_rejected_and_nothing_is_written(client, app):
    login(client)
    branch_id = branch_id_by_name(app, 'Branch 1')
    payload = hours_payload()
    payload['open_time_2'] = 'not-a-time'

    res = client.post(f'/branches/{branch_id}/hours', data=payload, follow_redirects=True)
    assert res.status_code == 200
    assert b'Trading hours must be entered as HH:MM' in res.data
    with app.app_context():
        assert hours_saved(branch_id) is False


def test_closing_before_opening_is_rejected(client, app):
    login(client)
    branch_id = branch_id_by_name(app, 'Branch 1')
    payload = hours_payload()
    payload['open_time_1'] = '17:00'
    payload['close_time_1'] = '09:00'

    res = client.post(f'/branches/{branch_id}/hours', data=payload, follow_redirects=True)
    assert res.status_code == 200
    assert b'Monday: closing time must be after opening time' in res.data
    with app.app_context():
        assert hours_saved(branch_id) is False

    # A day that is marked Closed ignores its times, so reversed times are fine there.
    closed_payload = hours_payload()
    closed_payload['open_time_0'] = '17:00'
    closed_payload['close_time_0'] = '09:00'
    closed_payload['closed_0'] = '1'
    ok = client.post(f'/branches/{branch_id}/hours', data=closed_payload, follow_redirects=True)
    assert b'Trading hours saved' in ok.data
    with app.app_context():
        sunday = list_branch_hours(branch_id)[0]
        assert sunday['closed'] == 1


def test_blank_times_fall_back_to_the_defaults(client, app):
    login(client)
    branch_id = branch_id_by_name(app, 'Branch 1')
    payload = hours_payload()
    payload['open_time_3'] = ''
    payload['close_time_3'] = ''

    client.post(f'/branches/{branch_id}/hours', data=payload, follow_redirects=True)

    with app.app_context():
        wednesday = list_branch_hours(branch_id)[3]
        assert wednesday['open_time'] == '09:00'
        assert wednesday['close_time'] == '17:00'
        assert wednesday['saved'] == 1


def test_hours_for_an_unknown_branch_are_refused(client, app):
    login(client)
    res = client.post('/branches/9999/hours', data=hours_payload(), follow_redirects=True)
    assert res.status_code == 200
    assert b'Branch not found' in res.data
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) AS c FROM branch_operating_hours").fetchone()['c'] == 0


def test_new_branch_starts_with_its_own_default_week(client, app):
    login(client)
    client.post('/branches', data={'name': 'Kyalami Depot', 'code': 'KYA', 'active': '1'}, follow_redirects=True)
    new_id = branch_id_by_name(app, 'Kyalami Depot')
    assert new_id is not None

    with app.app_context():
        hours = list_branch_hours(new_id)
        assert len(hours) == 7
        assert all(row['saved'] == 1 for row in hours)
        assert [row['closed'] for row in hours] == [1, 0, 0, 0, 0, 0, 1]
        assert get_db().execute(
            "SELECT COUNT(*) AS c FROM branch_operating_hours WHERE branch_id = ?", (new_id,)
        ).fetchone()['c'] == 7
        # Existing branches keep showing the (unsaved) defaults.
        first = branch_id_by_name(app, 'Branch 1')
        assert hours_saved(first) is False


def test_deleting_a_branch_removes_its_trading_hours(client, app):
    login(client)
    branch_id = branch_id_by_name(app, 'Branch 2')
    client.post(f'/branches/{branch_id}/hours', data=hours_payload(), follow_redirects=True)

    with app.app_context():
        assert get_db().execute(
            "SELECT COUNT(*) AS c FROM branch_operating_hours WHERE branch_id = ?", (branch_id,)
        ).fetchone()['c'] == 7

    client.post(f'/branches/{branch_id}/delete', follow_redirects=True)

    with app.app_context():
        assert get_db().execute(
            "SELECT COUNT(*) AS c FROM branch_operating_hours WHERE branch_id = ?", (branch_id,)
        ).fetchone()['c'] == 0


def test_branches_table_summarises_the_saved_hours(client, app):
    login(client)
    branch_id = branch_id_by_name(app, 'Branch 1')
    client.post(f'/branches/{branch_id}/hours', data=hours_payload('09:00', '17:00'), follow_redirects=True)

    page = client.get('/branches')
    body = page.data.decode()
    assert 'Sun Closed, Mon-Fri 09:00-17:00, Sat Closed' in body

    with app.app_context():
        summaries = branch_hours_summaries()
        assert summaries[branch_id] == 'Sun Closed, Mon-Fri 09:00-17:00, Sat Closed'


def test_trading_hours_are_informational_and_never_block_a_booking(client, app):
    """A branch closed every day still books: hours are display-only."""
    login(client)
    branch_id = branch_id_by_name(app, 'Branch 1')
    client.post('/inventory/new', data={
        'name': 'Closed Branch Trailer', 'sku': 'CBT-1', 'quantity': '3',
        'description': 'Informational hours test.', 'product_type': 'rental',
        'price_amount': '200', 'price_unit': 'day', 'security_deposit': '0',
        'tax_profile_id': '1', 'active': '1', 'public_visible': '1',
    }, follow_redirects=True)

    closed_week = hours_payload(closed_days=tuple(range(7)))
    res = client.post(f'/branches/{branch_id}/hours', data=closed_week, follow_redirects=True)
    assert b'Trading hours saved' in res.data

    with app.app_context():
        assert [row['closed'] for row in list_branch_hours(branch_id)] == [1] * 7
        product_id = get_db().execute("SELECT id FROM products WHERE sku = 'CBT-1'").fetchone()['id']

    order = client.post('/orders/new', data={
        'customer_type': 'individual',
        'name': 'Closed Branch Client',
        'email': 'closed@example.test',
        'product_id': str(product_id),
        'quantity': '1',
        'start_date': '2026-08-03',
        'start_time': '09:00',
        'end_date': '2026-08-04',
        'end_time': '09:00',
        'collect_branch_id': str(branch_id),
        'return_branch_id': str(branch_id),
        'deposit_option': 'no_deposit',
    }, follow_redirects=True)
    assert order.status_code == 200
    assert b'Draft order created' in order.data

    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) AS c FROM orders").fetchone()['c'] == 1


def test_default_hours_helper_reads_the_global_settings(app):
    with app.app_context():
        from app.services.settings import list_operating_hours

        global_hours = {int(row['day_of_week']): row for row in list_operating_hours()}
        defaults = default_hours()
        assert len(defaults) == 7
        for row in defaults:
            source = global_hours[row['day_of_week']]
            assert row['open_time'] == source['open_time']
            assert row['close_time'] == source['close_time']
            assert row['closed'] == (1 if source['closed'] else 0)


def test_hours_save_helper_validates_times_without_a_request(app):
    with app.app_context():
        init_db()
        branch_id = branch_id_by_name(app, 'Branch 1')
        with pytest.raises(ValueError):
            save_branch_hours(branch_id, {'open_time_0': '25:00', 'close_time_0': '17:00'})
        with pytest.raises(ValueError):
            save_branch_hours(branch_id, {'open_time_0': '09:00', 'close_time_0': '09:00'})
        assert hours_saved(branch_id) is False
