"""Customer province dropdowns preserve submitted and legacy data."""
import importlib.util
from pathlib import Path
from html.parser import HTMLParser

import pytest
from flask import render_template
from app.services.customers import create_customer, get_customer

spec = importlib.util.spec_from_file_location('province_helpers', Path(__file__).with_name('test_ticket_341953028_customer_blocking.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
app = helpers.app
client = helpers.client
PROVINCES = ['Eastern Cape', 'Free State', 'Gauteng', 'KwaZulu-Natal', 'Limpopo', 'Mpumalanga', 'North West', 'Northern Cape', 'Western Cape']


class ProvinceParser(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.fields = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'select' and attrs.get('name') == 'province':
            self.current = []
            self.fields.append(self.current)
        elif tag == 'option' and self.current is not None:
            self.current.append((attrs.get('value'), 'selected' in attrs))
        assert not (tag == 'input' and attrs.get('name') == 'province')

    def handle_endtag(self, tag):
        if tag == 'select':
            self.current = None


def check(html, selected, index=0):
    fields = ProvinceParser(html).fields
    options = fields[index]
    assert all(p in [v for v, _ in options] for p in PROVINCES)
    assert [v for v, chosen in options if chosen] == [selected]
    return fields


@pytest.mark.parametrize('url', ['/customers/new', '/orders/new', '/portal'])
def test_new_defaults(client, url, monkeypatch):
    from app.services import portal_intake
    monkeypatch.setattr(portal_intake, 'registration_is_open', lambda: True)
    helpers.login(client)
    response = client.get(url)
    assert response.status_code == 200
    check(response.get_data(as_text=True), 'Gauteng')


@pytest.mark.parametrize('province', PROVINCES + ['', 'Transvaal', '<legacy>'])
def test_saved_province_survives_edit_and_order_picker(client, app, province):
    helpers.login(client)
    with app.app_context():
        customer_id = create_customer({'name': 'Province Fixture', 'province': province})
    check(client.get(f'/customers/{customer_id}/edit').get_data(as_text=True), province)
    response = client.get(f'/orders/new?customer_id={customer_id}')
    assert response.status_code == 200
    check(response.get_data(as_text=True), province)


@pytest.mark.parametrize('province', PROVINCES)
def test_save_reload(client, app, province):
    helpers.login(client)
    response = client.post('/customers/new', data={'name': 'Province Fixture', 'province': province})
    assert response.status_code == 302
    customer_id = int(response.location.rsplit('/', 1)[-1])
    check(client.get(f'/customers/{customer_id}/edit').get_data(as_text=True), province)
    response = client.post(f'/customers/{customer_id}/edit', data={'name': 'Province Fixture', 'province': 'Limpopo'})
    assert response.status_code == 302
    with app.app_context():
        assert get_customer(customer_id)['province'] == 'Limpopo'


@pytest.mark.parametrize('editing', [False, True])
def test_validation_retains_selection(client, app, editing):
    helpers.login(client)
    url = '/customers/new'
    if editing:
        with app.app_context():
            customer_id = create_customer({'name': 'Existing', 'province': 'Gauteng'})
        url = f'/customers/{customer_id}/edit'
    response = client.post(url, data={'name': '', 'province': 'Western Cape'})
    assert response.status_code == 200
    check(response.get_data(as_text=True), 'Western Cape')


@pytest.mark.parametrize('customer_type,example', [('individual', 'e.g. Alex Morgan'), ('company', 'e.g. ABC Trading (Pty) Ltd.')])
def test_portal_redisplay_and_example(app, customer_type, example):
    from app.services.settings import get_company_settings
    from app.services.branches import list_branches
    with app.test_request_context('/portal/register', method='POST'):
        html = render_template('public/portal_form.html', settings=get_company_settings(), branch=list_branches()[0], universal=True, portal_url='/portal', intake_closed=False, form_values={'customer_type': customer_type, 'province': 'Northern Cape', 'name': 'Do not change'}, errors=[])
    check(html, 'Northern Cape')
    assert f'placeholder="{example}"' in html
    assert 'value="Do not change"' in html


def test_branch_and_settings_are_not_changed(client):
    helpers.login(client)
    for url in ['/branches', '/settings']:
        html = client.get(url).get_data(as_text=True)
        assert '<select name="province"' not in html
