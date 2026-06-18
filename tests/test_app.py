import os
import tempfile

import pytest

from app import create_app


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    app = create_app({
        'TESTING': True,
        'DATABASE': path,
        'SECRET_KEY': 'test',
        'ADMIN_EMAIL': 'admin@abi.local',
        'ADMIN_PASSWORD': 'admin123',
    })
    yield app
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client):
    return client.post('/login', data={'email': 'admin@abi.local', 'password': 'admin123'}, follow_redirects=True)


def test_health(client):
    res = client.get('/health')
    assert res.status_code == 200
    assert res.get_json()['ok'] is True


def test_login_and_setup(client):
    res = login(client)
    assert res.status_code == 200
    assert b'Welcome, follow these steps' in res.data
    assert b'Create a tax profile' in res.data


def test_protected_pages_redirect(client):
    res = client.get('/dashboard')
    assert res.status_code == 302
    assert '/login' in res.headers['Location']


def test_settings_persist(client, app):
    login(client)
    res = client.post('/settings/general', data={
        'company_name': 'ABI Solutions Test',
        'email': 'hello@example.com',
        'phone': '123',
        'website': 'https://example.com',
        'country': 'South Africa',
        'city': 'Cape Town',
        'province': 'Western Cape',
        'postcode': '8000',
        'address_line1': '1 Road',
        'address_line2': '',
        'timezone': 'Africa/Johannesburg',
        'date_format': 'dd-mm-yyyy',
        'units': 'metric',
        'first_day_of_week': 'Sunday',
        'currency': 'ZAR',
        'currency_symbol': 'R',
        'currency_position': 'before',
        'tax_mode': 'exclusive',
        'default_pickup_time': '09:00',
        'default_return_time': '15:00',
        'time_increment_minutes': '60',
        'deposit_mode': 'product_specific',
        'deposit_value': '0',
        'pricing_enabled': '1',
        'enable_time_selection': '1',
    }, follow_redirects=True)
    assert res.status_code == 200
    assert b'ABI Solutions Test' in res.data


def test_tax_profile_creation(client):
    login(client)
    res = client.post('/settings/taxes', data={'name': 'VAT', 'rate': '15', 'is_default': 'on'}, follow_redirects=True)
    assert res.status_code == 200
    assert b'VAT' in res.data
    assert b'15.0%' in res.data


def test_store_empty_state(client):
    res = client.get('/store')
    assert res.status_code == 200
    assert b'No products' in res.data
    assert b'Select a rental period' in res.data
