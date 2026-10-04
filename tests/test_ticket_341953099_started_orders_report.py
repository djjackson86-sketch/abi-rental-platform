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


def drawn_text(pdf_bytes):
    texts = []
    for stream in re.findall(r'stream\r?\n(.*?)\r?\nendstream', pdf_bytes.decode('latin-1'), re.S):
        texts.extend(re.findall(r'Tm \((.*?)\) Tj', stream))
    return '\n'.join(texts)


def drawn_page_texts(pdf_bytes):
    pages = []
    for stream in re.findall(r'stream\r?\n(.*?)\r?\nendstream', pdf_bytes.decode('latin-1'), re.S):
        texts = re.findall(r'Tm \((.*?)\) Tj', stream)
        if texts:
            pages.append('\n'.join(texts))
    return pages


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
    assert re.search(r'Damage Waiver: No\n\s*Balance Due: R1150\.00', text)
    assert 'South Started' in text
    assert 'Trailer: STH-1' in text
    assert 'Expected Time: 10:15' in text
    assert 'Available Deposit: N/A' in text
    assert re.search(r'Damage Waiver: Yes\n\s*Balance Due: R225\.00', text)
    assert 'ORD-RESERVED' not in text
    assert 'PART-1' not in text


def test_started_orders_report_keeps_each_order_block_on_one_page(client, app, monkeypatch):
    monkeypatch.setattr(reports_service, 'local_now', lambda: datetime(2026, 7, 2, 8, 0))
    seed_report_data(app)
    with app.app_context():
        db = get_db()
        db.execute("""INSERT INTO products (id, name, product_type, sku, price_amount, price_unit, security_deposit, quantity, branch_id, created_at)
                    VALUES (104, 'Trailer C', 'rental', 'TRL-C', 100, 'day', 500, 1, 101, '2026-01-01')""")
        db.execute("""INSERT INTO orders (id, order_number, customer_id, collect_branch_id, return_branch_id, status, payment_status, start_at, end_at, deposit_total, deposit_option, damage_waiver_amount, deposit_applied_amount, deposit_refund_amount, total, due_total, created_at)
                    VALUES (204, 'ORD-PAGE-BREAK', 10, 101, 101, 'started', 'paid', '2026-07-01T09:00', '2026-07-05T12:00', 500, 'security_deposit', 0, 0, 0, 500, 0, '2026-01-01')""")
        db.execute("INSERT INTO order_items (order_id, product_id, quantity, unit_price, line_subtotal, line_total) VALUES (204, 104, 1, 100, 100, 100)")
        db.commit()
    login(client)

    response = client.get('/orders/started-orders-report.pdf')

    assert response.status_code == 200
    pages = drawn_page_texts(response.data)
    assert len(pages) == 2
    page_break_block = [
        '4. Customer Name: Alice Started',
        'Order No: ORD-PAGE-BREAK',
        'Trailer: TRL-C',
        'Expected Date: 2026-07-05',
        'Expected Time: 12:00',
        'Available Deposit: R500.00',
        'Damage Waiver: No',
        'Balance Due: R700.00',
    ]
    unique_block_lines = page_break_block[:5] + [page_break_block[-1]]
    containing_pages = [page for page in pages if any(line in page for line in unique_block_lines)]
    assert len(containing_pages) == 1
    assert all(line in containing_pages[0] for line in page_break_block)


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
