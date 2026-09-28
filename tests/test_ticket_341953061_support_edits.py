"""Ticket ABI-341953061 support edits."""
import os
import tempfile

import pytest

from app import create_app
from app.db import get_db
from app.services.access import module_for_endpoint
from app.services.pdf_documents import _invoice_template_pdf, INVOICE_TABLE_RIGHT_EDGE
from tests.test_pdf_text_encoding import _drawn_text_positions, _sample_document, _sample_settings


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


def login_owner(client):
    with client.application.app_context():
        owner = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
    return client.post('/login', data={'user_id': str(owner['id']), 'password': 'admin123'}, follow_redirects=True)


def add_staff_and_login(client, app):
    login_owner(client)
    client.post('/settings/users/add', data={'name': 'Ticket Staff', 'password': 'staff123', 'branch_id': '1'}, follow_redirects=True)
    with app.app_context():
        staff = get_db().execute("SELECT id FROM users WHERE name = 'Ticket Staff'").fetchone()
    client.post('/logout')
    return client.post('/login', data={'user_id': str(staff['id']), 'password': 'staff123'}, follow_redirects=True)


def insert_order(app, number='ORD-61001', status='returned', deposit=0, refund=0, method=''):
    with app.app_context():
        db = get_db()
        db.execute(
            """INSERT INTO customers (name, email, phone, created_at)
            VALUES ('Ticket Customer', 'ticket@example.test', '+27000000000', '2026-09-27T08:00:00')"""
        )
        customer_id = db.execute("SELECT id FROM customers WHERE email = 'ticket@example.test'").fetchone()['id']
        db.execute(
            """INSERT INTO orders (order_number, customer_id, booking_type, collect_branch_id, return_branch_id,
            status, payment_status, start_at, end_at, subtotal, tax_total, deposit_total,
            deposit_refund_amount, deposit_process_method, total, due_total, created_at)
            VALUES (?, ?, 'return', 1, 1, ?, 'payment_due', '2026-09-27T09:00:00', '2026-09-28T09:00:00',
            100, 15, ?, ?, ?, 115, 115, '2026-09-27T08:00:00')""",
            (number, customer_id, status, deposit, refund, method),
        )
        order_id = db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()['id']
        db.commit()
        return order_id


def status_of(app, order_id):
    with app.app_context():
        return get_db().execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()['status']


def test_archive_button_and_post_are_main_user_only(client, app):
    order_id = insert_order(app)
    login_owner(client)
    owner_page = client.get(f'/orders/{order_id}').get_data(as_text=True)
    assert 'Archive order' in owner_page

    add_staff_and_login(client, app)
    staff_page = client.get(f'/orders/{order_id}').get_data(as_text=True)
    assert 'Archive order' not in staff_page
    response = client.post(f'/orders/{order_id}/archive')
    assert response.status_code == 403
    assert status_of(app, order_id) == 'returned'


def test_cancelled_order_shows_partial_full_refund_settlement_copy(client, app):
    order_id = insert_order(app, status='canceled', deposit=750)
    login_owner(client)
    html = client.get(f'/orders/{order_id}').get_data(as_text=True)
    assert 'name="deposit_refund_amount"' in html
    assert 'value="750.00"' in html
    assert 'Refund Deposit (partial/full)' in html
    assert 'Canceled orders can still be settled here for a partial or full refund.' in html


def test_cancelled_order_can_record_partial_and_full_deposit_refund(client, app):
    order_id = insert_order(app, status='canceled', deposit=750)
    login_owner(client)

    partial = client.post(f'/orders/{order_id}/settle-return', data={
        'deposit_refund_amount': '300.00',
        'deposit_process_method': 'cash',
        'deposit_processed_at': '',
        'deposit_note': 'Partial cancel refund',
    }, follow_redirects=True)
    assert b'Deposit settled: R450.00 used; R300.00 refunded' in partial.data
    with app.app_context():
        row = get_db().execute(
            """SELECT deposit_applied_amount, deposit_refund_amount, deposit_process_method,
            payment_status FROM orders WHERE id = ?""",
            (order_id,),
        ).fetchone()
        payment = get_db().execute(
            "SELECT amount, status FROM payments WHERE order_id = ? AND method = 'deposit_applied'",
            (order_id,),
        ).fetchone()
    assert row['deposit_applied_amount'] == 450
    assert row['deposit_refund_amount'] == 300
    assert row['deposit_process_method'] == 'cash'
    assert payment['amount'] == 450 and payment['status'] == 'paid'

    full = client.post(f'/orders/{order_id}/settle-return', data={
        'deposit_refund_amount': '750.00',
        'deposit_process_method': 'eft',
        'deposit_processed_at': '',
        'deposit_note': 'Full cancel refund',
    }, follow_redirects=True)
    assert b'Deposit refund processed: R750.00' in full.data
    with app.app_context():
        row = get_db().execute(
            "SELECT deposit_applied_amount, deposit_refund_amount, deposit_process_method FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        payment = get_db().execute(
            "SELECT amount, status FROM payments WHERE order_id = ? AND method = 'deposit_applied' ORDER BY id DESC LIMIT 1",
            (order_id,),
        ).fetchone()
    assert row['deposit_applied_amount'] == 0
    assert row['deposit_refund_amount'] == 750
    assert row['deposit_process_method'] == 'eft'
    assert payment['status'] == 'archived'


