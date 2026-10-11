import hashlib
import pytest
from app.db import get_db
from app.services import portal_intake
from test_portal_duplicate_submission import app, client, branch, submission, register_url


@pytest.mark.parametrize('kind,value', [('phone', '0835551234'), ('email', 'pieter@example.co.za')])
def test_contact_lock_blocks_same_identifier_across_branches(app, client, branch, kind, value):
    data = submission(request_key='different-form-key-global-contact-123456')
    # Match the fixture identifier but represent a first request from another portal branch.
    if kind == 'email':
        data['email'] = value
        data['phone'] = '0829991111'
    else:
        data['phone'] = '+27 83 555 1234'
        data['email'] = 'other-new@example.test'
    lock_key = 'contact:' + kind + ':' + hashlib.sha256(value.encode()).hexdigest()
    with app.app_context():
        assert portal_intake.claim_submission('@portal-contact-locks', lock_key)[0] == portal_intake.SUBMISSION_CLAIMED
        get_db().commit()
    response = client.post(register_url(app, branch), data=data)
    assert response.status_code == 409
    assert portal_intake.DUPLICATE_MESSAGE in response.get_data(as_text=True)
    with app.app_context():
        assert get_db().execute('SELECT COUNT(*) FROM customers').fetchone()[0] == 0
        # Never release the competing request's lock.
        assert get_db().execute('SELECT COUNT(*) FROM portal_submission_claims WHERE scope=? AND request_key=?', ('@portal-contact-locks',lock_key)).fetchone()[0] == 1
