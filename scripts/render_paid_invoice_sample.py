"""Render a PAID invoice from the real template so the stamp can be eyeballed.

Mirrors the ORD-10169 shape (2 lines, no discount row, settled) - the client's
proforma - so the PAID stamp position can be checked on an actual page.

Run:  .venv/bin/python scripts/render_paid_invoice_sample.py [out.pdf]
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.services.pdf_documents import _invoice_template_pdf, _document_logo_bytes  # noqa: E402

document = {
    'document_type': 'invoice', 'status': 'draft', 'number': '',
    'order_number': 'ORD-10169', 'created_at': '2026-09-15T15:00:00',
    'start_at': '2026-09-15T15:00:00', 'end_at': '2026-09-16T15:00:00',
    'branch_name': 'Midrand', 'branch_email': 'mid@sanotrailers.co.za',
    'branch_phone': '010 221 1723', 'branch_address_line1': '229 Summit Road',
    'branch_address_line2': 'Bridle Park AH', 'branch_city': 'Midrand',
    'branch_province': 'Gauteng', 'branch_postal_code': '0157',
    'branch_bank_name': 'CAPITEC BUSINESS', 'branch_bank_account_name': 'Sano Nkosi (Pty) Ltd',
    'branch_bank_account_number': '1053 7373 60', 'branch_bank_branch_code': '450 105',
    'branch_bank_account_type': 'Cheque', 'branch_bank_reference_note': 'Order No.',
    'customer_name': 'Mduduzi Ntshangase', 'customer_email': 'mduduzi77@gmail.com',
    'customer_phone': '+276****3605', 'customer_address_line1': '6405 Malenga Street',
    'customer_address_line2': 'Olivenhoutbosch Ext 26', 'customer_suburb': '',
    'customer_city': 'Tshwane', 'customer_province': 'Gauteng',
    'customer_postal_code': '0130', 'customer_country': 'South Africa',
    'custom_fields_json': '{"alternative_contact_name": "Evonia", "alternative_contact_number": "(071) 440-4165"}',
    'deposit_option': 'no_deposit', 'discount_total': 0.0, 'discount_mode': '', 'discount_value': 0.0,
    'subtotal': 552.17, 'tax_total': 82.83, 'deposit_total': 0.0,
    'total': 635.0, 'paid_total': 635.0, 'due_total': 0.0,
    'payment_status': 'paid',
}

items = [
    {'product_name': '4m Flat Bed Trailer - 2.5ton', 'product_sku': '', 'custom_name': '',
     'quantity': 1, 'unit_price': 595.0, 'line_subtotal': 517.39, 'line_tax': 77.61, 'line_total': 595.0},
    {'product_name': 'Ratchet + Strap Rental', 'product_sku': '', 'custom_name': '',
     'quantity': 2, 'unit_price': 20.0, 'line_subtotal': 34.78, 'line_tax': 5.22, 'line_total': 40.0},
]

settings = {
    'company_name': 'Sano Trailers', 'email': 'info@sanotrailers.co.za',
    'phone': '010 221 1723', 'address_line1': '229 Summit Road', 'address_line2': 'Bridle Park AH',
    'city': 'Midrand', 'province': 'Gauteng', 'postcode': '0157',
}

app = create_app({'TESTING': True})
with app.app_context():
    pdf_bytes = _invoice_template_pdf(document, items, settings, logo_bytes=_document_logo_bytes())

out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / 'test_invoice_paid.pdf'
out.write_bytes(pdf_bytes)
print('Wrote', out, len(pdf_bytes), 'bytes')

stream = pdf_bytes.decode('latin-1')
match = re.search(
    r'([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+) ([\d.]+) ([\d.]+) cm\s*\n1\.8 w 0 0 ([\d.]+) ([\d.]+) re S',
    stream,
)
if match:
    cos, sin, _c, _d, x, y, width, height = (float(value) for value in match.groups())
    print('stamp box centre: x=%.1f y=%.1f (page 595x842, so centre x=297.5)'
          % (x + cos * width / 2 - sin * height / 2, y + sin * width / 2 + cos * height / 2))
else:
    print('NO PAID STAMP FOUND')
