"""Private optional multiline reason on the staff Suspect/Fraud capture dialog."""
import json
import os
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from test_ticket_341953268_matching import (
    app, client, sign_in, created_id, post_suspect_form, raw_fields, make_customer,
    customer_row, staff_request, owner_id,
)
from app.services.customers import (
    get_customer, custom_fields_for, customer_summary_for, customer_form_values_for,
    update_customer, create_customer, customer_suspect_reason_for,
)

REASON = 'Check documents before hiring.\n<script>alert("private")</script>'


def test_reason_roundtrip_escape_and_customer_association(app, client):
    sign_in(client)
    flagged = created_id(post_suspect_form(client, suspect_reason='  ' + REASON + '  '))
    other = make_customer(app, 'Unrelated Person')
    assert raw_fields(app, flagged)['suspect_reason'] == REASON
    assert 'suspect_reason' not in raw_fields(app, other)
    body = client.get(f'/customers/{flagged}').get_data(as_text=True)
    assert 'Check documents before hiring.\n&lt;script&gt;' in body
    assert '<script>alert("private")</script>' not in body
    edit = client.get(f'/customers/{flagged}/edit').get_data(as_text=True)
    assert 'Check documents before hiring.\n&lt;script&gt;' in edit
    assert 'Suspect/Fraud reason' not in client.get(f'/customers/{other}').get_data(as_text=True)
    assert customer_row(app, flagged)['is_blocked'] == 1


def test_reason_optional_clear_and_ordinary_save_preserves(app, client):
    sign_in(client)
    flagged = created_id(post_suspect_form(client, suspect_reason='Original note'))
    with staff_request(app, owner_id(app)):
        update_customer(flagged, {'name': 'Suspect Client'})
    assert raw_fields(app, flagged)['suspect_reason'] == 'Original note'
    data = dict(name='Suspect Client', blocking_panel='1', action='save_suspect', suspect_fraud='1')
    assert client.post(f'/customers/{flagged}/edit', data=data).status_code == 302
    assert raw_fields(app, flagged)['suspect_reason'] == 'Original note'
    data['suspect_reason'] = '   '
    assert client.post(f'/customers/{flagged}/edit', data=data).status_code == 302
    assert raw_fields(app, flagged)['suspect_reason'] == ''
    empty = created_id(post_suspect_form(client, name='Optional', email='optional@example.test', suspect_reason=''))
    assert raw_fields(app, empty)['suspect_reason'] == ''


@pytest.mark.parametrize('reason', ['x' * 2001, 'bad\x00note', 'bad\x01note'])
def test_server_validation_is_atomic(app, client, reason):
    sign_in(client)
    flagged = created_id(post_suspect_form(client, suspect_reason='Keep me'))
    response = client.post(f'/customers/{flagged}/edit', data={
        'name': 'Suspect Client', 'blocking_panel': '1', 'action': 'save_suspect',
        'suspect_fraud': '1', 'suspect_reason': reason,
    })
    assert response.status_code == 200
    assert 'Suspect/Fraud reason' in response.get_data(as_text=True)
    assert raw_fields(app, flagged)['suspect_reason'] == 'Keep me'


def test_limit_and_windows_newlines(app, client):
    sign_in(client)
    flagged = created_id(post_suspect_form(client, suspect_reason='x' * 2000))
    assert len(raw_fields(app, flagged)['suspect_reason']) == 2000
    another = created_id(post_suspect_form(client, name='Line endings', suspect_reason='One\r\nTwo\rThree\tOK'))
    assert raw_fields(app, another)['suspect_reason'] == 'One\nTwo\nThree\tOK'


def test_reason_private_and_untrusted_callers_cannot_set_it(app, client):
    sign_in(client)
    flagged = created_id(post_suspect_form(client, suspect_reason=REASON))
    assert REASON not in client.get('/customers/export.csv').get_data(as_text=True)
    assert 'suspect_reason' not in client.get('/customers/export.csv').get_data(as_text=True)
    assert REASON not in client.get('/store').get_data(as_text=True)
    payload = client.post('/customers/suspect-check', data={'email': 'suspect@example.test'}).json
    assert 'suspect_reason' not in json.dumps(payload)
    with app.app_context():
        row = get_customer(flagged)
        assert 'suspect_reason' not in custom_fields_for(row)
        assert 'suspect_reason' not in customer_form_values_for(row)
        assert customer_suspect_reason_for(row) == REASON
        with app.test_request_context('/store'):
            assert REASON not in json.dumps(customer_summary_for(row))
            forged = create_customer({'name': 'Public forgery', 'suspect_reason': REASON,
                                     'blocking_panel': '1', 'action': 'save_suspect', 'suspect_fraud': '1'})
        assert 'suspect_reason' not in raw_fields(app, forged)
    with client.session_transaction() as session:
        session.clear()
    assert client.get(f'/customers/{flagged}').status_code == 302


@pytest.mark.parametrize('width,height', [(1440, 1000), (390, 844)])
def test_real_browser_capture_cancel_save_reopen(app, client, width, height):
    sign_in(client)
    cookie = client.get_cookie('session')
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    output = Path(os.environ['TMPDIR']) / 'ABI-341953270'
    output.mkdir(exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True)
            context = browser.new_context(viewport={'width': width, 'height': height})
            context.add_cookies([{'name': 'session', 'value': cookie.value, 'domain': '127.0.0.1', 'path': '/'}])
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base + '/customers/new')
            page.locator('[name=name]').fill('Reason Workflow Test')
            page.locator('#save-suspect').click()
            dialog = page.locator('#suspect-capture')
            expect(dialog).to_be_visible()
            assert page.locator('#suspect-capture-reason').bounding_box()['y'] > page.locator('#suspect-capture-id').bounding_box()['y']
            page.locator('#suspect-capture-id').fill('8001015000081')
            page.locator('#suspect-capture-reason').fill(REASON)
            page.screenshot(path=str(output / f'capture-{width}.png'))
            box = dialog.bounding_box()
            assert box['x'] >= 0 and box['x'] + box['width'] <= width
            assert page.evaluate('document.querySelector("#suspect-capture").scrollWidth <= document.querySelector("#suspect-capture").clientWidth')
            page.locator('#suspect-capture-cancel').click()
            expect(dialog).not_to_be_visible()
            assert page.url.endswith('/customers/new')
            assert page.locator('#suspect-action').input_value() == ''
            page.locator('#save-suspect').click()
            page.keyboard.press('Escape')
            expect(dialog).not_to_be_visible()
            page.locator('#save-suspect').click()
            page.locator('#suspect-capture-form button[type=submit]').click()
            page.wait_for_url(lambda url: url.rsplit('/', 1)[-1].isdigit())
            flagged = int(page.url.rsplit('/', 1)[1])
            assert raw_fields(app, flagged)['suspect_reason'] == REASON
            expect(page.locator('.customer-info-card')).to_contain_text(REASON)
            page.goto(base + f'/customers/{flagged}/edit')
            page.locator('#save-suspect').click()
            expect(page.locator('#suspect-capture-id')).to_have_value('8001015000081')
            expect(page.locator('#suspect-capture-reason')).to_have_value(REASON)
            page.screenshot(path=str(output / f'reopen-{width}.png'))
            page.locator('#suspect-capture-reason').fill('Updated\nSecond line')
            page.locator('#suspect-capture-form button[type=submit]').click()
            page.wait_for_url(base + f'/customers/{flagged}')
            assert raw_fields(app, flagged)['suspect_reason'] == 'Updated\nSecond line'
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
