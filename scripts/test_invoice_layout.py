"""Dump every drawn text command of a sample invoice, with its x/y and font.

Builds an invoice straight from the PDF template (no database) so a layout change
can be checked from the content stream rather than from a rendered image.
Writes test_invoice.pdf next to the repo root - delete it afterwards.

Run:  .venv/bin/python scripts/test_invoice_layout.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.services.pdf_documents import _invoice_template_pdf, _document_logo_bytes  # noqa: E402

sample_document = {
    'document_type': 'invoice',
    'status': 'finalized',
    'number': 'INV-2026-0001',
    'order_number': 'ORD-2026-0042',
    'created_at': '2026-09-01T10:00:00',
    'start_at': '2026-09-01T10:00:00',
    'end_at': '2026-09-05T10:00:00',
    'branch_name': 'Sano Trailers',
    'branch_email': 'info@sanotrailers.co.za',
    'branch_phone': '+27 82 123 4567',
    'branch_address_line1': '12 Industrial Road',
    'branch_address_line2': None,
    'branch_city': 'Johannesburg',
    'branch_province': 'Gauteng',
    'branch_postal_code': '2001',
    'branch_bank_name': 'FNB',
    'branch_bank_account_name': 'Sano Trailers (Pty) Ltd',
    'branch_bank_account_number': '62012345678',
    'branch_bank_branch_code': '250655',
    'branch_bank_account_type': 'Business Cheque',
    'branch_bank_reference_note': 'Invoice {number}',
    'customer_name': 'Acme Rentals',
    'customer_email': 'billing@acme.co.za',
    'customer_phone': '+27 71 987 6543',
    'customer_address_line1': '45 Main Street',
    'customer_address_line2': 'Unit 7',
    'customer_suburb': 'Rosebank',
    'customer_city': 'Johannesburg',
    'customer_province': 'Gauteng',
    'customer_postal_code': '2196',
    'customer_country': 'South Africa',
    'subtotal': 4500.0,
    'tax_total': 675.0,
    'deposit_total': 2500.0,
    'total': 7675.0,
    'paid_total': 3000.0,
    'due_total': 4675.0,
}

sample_items = [
    {'product_name': '6ft Enclosed Trailer', 'product_sku': 'EN6', 'custom_name': None,
     'quantity': 1, 'unit_price': 4500.0, 'line_subtotal': 4500.0, 'line_tax': 675.0, 'line_total': 5175.0},
    {'product_name': 'Tow Hitch Adapter', 'product_sku': 'THA', 'custom_name': None,
     'quantity': 1, 'unit_price': 2500.0, 'line_subtotal': 2500.0, 'line_tax': 0.0, 'line_total': 2500.0},
]

sample_settings = {
    'company_name': 'Sano Trailers',
    'email': 'info@sanotrailers.co.za',
    'phone': '+27 82 123 4567',
    'address_line1': '12 Industrial Road',
    'address_line2': None,
    'city': 'Johannesburg',
    'province': 'Gauteng',
    'postcode': '2001',
}

app = create_app({'TESTING': True})
with app.app_context():
    pdf_bytes = _invoice_template_pdf(sample_document, sample_items, sample_settings, logo_bytes=_document_logo_bytes())
out = ROOT / 'test_invoice.pdf'
out.write_bytes(pdf_bytes)
print('Wrote', out, len(pdf_bytes), 'bytes')

# Extract text positions from the content stream to verify layout
stream = pdf_bytes.decode('latin-1')
streams = re.findall(r'stream\n(.*?)\nendstream', stream, re.DOTALL)
content = next(s for s in streams if ' Tm ' in s and ' Tj' in s)
for cmd in re.finditer(r'/(F\d) ([\d.]+) Tf 1 0 0 1 ([\d.]+) ([\d.]+) Tm \((.*?)\) Tj', content):
    font, size, x, y, text = cmd.group(1), cmd.group(2), float(cmd.group(3)), float(cmd.group(4)), cmd.group(5)
    if text.strip():
        print(f'  x={x:6.1f} y={y:6.1f} font={font} size={size} text={text[:60]}')
