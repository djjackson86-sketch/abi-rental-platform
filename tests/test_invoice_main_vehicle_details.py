"""Invoice PDF main-vehicle display (customer custom fields, not extra vehicles).

Confirms the invoice/quote PDF prints the MAIN vehicle held on the customer
record (``custom_fields_json`` keys ``vehicle_*``) as a labelled block, expanding
the old three-line legacy block (Vehicle Make / Vehicle Color / Veh Reg No) to
every non-blank captured field, and that the block never leaks a row from the
per-client ``vehicles`` table (which is where ADDITIONAL vehicles live).

Layout invariants are checked on the drawn PDF content streams, page by page - a
byte search cannot tell "the line is on the page" from "the line is printed over
something else".
"""
import json
import os
import re
import tempfile

import pytest
from werkzeug.datastructures import MultiDict

from app import create_app
from app.db import get_db, now
from app.services.pdf_documents import (
    INVOICE_TABLE_X,
    TOTAL_INCL_COLUMN_X,
    _invoice_template_pdf,
    _main_vehicle_lines,
)

ALL_MAIN_VEHICLE_FIELDS = {
    'vehicle_make': 'Toyota Hilux',
    'vehicle_model': 'Hilux 2.4GD-6',
    'vehicle_color': 'Arctic White',
    'vehicle_type': 'LDV',
    'vehicle_reg_no': 'CA 123-456',
    'vehicle_vin': 'AHTBB3CD500123456',
    'vehicle_engine_number': '2GD9174456',
    'vehicle_registration_number': 'BB33CD',
    'vehicle_licence_number': '5120367QP4HD',
    'vehicle_licence_disk_expiry': '2027-03-31',
    'vehicle_registering_authority': 'City of Tshwane',
    'vehicle_control_number': 'CN-778899',
}

EXPECTED_LINES = [
    'Vehicle Make: Toyota Hilux',
    'Vehicle Model: Hilux 2.4GD-6',
    'Vehicle Color: Arctic White',
    'Vehicle Type: LDV',
    'Veh Reg No: CA 123-456',
    'NaTIS registration number: BB33CD',
    'VIN: AHTBB3CD500123456',
    'Engine number: 2GD9174456',
    'Disk licence number: 5120367QP4HD',
    'Licence disk expiry: 2027-03-31',
    'Registering authority: City of Tshwane',
    'Control number: CN-778899',
]


# --- fixtures / helpers -------------------------------------------------------

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


def _drawn_text_positions(pdf_bytes):
    """Every drawn text run with its page number, from the content streams."""
    decoded = pdf_bytes.decode('latin-1')
    entries = []
    page_number = 0
    for content in re.findall(r'stream\n(.*?)\nendstream', decoded, re.DOTALL):
        if ' Tj' not in content:
            continue
        page_number += 1
        for font, size, x, y, text in re.findall(
            r'/(F\d) ([\d.]+) Tf 1 0 0 1 ([\d.]+) ([\d.]+) Tm \((.*?)\) Tj', content
        ):
            entries.append({'page': page_number, 'font': font, 'size': float(size),
                            'x': float(x), 'y': float(y), 'text': text})
    return entries


def _drawn_texts(pdf_bytes):
    return [entry['text'] for entry in _drawn_text_positions(pdf_bytes)]


def _sample_document(document_type='invoice'):
    return {
        'document_type': document_type, 'status': 'finalized', 'number': 'INV-10145',
        'order_number': 'ORD-10145', 'created_at': '2026-09-13T10:00:00',
        'start_at': '2026-09-13T10:30:00', 'end_at': '2026-09-14T10:30:00',
        'branch_name': 'Midrand', 'branch_email': 'info@sanotrailers.co.za',
        'branch_phone': '+27 82 123 4567', 'branch_address_line1': '12 Industrial Road',
        'branch_address_line2': None, 'branch_city': 'Johannesburg',
        'branch_province': 'Gauteng', 'branch_postal_code': '2001',
        'branch_bank_name': 'FNB', 'branch_bank_account_name': 'Sano Trailers',
        'branch_bank_account_number': '62012345678', 'branch_bank_branch_code': '250655',
        'branch_bank_account_type': 'Business Cheque', 'branch_bank_reference_note': '',
        'customer_name': 'Thabisile Skosana', 'customer_email': 'client@example.test',
        'customer_phone': '+27 71 987 6543', 'customer_address_line1': '45 Main Street',
        'customer_address_line2': None, 'customer_suburb': 'Vorna Valley',
        'customer_city': 'Midrand', 'customer_province': 'Gauteng',
        'customer_postal_code': '1685', 'customer_country': 'South Africa',
        'custom_fields_json': '{}', 'deposit_option': 'security_deposit',
        'subtotal': 4800.0, 'tax_total': 720.0, 'deposit_total': 0.0,
        'discount_total': 0.0, 'total': 5520.0, 'paid_total': 0.0, 'due_total': 5520.0,
        'payment_status': 'payment_due',
    }


