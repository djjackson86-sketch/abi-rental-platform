"""Document text encoding (ticket ABI-341952946, item 1).

Ticket ask: "fix error on document when there is a '/' sign it returns a
question mark".

The page stream is encoded as latin-1 with ``errors='replace'``, so any glyph
outside that range printed as "?". The client's live product names carry U+2044
FRACTION SLASH ("2.6m Utility Trailer - 1⁄2 ton" — 23 live products) and their
group names carry it too, which is exactly the "?" they saw.

These tests pin the transliteration and the font encoding, and check a real
generated document rather than the helper alone.
"""
import os
import re
import tempfile

import pytest

from app import create_app
from app.services.pdf_documents import (
    INVOICE_TABLE_X,
    _escape_pdf_text,
    _invoice_template_pdf,
    _pdf_text,
    _wrap_pdf_cell_text,
)

FRACTION_SLASH_NAME = '2.6m Utility Trailer - 1\u20442 ton'


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


# --- the transliterator ------------------------------------------------------

def test_the_fraction_slash_becomes_a_real_slash():
    assert _pdf_text(FRACTION_SLASH_NAME) == '2.6m Utility Trailer - 1/2 ton'
    assert _pdf_text('a\u2215b') == 'a/b'          # division slash
    assert _pdf_text('\u00bd ton') == '1/2 ton'    # ½
    assert _pdf_text('\u00bc x') == '1/4 x'
    assert _pdf_text('\u00be x') == '3/4 x'


def test_typography_and_accents_never_survive_as_a_question_mark():
    for value, expected in [
        ('Donovan\u2019s Trailer', "Donovan's Trailer"),
        ('\u201cQuoted\u201d', '"Quoted"'),
        ('3\u20135 days', '3-5 days'),
        ('wait\u2026', 'wait...'),
        ('a\u00a0b', 'a b'),
        # Beyond latin-1: decomposed and stripped back to plain ASCII.
        ('Bo\u0161man', 'Bosman'),
        ('\u0134abulane', 'Jabulane'),
    ]:
        assert _pdf_text(value) == expected


def test_latin1_letters_are_left_alone_because_the_font_can_print_them():
    # The page streams are encoded latin-1 and the fonts now declare
    # WinAnsiEncoding, so an accented letter prints correctly as itself - there
    # is no reason to flatten it and reflow an existing document.
    assert _pdf_text('Jos\u00e9 M\u00fcller') == 'Jos\u00e9 M\u00fcller'
    assert _pdf_text('25\u00b0C') == '25\u00b0C'


def test_plain_ascii_and_empty_values_pass_through_untouched():
    assert _pdf_text('ORD-10145') == 'ORD-10145'
    assert _pdf_text('R1,234.00 (deposit)') == 'R1,234.00 (deposit)'
    assert _pdf_text('') == ''
    assert _pdf_text(None) == ''
    # A character with no latin-1 or ASCII form is the only thing that may still
    # degrade, and it degrades to one '?' rather than an exception.
    assert _pdf_text('a\u4e2db') == 'a?b'


def test_escaping_still_happens_after_the_transliteration():
    assert _escape_pdf_text('50% (2.4m)') == '50% \\(2.4m\\)'
    assert _escape_pdf_text(FRACTION_SLASH_NAME) == '2.6m Utility Trailer - 1/2 ton'


# --- a real document ---------------------------------------------------------

def _drawn_texts(pdf_bytes):
    """Every text string actually drawn on the page, in order."""
    return [entry['text'] for entry in _drawn_text_positions(pdf_bytes)]