def test_reports_page_downloads_pdf(client):
    login_owner(client)
    page = client.get('/reports').get_data(as_text=True)
    assert 'Download PDF report' in page
    response = client.get('/reports/report.pdf')
    assert response.status_code == 200
    assert response.mimetype == 'application/pdf'
    assert response.data.startswith(b'%PDF-')
    assert 'attachment; filename=abi-report.pdf' in response.headers['Content-Disposition']


def test_reports_pdf_carries_the_reports_module_gate(client, app):
    """Ticket ABI-341953061(4): the print route is not a way around the module."""
    assert module_for_endpoint('admin.reports_pdf') == 'reports'
    add_staff_and_login(client, app)          # staff default: no reports module
    assert client.get('/reports').status_code == 403
    assert client.get('/reports/report.pdf').status_code == 403


def test_documents_index_has_inline_view_pdf_button(client, app):
    order_id = insert_order(app)
    with app.app_context():
        db = get_db()
        db.execute(
            """INSERT INTO documents (order_id, document_type, status, number, pdf_path, created_at)
            VALUES (?, 'invoice', 'finalized', 'INV-61001', '', '2026-09-27T10:00:00')""",
            (order_id,),
        )
        document_id = db.execute("SELECT id FROM documents WHERE number = 'INV-61001'").fetchone()['id']
        db.commit()
    login_owner(client)
    html = client.get('/documents').get_data(as_text=True)
    assert f'/documents/{document_id}/download.pdf?view=1' in html
    assert 'View PDF' in html


def test_pdf_money_values_are_right_aligned_and_banking_block_moved_down(app):
    document = _sample_document('invoice')
    document.update({'subtotal': 1000.0, 'tax_total': 150.0, 'total': 1150.0, 'due_total': 1150.0})
    item = {'product_name': 'Right align item', 'product_sku': '', 'custom_name': '', 'quantity': 1,
            'unit_price': 1000.0, 'line_subtotal': 1000.0, 'line_tax': 150.0, 'line_total': 1150.0}
    with app.app_context():
        pdf = _invoice_template_pdf(document, [item], _sample_settings())
    positions = _drawn_text_positions(pdf)
    money = [entry for entry in positions if entry['text'] in {'R1000.00', 'R150.00', 'R1150.00'}]
    assert money
    assert all(entry['x'] < INVOICE_TABLE_RIGHT_EDGE for entry in money)
    assert max(entry['x'] for entry in money if entry['text'] == 'R1150.00') > 515
    thank_you = next(entry for entry in positions if entry['text'] == 'Thank you for your business.')
    banking = next(entry for entry in positions if entry['text'] == 'Banking details')
    total_without_vat = next(entry for entry in positions if entry['text'] == 'Total without VAT')
    # Ticket ABI-341953072(2): the client asked for space between the thank-you
    # line and the banking block, so the thank-you line moved up to make room. It
    # still closes the money block above it (the totals sit to its right) and
    # still opens the banking block below it.
    assert thank_you['y'] < total_without_vat['y']
    assert banking['y'] < thank_you['y']
    assert thank_you['y'] - banking['y'] >= 20


def test_order_detail_discount_label_stays_on_percent_amount_line(client, app):
    order_id = insert_order(app, status='returned')
    login_owner(client)
    html = client.get(f'/orders/{order_id}').get_data(as_text=True)
    # Ticket ABI-341953061(5): the word "Discount" must not push the percentage /
    # amount controls onto another line, and the row must keep all three parts
    # (label | controls | amount) on one line.
    #
    # The wrapper span this ticket originally added to achieve that
    # (`<span class="discount-label-wrap">` around the label AND the form) was itself
    # the bug: it left the row with two items instead of three, so the amount was
    # painted over the number field and the Apply button and the discount could not
    # be entered at all. The row is a wrapping flex line whose direct children ARE
    # the label, the form and the amount, so the intent holds without the wrapper.
    assert '<span>Discount</span>' in html
    assert 'discount-label-wrap' not in html
    assert '.totals .discount-row{display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px}' in html