def _sample_settings():
    return {
        'company_name': 'Sano Trailers', 'email': 'info@sanotrailers.co.za',
        'phone': '+27 82 123 4567', 'address_line1': '12 Industrial Road',
        'address_line2': None, 'city': 'Johannesburg', 'province': 'Gauteng',
        'postcode': '2001',
    }


def _items(count):
    return [
        {'product_name': f'Visible line item {index}', 'product_sku': f'SKU-{index:02d}',
         'custom_name': '', 'quantity': 1, 'unit_price': 600.0, 'line_subtotal': 600.0,
         'line_tax': 90.0, 'line_total': 690.0}
        for index in range(1, count + 1)
    ]


def _document_with_fields(fields, document_type='invoice'):
    document = _sample_document(document_type)
    document['custom_fields_json'] = json.dumps(fields)
    return document


def _render(document, items):
    return _invoice_template_pdf(document, items, _sample_settings())


# --- the helper itself --------------------------------------------------------

def test_helper_reads_only_decoded_custom_fields_and_skips_blanks():
    lines = _main_vehicle_lines({'vehicle_make': 'Toyota Hilux', 'vehicle_model': '',
                                 'vehicle_reg_no': 'CA 123-456', 'vehicle_vin': '   '})
    assert lines == ['Vehicle Make: Toyota Hilux', 'Veh Reg No: CA 123-456']
    # An unrelated key that happens to sit in the same blob is never printed here.
    assert _main_vehicle_lines({'vat_number': '4990123456'}) == []
    assert _main_vehicle_lines({}) == []


# --- the invoice/quote block --------------------------------------------------

def test_legacy_three_vehicle_lines_keep_their_labels(app):
    fields = {'vehicle_make': 'Toyota Quantum', 'vehicle_color': 'White',
              'vehicle_reg_no': 'LKL 455 NW'}
    with app.app_context():
        texts = _drawn_texts(_render(_document_with_fields(fields), _items(2)))
    assert 'Vehicle Make: Toyota Quantum' in texts
    assert 'Vehicle Color: White' in texts
    assert 'Veh Reg No: LKL 455 NW' in texts


def test_every_nonblank_main_vehicle_field_is_printed(app):
    with app.app_context():
        texts = _drawn_texts(_render(_document_with_fields(dict(ALL_MAIN_VEHICLE_FIELDS)), _items(2)))
    for expected in EXPECTED_LINES:
        assert expected in texts, f'missing main-vehicle line: {expected}'


@pytest.mark.parametrize('document_type', ['invoice', 'quote'])
def test_the_main_vehicle_block_prints_on_invoices_and_quotes(app, document_type):
    with app.app_context():
        texts = _drawn_texts(_render(_document_with_fields(dict(ALL_MAIN_VEHICLE_FIELDS), document_type), _items(1)))
    assert 'Vehicle Make: Toyota Hilux' in texts
    assert 'Control number: CN-778899' in texts


def test_blank_main_vehicle_fields_are_omitted_and_nothing_is_fabricated(app):
    fields = {'vehicle_make': 'Toyota Hilux', 'vehicle_reg_no': 'CA 123-456'}
    with app.app_context():
        texts = _drawn_texts(_render(_document_with_fields(fields), _items(1)))
    for label in ('Vehicle Model', 'Vehicle Color', 'Vehicle Type', 'VIN', 'Engine number',
                  'NaTIS registration number', 'Disk licence number', 'Licence disk expiry',
                  'Registering authority', 'Control number'):
        assert not any(text.startswith(f'{label}:') for text in texts), f'{label} printed despite being blank'
    # the two that ARE set still print, and no vehicle line is blank-valued
    assert 'Vehicle Make: Toyota Hilux' in texts
    assert 'Veh Reg No: CA 123-456' in texts


def test_rich_vehicle_block_does_not_cut_customer_info_or_line_items(app):
    """12 vehicle fields + 3 alt contacts + 8 items: nothing is dropped or overlapped."""
    fields = dict(ALL_MAIN_VEHICLE_FIELDS)
    fields.update({
        'alternative_contact_name': 'Sbu Mahlangu',
        'alternative_contact_number': '0761492049',
        'alternative_contact_relationship': 'Spouse',
    })
    with app.app_context():
        pdf_bytes = _render(_document_with_fields(fields), _items(8))
    positions = _drawn_text_positions(pdf_bytes)
    texts = [entry['text'] for entry in positions]

    # vehicle + alternative contact detail survives intact
    for expected in EXPECTED_LINES:
        assert expected in texts, f'missing main-vehicle line: {expected}'
    assert 'Alternative Contact Name: Sbu Mahlangu' in texts
    assert 'Alternative Contact Number: 0761492049' in texts
    assert 'Alternative Contact Relationship: Spouse' in texts
    # every line item prints
    for index in range(1, 9):
        assert f'Visible line item {index}' in texts
    assert 'Total without VAT' in texts
    assert 'Amount due' in texts

    # nothing is drawn below the page floor
    assert all(entry['y'] > 40 for entry in positions)

    # the summary never overlaps the last item row on any page
    page_of = {}
    for entry in positions:
        page_of.setdefault(entry['page'], []).append(entry)
    for page, entries in page_of.items():
        rows = [entry for entry in entries
                if abs(entry['x'] - INVOICE_TABLE_X) < 0.01 and entry['text'].startswith('Visible line item')]
        summary = [entry for entry in entries if entry['text'] in ('Total without VAT', 'Amount due')]
        if rows and summary:
            assert min(entry['y'] for entry in rows) - 2 > max(entry['y'] for entry in summary), (
                f'page {page}: item rows overlap the summary')


