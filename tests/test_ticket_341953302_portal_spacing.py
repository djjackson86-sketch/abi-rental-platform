"""Presentation-only portal regression checks; synthetic DB, no live submissions."""
import os
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from test_client_portal_frontend import app, client, branch  # noqa: F401
from test_ticket_341953300_portal_requirements_numbering import add_branch, serve, stop


@pytest.mark.parametrize('width,height', [(1440, 1000), (390, 844), (320, 740)])
def test_portal_contact_spacing_alignment_and_requirements(app, client, width, height):
    add_branch(app, 'midrand')
    server, thread = serve(app)
    shots = Path(os.environ['LOCALAPPDATA']) / 'hermes/cache/scratch/ABI-341953302' if os.name == 'nt' else Path(os.environ['TMPDIR']) / 'ABI-341953302'
    shots.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome')
            page = browser.new_page(viewport={'width': width, 'height': height})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/portal/midrand')
            for customer_type in ('individual', 'company', 'individual', 'company'):
                page.locator(f'[name="customer_type"][value="{customer_type}"]').check()
                phone = page.locator('.customer-form [name="phone"]').bounding_box()
                whatsapp = page.locator('.portal-whatsapp').bounding_box()
                assert phone is not None and whatsapp is not None
                assert whatsapp['y'] - (phone['y'] + phone['height']) >= 16
                assert page.locator('[data-driver-requirements]').evaluate('el => el.start') == 1
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                box = page.locator('[name="contact_person_is_director"]')
                assert box.is_visible() == (customer_type == 'company')
                if customer_type == 'individual':
                    assert not page.locator('[data-business-requirements]').is_visible()
                    continue
                contact = page.locator('[name="contact_person_name"]').bounding_box()
                switch = page.locator('.portal-contact-person-row .switch').bounding_box()
                checkbox = box.bounding_box()
                assert contact is not None and switch is not None and checkbox is not None
                assert abs((checkbox['y'] + checkbox['height']/2) - (switch['y'] + switch['height']/2)) < 2
                if width > 520:
                    assert abs((switch['y'] + switch['height']/2) - (contact['y'] + contact['height']/2)) < 2
                    assert switch['x'] >= contact['x'] + contact['width']
                else:
                    assert switch['y'] >= contact['y'] + contact['height']
                email = page.locator('.portal-requirement-email')
                assert email.inner_text() == 'To be email at mid@sanotrailers.co.za'
                assert email.evaluate('el => [getComputedStyle(el).marginTop,getComputedStyle(el).marginBottom]') == ['12px', '12px']
                intro = page.locator('[data-business-requirements] em')
                assert intro.inner_text() == 'The driver must bring the following:'
                assert intro.evaluate('el => getComputedStyle(el).fontStyle') == 'italic'
            page.locator('.portal-contact-person-row').screenshot(path=str(shots / f'contact-{width}.png'))
            page.locator('.portal-requirements').screenshot(path=str(shots / f'requirements-{width}.png'))
            box.check()
            assert not page.locator('[data-company-director]').is_visible()
            box.uncheck()
            assert page.locator('[data-company-director]').is_visible()
            assert not errors
            browser.close()
    finally:
        stop(server, thread)
