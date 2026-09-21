"""Ticket ABI-341953022 - items added by an order edit must print on every PDF.

Requested edit: "fix error on documents where after editing the order, the new
items don't show on the pdf documents".

Cause: `_invoice_template_pdf()` sliced the line items (`items[:8]`) and pushed
everything past row 6 onto ONE continuation page, so an order edited up to ten
lines printed only eight of them on the invoice and quote PDFs while the
on-screen document showed all ten (and the totals - which are computed over the
full list - then disagreed with the rows the reader could see). The table now
paginates until the list is exhausted. The same defect existed deeper in the
plain contract/packing-slip renderer, which stopped drawing at the page floor
and lost its tail on very long orders; that path paginates too.

Every assertion below reads the drawn PDF content streams page by page - a byte
search alone cannot tell "the row is on the page" from "the row is printed over
something else".
"""
import os
import re
import tempfile
import json
from decimal import Decimal

import pytest
from werkzeug.datastructures import MultiDict

from app import create_app
from app.db import get_db
from app.services.pdf_documents import (
    DAYS_COLUMN_X,
    INVOICE_TABLE_X,
    QTY_COLUMN_X,
    RATE_COLUMN_X,
    SUBTOTAL_COLUMN_X,
    TAX_COLUMN_X,
    TOTAL_INCL_COLUMN_X,
)

BASE_PRICE = 100.0
PRICE_STEP = 5.0
ROW_PITCH = 43
CONTINUATION_TABLE_Y = 675
TAX_RATE = 15


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


def item_name(index):
    """Indices are 1-based; keep the names short enough for one wrapped line."""
    return f'Extra Item {index}'


def item_price(index):
    return BASE_PRICE + (PRICE_STEP * index)


def seed_customer_and_products(client, count):
    customer = client.post('/customers/new', data={
        'customer_type': 'individual',
        'name': 'Pagination Customer',
        'email': 'pagination@example.com',
        'phone': '+27000000000',
    }, follow_redirects=True)
    assert customer.status_code == 200
    for index in range(1, count + 1):
        created = client.post('/inventory/new', data={
            'name': item_name(index),
            'sku': f'PAG-{index:03d}',
            'description': 'Item added after the documents were created.',
            'product_type': 'sale',
            'price_amount': f'{item_price(index):.2f}',
            'price_unit': 'each',
            'security_deposit': '0',
            'quantity': '200',
            'tax_profile_id': '1',
            'active': '1',
            'public_visible': '1',
        }, follow_redirects=True)
        assert created.status_code == 200


def create_order(client, lines, notes='Pagination ticket order'):
    """lines: [(product_id, quantity), ...] posted the way the order form does."""
    payload = [('customer_id', '1'), ('booking_type', 'return'),
               ('collect_branch_id', '1'), ('return_branch_id', '1'),
               ('deposit_option', 'security_deposit'),
               ('start_date', '2026-07-01'), ('start_time', '09:00'),
               ('end_date', '2026-07-03'), ('end_time', '15:00'),
               ('notes', notes)]
    for product_id, quantity in lines:
        payload += [('product_id', str(product_id)), ('quantity', str(quantity))]
    res = client.post('/orders/new', data=MultiDict(payload), follow_redirects=False)
    assert res.status_code == 302
    return res.headers['Location'].rstrip('/').split('/')[-1]


def edit_order(client, order_id, lines):
    """Replace the order's lines the way the edit form posts them."""
    payload = [('customer_id', '1'), ('booking_type', 'return'),
               ('collect_branch_id', '1'), ('return_branch_id', '1'),
               ('deposit_option', 'security_deposit'),
               ('start_date', '2026-07-01'), ('start_time', '09:00'),
               ('end_date', '2026-07-03'), ('end_time', '15:00'),
               ('notes', 'Pagination ticket order edited')]
    for product_id, quantity in lines:
        payload += [('product_id', str(product_id)), ('quantity', str(quantity))]
    edited = client.post(f'/orders/{order_id}/edit', data=MultiDict(payload), follow_redirects=True)
    assert b'Order saved' in edited.data
    return edited


