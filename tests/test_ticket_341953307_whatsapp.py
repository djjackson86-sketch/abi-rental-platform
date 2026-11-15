"""Portal WhatsApp checkbox: request parsing, persistence, redisplay and real Chrome."""
import os
import threading
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from app.services import portal_intake
from test_client_portal_frontend import app, branch, client, slug_of
from test_ticket_341953299_portal import (
    INDIVIDUAL, COMPANY, register, stored_customer, custom_details,
)


@pytest.mark.parametrize('payload', [INDIVIDUAL, COMPANY])
@pytest.mark.parametrize('same', [False, True])
def test_persists_selected_number(client, app, branch, payload, same):
    data = {**payload, 'whatsapp_same_as_phone': '1' if same else '0'}
    parsed = portal_intake.submission_values(data)
    expected = data['phone'] if same else data['whatsapp_number']
    assert parsed['whatsapp_number'] == expected
    assert portal_intake.submission_values(parsed)['whatsapp_number'] == expected
    assert register(client, app, branch, data).status_code == 200
    assert custom_details(stored_customer(app, data['email']))['whatsapp_number'] == expected


def test_optional_blank_stays_blank(client, app, branch):
    data = {**INDIVIDUAL, 'whatsapp_number': ''}
    assert register(client, app, branch, data).status_code == 200
    assert not custom_details(stored_customer(app, data['email'])).get('whatsapp_number')


@pytest.mark.parametrize('missing', ['first_names', 'phone', 'popia_consent'])
@pytest.mark.parametrize('same', [False, True])
def test_validation_redisplay(client, app, branch, missing, same):
    data = {**INDIVIDUAL, 'whatsapp_same_as_phone': '1' if same else '0', missing: ''}
    response = register(client, app, branch, data)
    assert response.status_code == 400
    soup = BeautifulSoup(response.get_data(as_text=True), 'html.parser')
    assert soup.select_one('[name="whatsapp_same_as_phone"]').has_attr('checked') == same
    assert soup.select_one('#portal-whatsapp').has_attr('hidden') == same
    assert soup.select_one('[name="whatsapp_number"]').has_attr('disabled') == same
    assert '(if different from the above)' not in response.get_data(as_text=True)
    assert stored_customer(app, data['email']) is None


@pytest.mark.parametrize('width,height', [(1440, 1000), (390, 844)])
@pytest.mark.parametrize('company', [False, True])
def test_browser_toggle_current_phone_and_submit(app, client, branch, width, height, company):
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome')
            page = browser.new_page(viewport={'width': width, 'height': height})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/portal/{slug_of(app, branch)}')
            form = page.locator('.customer-form')
            same = form.locator('[name="whatsapp_same_as_phone"]')
            phone = form.locator('[name="phone"]')
            whatsapp = form.locator('[name="whatsapp_number"]')
            assert not same.is_checked() and whatsapp.is_visible()
            whatsapp.fill('082 999 0000')
            same.check()
            assert not whatsapp.is_visible() and whatsapp.is_disabled()
            assert 'whatsapp_number' not in form.evaluate('el => Object.fromEntries(new FormData(el))')
            phone.fill('083 555 4321')
            same.uncheck()
            assert whatsapp.is_visible() and whatsapp.input_value() == '082 999 0000'
            same.check()
            payload = COMPANY if company else INDIVIDUAL
            if company:
                form.locator('[name="customer_type"][value="company"]').check()
                assert not whatsapp.is_visible()
            for key, value in payload.items():
                if key in ('customer_type', 'whatsapp_number', 'phone', 'popia_consent', 'own_use_declaration'):
                    continue
                field = form.locator(f'[name="{key}"]')
                if key == 'province':
                    field.select_option(value)
                else:
                    field.fill(value)
            form.locator('[name="popia_consent"]').check()
            form.locator('[name="own_use_declaration"]').check()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            shots = Path(os.environ['LOCALAPPDATA']) / 'hermes/cache/scratch/ABI-341953307'
            shots.mkdir(parents=True, exist_ok=True)
            phone.scroll_into_view_if_needed()
            page.screenshot(path=str(shots / f'contact-{width}-{company}.png'))
            form.locator('button[type="submit"]').click()
            page.wait_for_load_state('networkidle')
            row = stored_customer(app, payload['email'])
            assert row is not None
            assert custom_details(row)['whatsapp_number'] == '083 555 4321'
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
