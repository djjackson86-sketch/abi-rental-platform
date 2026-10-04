import json
import os
import re
import tempfile
from datetime import datetime

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from app.db import get_db
from app.services import reports as reports_service


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    app = create_app({
        'TESTING': True,
        'DATABASE': path,
        'SECRET_KEY': 'test',
        'ADMIN_EMAIL': 'admin@abi.local',
        'ADMIN_PASSWORD': 'admin123',
    })
    yield app
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client, name=None, password='admin123'):
    with client.application.app_context():
        if name is None:
            row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
        else:
            row = get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
        user_id = row['id'] if row else None
    return client.post('/login', data={'user_id': str(user_id), 'password': password}, follow_redirects=True)


def pdf_streams(pdf_bytes):
    """The decoded page-content streams (the text between ``stream``/``endstream``)."""
    streams = []
    for chunk in pdf_bytes.split(b'endstream'):
        if b'stream' not in chunk:
            continue
        streams.append(chunk.split(b'stream', 1)[1].decode('latin-1'))
    return streams


def drawn_text(pdf_bytes):
    texts = []
    for stream in pdf_streams(pdf_bytes):
        texts.extend(re.findall(r'Tm \((.*?)\) Tj', stream))
    return '\n'.join(texts)


def drawn_page_texts(pdf_bytes):
    pages = []
    for stream in pdf_streams(pdf_bytes):
        texts = re.findall(r'Tm \((.*?)\) Tj', stream)
        if texts:
            pages.append('\n'.join(texts))
    return pages


def drawn_positions(pdf_bytes):
    """Every drawn text run per page as (x, y, text), so layout can be asserted."""
    pages = []
    for stream in pdf_streams(pdf_bytes):
        runs = re.findall(r'1 0 0 1 ([\d.]+) ([\d.]+) Tm \((.*?)\) Tj', stream)
        if runs:
            pages.append([(float(x), float(y), text) for x, y, text in runs])
    return pages


def all_streams(pdf_bytes):
    return '\n'.join(pdf_streams(pdf_bytes))


def seed_report_data(app):
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO branches (id, name, address_line1, created_at, updated_at) VALUES (101, 'North', '', '2026-01-01', '2026-01-01')")
        db.execute("INSERT INTO branches (id, name, address_line1, created_at, updated_at) VALUES (102, 'South', '', '2026-01-01', '2026-01-01')")
        db.execute("INSERT INTO customers (id, name, email, created_at) VALUES (10, 'Alice Started', 'alice@example.test', '2026-01-01')")
        db.execute("INSERT INTO customers (id, name, email, created_at) VALUES (11, 'Bob Reserved', 'bob@example.test', '2026-01-01')")
        db.execute("INSERT INTO customers (id, name, email, created_at) VALUES (12, 'South Started', 'south@example.test', '2026-01-01')")
        db.execute("""INSERT INTO products (id, name, product_type, sku, price_amount, price_unit, security_deposit, quantity, branch_id, created_at)
                    VALUES (100, 'Trailer A', 'rental', 'TRL-A', 100, 'day', 500, 1, 101, '2026-01-01')""")
        db.execute("""INSERT INTO products (id, name, product_type, sku, price_amount, price_unit, security_deposit, quantity, branch_id, created_at)
                    VALUES (101, 'Trailer B', 'rental', 'TRL-B', 100, 'day', 500, 1, 101, '2026-01-01')""")
        db.execute("""INSERT INTO products (id, name, product_type, sku, price_amount, price_unit, security_deposit, quantity, branch_id, created_at)
                    VALUES (102, 'South Trailer', 'rental', 'STH-1', 100, 'day', 500, 1, 102, '2026-01-01')""")
        db.execute("""INSERT INTO products (id, name, product_type, sku, price_amount, price_unit, security_deposit, quantity, branch_id, created_at)
                    VALUES (103, 'Part', 'part', 'PART-1', 10, 'fixed', 0, 1, 101, '2026-01-01')""")
        orders = [
            (200, 'ORD-START-1', 10, 101, 101, 'started', '2026-07-01T09:00', '2026-07-03T14:30', 750, 'security_deposit', 0, 0, 0, 750),
            (201, 'ORD-RESERVED', 11, 101, 101, 'reserved', '2026-07-01T09:00', '2026-07-03T09:00', 750, 'security_deposit', 0, 0, 0, 750),
            (202, 'ORD-SOUTH', 12, 102, 102, 'started', '2026-07-02T09:00', '2026-07-04T10:15', 0, 'damage_waiver', 125, 0, 0, 125),
            (203, 'ORD-PART', 10, 101, 101, 'started', '2026-07-02T09:00', '2026-07-04T10:15', 0, 'no_deposit', 0, 0, 0, 10),
        ]
        for row in orders:
            db.execute("""INSERT INTO orders (id, order_number, customer_id, collect_branch_id, return_branch_id, status, payment_status, start_at, end_at, deposit_total, deposit_option, damage_waiver_amount, deposit_applied_amount, deposit_refund_amount, total, due_total, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, 'paid', ?, ?, ?, ?, ?, ?, ?, ?, 0, '2026-01-01')""", row)
        items = [(200, 100), (200, 101), (201, 100), (202, 102), (203, 103)]
        for order_id, product_id in items:
            db.execute("INSERT INTO order_items (order_id, product_id, quantity, unit_price, line_subtotal, line_total) VALUES (?, ?, 1, 100, 100, 100)", (order_id, product_id))
        db.execute("""INSERT INTO users (name, email, password_hash, initials, role, branch_id, can_view_all_branches, active, modules_json, created_at)
                    VALUES ('North reports', 'north-reports@example.test', ?, 'NR', 'staff', 101, 0, 1, ?, '2026-01-01')""",
                   (generate_password_hash('admin123'), json.dumps(['orders', 'reports'])))
        db.execute("""INSERT INTO users (name, email, password_hash, initials, role, branch_id, can_view_all_branches, active, modules_json, created_at)
                    VALUES ('North orders only', 'north-orders@example.test', ?, 'NO', 'staff', 101, 0, 1, ?, '2026-01-01')""",
                   (generate_password_hash('admin123'), json.dumps(['orders'])))
        db.execute("""INSERT INTO users (name, email, password_hash, initials, role, branch_id, can_view_all_branches, active, modules_json, created_at)
                    VALUES ('North reports only', 'north-reports-only@example.test', ?, 'RO', 'staff', 101, 0, 1, ?, '2026-01-01')""",
                   (generate_password_hash('admin123'), json.dumps(['reports'])))
        db.commit()


