import html
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from app import create_app
from app.db import get_db
from app.services.customers import create_customer
from app.services.portal_registrations import record_pending_registration


@pytest.mark.parametrize('page', ['edit', 'detail'])
def test_verification_checklist_links_preserve_customer_and_direct_scan(tmp_path, page):
    app = create_app({'TESTING': True, 'DATABASE': str(tmp_path / 'navigation.sqlite'), 'TURSO_DATABASE_URL': '', 'TURSO_AUTH_TOKEN': '', 'SECRET_KEY': 'navigation-test', 'ADMIN_PASSWORD': 'local-test-owner', 'TELEGRAM_NOTIFICATIONS_ENABLED': ''})
    with app.app_context():
        owner = get_db().execute("SELECT id FROM users WHERE role='owner'").fetchone()[0]
        cid = create_customer({'name': 'Synthetic Checklist', 'phone': '0820000011', 'email': 'checklist@example.test'}, branch_id=1, notify=False)
        record_pending_registration(cid, 1, 'TEST-NAV', name='Synthetic Checklist', phone='0820000011')
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=owner, user_role='owner', branch_id=1, can_view_all_branches=True, branch_ids=[])
    response = client.get(f'/customers/{cid}/edit' if page == 'edit' else f'/customers/{cid}')
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    links = [html.unescape(x) for x in re.findall(r'<a class="pending-check-link" href="([^"]+)"', text)]
    assert len(links) == 2
    assert links[0] == f'/customers/{cid}/edit#client-verified'
    vehicle = urlsplit(links[1])
    assert vehicle.path == '/scan-vehicle'
    assert parse_qs(vehicle.query) == {'customer_id': [str(cid)], 'vehicle_role': ['main'], 'start_scan': ['1'], 'return_to_verification': ['1']}
    assert 'Set ID verification' in text and 'Scan main vehicle →' in text
    assert ">Approve client</button>" not in text
    assert "pending-approve-pointer" in text
