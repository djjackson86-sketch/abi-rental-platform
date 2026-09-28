"""Ticket ABI-341953072 visual proof: render the invoice template and LOOK at it.

Writes /tmp/abi372_invoice.pdf (page 1 beside the totals) and a PNG next to it so
the thank-you/banking gap and the totals alignment can be checked by eye rather
than from coordinates.

Run:  .venv/bin/python scripts/abi_372_visual_proof.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.services.pdf_documents import _invoice_template_pdf  # noqa: E402

DOCUMENT = {
    'document_type': 'invoice', 'status': 'finalized', 'number': 'INV-341953072',
    'order_number': 'ORD-341953072', 'created_at': '2026-09-27T10:00:00',
    'start_at': '2026-09-27T10:30:00', 'end_at': '2026-09-29T10:30:00',
    'branch_name': 'Sano Trailers', 'branch_email': 'info@sanotrailers.co.za',
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
    'subtotal': 600.0, 'tax_total': 90.0, 'deposit_total': 750.0,
    'discount_total': 0.0, 'total': 690.0, 'paid_total': 690.0, 'due_total': 0.0,
    'payment_status': 'paid',
}

ITEMS = [
    {'product_name': '2.4m Utility Trailer - 1/2 ton', 'product_sku': 'UTIL-24',
     'custom_name': '', 'quantity': 1, 'unit_price': 600.0, 'line_subtotal': 600.0,
     'line_tax': 90.0, 'line_total': 690.0},
    {'product_name': 'Ratchet + Strap Rental', 'product_sku': '', 'custom_name': '',
     'quantity': 2, 'unit_price': 0.0, 'line_subtotal': 0.0, 'line_tax': 0.0,
     'line_total': 0.0},
]

SETTINGS = {
    'company_name': 'Sano Trailers', 'email': 'info@sanotrailers.co.za',
    'phone': '+27 82 123 4567', 'address_line1': '12 Industrial Road',
    'address_line2': None, 'city': 'Johannesburg', 'province': 'Gauteng',
    'postcode': '2001',
}

app = create_app({'TESTING': True, 'DATABASE': '/tmp/abi372_proof.db', 'SECRET_KEY': 'proof'})
with app.app_context():
    pdf_bytes = _invoice_template_pdf(DOCUMENT, ITEMS, SETTINGS)

out = Path('/tmp/abi372_invoice.pdf')
out.write_bytes(pdf_bytes)
png = out.with_suffix('.png')
subprocess.run(['gs', '-q', '-dNOPAUSE', '-dBATCH', '-sDEVICE=png16m', '-r150',
                f'-sOutputFile={png}', str(out)], check=True)
print(f'wrote {out} ({len(pdf_bytes)} bytes) and {png}')
print(subprocess.run(['gs', '-q', '-dNOPAUSE', '-dBATCH', '-sDEVICE=bbox', str(out)],
                     capture_output=True, text=True).stderr.strip()[:400])
