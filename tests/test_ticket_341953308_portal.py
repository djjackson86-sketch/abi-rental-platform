"""Portal-only copy/spacing; local synthetic DB, no customer registration."""
import threading
from pathlib import Path
import os

import pytest
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from test_client_portal_frontend import app, branch, client, slug_of


@pytest.mark.parametrize('checked', [False, True])
def test_director_label_and_binding_on_validation(client, app, branch, checked):
    response = client.post(f'/portal/{slug_of(app, branch)}/register', data={
        'customer_type': 'company', 'contact_person_is_director': '1' if checked else '0',
    })
    assert response.status_code == 400
    soup = BeautifulSoup(response.data, 'html.parser')
    checkbox = soup.select_one('[name="contact_person_is_director"]')
    assert checkbox['type'] == 'checkbox' and checkbox['value'] == '1'
    assert checkbox.has_attr('checked') == checked
    assert not checkbox.has_attr('disabled')
    assert checkbox.find_parent('label').get_text(strip=True) == 'Tick if Director'


@pytest.mark.parametrize('width,height', [(1440, 1000), (390, 844), (320, 740)])
def test_browser_spacing_label_toggle_and_check(app, client, branch, width, height):
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    scratch = Path(os.environ['LOCALAPPDATA']) / 'hermes/cache/scratch' if os.name == 'nt' else Path(os.environ['TMPDIR'])
    shots = scratch / 'ABI-341953308'
    shots.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome')
            page = browser.new_page(viewport={'width': width, 'height': height})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            for path in ('/portal', f'/portal/{slug_of(app, branch)}'):
                page.goto(f'http://127.0.0.1:{server.server_port}{path}')
                phone = page.locator('#lookup-phone').bounding_box()
                button = page.locator('.portal-lookup button').bounding_box()
                assert button['y'] - (phone['y'] + phone['height']) >= 12
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('.portal-lookup').screenshot(path=str(shots / f'lookup-{width}.png'))
                page.locator('[name="customer_type"][value="company"]').check()
                checkbox = page.get_by_role('checkbox', name='Tick if Director', exact=True)
                assert checkbox.is_enabled() and not checkbox.is_checked()
                checkbox.check()
                assert not page.locator('[data-company-director]').is_visible()
                assert page.locator('.customer-form').evaluate('el => new FormData(el).get("contact_person_is_director")') == '1'
                checkbox.uncheck()
                assert page.locator('[data-company-director]').is_visible()
                page.locator('.portal-contact-person-row').screenshot(path=str(shots / f'director-{width}.png'))
                # Empty phone via direct POST: exercises the existing lookup without searching real people.
                response = page.request.post(f'http://127.0.0.1:{server.server_port}{path}/check', data={'name': 'Portal test', 'phone': ''})
                assert response.status == 200
                page.locator('#lookup-name').fill('Portal test')
                page.locator('#lookup-phone').fill('0000000000')
                page.locator('.portal-lookup button').click()
                page.wait_for_url('**/check')
                assert page.locator('body').inner_text()
                assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