# --- integration: the vehicle table must never leak in -------------------------

def _login(client):
    with client.application.app_context():
        row = get_db().execute(
            "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
        ).fetchone()
    return client.post('/login', data={'user_id': str(row['id']), 'password': 'admin123'},
                       follow_redirects=True)


def _seed_customer_and_products(client, count):
    created = client.post('/customers/new', data={
        'customer_type': 'individual', 'name': 'Vehicle Customer',
        'email': 'vehicle@example.com', 'phone': '+27000000000',
    }, follow_redirects=True)
    assert created.status_code == 200
    for index in range(1, count + 1):
        made = client.post('/inventory/new', data={
            'name': f'Extra Item {index}', 'sku': f'VEH-{index:03d}',
            'description': 'Vehicle block test item.', 'product_type': 'sale',
            'price_amount': '100.00', 'price_unit': 'each', 'security_deposit': '0',
            'quantity': '50', 'tax_profile_id': '1', 'active': '1', 'public_visible': '1',
        }, follow_redirects=True)
        assert made.status_code == 200


def _create_order(client, lines):
    payload = [('customer_id', '1'), ('booking_type', 'return'),
               ('collect_branch_id', '1'), ('return_branch_id', '1'),
               ('deposit_option', 'security_deposit'),
               ('start_date', '2026-07-01'), ('start_time', '09:00'),
               ('end_date', '2026-07-03'), ('end_time', '15:00'),
               ('notes', 'Vehicle block test order')]
    for product_id, quantity in lines:
        payload += [('product_id', str(product_id)), ('quantity', str(quantity))]
    res = client.post('/orders/new', data=MultiDict(payload), follow_redirects=False)
    assert res.status_code == 302
    return res.headers['Location'].rstrip('/').split('/')[-1]


def _create_invoice(client, app, order_id):
    created = client.post(f'/orders/{order_id}/documents',
                          data={'document_type': 'invoice'}, follow_redirects=True)
    assert created.status_code == 200
    with app.app_context():
        row = get_db().execute(
            'SELECT id FROM documents WHERE order_id = ? AND document_type = ? ORDER BY id DESC LIMIT 1',
            (order_id, 'invoice'),
        ).fetchone()
    return row['id']


def test_invoice_shows_the_customer_main_vehicle_not_the_vehicles_table(client, app):
    """The PDF prints the vehicle on the customer record, never the extra `vehicles` row."""
    _login(client)
    _seed_customer_and_products(client, 1)

    main_vehicle = {'vehicle_make': 'Toyota Hilux', 'vehicle_model': 'Hilux 2.4GD-6',
                    'vehicle_reg_no': 'MAIN-123'}
    with app.app_context():
        db = get_db()
        db.execute('UPDATE customers SET custom_fields_json = ? WHERE id = 1',
                   (json.dumps(main_vehicle),))
        # An ADDITIONAL vehicle recorded in the separate table for the same client.
        db.execute(
            """INSERT INTO vehicles (customer_id, registration, make, model, year, vin,
                engine_number, colour, licence_number, registration_number, control_number,
                registering_authority, vehicle_type, licence_disk_expiry, raw_scan_text,
                source, created_at, updated_at)
            VALUES (1, 'ADD-999', 'Nissan Note', 'Acenta', '2019', 'VIN-EXTRA', 'ENG-EXTRA',
                'Silver', 'LIC-EXTRA', 'REG-EXTRA', 'CTRL-EXTRA', 'Some Authority', 'Sedan',
                '2026-01-31', '', 'scan', ?, ?)""",
            (now(), now()),
        )
        db.commit()

    order_id = _create_order(client, [(1, 1)])
    document_id = _create_invoice(client, app, order_id)
    response = client.get(f'/documents/{document_id}/download.pdf')
    assert response.status_code == 200
    assert response.data.startswith(b'%PDF-')
    texts = _drawn_texts(response.data)

    # the main vehicle from the customer's own fields is printed
    assert 'Vehicle Make: Toyota Hilux' in texts
    assert 'Vehicle Model: Hilux 2.4GD-6' in texts
    assert 'Veh Reg No: MAIN-123' in texts

    # nothing from the extra `vehicles` table row appears anywhere
    joined = '\n'.join(texts)
    for leaked in ('ADD-999', 'Nissan Note', 'Acenta', 'VIN-EXTRA', 'ENG-EXTRA',
                   'LIC-EXTRA', 'REG-EXTRA', 'CTRL-EXTRA', 'Some Authority', 'Sedan'):
        assert leaked not in joined, f'{leaked!r} leaked from the vehicles table onto the invoice'