def create_documents(client, app, order_id, document_types=('invoice', 'quote', 'contract')):
    documents = {}
    for document_type in document_types:
        created = client.post(f'/orders/{order_id}/documents', data={'document_type': document_type}, follow_redirects=True)
        assert created.status_code == 200
        with app.app_context():
            row = get_db().execute(
                'SELECT id FROM documents WHERE order_id = ? AND document_type = ? ORDER BY id DESC LIMIT 1',
                (order_id, document_type),
            ).fetchone()
        documents[document_type] = row['id']
    return documents


def download_pdf(client, document_id):
    response = client.get(f'/documents/{document_id}/download.pdf')
    assert response.status_code == 200
    assert response.data.startswith(b'%PDF-')
    return response.data


def order_items(app, order_id):
    with app.app_context():
        rows = get_db().execute(
            'SELECT COALESCE(p.name, oi.custom_name) AS product_name, oi.quantity, oi.unit_price, '
            'oi.line_subtotal, oi.line_tax, oi.line_total '
            'FROM order_items oi LEFT JOIN products p ON p.id = oi.product_id '
            'WHERE oi.order_id = ? ORDER BY oi.id',
            (order_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def unescape_drawn_text(text):
    """PDF escapes ( ) and \\ inside a text string - undo it for comparison."""
    return re.sub(r'\\([()\\])', r'\1', text)


def pdf_pages(pdf_bytes):
    """The drawn text runs and rectangles of every page, page by page.

    Only the content streams carry text - the image stream does not - so a
    document's page count here matches the page objects in the file.
    """
    pages = []
    for stream in re.findall(r'stream\r?\n(.*?)\r?\nendstream', pdf_bytes.decode('latin-1'), re.S):
        if ' Tm ' not in stream or ' Tj' not in stream:
            continue
        runs = [
            {'font': match.group(1), 'size': float(match.group(2)), 'x': float(match.group(3)),
             'y': float(match.group(4)), 'text': unescape_drawn_text(match.group(5))}
            for match in re.finditer(r'/(F\d) ([\d.]+) Tf 1 0 0 1 ([\d.]+) ([\d.]+) Tm \((.*?)\) Tj', stream)
        ]
        rects = [
            {'x': float(match.group(1)), 'y': float(match.group(2)),
             'width': float(match.group(3)), 'height': float(match.group(4))}
            for match in re.finditer(r'([\d.]+) ([\d.]+) ([\d.]+) ([\d.]+) re', stream)
        ]
        pages.append({'runs': runs, 'rects': rects})
    return pages


def row_runs(page):
    """Item rows on a page, top to bottom: (name, qty, rate, subtotal, tax, total)."""
    by_y = {}
    for run in page['runs']:
        by_y.setdefault(run['y'], []).append(run)
    rows = []
    for y in sorted(by_y, reverse=True):
        runs = [run for run in by_y[y] if abs(run['x'] - INVOICE_TABLE_X) < 0.01]
        names = [run for run in runs if run['text'].startswith('Extra Item')]
        if not names:
            continue
        columns = {round(run['x']): run['text'] for run in by_y[y]}
        rows.append({
            'y': y,
            'name': names[0]['text'],
            'qty': columns.get(round(QTY_COLUMN_X)),
            'rate': columns.get(round(RATE_COLUMN_X)),
            'subtotal': columns.get(round(SUBTOTAL_COLUMN_X)),
            'tax': columns.get(round(TAX_COLUMN_X)),
            'total': columns.get(round(TOTAL_INCL_COLUMN_X)),
        })
    return rows


def all_rows(pages):
    return [row for page in pages for row in row_runs(page)]


def money(value):
    return f'R{value:.2f}'


def summary_amount(pages, label):
    """The amount drawn on the same line as a summary label."""
    for page in pages:
        for run in page['runs']:
            if run['text'] == label:
                for other in page['runs']:
                    if other['y'] == run['y'] and other['x'] > run['x']:
                        return other['text']
    return None


def test_edited_order_items_all_print_on_the_invoice_and_quote_pdfs(client, app):
    """The reported case: docs exist, the order grows to ten lines, all ten print."""
    login(client)
    seed_customer_and_products(client, 10)
    order_id = create_order(client, [(1, 1)])
    documents = create_documents(client, app, order_id)

    edit_order(client, order_id, [(index, 1) for index in range(1, 11)])
    assert len(order_items(app, order_id)) == 10

    detail = client.get(f'/documents/{documents["invoice"]}').data.decode('utf-8')
    assert all(item_name(index) in detail for index in range(1, 11)), 'on-screen view lost a line'

    for document_type in ('invoice', 'quote', 'contract'):
        pdf = download_pdf(client, documents[document_type])
        pages = pdf_pages(pdf)
        printed = [row['name'] for row in all_rows(pages)]
        if document_type == 'contract':
            text = pdf.decode('latin-1')
            printed = [item_name(index) for index in range(1, 11) if f'({item_name(index)} x 1 @' in text]
        assert printed == [item_name(index) for index in range(1, 11)], (
            f'{document_type} PDF is missing edited lines: {printed}')
        # the two rows the old eight-item cap dropped (ticket's reported symptom)
        assert item_name(9) in printed and item_name(10) in printed


def test_every_edited_line_prints_its_own_qty_rate_and_totals(client, app):
    """Row level, not name level: qty, rate, subtotal, tax and incl. total per line."""
    login(client)
    seed_customer_and_products(client, 12)
    with app.app_context():
        db = get_db()
        db.execute(f'UPDATE tax_profiles SET rate = {TAX_RATE} WHERE id = 1')
        db.execute("UPDATE company_settings SET tax_mode = 'exclusive' WHERE id = 1")
        db.commit()

    order_id = create_order(client, [(1, 1)])
    documents = create_documents(client, app, order_id, ('invoice', 'quote'))
    lines = [(index, 2 if index == 3 else 1) for index in range(1, 13)]
    edit_order(client, order_id, lines)
    items = order_items(app, order_id)
    assert len(items) == 12

    for document_type, document_id in documents.items():
        pages = pdf_pages(download_pdf(client, document_id))
        rows = all_rows(pages)
        assert len(rows) == 12, f'{document_type}: {len(rows)} item rows drawn, expected 12'
        assert [row['name'] for row in rows] == [item_name(index) for index in range(1, 13)]

        for row, item in zip(rows, items):
            assert row['qty'] == str(item['quantity']), f'{row["name"]} quantity'
            assert row['rate'] == money(item['unit_price']), f'{row["name"]} rate'
            assert row['subtotal'] == money(item['line_subtotal']), f'{row["name"]} subtotal'
            assert row['tax'] == money(item['line_tax']), f'{row["name"]} tax'
            assert row['total'] == money(item['line_subtotal'] + item['line_tax']), f'{row["name"]} incl. total'

        # the duplicate-quantity line really is quantity 2, and its money doubles
        third = next(row for row in rows if row['name'] == item_name(3))
        assert third['qty'] == '2'
        assert third['total'] == money(item_price(3) * 2 * (1 + TAX_RATE / 100))

        # the summary must reconcile with the rows the reader can see
        drawn_net = sum(Decimal(row['subtotal'][1:]) for row in rows)
        drawn_tax = sum(Decimal(row['tax'][1:]) for row in rows)
        assert summary_amount(pages, 'Total without VAT') == money(float(drawn_net))
        assert summary_amount(pages, f'VAT ({TAX_RATE}%)') == money(float(drawn_tax))
        assert summary_amount(pages, 'Total with VAT') == money(float(drawn_net + drawn_tax))
        assert summary_amount(pages, 'Amount due') == summary_amount(pages, 'Total with VAT')


def test_page_one_fills_available_space_and_continuation_pages_repeat_the_headings(client, app):
    """Page 1 should not leave two blank rows before jumping to page 2."""
    login(client)
    seed_customer_and_products(client, 25)
    order_id = create_order(client, [(1, 1)])
    documents = create_documents(client, app, order_id, ('invoice',))
    edit_order(client, order_id, [(index, 1) for index in range(1, 26)])

    pages = pdf_pages(download_pdf(client, documents['invoice']))
    assert len(pages) > 2
    per_page = [len(row_runs(page)) for page in pages]
    assert per_page[0] > 6, f'page 1 should use its available space, got {per_page[0]}'
    assert sum(per_page) == 25
    assert all(count > 0 for count in per_page[:3]), per_page

    headings = ['PRODUCT', 'QTY', 'DAYS', 'RATE', 'SUBTOTAL', 'TAX', 'TOTAL INCL. VAT']
    for number, page in enumerate(pages, start=1):
        drawn = [run['text'] for run in page['runs']]
        for heading in headings:
            assert heading in drawn, f'page {number} lost the {heading} heading'
        if number == 1:
            assert 'Page 1' not in drawn
        else:
            assert f'Page {number}' in drawn
        # every continuation page starts its table at the same y as before the fix
        first_row = row_runs(page)[0] if row_runs(page) else None
        if first_row and number > 1:
            assert first_row['y'] == CONTINUATION_TABLE_Y - 24, f'page {number} table top moved'
        rows = row_runs(page)
        for upper, lower in zip(rows, rows[1:]):
            assert round(upper['y'] - lower['y']) == ROW_PITCH, 'row pitch changed'


def test_summary_and_banking_stay_on_the_page_with_the_last_item_rows(client, app):
    """Crowded pages must not push the totals off the page or print over a row."""
    login(client)
    seed_customer_and_products(client, 25)
    order_id = create_order(client, [(1, 1)])
    documents = create_documents(client, app, order_id, ('invoice', 'quote'))
    edit_order(client, order_id, [(index, 1) for index in range(1, 26)])

    for document_type, document_id in documents.items():
        pages = pdf_pages(download_pdf(client, document_id))
        banking_page = None
        summary_page = None
        for number, page in enumerate(pages, start=1):
            drawn = [run['text'] for run in page['runs']]
            rows = row_runs(page)
            summary = [run for run in page['runs']
                       if run['text'] in ('Total without VAT', 'Total with VAT', 'Amount due', 'Paid')]
            boxes = [rect for rect in page['rects'] if abs(rect['x'] - 382) < 0.5 and abs(rect['width'] - 177) < 0.5]
            if summary:
                summary_page = number
                assert boxes, f'{document_type} page {number}: summary drawn without its box'
                lowest_item_y = min(row['y'] for row in rows)
                box_top = max(rect['y'] + rect['height'] for rect in boxes)
                assert lowest_item_y - 2 > box_top, (
                    f'{document_type} page {number}: item row ink at {lowest_item_y} overlaps the '
                    f'summary box top at {box_top}')
                assert min(run['y'] for run in summary) - 2 >= 58, 'summary row pushed below the page floor'
            if 'Banking details' in drawn:
                banking_page = number
                banking = [run for run in page['runs']
                           if run['text'] in ('Banking details', 'Thank you for your business.')]
                assert min(run['y'] for run in banking) - 2 >= 58, 'banking block pushed below the page floor'
            # nothing may be drawn under the page floor except the page number/headers
            assert all(run['y'] > 40 for run in page['runs']), f'{document_type} page {number}: text below the floor'
        assert summary_page is not None, f'{document_type}: totals missing entirely'
        assert banking_page is not None, f'{document_type}: banking details missing entirely'


def test_short_orders_keep_their_exact_pre_fix_layout(client, app):
    """Documents that already fitted must not move: page 1 still ends at the last row."""
    login(client)
    seed_customer_and_products(client, 8)
    order_id = create_order(client, [(1, 1)])
    documents = create_documents(client, app, order_id, ('invoice',))

    for count, expected_pages in ((2, 1), (6, 1), (8, 1)):
        edit_order(client, order_id, [(index, 1) for index in range(1, count + 1)])
        pages = pdf_pages(download_pdf(client, documents['invoice']))
        assert len(pages) == expected_pages, f'{count} items: {len(pages)} pages'
        rows = all_rows(pages)
        assert [row['name'] for row in rows] == [item_name(index) for index in range(1, count + 1)]
        if expected_pages > 1:
            # unchanged continuation geometry: first row at 675-24, second 43pt lower
            page_two = row_runs(pages[1])
            assert [row['y'] for row in page_two[:2]] == [CONTINUATION_TABLE_Y - 24, CONTINUATION_TABLE_Y - 24 - ROW_PITCH]
            # the summary still trails the last row by exactly 12pt + one row pitch
            assert summary_amount(pages, 'Total without VAT') is not None
            summary_y = next(run['y'] for run in pages[1]['runs'] if run['text'] == 'Total without VAT')
            assert page_two[-1]['y'] - ROW_PITCH - 12 == summary_y
        else:
            assert 'Page 2' not in [run['text'] for run in pages[0]['runs']]


def test_invoice_with_vehicle_details_and_eight_items_prints_cleanly(client, app):
    """Client regression: vehicle/customer detail blocks must not break an 8-item invoice."""
    login(client)
    seed_customer_and_products(client, 8)
    with app.app_context():
        get_db().execute(
            'UPDATE customers SET custom_fields_json = ? WHERE id = 1',
            (json.dumps({
                'vehicle_make': 'Toyota Quantum',
                'vehicle_color': 'White',
                'vehicle_reg_no': 'LKL 455 NW',
                'alternative_contact_name': 'Sbu',
                'alternative_contact_number': '0761492049',
            }),),
        )
        get_db().commit()
    order_id = create_order(client, [(index, 1) for index in range(1, 9)])
    documents = create_documents(client, app, order_id, ('invoice',))

    pages = pdf_pages(download_pdf(client, documents['invoice']))
    drawn_text = [run['text'] for page in pages for run in page['runs']]
    assert 'Vehicle Make: Toyota Quantum' in drawn_text
    assert 'Vehicle Color: White' in drawn_text
    assert 'Veh Reg No: LKL 455 NW' in drawn_text
    assert 'Alternative Contact Name: Sbu' in drawn_text
    assert [row['name'] for row in all_rows(pages)] == [item_name(index) for index in range(1, 9)]
    assert summary_amount(pages, 'Amount due') is not None
    assert 'Banking details' in drawn_text

    for page in pages:
        rows = row_runs(page)
        summary = [run for run in page['runs'] if run['text'] in ('Total without VAT', 'Amount due')]
        if rows and summary:
            assert min(row['y'] for row in rows) - 2 > max(run['y'] for run in summary)
        assert all(run['y'] > 40 for run in page['runs'])


def test_contract_prints_every_line_of_a_long_order(client, app):
    """The contract renderer used to stop at the page floor and lose its tail."""
    login(client)
    seed_customer_and_products(client, 40)
    order_id = create_order(client, [(1, 1)])
    documents = create_documents(client, app, order_id, ('contract', 'packing_slip'))
    edit_order(client, order_id, [(index, 1) for index in range(1, 41)])

    for document_type, document_id in documents.items():
        text = download_pdf(client, document_id).decode('latin-1')
        printed = [index for index in range(1, 41) if f'({item_name(index)} x 1 @' in text]
        assert printed == list(range(1, 41)), f'{document_type} lost lines: missing {set(range(1, 41)) - set(printed)}'
        assert f'({item_name(40)} x 1 @ R{item_price(40):.2f} = R{item_price(40):.2f}) Tj' in text