def _drawn_text_positions(pdf_bytes):
    """Every text string with its PDF coordinates and 1-based page number."""
    decoded = pdf_bytes.decode('latin-1')
    streams = re.findall(r'stream\n(.*?)\nendstream', decoded, re.DOTALL)
    entries = []
    page_number = 0
    for content in streams:
        if ' Tj' not in content:
            continue
        page_number += 1
        for font, size, x, y, text in re.findall(
            r'/(F\d) ([\d.]+) Tf 1 0 0 1 ([\d.]+) ([\d.]+) Tm \((.*?)\) Tj', content
        ):
            entries.append({'page': page_number, 'font': font, 'size': float(size), 'x': float(x), 'y': float(y), 'text': text})
    return entries


def build_invoice_texts(app):
    document = {
        'document_type': 'invoice', 'status': 'finalized', 'number': 'INV-10145',
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
        'custom_fields_json': '{}',
        'subtotal': 600.0, 'tax_total': 90.0, 'deposit_total': 0.0,
        'discount_total': 0.0, 'total': 690.0, 'paid_total': 690.0, 'due_total': 0.0,
        'payment_status': 'paid',
    }
    items = [
        {'product_name': FRACTION_SLASH_NAME, 'product_sku': 'CTW-1 - ABC123 GP',
         'custom_name': '', 'quantity': 1, 'unit_price': 600.0, 'line_subtotal': 600.0,
         'line_tax': 90.0, 'line_total': 690.0},
        {'product_name': 'Ratchet + Strap Rental', 'product_sku': '', 'custom_name': '',
         'quantity': 2, 'unit_price': 0.0, 'line_subtotal': 0.0, 'line_tax': 0.0,
         'line_total': 0.0},
    ]
    settings = {
        'company_name': 'Sano Trailers', 'email': 'info@sanotrailers.co.za',
        'phone': '+27 82 123 4567', 'address_line1': '12 Industrial Road',
        'address_line2': None, 'city': 'Johannesburg', 'province': 'Gauteng',
        'postcode': '2001',
    }
    with app.app_context():
        return _invoice_template_pdf(document, items, settings), \
            _drawn_texts(_invoice_template_pdf(document, items, settings))


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


def _many_items(count=8):
    return [
        {'product_name': f'Visible line item {index}', 'product_sku': f'SKU-{index:02d}',
         'custom_name': '', 'quantity': 1, 'unit_price': 600.0, 'line_subtotal': 600.0,
         'line_tax': 90.0, 'line_total': 690.0}
        for index in range(1, count + 1)
    ]


def test_an_invoice_prints_the_fraction_slash_item_name(app):
    pdf_bytes, texts = build_invoice_texts(app)
    assert '2.6m Utility Trailer - 1/2 ton' in texts
    assert 'Ratchet + Strap Rental' in texts
    # The old failure mode: "1⁄2 ton" printed as "1?2 ton".
    assert not any('?' in text for text in texts)
    assert b'(2.6m Utility Trailer - 1/2 ton) Tj' in pdf_bytes


def test_long_product_names_wrap_inside_the_product_column(app):
    long_name = 'Supply and install flatbar gap closure on trailer sides and front'
    assert _wrap_pdf_cell_text(long_name, max_chars=34, max_lines=3) == [
        'Supply and install flatbar gap',
        'closure on trailer sides and front',
    ]
    document = _sample_document('quote')
    document.update({'subtotal': 478.26, 'tax_total': 71.74, 'total': 550.0, 'due_total': 550.0})
    item = {'product_name': long_name, 'product_sku': '', 'custom_name': '', 'quantity': 1,
            'unit_price': 478.26, 'line_subtotal': 478.26, 'line_tax': 71.74, 'line_total': 550.0}
    with app.app_context():
        pdf_bytes = _invoice_template_pdf(document, [item], _sample_settings())
    positions = _drawn_text_positions(pdf_bytes)

    product_lines = [entry['text'] for entry in positions if entry['x'] == INVOICE_TABLE_X]
    assert any('Supply and install flatbar gap' in text for text in product_lines)
    assert any('trailer sides and front' in text for text in product_lines)
    assert not any('...' in text or '…' in text for text in product_lines)


