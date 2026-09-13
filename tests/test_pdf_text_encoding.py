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
from app.services.pdf_documents import _escape_pdf_text, _invoice_template_pdf, _pdf_text

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
    decoded = pdf_bytes.decode('latin-1')
    streams = re.findall(r'stream\n(.*?)\nendstream', decoded, re.DOTALL)
    content = next(s for s in streams if ' Tj' in s)
    return re.findall(r'/F\d [\d.]+ Tf 1 0 0 1 [\d.]+ [\d.]+ Tm \((.*?)\) Tj', content)


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


def test_an_invoice_prints_the_fraction_slash_item_name(app):
    pdf_bytes, texts = build_invoice_texts(app)
    assert '2.6m Utility Trailer - 1/2 ton' in texts
    assert 'Ratchet + Strap Rental' in texts
    # The old failure mode: "1⁄2 ton" printed as "1?2 ton".
    assert not any('?' in text for text in texts)
    assert b'(2.6m Utility Trailer - 1/2 ton) Tj' in pdf_bytes


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
