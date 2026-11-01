"""Ticket ABI-341953259: Started Orders Report lists the balance due per return time.

Under each order's "Balance Due" line the report prints the projected balance for
every whole hour from one hour after the expected return time, keeping only times
inside the shop's 12:00-17:30 window. The figures come from the same re-price the
return panel uses (24h blocks + extra hours at the trailer's own hourly rate, this
VAT mode, discount, deposit and damage waiver, less what is already paid).

The two fixtures are the real shapes quoted on the ticket:

    ORD-10817  due 01-11 12:00  ->  13:00 R20 ... 17:00 R100
    ORD-10780  due 01-11 10:45  ->  12:45 R40 ... 16:45 R120
"""
import os
import re
import tempfile
from datetime import datetime

import pytest

from app import create_app
from app.db import get_db
from app.services import reports as reports_service


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
        row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
        user_id = row['id'] if row else None
    return client.post('/login', data={'user_id': str(user_id), 'password': 'admin123'}, follow_redirects=True)


def today_today():
    return datetime(2026, 11, 1, 9, 0)


def pdf_streams(pdf_bytes):
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


def seed(app, tax_mode='inclusive'):
    """ORD-10817 (noon) and ORD-10780 (10:45) as they exist live, plus one part-paid hire."""
    with app.app_context():
        db = get_db()
        db.execute("UPDATE company_settings SET tax_mode = ? WHERE id = 1", (tax_mode,))
        db.execute("UPDATE tax_profiles SET rate = 15 WHERE id = 1")
        db.execute("INSERT INTO branches (id, name, address_line1, created_at, updated_at) VALUES (501, 'Midrand', '', '2026-01-01', '2026-01-01')")
        db.execute("INSERT INTO customers (id, name, email, created_at) VALUES (900, 'Noon Return', 'noon@example.test', '2026-01-01')")
        db.execute("INSERT INTO customers (id, name, email, created_at) VALUES (901, 'Quarter To Return', 'qtr@example.test', '2026-01-01')")
        # 1.8m utility trailer: R345/day incl VAT, R20 per extra hour (live values).
        db.execute("""INSERT INTO products (id, name, product_type, sku, price_amount, price_unit, security_deposit,
                                             hourly_extra_rate, tax_profile_id, quantity, branch_id, created_at)
                      VALUES (700, '1.8m Utility Trailer', 'rental', '036 - KR 90 ZB GP', 345, 'day', 0, 20, 1, 1, 501, '2026-01-01')""")
        # 2.4m utility trailer: R255/day incl VAT, R20 per extra hour (live values).
        db.execute("""INSERT INTO products (id, name, product_type, sku, price_amount, price_unit, security_deposit,
                                             hourly_extra_rate, tax_profile_id, quantity, branch_id, created_at)
                      VALUES (701, '2.4m Utility Trailer', 'rental', '040 - KT 77 JY GP', 255, 'day', 0, 20, 1, 1, 501, '2026-01-01')""")
        db.execute("""INSERT INTO products (id, name, product_type, sku, price_amount, price_unit, security_deposit,
                                             hourly_extra_rate, tax_profile_id, quantity, branch_id, created_at)
                      VALUES (702, 'Single Axle Damage Waiver', 'service', '', 40, 'day', 0, 0, 1, 1, 501, '2026-01-01')""")
        # A trailer with no extra-hour rate set (the fallback trap).
        db.execute("""INSERT INTO products (id, name, product_type, sku, price_amount, price_unit, security_deposit,
                                             hourly_extra_rate, tax_profile_id, quantity, branch_id, created_at)
                      VALUES (703, 'No Rate Trailer', 'rental', 'NO-RATE', 300, 'day', 0, 0, 1, 1, 501, '2026-01-01')""")

        orders = [
            # id, number, customer, start, end, deposit_total, deposit_option, waiver, total, due
            (800, 'ORD-10817', 900, '2026-10-31T12:00', '2026-11-01T12:00', 0, 'damage_waiver', 0, 385, 0),
            (801, 'ORD-10780', 901, '2026-10-30T10:45', '2026-11-01T10:45', 0, 'damage_waiver', 0, 590, 0),
            (802, 'ORD-10850', 900, '2026-10-31T18:00', '2026-11-01T18:00', 0, 'damage_waiver', 0, 385, 0),
            (803, 'ORD-10851', 900, '2026-10-31T12:00', '2026-11-01T12:00', 0, 'damage_waiver', 0, 385, 85),
            (804, 'ORD-10852', 901, '2026-10-31T12:00', '2026-11-01T12:00', 0, 'damage_waiver', 0, 340, 0),
        ]
        for row in orders:
            db.execute("""INSERT INTO orders (id, order_number, customer_id, collect_branch_id, return_branch_id,
                                              status, payment_status, start_at, end_at, deposit_total, deposit_option,
                                              damage_waiver_amount, deposit_applied_amount, deposit_refund_amount,
                                              total, due_total, created_at)
                          VALUES (?, ?, ?, 501, 501, 'started', 'paid', ?, ?, ?, ?, ?, 0, 0, ?, ?, '2026-01-01')""", row)
        items = [
            (800, 700, 345, 300.0, 45.0, 345.0),
            (800, 702, 40, 34.78, 5.22, 40.0),
            (801, 701, 255, 221.74, 33.26, 255.0),
            (801, 702, 40, 34.78, 5.22, 40.0),
            (802, 700, 345, 300.0, 45.0, 345.0),
            (802, 702, 40, 34.78, 5.22, 40.0),
            (803, 700, 345, 300.0, 45.0, 345.0),
            (803, 702, 40, 34.78, 5.22, 40.0),
            (804, 703, 300, 260.87, 39.13, 300.0),
        ]
        for order_id, product_id, price, *_stored in items:
            db.execute("""INSERT INTO order_items (order_id, product_id, quantity, unit_price, billing_mode,
                                                   line_subtotal, line_tax, line_total)
                          VALUES (?, ?, 1, ?, 'catalog', 0, 0, 0)""", (order_id, product_id, price))
        # Paid in full except ORD-10851, which has R85 outstanding.
        payments = [(800, 385.0), (801, 590.0), (802, 385.0), (803, 300.0), (804, 0.0)]
        for order_id, amount in payments:
            if amount:
                db.execute("""INSERT INTO payments (order_id, amount, method, status, payment_date, created_at)
                              VALUES (?, ?, 'cash', 'paid', '2026-10-31', '2026-10-31T12:00')""", (order_id, amount))
        db.commit()