def add_started_trailer_order(db, order_id, order_number, end_at):
    db.execute("""INSERT INTO orders (id, order_number, customer_id, collect_branch_id, return_branch_id, status, payment_status, start_at, end_at, deposit_total, deposit_option, damage_waiver_amount, deposit_applied_amount, deposit_refund_amount, total, due_total, created_at)
                VALUES (?, ?, 10, 101, 101, 'started', 'paid', '2026-07-01T09:00', ?, 500, 'security_deposit', 0, 0, 0, 500, 0, '2026-01-01')""",
               (order_id, order_number, end_at))
    db.execute("INSERT INTO order_items (order_id, product_id, quantity, unit_price, line_subtotal, line_total) VALUES (?, 104, 1, 100, 100, 100)", (order_id,))


def test_started_orders_report_pdf_rows_and_button(client, app, monkeypatch):
    monkeypatch.setattr(reports_service, 'local_now', lambda: datetime(2026, 7, 2, 8, 0))
    seed_report_data(app)
    login(client)
    page = client.get('/orders')
    assert b'Started Orders Report' in page.data
    assert b'/orders/started-orders-report.pdf' in page.data
    assert b'btn primary export-btn' in page.data

    response = client.get('/orders/started-orders-report.pdf')
    assert response.status_code == 200
    assert response.mimetype == 'application/pdf'
    assert response.headers['Content-Disposition'] == 'attachment; filename=started-orders-report.pdf'
    text = drawn_text(response.data)
    assert 'Started Orders Report' in text
    assert '1. Customer Name: Alice Started' in text
    assert 'Order No: ORD-START-1' in text
    assert 'Trailer: TRL-A' in text
    assert '2. Customer Name: Alice Started' in text
    assert 'Trailer: TRL-B' in text
    assert 'Expected Date: 2026-07-03' in text
    assert 'Expected Time: 14:30' in text
    assert 'Available Deposit: R750.00' in text
    assert re.search(r'Damage Waiver: No\s*Balance Due: R1150\.00', text)
    assert 'South Started' in text
    assert 'Trailer: STH-1' in text
    assert 'Expected Time: 10:15' in text
    assert 'Available Deposit: N/A' in text
    assert re.search(r'Damage Waiver: Yes\s*Balance Due: R225\.00', text)
    assert 'ORD-RESERVED' not in text
    assert 'PART-1' not in text