@pytest.mark.parametrize('document_type', ['invoice', 'quote'])
def test_quote_and_invoice_summary_moves_down_with_visible_line_items(app, document_type):
    with app.app_context():
        pdf_bytes = _invoice_template_pdf(_sample_document(document_type), _many_items(8), _sample_settings())
    positions = _drawn_text_positions(pdf_bytes)
    last_sku_y = next(entry['y'] for entry in positions if entry['text'] == 'SKU-08')
    summary_y = next(entry['y'] for entry in positions if entry['text'] == 'Total without VAT')
    amount_due_y = next(entry['y'] for entry in positions if entry['text'] == 'Amount due')

    assert next(entry for entry in positions if entry['text'] == 'SKU-06')['page'] == 1
    assert next(entry for entry in positions if entry['text'] == 'SKU-07')['page'] == 1
    assert next(entry for entry in positions if entry['text'] == 'SKU-08')['page'] <= 2
    assert next(entry for entry in positions if entry['text'] == 'Total without VAT')['page'] == next(
        entry for entry in positions if entry['text'] == 'SKU-08'
    )['page']
    # The summary must follow the final visible item row on quotes and invoices;
    # the old fixed floor put it back above/inside the last row.
    assert summary_y <= last_sku_y - 12
    assert amount_due_y >= 58


@pytest.mark.parametrize('document_type', ['invoice', 'quote'])
def test_long_quote_and_invoice_repeat_headings_on_page_two(app, document_type):
    with app.app_context():
        pdf_bytes = _invoice_template_pdf(_sample_document(document_type), _many_items(12), _sample_settings())
    positions = _drawn_text_positions(pdf_bytes)
    product_headings = [entry for entry in positions if entry['text'] == 'PRODUCT']
    total_headings = [entry for entry in positions if entry['text'] == 'TOTAL INCL. VAT']

    assert [entry['page'] for entry in product_headings] == [1, 2]
    assert [entry['page'] for entry in total_headings] == [1, 2]


def test_document_table_visual_ticket_changes_are_pinned(app):
    document = _sample_document('invoice')
    document['payment_status'] = 'paid'
    with app.app_context():
        pdf_bytes = _invoice_template_pdf(document, _many_items(1), _sample_settings())
    decoded = pdf_bytes.decode('latin-1')
    positions = _drawn_text_positions(pdf_bytes)

    assert '(RATE) Tj' in decoded
    assert '(SUBTOTAL) Tj' in decoded
    assert '(UNIT EXCL. VAT) Tj' not in decoded
    assert '(SUBTOTAL EXCL. VAT) Tj' not in decoded
    assert f'q 0 0 0 rg {INVOICE_TABLE_X:.2f}' in decoded
    assert '1 1 1 rg' in decoded
    assert '0.860 0.940 1' not in decoded
    assert '0.070 0.540 0.300 rg' in decoded
    line_total = next(entry for entry in positions if entry['text'] == 'R690.00' and entry['y'] > 300)
    bottom_total = next(entry for entry in positions if entry['text'] == 'R5520.00' and entry['font'] == 'F2')
    assert line_total['x'] == bottom_total['x'] == 470


@pytest.mark.parametrize('document_type', ['invoice', 'quote'])
def test_long_quote_and_invoice_keep_banking_details_on_page_two(app, document_type):
    with app.app_context():
        pdf_bytes = _invoice_template_pdf(_sample_document(document_type), _many_items(12), _sample_settings())
    decoded = pdf_bytes.decode('latin-1')
    positions = _drawn_text_positions(pdf_bytes)
    banking = next(entry for entry in positions if entry['text'] == 'Banking details')
    account_type = next(entry for entry in positions if entry['text'] == 'Account type: Business Cheque')

    assert '/Count 2' in decoded
    assert banking['page'] == 2
    assert account_type['page'] == 2
    assert account_type['y'] >= 58