def report_rows(app):
    with app.app_context():
        return {row['order_number']: row for row in reports_service.started_orders_report_rows()}


def slots_for(app, order_number):
    row = report_rows(app)[order_number]
    return [(label, round(amount, 2)) for label, amount in row['balance_slots']]


def test_noon_return_lists_one_balance_per_hour_from_the_live_figures(app, monkeypatch):
    """ORD-10817: due 12:00, R20 an hour -> 13:00 R20 ... 17:00 R100."""
    monkeypatch.setattr(reports_service, 'local_now', today_today)
    seed(app)

    assert slots_for(app, 'ORD-10817') == [
        ('13:00', 20.0),
        ('14:00', 40.0),
        ('15:00', 60.0),
        ('16:00', 80.0),
        ('17:00', 100.0),
    ]
    # 12:00 itself is never a slot (it is the due time, not a late return).
    assert '12:00' not in [label for label, _ in slots_for(app, 'ORD-10817')]


def test_quarter_to_eleven_due_skips_the_times_before_noon(app, monkeypatch):
    """ORD-10780: due 10:45 -> 11:45 is before the window, so 12:45 R40 ... 16:45 R120."""
    monkeypatch.setattr(reports_service, 'local_now', today_today)
    seed(app)

    assert slots_for(app, 'ORD-10780') == [
        ('12:45', 40.0),
        ('13:45', 60.0),
        ('14:45', 80.0),
        ('15:45', 100.0),
        ('16:45', 120.0),
    ]


def test_inclusive_vat_does_not_inflate_the_hourly_amount(app, monkeypatch):
    """The live books are VAT inclusive: an R20 hour stays R20, not R23."""
    monkeypatch.setattr(reports_service, 'local_now', today_today)
    seed(app, tax_mode='inclusive')
    assert dict(slots_for(app, 'ORD-10817'))['13:00'] == 20.0


