"""Ticket ABI-341953023 - VAT/reg numbers print on documents.

Requested edit: show Sano Trailers VAT No / company registration number and
company-customer VAT No / registration number in document views and PDFs, but
only when those fields are provided.
"""
import os
import re
import tempfile

import pytest
from werkzeug.datastructures import MultiDict

from app import create_app
from app.db import get_db, now


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    application = create_app({
        'TESTING': True,
        'DATABASE': path,
        'SECRET_KEY': 'test',
        'ADMIN_EMAIL': 'admin@abi.local',
        'ADMIN_PASSWORD': 'admin123',
    })
    yield application
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client):
    with client.application.app_context():
        row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
    return client.post('/login', data={'user_id': str(row['id']), 'password': 'admin123'}, follow_redirects=True)


def set_issuer_numbers(app, vat='SANO-VAT-123', reg='SANO-REG-456'):
    with app.app_context():
        get_db().execute(
            "UPDATE company_settings SET vat_number = ?, company_reg_no = ?, updated_at = ? WHERE id = 1",
            (vat, reg, now()),
        )
        get_db().commit()


def seed_company_customer_and_product(client):
    customer = client.post('/customers/new', data={
        'customer_type': 'company',
        'name': 'Acme Logistics (Pty) Ltd',
        'email': 'accounts@acme.test',
        'phone': '+271****1111',
        'vat_number': 'ACME-VAT-789',
        'company_reg_no': '2024/654321/07',
    }, follow_redirects=True)
    assert b'Customer created' in customer.data
    product = client.post('/inventory/new', data={
        'name': 'Document VAT Trailer',
        'sku': 'VAT-TRL',
        'quantity': '3',
        'description': 'VAT document test trailer.',
        'product_type': 'rental',
        'price_amount': '200',
        'price_unit': 'day',
        'security_deposit': '0',
        'tax_profile_id': '1',
        'active': '1',
        'public_visible': '1',
    }, follow_redirects=True)
    assert product.status_code == 200


def create_order_and_document(client, app, document_type='invoice'):
    payload = MultiDict([
        ('customer_id', '1'),
        ('booking_type', 'return'),
        ('collect_branch_id', '1'),
        ('return_branch_id', '1'),
        ('deposit_option', 'security_deposit'),
        ('start_date', '2026-07-01'),
        ('start_time', '09:00'),
        ('end_date', '2026-07-03'),
        ('end_time', '15:00'),
        ('product_id', '1'),
        ('quantity', '1'),
    ])
    order = client.post('/orders/new', data=payload, follow_redirects=False)
    assert order.status_code == 302
    order_id = int(order.headers['Location'].rstrip('/').split('/')[-1])
    created = client.post(f'/orders/{order_id}/documents', data={'document_type': document_type}, follow_redirects=True)
    assert created.status_code == 200
    with app.app_context():
        row = get_db().execute(
            'SELECT id FROM documents WHERE order_id = ? AND document_type = ? ORDER BY id DESC LIMIT 1',
            (order_id, document_type),
        ).fetchone()
    return row['id']


def pdf_drawn_text(pdf_bytes):
    streams = re.findall(r'stream\r?\n(.*?)\r?\nendstream', pdf_bytes.decode('latin-1'), re.S)
    return '\n'.join(stream for stream in streams if ' Tm ' in stream and ' Tj' in stream)


def test_invoice_html_and_pdf_show_issuer_and_company_customer_vat_reg(client, app):
    login(client)
    set_issuer_numbers(app)
    seed_company_customer_and_product(client)
    document_id = create_order_and_document(client, app, 'invoice')

    html = client.get(f'/documents/{document_id}').get_data(as_text=True)
    assert 'VAT No: SANO-VAT-123' in html
    assert 'Company Reg No: SANO-REG-456' in html
    assert 'VAT No: ACME-VAT-789' in html
    assert 'Company Reg No: 2024/654321/07' in html

    from app.services.pdf_documents import document_pdf_bytes
    with app.app_context():
        pdf_text = pdf_drawn_text(document_pdf_bytes(document_id))
    assert '(VAT No: SANO-VAT-123)' in pdf_text
    assert '(Company Reg No: SANO-REG-456)' in pdf_text
    assert '(VAT No: ACME-VAT-789)' in pdf_text
    assert '(Company Reg No: 2024/654321/07)' in pdf_text