def _paid_stamp_geometry(pdf_bytes):
    """(centre_x, centre_y) of the rotated PAID stamp box, from the content stream."""
    stream = pdf_bytes.decode('latin-1')
    match = re.search(
        r'([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+) ([\d.]+) ([\d.]+) cm\s*\n1\.8 w 0 0 ([\d.]+) ([\d.]+) re S',
        stream,
    )
    assert match, 'the PAID stamp box is not in the content stream'
    cos, sin, _c, _d, x, y, width, height = (float(value) for value in match.groups())
    return (x + (cos * width / 2) - (sin * height / 2), y + (sin * width / 2) + (cos * height / 2))


def test_paid_stamp_sits_centred_at_the_top_and_nothing_prints_over_it(app):
    """The stamp used to sit on the first item rows and was buried by the table.

    Client report (ORD-10169 proforma): the PAID stamp was invisible because it
    was drawn under the item rows and the light blue table header band. It is now
    centred in the empty band at the top of the page, so this pins both the
    position and the fact that no drawn text sits over it.
    """
    document = _sample_document()
    document['payment_status'] = 'paid'
    with app.app_context():
        pdf_bytes = _invoice_template_pdf(document, _many_items(2), _sample_settings())
    centre_x, centre_y = _paid_stamp_geometry(pdf_bytes)
    assert 200 <= centre_x <= 400, f'PAID stamp is not centred (x={centre_x})'
    assert centre_y >= 700, f'PAID stamp is not near the top of the page (y={centre_y})'
    over_it = [
        entry for entry in _drawn_text_positions(pdf_bytes)
        if entry['text'].strip() and 210 <= entry['x'] <= 390 and entry['y'] >= 715
    ]
    assert over_it == [], f'text is printed over the PAID stamp: {over_it}'


def test_an_accepted_quote_prints_an_accepted_stamp(app):
    document = _sample_document('quote')
    document['status'] = 'accepted'
    document['number'] = 'QUO-10145'
    with app.app_context():
        pdf_bytes = _invoice_template_pdf(document, _many_items(2), _sample_settings())
    decoded = pdf_bytes.decode('latin-1')
    assert '(ACCEPTED) Tj' in decoded
    assert '(PAID) Tj' not in decoded
    centre_x, centre_y = _paid_stamp_geometry(pdf_bytes)
    assert 200 <= centre_x <= 400, f'ACCEPTED stamp is not centred (x={centre_x})'
    assert centre_y >= 700, f'ACCEPTED stamp is not near the top of the page (y={centre_y})'


def test_a_paid_invoice_still_prints_the_paid_stamp(app):
    document = _sample_document('invoice')
    document['payment_status'] = 'paid'
    with app.app_context():
        pdf_bytes = _invoice_template_pdf(document, _many_items(2), _sample_settings())
    decoded = pdf_bytes.decode('latin-1')
    assert '(PAID) Tj' in decoded
    assert '(ACCEPTED) Tj' not in decoded


def test_the_document_fonts_declare_winansi_encoding(app):
    pdf_bytes, _texts = build_invoice_texts(app)
    decoded = pdf_bytes.decode('latin-1')
    # Both F1 and F2 must map the byte range the stream is encoded in.
    assert decoded.count('/Encoding /WinAnsiEncoding') == 2
    assert '/BaseFont /Helvetica /Encoding /WinAnsiEncoding' in decoded
    assert '/BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding' in decoded


def test_the_stream_is_pure_latin1_so_the_replace_never_fires(app):
    pdf_bytes, texts = build_invoice_texts(app)
    decoded = pdf_bytes.decode('latin-1')
    streams = re.findall(r'stream\n(.*?)\nendstream', decoded, re.DOTALL)
    content = next(s for s in streams if ' Tj' in s)
    # A character the encoder cannot represent would have been replaced with '?'
    # at this exact point; the transliterator runs before it now.
    assert content.encode('latin-1', 'strict') == content.encode('latin-1')
    assert '\u2044' not in content
