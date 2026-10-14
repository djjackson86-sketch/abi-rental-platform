"""Blocked customers stay visible without approving portal registrations."""
import pytest
from bs4 import BeautifulSoup

from app.db import get_db
from app.services import portal_registrations
from app.services.customers import (
    create_customer, customer_counts, customer_filter_counts, customer_filtered_total,
    find_customer_id_by_text, get_customer, list_customers, search_customers,
)
from test_client_portal_pending_registrations import (
    app, branches, client, register, customer_by_email, registration_for, sign_in,
)


@pytest.fixture()
def directory(client, app, branches):
    response = register(client, app, branches['midrand'], name='Blocked Portal', email='blocked-portal@example.test')
    assert response.status_code == 200
    portal = customer_by_email(app, 'blocked-portal@example.test')
    registration = registration_for(app, portal['id'])
    sign_in(client, app)
    client.get(f"/customers/{portal['id']}/edit")
    with client.session_transaction() as session:
        token = session['_csrf_tokens']['portal_pending_delete']
    response = client.post(f"/customers/portal/{registration['id']}/delete", data={
        'csrf_token': token, 'reason': 'Verification refused',
    })
    assert response.status_code == 302
    with app.app_context():
        staff = create_customer({'name': 'Blocked Staff', 'customer_type': 'company',
                                 'blocking_panel': '1', 'is_blocked': '1', 'blocked_reason': 'Unpaid'})
        ordinary = create_customer({'name': 'Unverified Ordinary', 'customer_type': 'individual'})
        db = get_db()
        db.execute('UPDATE customers SET marketing_opt_in = 1 WHERE id = ?', (staff,))
        db.commit()
    response = register(client, app, branches['midrand'], name='Awaiting Review',
                        email='awaiting@example.test', phone='082 666 7890')
    assert response.status_code == 200
    return portal['id'], staff, ordinary


def test_directory_shows_both_blocked_sources_only_with_label(client, app, directory):
    portal, staff, ordinary = directory
    page = client.get('/customers')
    assert page.status_code == 200
    soup = BeautifulSoup(page.data, 'html.parser')
    for customer_id in (portal, staff):
        name = soup.select_one(f'.customer-name-cell[href="/customers/{customer_id}"]')
        assert name is not None
        label = name.select_one('.customer-blocked-label')
        assert label.get_text(strip=True) == 'Blocked'
        assert label.find_previous_sibling().name == 'strong'
    normal = soup.select_one(f'.customer-name-cell[href="/customers/{ordinary}"]')
    assert normal is not None and normal.select_one('.blocked') is None
    assert len(soup.select('.customers-table .customer-blocked-label')) == 2
    assert 'Awaiting Review' not in soup.select_one('.customers-table').get_text()


@pytest.mark.parametrize('filters,expected', [
    ({}, 3), ({'query': 'Blocked'}, 2), ({'query': 'Blocked Portal'}, 1),
    ({'customer_type': 'company'}, 1), ({'marketing': 'subscribed'}, 1),
    ({'marketing': 'not_subscribed'}, 2),
    ({'query': 'Blocked Portal', 'customer_type': 'company'}, 0),
])
def test_directory_filters_totals_and_pages_agree(app, directory, filters, expected):
    with app.app_context():
        rows = list_customers(**filters)
        assert len(rows) == customer_filtered_total(**filters) == expected
        pages = [row['id'] for offset in range(expected)
                 for row in list_customers(**filters, limit=1, offset=offset)]
        assert pages == [row['id'] for row in rows]
        assert len(pages) == len(set(pages))
        assert list_customers(**filters, limit=1, offset=expected) == []
        assert customer_counts() == {'total': 3, 'individuals': 2, 'companies': 1,
                                     'subscribed': 1, 'not_subscribed': 2}
        assert customer_filter_counts()['customer_type'] == {'individual': 2, 'company': 1}


def test_visibility_does_not_approve_or_change_booking_selectors(client, app, directory):
    portal, staff, ordinary = directory
    with app.app_context():
        assert get_customer(portal)['is_blocked'] == 1
        assert registration_for(app, portal)['status'] == 'pending'
        assert portal_registrations.is_pending(portal)
        assert search_customers('Blocked Portal') == []
        assert find_customer_id_by_text('Blocked Portal') is None
        # An unblock must return this registration to verification, not approve it.
        db = get_db()
        db.execute('UPDATE customers SET is_blocked = 0 WHERE id = ?', (portal,))
        db.commit()
        assert portal not in [c['id'] for c in list_customers()]
        assert portal_registrations.is_pending(portal)
        assert any(r['customer_id'] == portal for r in portal_registrations.list_pending_registrations())


def test_deleted_registration_stays_hidden_even_if_blocked(app, directory):
    portal, staff, ordinary = directory
    with app.app_context():
        db = get_db()
        db.execute("UPDATE portal_registrations SET status = 'deleted' WHERE customer_id = ?", (portal,))
        db.commit()
        assert portal not in [c['id'] for c in list_customers()]
        assert customer_filtered_total() == customer_counts()['total'] == 2
