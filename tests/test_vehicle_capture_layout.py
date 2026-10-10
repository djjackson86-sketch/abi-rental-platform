import re
from html.parser import HTMLParser
from pathlib import Path

from app import create_app
from app.db import get_db
from app.services.customers import create_customer


class FormDepth(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.nested = False

    def handle_starttag(self, tag, attrs):
        if tag == 'form':
            self.nested |= self.depth > 0
            self.depth += 1

    def handle_endtag(self, tag):
        if tag == 'form':
            self.depth -= 1


def test_staff_main_fields_and_additional_vehicle_prompt(tmp_path):
    app = create_app({'TESTING': True, 'DATABASE': str(tmp_path / 'vehicle-layout.db')})
    with app.app_context():
        owner = get_db().execute("SELECT id FROM users WHERE role='owner'").fetchone()
        assert owner is not None
        customer_id = create_customer({'name': 'Vehicle Layout Test'})
    client = app.test_client()
    with client.session_transaction() as s:
        s['user_id'] = owner['id']
        s['user_role'] = 'owner'
    html = client.get(f'/customers/{customer_id}/edit').get_data(as_text=True)
    for key in ('vehicle_make', 'vehicle_reg_no', 'vehicle_registration_number',
                'vehicle_licence_number', 'vehicle_vin', 'vehicle_engine_number',
                'vehicle_licence_disk_expiry'):
        assert f'name="{key}"' in html
        assert html.index(f'name="{key}"') < html.index('name="alternative_contact_name"')
    assert 'Main client vehicle' in html
    assert 'Add another vehicle' in html
    parser = FormDepth()
    parser.feed(html)
    assert not parser.nested
    scan = client.post(f'/scan-vehicle?customer_id={customer_id}&vehicle_role=additional',
                       data={'action': 'manual', 'customer_id': str(customer_id)}).get_data(as_text=True)
    assert 'Is this the main client vehicle?' in scan
    assert 'name="make_main" value="yes" required' in scan
    assert 'name="make_main" value="no" required' in scan
    for name in ('model', 'colour', 'vehicle_type', 'registering_authority', 'control_number'):
        assert f'name="{name}"' not in scan


def test_both_scan_pages_share_the_forgiving_barcode_guide():
    root = Path(__file__).resolve().parents[1]
    css = (root / 'static/css/app.css').read_text(encoding='utf-8')
    # A forgiving alignment area, not the exact 153/13 silhouette: shorter
    # end-to-end (narrower) and roughly twice as tall, so a barcode is easier
    # to fit. The guide is a screen overlay only; it never crops the capture.
    assert 'aspect-ratio:6/1' in css
    assert 'aspect-ratio:153/13' not in css
    guide = re.search(r'\.scan-barcode-guide\{[^}]*\}', css).group(0)
    assert 'left:9%' in guide and 'width:82%' in guide
    # The guide + hint now live in the shared scanner partial, and the constraint
    # ladder in the shared module — both pages include/reference them.
    partial = (root / 'templates/admin' / '_scan_camera.html').read_text(encoding='utf-8')
    assert 'class="scan-barcode-guide"' in partial
    assert 'Fit the whole barcode inside the guide' in partial
    assert '153 mm × 13 mm' not in partial
    for name in ('scan_vehicle.html', 'scan_return.html'):
        template = (root / 'templates/admin' / name).read_text(encoding='utf-8')
        assert 'admin/_scan_camera.html' in template
    module = (root / 'static/js' / 'scan-camera.js').read_text(encoding='utf-8')
    assert 'width: {ideal: 1920}' in module
