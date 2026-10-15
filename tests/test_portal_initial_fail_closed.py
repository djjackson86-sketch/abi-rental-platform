import pytest
from app import create_app
from app.db import get_db
from app.services import customers, portal_registrations, portal_intake
from app.routes.orders import _customers

@pytest.fixture
def app(tmp_path):
    return create_app({'TESTING':True,'DATABASE':str(tmp_path/'initial.sqlite'),'TURSO_DATABASE_URL':'','TURSO_AUTH_TOKEN':'','SECRET_KEY':'test','ADMIN_PASSWORD':'local-test-owner','TELEGRAM_NOTIFICATIONS_ENABLED':''})


def test_initial_portal_insert_is_hidden_even_without_queue_row(app):
    with app.test_request_context('/'):
        cid=customers.create_customer({'name':'Initial Pending','email':'initial@example.test','phone':'0820000001'},notify=False,pending_portal=True)
        assert get_db().execute('SELECT source_system FROM customers WHERE id=?',(cid,)).fetchone()[0]=='portal_pending'
        assert portal_registrations.is_pending(cid)
        assert not customers.list_customers()
        assert not customers.search_customers('Initial')
        assert not _customers()
        assert get_db().execute('SELECT COUNT(*) FROM portal_registrations').fetchone()[0]==0


def test_success_drops_initial_marker_only_after_queue_exists(app):
    with app.test_request_context('/'):
        form={'first_names':'Complete','surname':'Portal','email':'complete@example.test','phone':'0820000002','address_line1':'1 Test Road','suburb':'Test Suburb','city':'Midrand','province':'Gauteng','postal_code':'1685','alternative_contact_name':'Test Contact','alternative_contact_number':'0830000002','alternative_contact_relationship':'Friend'}
        result=portal_intake.create_or_link_customer(form,1,'create',slug='test')
        cid=result['customer_id']
        assert get_db().execute('SELECT source_system FROM customers WHERE id=?',(cid,)).fetchone()[0]=='portal'
        assert portal_registrations.pending_registration_for_customer(cid)
        assert portal_registrations.is_pending(cid)
        assert not customers.list_customers()