def test_exclusive_vat_adds_the_taxed_hourly_amount(app, monkeypatch):
    """On an exclusive-VAT setup the extra hour carries its 15% tax (R20 -> R23)."""
    monkeypatch.setattr(reports_service, 'local_now', today_today)
    seed(app, tax_mode='exclusive')
    slots = dict(slots_for(app, 'ORD-10817'))
    assert round(slots['14:00'] - slots['13:00'], 2) == 23.0
    assert round(slots['17:00'] - slots['16:00'], 2) == 23.0


def test_part_paid_order_carries_its_outstanding_balance_on_every_slot(app, monkeypatch):
    """R85 still unpaid at the due time is still owed at every later hour."""
    monkeypatch.setattr(reports_service, 'local_now', today_today)
    seed(app)

    row = report_rows(app)['ORD-10851']
    assert round(row['balance_due'], 2) == 85.0
    assert slots_for(app, 'ORD-10851') == [
        ('13:00', 105.0),
        ('14:00', 125.0),
        ('15:00', 145.0),
        ('16:00', 165.0),
        ('17:00', 185.0),
    ]


def test_an_order_due_after_the_window_has_no_return_time_list(app, monkeypatch):
    monkeypatch.setattr(reports_service, 'local_now', today_today)
    seed(app)

    row = report_rows(app)['ORD-10850']
    assert row['balance_slots'] == []
    assert row['balance_slots_note'] == ''

    client = app.test_client()
    login(client)
    response = client.get('/orders/started-orders-report.pdf')
    assert response.status_code == 200
    text = drawn_text(response.data)
    assert 'Order No: ORD-10850' in text
    assert '@19:00' not in text
    assert '@13:00 - R' in text  # the noon order still has its slots


def test_a_trailer_without_an_hourly_rate_says_rates_are_not_set(app, monkeypatch):
    """No hourly rate must never be quoted as the day price per hour."""
    monkeypatch.setattr(reports_service, 'local_now', today_today)
    seed(app)

    row = report_rows(app)['ORD-10852']
    assert row['balance_slots'] == []
    assert row['balance_slots_note'] == 'Rates not set'

    client = app.test_client()
    login(client)
    text = drawn_text(client.get('/orders/started-orders-report.pdf').data)
    assert 'Rates not set' in text
    # R300 is the DAY price of that trailer - it must not appear as an hour.
    assert '@13:00 - R300' not in text


def test_the_pdf_prints_the_slots_indented_under_balance_due(app, monkeypatch):
    monkeypatch.setattr(reports_service, 'local_now', today_today)
    seed(app)
    client = app.test_client()
    login(client)

    stream = '\n'.join(pdf_streams(client.get('/orders/started-orders-report.pdf').data))
    text = re.sub(r'\n+', '\n', drawn_text(client.get('/orders/started-orders-report.pdf').data))

    assert re.search(
        r'Balance Due: R0\.00\n'
        r'      @13:00 - R20\.00\n'
        r'      @14:00 - R40\.00\n'
        r'      @15:00 - R60\.00\n'
        r'      @16:00 - R80\.00\n'
        r'      @17:00 - R100\.00',
        text,
    ), text

    # Same size and font as the other block lines: no bold band, no second font.
    assert re.search(r'/F1 12 Tf 1 0 0 1 [\d.]+ [\d.]+ Tm \(      @13:00 - R20\.00\) Tj', stream)
    # One trailer band per started trailer line, exactly as before.
    assert stream.count('1.00 0.96 0.72 rg') == 5


def test_a_hire_due_after_the_report_date_gets_no_time_list(app, monkeypatch):
    """ABI-341953262: only a hire due on or before the report date is re-timed.

    ORD-10817 due 02-11, report run 01-11: the trailer is still out on hire, so
    it keeps its single Balance Due line instead of quoting today's hours.
    """
    monkeypatch.setattr(reports_service, 'local_now', today_today)
    with app.app_context():
        seed(app)
        db = get_db()
        db.execute("UPDATE orders SET end_at = '2026-11-02T12:00' WHERE order_number = 'ORD-10817'")
        db.commit()
        rows = {row['order_number']: row for row in reports_service.started_orders_report_rows()}
    assert rows['ORD-10817']['balance_slots'] == []
    assert rows['ORD-10817']['balance_slots_date'] == ''

    client = app.test_client()
    login(client)
    text = drawn_text(client.get('/orders/started-orders-report.pdf').data)
    assert '@02-11' not in text
    assert '      @13:00 - R20.00' not in text


