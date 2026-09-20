"""Ticket ABI-341952991 - show the security deposit consumed by a settlement.

Requested edit: "once a deposit is used to cover extra costs, minus the used
amount on the invoice document as well". Clarified as Option A: keep the
existing Security deposit and Paid lines exactly as they are and add a
"Less: deposit used" deduction line between them. Invoices only - a quote never
has a settled deposit - and nothing about the totals maths changes.

Covers both document surfaces (on-screen HTML document view and the PDF).
"""
import os
import re
import tempfile

import pytest

from app import create_app
from app.db import get_db


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
        row = get_db().execute(
            "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
        ).fetchone()
    return client.post('/login', data={'user_id': str(row['id']), 'password': 'admin123'}, follow_redirects=True)


def seed_customer_and_product(client):
    """Product: R200/day rental with a R750 refundable security deposit."""
    client.post('/customers/new', data={
        'customer_type': 'individual',
        'name': 'Deposit Customer',
        'email': 'deposit@example.com',
        'phone': '+27000000000',
    }, follow_redirects=True)
    client.post('/inventory/new', data={
        'name': 'Deposit Trailer',
        'sku': 'DEP-TRL',
        'quantity': '4',
        'description': 'Deposit test trailer.',
        'product_type': 'rental',
        'price_amount': '200',
        'price_unit': 'day',
        'security_deposit': '750',
        'tax_profile_id': '1',
        'active': '1',
        'public_visible': '1',
    }, follow_redirects=True)


def create_order_for_status(client, quantity='1', start_date='2026-07-01', end_date='2026-07-02'):
    res = client.post('/orders/new', data={
        'customer_id': '1',
        'product_id': '1',
        'quantity': quantity,
        'start_date': start_date,
        'start_time': '09:00',
        'end_date': end_date,
        'end_time': '09:00',
        'notes': 'Deposit used document order',
    }, follow_redirects=False)
    assert res.status_code == 302
    return res.headers['Location'].rstrip('/').split('/')[-1]


def order_row(app, order_id):
    with app.app_context():
        return dict(get_db().execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone())


def document_id(app, order_id, document_type='invoice'):
    with app.app_context():
        row = get_db().execute(
            'SELECT id FROM documents WHERE order_id = ? AND document_type = ? ORDER BY id DESC LIMIT 1',
            (order_id, document_type),
        ).fetchone()
    return row['id'] if row else None


def start_and_return_with_invoice(client, app, *, pay_balance_down_to):
    """Pick up, part-pay as asked, finalize the invoice, return, use the deposit.

    The rental balance left after the part payment becomes the amount the
    settlement applies out of the R750 security deposit.
    """
    order_id = create_order_for_status(client)
    assert b'Order reserved' in client.post(f'/orders/{order_id}/reserve', follow_redirects=True).data
    assert b'Order started' in client.post(f'/orders/{order_id}/start', follow_redirects=True).data

    total = order_row(app, order_id)['total']
    part_payment = round(float(total) - pay_balance_down_to, 2)
    if part_payment > 0:
        paid = client.post(f'/orders/{order_id}/payments', data={
            'amount': f'{part_payment:.2f}', 'method': 'cash', 'reference': 'part payment',
        }, follow_redirects=True)
        assert b'Payment recorded' in paid.data

    client.post(f'/orders/{order_id}/return-checklist', data={
        'no_damages': '1', 'no_revision_required': '1',
    }, follow_redirects=True)
    client.post(f'/orders/{order_id}/documents', data={'document_type': 'invoice'}, follow_redirects=True)
    invoice_id = document_id(app, order_id, 'invoice')
    client.post(f'/documents/{invoice_id}/finalize', follow_redirects=True)
    assert b'Order returned' in client.post(f'/orders/{order_id}/return', follow_redirects=True).data
    return order_id, invoice_id


