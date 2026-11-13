"""ABI-341953296: document vehicle presentation only; never alter stored data."""
import json
from email import policy
from email.parser import BytesParser

import pytest

from app.db import get_db
from app.routes.documents import _prepare_email_draft
from app.services.pdf_documents import _main_vehicle_lines
from test_invoice_main_vehicle_details import (
    ALL_MAIN_VEHICLE_FIELDS, EXPECTED_LINES, app, client, _login,
    _seed_customer_and_products, _create_order, _drawn_texts,
)


@pytest.mark.parametrize('fields, expected', [
    ({}, []),
    ({'vehicle_color': 'Blue'}, ['Vehicle Colour: Blue']),
    ({'vehicle_reg_no': 'ABC 123'}, ['Vehicle Reg No: ABC 123']),
    ({'vehicle_make': '  Toyota  ', 'vehicle_color': ' ', 'vehicle_reg_no': None},
     ['Vehicle Make: Toyota']),
    (ALL_MAIN_VEHICLE_FIELDS, EXPECTED_LINES),
    ({'vehicle_vin': 'HIDDEN-VIN', 'vehicle_model': 'HIDDEN-MODEL'}, []),
])
def test_exact_whitelist_order_and_missing_values(fields, expected):
    assert _main_vehicle_lines(fields) == expected


@pytest.mark.parametrize('document_type', ['invoice', 'quote', 'contract', 'packing_slip'])
def test_document_view_pdf_and_email_preserve_unrelated_details(client, app, document_type):
    _login(client)
    _seed_customer_and_products(client, 1)
    fields = dict(ALL_MAIN_VEHICLE_FIELDS, alternative_contact_name='Keep Contact',
                  vat_number='VAT-KEEP-123', company_reg_no='REG-KEEP-456')
    stored = json.dumps(fields)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE customers SET customer_type='company', custom_fields_json=? WHERE id=1", (stored,))
        db.execute("UPDATE branches SET bank_name='Fixture Bank', bank_account_number='123456' WHERE id=1")
        db.commit()
    order_id = _create_order(client, [(1, 1)])
    created = client.post(f'/orders/{order_id}/documents', data={'document_type': document_type})
    assert created.status_code == 302
    with app.app_context():
        document_id = get_db().execute('SELECT id FROM documents WHERE order_id=? AND document_type=?',
                                      (order_id, document_type)).fetchone()['id']
    view = client.get(f'/documents/{document_id}')
    pdf = client.get(f'/documents/{document_id}/download.pdf')
    assert view.status_code == pdf.status_code == 200
    assert pdf.data.startswith(b'%PDF-')
    html = view.get_data(as_text=True)
    texts = _drawn_texts(pdf.data)
    drawn = '\n'.join(texts)
    for content in (html, drawn):
        assert 'VAT-KEEP-123' in content
        assert 'REG-KEEP-456' in content
        assert 'Extra Item 1' in content
        for key, value in ALL_MAIN_VEHICLE_FIELDS.items():
            if key not in {'vehicle_make', 'vehicle_color', 'vehicle_reg_no'}:
                assert value not in content
        assert 'Vehicle Color:' not in content
        assert 'Veh Reg No:' not in content
    if document_type in {'invoice', 'quote'}:
        assert [t for t in texts if t.startswith(('Vehicle Make:', 'Vehicle Colour:', 'Vehicle Reg No:'))] == EXPECTED_LINES
        labels = ['Vehicle Make:', 'Vehicle Colour:', 'Vehicle Reg No:']
        assert [html.index(label) for label in labels] == sorted(html.index(label) for label in labels)
        assert 'Keep Contact' in html and 'Keep Contact' in drawn
        assert 'Banking details' in html and 'Banking details' in drawn
        # Generate the actual Outlook draft without sending email or marking a document sent.
        with app.test_request_context():
            _, eml, _ = _prepare_email_draft(document_id, 'fixture@example.test')
        message = BytesParser(policy=policy.default).parsebytes(eml)
        attachments = [p.get_payload(decode=True) for p in message.walk() if p.get_content_type() == 'application/pdf']
        assert len(attachments) == 1
        assert [t for t in _drawn_texts(attachments[0]) if t.startswith(('Vehicle Make:', 'Vehicle Colour:', 'Vehicle Reg No:'))] == EXPECTED_LINES
    with app.app_context():
        assert get_db().execute('SELECT custom_fields_json FROM customers WHERE id=1').fetchone()[0] == stored