def seed_early_return(app, paid=770.0):
    """ORD-10829: two days booked and paid (31-10 10:00 -> 02-11 10:00)."""
    with app.app_context():
        db = get_db()
        db.execute("""INSERT INTO orders (id, order_number, customer_id, collect_branch_id, return_branch_id,
                                          status, payment_status, start_at, end_at, deposit_total, deposit_option,
                                          damage_waiver_amount, deposit_applied_amount, deposit_refund_amount,
                                          total, due_total, created_at)
                      VALUES (805, 'ORD-10829', 900, 501, 501, 'started', 'paid',
                              '2026-10-31T10:00', '2026-11-02T10:00', 0, 'damage_waiver',
                              0, 0, 0, 770, 0, '2026-10-31')""")
        for product_id, price in ((700, 345), (702, 40)):
            db.execute("""INSERT INTO order_items (order_id, product_id, quantity, unit_price, billing_mode,
                                                   line_subtotal, line_tax, line_total)
                          VALUES (805, ?, 1, ?, 'catalog', 0, 0, 0)""", (product_id, price))
        if paid:
            db.execute("""INSERT INTO payments (order_id, amount, method, status, payment_date, created_at)
                          VALUES (805, ?, 'cash', 'paid', '2026-10-31', '2026-10-31T10:00')""", (paid,))
        db.commit()


def test_a_hire_due_later_keeps_its_single_signed_balance_line(app, monkeypatch):
    """The ticket's ORD-10819 shape: due 02-11, paid in full up front.

    ABI-341953262 leaves that order exactly as it was — one Balance Due line,
    signed, with no hourly list under it.
    """
    monkeypatch.setattr(reports_service, 'local_now', today_today)
    seed(app)
    seed_early_return(app)

    row = report_rows(app)['ORD-10829']
    assert row['balance_slots'] == []
    assert row['balance_slots_date'] == ''
    # Paid 2 days (R770) but only 1 day is used if it comes back today.
    assert round(row['balance_due'], 2) == -385.0

    client = app.test_client()
    login(client)
    text = drawn_text(client.get('/orders/started-orders-report.pdf').data)
    assert 'Order No: ORD-10829' in text
    assert 'Balance Due: -R385.00' in text
    assert '@12:00' not in text
    assert '@02-11' not in text


def test_an_overdue_hire_quotes_todays_times(app, monkeypatch):
    """A trailer still out from yesterday is costing money today, not then."""
    monkeypatch.setattr(reports_service, 'local_now', today_today)
    with app.app_context():
        seed(app)
        db = get_db()
        db.execute("UPDATE orders SET start_at = '2026-10-29T12:00', end_at = '2026-10-31T12:00' WHERE order_number = 'ORD-10817'")
        db.commit()
        row = {r['order_number']: r for r in reports_service.started_orders_report_rows()}['ORD-10817']
    assert row['balance_slots_date'] == '2026-11-01'
    # Two whole 24h blocks (29-10 12:00 -> 31-10 12:00) plus the hours into today.
    assert [label for label, _ in row['balance_slots']] == ['13:00', '14:00', '15:00', '16:00', '17:00']
    assert round(dict(row['balance_slots'])['13:00'], 2) > 0


def test_every_block_still_lands_whole_on_one_page(app, monkeypatch):
    """More lines per order must not split an order across a page break."""
    monkeypatch.setattr(reports_service, 'local_now', today_today)
    seed(app)
    client = app.test_client()
    login(client)

    response = client.get('/orders/started-orders-report.pdf')
    assert response.status_code == 200
    pages = []
    for stream in pdf_streams(response.data):
        texts = re.findall(r'Tm \((.*?)\) Tj', stream)
        if texts:
            pages.append('\n'.join(texts))

    with app.app_context():
        blocks = reports_service.started_orders_report_pdf_blocks()
    assert len(blocks) == 6  # header + 5 started trailer rows
    for block in blocks[1:]:
        lines = [line for line in block if line.strip()]
        containing = [index for index, page in enumerate(pages) if all(line in page for line in lines)]
        assert len(containing) == 1, (block, containing)