def pdf_text_runs(pdf_bytes):
    """Every drawn text run in the PDF content stream: font, size, x, y, text."""
    streams = re.findall(r'stream\r?\n(.*?)\r?\nendstream', pdf_bytes.decode('latin-1'), re.S)
    content = next(stream for stream in streams if ' Tm ' in stream and ' Tj' in stream)
    return [
        {'font': match.group(1), 'size': float(match.group(2)), 'x': float(match.group(3)),
         'y': float(match.group(4)), 'text': match.group(5)}
        for match in re.finditer(r'/(F\d) ([\d.]+) Tf 1 0 0 1 ([\d.]+) ([\d.]+) Tm \((.*?)\) Tj', content)
    ]


def test_invoice_document_and_pdf_show_the_deposit_used_line(client, app):
    """R750 deposit, R300 of it settled: the invoice shows the R300 deduction."""
    login(client)
    seed_customer_and_product(client)
    order_id, invoice_id = start_and_return_with_invoice(client, app, pay_balance_down_to=300)

    used = client.post(f'/orders/{order_id}/use-deposit', data={'deposit_note': 'Covered extra costs'}, follow_redirects=True)
    assert b'Security deposit used: R300.00; refund R450.00' in used.data

    order = order_row(app, order_id)
    assert order['deposit_total'] == 750
    assert order['deposit_applied_amount'] == 300
    assert order['deposit_refund_amount'] == 450
    # the deposit_applied payment is what makes Paid include the R300
    with app.app_context():
        payment = get_db().execute(
            "SELECT amount, status FROM payments WHERE order_id = ? AND method = 'deposit_applied'",
            (order_id,),
        ).fetchone()
    assert payment['amount'] == 300 and payment['status'] == 'paid'

    # --- on-screen document view -------------------------------------------------
    html = client.get(f'/documents/{invoice_id}').data.decode('utf-8')
    assert 'Less: deposit used' in html
    assert '-R300.00' in html
    # the lines the client asked to keep are untouched
    assert 'Security deposit (refundable)' in html and 'R750.00' in html
    assert html.index('Security deposit (refundable)') < html.index('Less: deposit used') < html.index('Amount due')

    # --- PDF ---------------------------------------------------------------------
    from app.services.pdf_documents import document_pdf_bytes

    with app.app_context():
        runs = pdf_text_runs(document_pdf_bytes(invoice_id))
    labels = {run['text']: run for run in runs}
    assert 'Less: deposit used' in labels
    deduction = labels['Less: deposit used']

    # the deduction carries the minus sign and sits between the deposit and Paid
    deduction_amount = next(run for run in runs if run['y'] == deduction['y'] and run['text'] == '-R300.00')
    assert deduction_amount['x'] == 470
    deposit_run = labels['Security deposit']
    paid_run = labels['Paid']
    assert deposit_run['y'] == deduction['y'] + 14
    assert paid_run['y'] == deduction['y'] - 14
    # Amount due and the banking block stay on the page
    assert labels['Amount due']['y'] > 40
    assert 'Banking details' in labels


def test_quote_document_never_shows_the_deposit_used_line(client, app):
    """A quote has no settled deposit, so the deduction line must not appear."""
    login(client)
    seed_customer_and_product(client)
    order_id, invoice_id = start_and_return_with_invoice(client, app, pay_balance_down_to=300)
    used = client.post(f'/orders/{order_id}/use-deposit', data={'deposit_note': 'Covered extra costs'}, follow_redirects=True)
    assert b'Security deposit used: R300.00; refund R450.00' in used.data

    client.post(f'/orders/{order_id}/documents', data={'document_type': 'quote'}, follow_redirects=True)
    quote_id = document_id(app, order_id, 'quote')
    assert quote_id

    html = client.get(f'/documents/{quote_id}').data.decode('utf-8')
    assert 'Less: deposit used' not in html
    assert '-R300.00' not in html

    from app.services.pdf_documents import document_pdf_bytes

    with app.app_context():
        pdf = document_pdf_bytes(quote_id).decode('latin-1')
    assert 'Less: deposit used' not in pdf