def test_started_orders_report_highlights_the_trailer_sku_with_a_band(client, app, monkeypatch):
    monkeypatch.setattr(reports_service, 'local_now', lambda: datetime(2026, 7, 2, 8, 0))
    seed_report_data(app)
    login(client)
    response = client.get('/orders/started-orders-report.pdf')
    assert response.status_code == 200
    stream = all_streams(response.data)

    # The SKU line sits on a pale fill rectangle (colour + `re`)...
    assert re.search(r'1\.00 0\.96 0\.72 rg [\d.]+ [\d.]+ [\d.]+ [\d.]+ re f', stream)
    # ...and is drawn with the bold font, while the customer line stays regular.
    assert re.search(r'/F2 12 Tf 1 0 0 1 [\d.]+ [\d.]+ Tm \(   Trailer: TRL-A\) Tj', stream)
    assert re.search(r'/F1 12 Tf 1 0 0 1 [\d.]+ [\d.]+ Tm \(1\. Customer Name: Alice Started\) Tj', stream)
    # One band per started trailer line - nothing else is highlighted.
    assert stream.count('1.00 0.96 0.72 rg') == 3


def test_started_orders_report_is_two_columns_and_keeps_each_order_block_on_one_page(client, app, monkeypatch):
    monkeypatch.setattr(reports_service, 'local_now', lambda: datetime(2026, 7, 2, 8, 0))
    seed_report_data(app)
    with app.app_context():
        db = get_db()
        db.execute("""INSERT INTO products (id, name, product_type, sku, price_amount, price_unit, security_deposit, quantity, branch_id, created_at)
                    VALUES (104, 'Trailer C', 'rental', 'TRL-C', 100, 'day', 500, 1, 101, '2026-01-01')""")
        # Four more single-trailer started orders so the report needs a second page
        # (three two-block rows fit under the header, the fourth overflows).
        add_started_trailer_order(db, 204, 'ORD-PAGE-BREAK', '2026-07-05T12:00')
        add_started_trailer_order(db, 205, 'ORD-205', '2026-07-06T09:00')
        add_started_trailer_order(db, 206, 'ORD-206', '2026-07-07T09:00')
        add_started_trailer_order(db, 207, 'ORD-207', '2026-07-08T09:00')
        db.commit()
    login(client)

    response = client.get('/orders/started-orders-report.pdf')

    assert response.status_code == 200
    pages = drawn_page_texts(response.data)
    positions = drawn_positions(response.data)
    assert len(pages) == 2
    assert len(positions) == 2

    # Each order block is drawn whole on exactly one page (never split by a break).
    with app.app_context():
        blocks = reports_service.started_orders_report_pdf_blocks()
    assert len(blocks) == 8  # header block + 7 started trailer rows
    for block in blocks[1:]:
        lines = [line for line in block if line.strip()]
        assert lines, block
        containing = [index for index, page in enumerate(pages) if all(line in page for line in lines)]
        assert len(containing) == 1, (block, containing)

    # Two order blocks share a page row: same y-band, different column x.
    page_one = positions[0]
    first = next(pos for pos in page_one if pos[2] == '1. Customer Name: Alice Started')
    second = next(pos for pos in page_one if pos[2] == '2. Customer Name: Alice Started')
    assert first[1] == second[1]
    assert first[0] != second[0]
    assert min(first[0], second[0]) == 50.0
    assert max(first[0], second[0]) > 297.5  # right-hand column sits past mid-page

    # The block whose order dated last (ORD-207) is the odd trailing half-row.
    assert 'ORD-207' in pages[1]
    assert 'ORD-207' not in pages[0]


def test_started_orders_report_is_orders_gated_and_branch_scoped(client, app, monkeypatch):
    monkeypatch.setattr(reports_service, 'local_now', lambda: datetime(2026, 7, 2, 8, 0))
    seed_report_data(app)
    login(client, 'North reports')
    response = client.get('/orders/started-orders-report.pdf')
    assert response.status_code == 200
    text = drawn_text(response.data)
    assert 'ORD-START-1' in text
    assert 'TRL-A' in text
    assert 'STH-1' not in text

    login(client, 'North orders only')
    page = client.get('/orders')
    assert page.status_code == 200
    assert b'Started Orders Report' in page.data
    assert client.get('/orders/started-orders-report.pdf').status_code == 200

    login(client, 'North reports only')
    assert client.get('/orders/started-orders-report.pdf').status_code == 403