def test_contract_pdf_and_html_show_same_numbers(client, app):
    login(client)
    set_issuer_numbers(app)
    seed_company_customer_and_product(client)
    document_id = create_order_and_document(client, app, 'contract')

    html = client.get(f'/documents/{document_id}').get_data(as_text=True)
    assert 'VAT No: SANO-VAT-123' in html
    assert 'Company Reg No: SANO-REG-456' in html
    assert 'VAT No: ACME-VAT-789' in html
    assert 'Company Reg No: 2024/654321/07' in html

    pdf = client.get(f'/documents/{document_id}/download.pdf')
    assert pdf.status_code == 200
    text = pdf_drawn_text(pdf.data)
    assert 'Issuer VAT No: SANO-VAT-123' in text
    assert 'Issuer Company Reg No: SANO-REG-456' in text
    assert 'Customer VAT No: ACME-VAT-789' in text
    assert 'Customer Company Reg No: 2024/654321/07' in text


def test_individual_customer_vat_reg_custom_fields_are_not_printed(client, app):
    login(client)
    set_issuer_numbers(app, vat='', reg='')
    client.post('/customers/new', data={
        'customer_type': 'individual',
        'name': 'Individual With Hidden Numbers',
        'email': 'person@example.test',
        'phone': '+271****2222',
        'vat_number': 'HIDDEN-VAT',
        'company_reg_no': 'HIDDEN-REG',
    }, follow_redirects=True)
    client.post('/inventory/new', data={
        'name': 'Individual Number Trailer',
        'sku': 'IND-TRL',
        'quantity': '1',
        'description': 'Individual customer document test.',
        'product_type': 'rental',
        'price_amount': '100',
        'price_unit': 'day',
        'security_deposit': '0',
        'tax_profile_id': '1',
        'active': '1',
        'public_visible': '1',
    }, follow_redirects=True)
    document_id = create_order_and_document(client, app, 'invoice')

    html = client.get(f'/documents/{document_id}').get_data(as_text=True)
    assert 'HIDDEN-VAT' not in html
    assert 'HIDDEN-REG' not in html

    from app.services.pdf_documents import document_pdf_bytes
    with app.app_context():
        pdf_text = pdf_drawn_text(document_pdf_bytes(document_id))
    assert 'HIDDEN-VAT' not in pdf_text
    assert 'HIDDEN-REG' not in pdf_text


def test_general_settings_form_saves_issuer_vat_and_registration(client, app):
    login(client)
    page = client.get('/settings/general')
    assert page.status_code == 200
    assert b'name="vat_number"' in page.data
    assert b'name="company_reg_no"' in page.data

    saved = client.post('/settings/general', data={
        'company_name': 'Sano Trailers',
        'email': 'info@sanotrailers.co.za',
        'phone': '010-000-0000',
        'website': 'https://sanotrailers.co.za',
        'vat_number': 'SAVE-VAT-123',
        'company_reg_no': 'SAVE-REG-456',
        'country': 'South Africa',
        'city': 'Johannesburg',
        'province': 'Gauteng',
        'postcode': '2000',
        'address_line1': '1 Trailer Street',
        'address_line2': '',
        'units': 'metric',
        'currency': 'ZAR',
        'currency_symbol': 'R',
        'currency_position': 'before',
        'tax_mode': 'exclusive',
        'default_pickup_time': '09:00',
        'default_return_time': '15:00',
        'time_increment_minutes': '60',
        'deposit_mode': 'product_specific',
        'deposit_value': '0',
        'invoice_email_message': 'Hello {customer_name}',
        'invoice_email_signature': 'Regards',
        'pricing_enabled': '1',
        'enable_time_selection': '1',
    }, follow_redirects=True)
    assert saved.status_code == 200
    with app.app_context():
        settings = get_db().execute('SELECT vat_number, company_reg_no FROM company_settings WHERE id = 1').fetchone()
    assert settings['vat_number'] == 'SAVE-VAT-123'
    assert settings['company_reg_no'] == 'SAVE-REG-456'
