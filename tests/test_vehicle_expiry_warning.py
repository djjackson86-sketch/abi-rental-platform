import json
from app import create_app
from app.db import get_db
from app.services.customers import create_customer


def test_expired_warning_clears_on_renewal_and_transfer_requires_choice(tmp_path):
    app = create_app({'TESTING': True, 'DATABASE': str(tmp_path/'expiry.db')})
    with app.app_context():
        owner = get_db().execute("SELECT id FROM users WHERE role='owner'").fetchone()['id']
        first = create_customer({'name': 'Expiry QA'})
        second = create_customer({'name': 'Transfer QA'})
    client = app.test_client()
    with client.session_transaction() as s:
        s['user_id'] = owner
        s['user_role'] = 'owner'
    fields = {'customer_id': first, 'registration': 'QA123GP', 'make': 'TOYOTA',
              'vin': 'AHTFR22G10L123456', 'engine_number': '2GD1234567',
              'licence_number': 'T9876543210X', 'registration_number': 'ZZ1234Z',
              'licence_disk_expiry': '2000-01-01', 'make_main': 'yes'}
    saved = client.post('/scan-vehicle/save', data=fields, follow_redirects=True)
    assert b'Warning: the main vehicle licence disk expired' in saved.data
    fields['licence_disk_expiry'] = '2099-01-01'
    updated = client.post('/scan-vehicle/save', data=fields, follow_redirects=True)
    assert b'Warning: the main vehicle licence disk expired' not in updated.data
    assert b'value="2099-01-01"' in updated.data
    with app.app_context():
        assert get_db().execute('SELECT COUNT(*) FROM vehicles').fetchone()[0] == 1
        row = get_db().execute('SELECT custom_fields_json FROM customers WHERE id=?',(first,)).fetchone()
        assert json.loads(row[0])['vehicle_licence_disk_expiry'] == '2099-01-01'
    fields.update(customer_id=second, transfer='yes')
    fields.pop('make_main')
    denied = client.post('/scan-vehicle/save',data=fields)
    assert b'before transferring it' in denied.data
    with app.app_context():
        assert get_db().execute('SELECT customer_id FROM vehicles').fetchone()[0] == first