def test_invoice_without_a_used_deposit_renders_unchanged(client, app):
    """deposit_applied_amount = 0 -> no new line on the document or the PDF."""
    login(client)
    seed_customer_and_product(client)
    order_id, invoice_id = start_and_return_with_invoice(client, app, pay_balance_down_to=0)
    assert order_row(app, order_id)['deposit_applied_amount'] == 0

    html = client.get(f'/documents/{invoice_id}').data.decode('utf-8')
    assert 'Less: deposit used' not in html
    assert 'Security deposit (refundable)' in html

    from app.services.pdf_documents import document_pdf_bytes

    with app.app_context():
        runs = pdf_text_runs(document_pdf_bytes(invoice_id))
    assert not any('Less: deposit used' in run['text'] for run in runs)
    # the pre-existing totals rows are all still drawn
    drawn = [run['text'] for run in runs]
    for expected in ('Security deposit', 'Paid', 'Amount due', 'Banking details'):
        assert expected in drawn


def test_invoice_template_tolerates_a_document_without_the_new_key(app):
    """The template reads the new field defensively, so a missing key is safe."""
    from app.services.pdf_documents import _invoice_template_pdf

    document = {
        'document_type': 'invoice', 'status': 'finalized', 'number': 'INV-1',
        'order_number': 'ORD-1', 'created_at': '2026-09-15T15:00:00',
        'start_at': '2026-09-15T15:00:00', 'end_at': '2026-09-16T15:00:00',
        'branch_name': '', 'branch_email': '', 'branch_phone': '',
        'branch_address_line1': '', 'branch_address_line2': '', 'branch_city': '',
        'branch_province': '', 'branch_postal_code': '',
        'branch_bank_name': 'CAPITEC BUSINESS', 'branch_bank_account_name': 'Sano',
        'branch_bank_account_number': '1053', 'branch_bank_branch_code': '450105',
        'branch_bank_account_type': 'Cheque', 'branch_bank_reference_note': 'Order No.',
        'customer_name': 'Test Customer', 'customer_email': '', 'customer_phone': '',
        'customer_address_line1': '', 'customer_address_line2': '', 'customer_suburb': '',
        'customer_city': '', 'customer_province': '', 'customer_postal_code': '',
        'customer_country': '', 'custom_fields_json': '',
        'deposit_option': 'security_deposit', 'discount_total': 0.0, 'discount_mode': '',
        'discount_value': 0.0, 'subtotal': 200.0, 'tax_total': 0.0, 'deposit_total': 750.0,
        'total': 950.0, 'paid_total': 950.0, 'due_total': 0.0, 'payment_status': 'paid',
    }
    settings = {
        'company_name': 'Sano Trailers', 'email': '', 'phone': '',
        'address_line1': '', 'address_line2': '', 'city': '', 'province': '', 'postcode': '',
    }
    with app.app_context():
        pdf = _invoice_template_pdf(document, [], settings)
    assert pdf.startswith(b'%PDF-')
    assert b'Less: deposit used' not in pdf


@pytest.mark.parametrize('applied,expected', [(0, False), (300, True)])
def test_document_service_exposes_the_applied_amount(app, applied, expected):
    """get_document() has to carry deposit_applied_amount for the templates."""
    from app.services.documents import get_document

    with app.app_context():
        db = get_db()
        db.execute(
            """INSERT INTO orders (order_number, booking_type, collect_branch_id, return_branch_id,
            status, payment_status, start_at, end_at, subtotal, tax_total, deposit_total,
            deposit_applied_amount, deposit_refund_amount, total, due_total, notes, created_at)
            VALUES ('ORD-T1', 'return', 1, 1, 'returned', 'paid', '2026-09-15T09:00:00',
            '2026-09-16T09:00:00', 200, 0, 750, ?, ?, 950, 0, '', '2026-09-15T09:00:00')""",
            (applied, 750 - applied),
        )
        order_id = db.execute("SELECT id FROM orders WHERE order_number = 'ORD-T1'").fetchone()['id']
        db.execute(
            """INSERT INTO documents (order_id, document_type, status, number, pdf_path, created_at)
            VALUES (?, 'invoice', 'draft', '', '', '2026-09-15T09:00:00')""",
            (order_id,),
        )
        db.commit()
        document_id = db.execute('SELECT id FROM documents WHERE order_id = ?', (order_id,)).fetchone()['id']
        document = get_document(document_id)

    assert float(document['deposit_applied_amount'] or 0) == applied
    assert bool(float(document['deposit_applied_amount'] or 0)) is expected
