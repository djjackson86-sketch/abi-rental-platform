"""Ticket ABI-341953262: balances per return time, only for orders due by today.

Under each started order's "Balance Due" line the Started Orders Report prints
the projected balance at every whole hour after its expected return time, kept
inside the shop's 12:00-17:30 window. The list belongs only to an order that was
meant to come back on the report's own date or earlier; a trailer still out on
hire until a later date keeps its single Balance Due line.

The three fixtures are the ticket's real orders (values read from the live
books on 01-11-2026):

    ORD-10817  due 01-11 12:00  paid 385   ->  13:00 R20 ... 17:00 R100
    ORD-10780  due 01-11 10:45  paid 590   ->  12:45 R40 ... 16:45 R120
    ORD-10819  due 02-11 12:45  paid 1140  ->  Balance Due: -R395, as it was
"""
import os
import re
import tempfile
from datetime import datetime

import pytest

from app import create_app
from app.db import get_db
from app.services import reports as reports_service

REPORT_MOMENT = datetime(2026, 11, 1, 9, 0)   # the report is run on 01-11


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


def login(client):
    with client.application.app_context():
        row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
        user_id = row['id'] if row else None
    return client.post('/login', data={'user_id': str(user_id), 'password': 'admin123'}, follow_redirects=True)


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


def seed(app):
    """The ticket's three orders as they stand live, plus one overdue hire."""
    with app.app_context():
        db = get_db()
        db.execute("UPDATE company_settings SET tax_mode = 'inclusive' WHERE id = 1")
        db.execute("UPDATE tax_profiles SET rate = 15 WHERE id = 1")
        db.execute("INSERT INTO branches (id, name, address_line1, created_at, updated_at)"
                   " VALUES (501, 'Midrand', '', '2026-01-01', '2026-01-01')")
        for customer_id, name in ((900, 'Hulisani Tshishonga'), (901, 'Daniel Tshale'),
                                  (902, 'Alton Mudau'), (903, 'Overdue Hire')):
            db.execute("INSERT INTO customers (id, name, email, created_at) VALUES (?, ?, ?, '2026-01-01')",
                       (customer_id, name, f'c{customer_id}@example.test'))
        products = [
            (700, '2.4m Utility Trailer', 'rental', '040 - KT 77 JY GP', 345, 20),
            (701, '1.8m Utility Trailer', 'rental', '036 - KR 90 ZB GP', 255, 20),
            (702, 'Single Axle Damage Waiver', 'service', '', 40, 0),
            (704, '3m Utility Trailer', 'rental', '015 - JY 95 XH GP', 395, 20),
        ]
        for product_id, name, kind, sku, price, hourly in products:
            db.execute("""INSERT INTO products (id, name, product_type, sku, price_amount, price_unit,
                                                 security_deposit, hourly_extra_rate, tax_profile_id,
                                                 quantity, branch_id, created_at)
                          VALUES (?, ?, ?, ?, ?, 'day', 0, ?, 1, 1, 501, '2026-01-01')""",
                       (product_id, name, kind, sku, price, hourly))
        orders = [
            # id, number, customer, start, end, deposit, deposit option, total
            (720, 'ORD-10817', 900, '2026-10-31T12:00', '2026-11-01T12:00', 0, 'damage_waiver', 385),
            (683, 'ORD-10780', 901, '2026-10-30T10:45', '2026-11-01T10:45', 0, 'damage_waiver', 590),
            (722, 'ORD-10819', 902, '2026-10-31T12:45', '2026-11-02T12:45', 350, 'security_deposit', 1140),
            (740, 'ORD-10803', 903, '2026-10-27T09:00', '2026-10-31T09:00', 0, 'damage_waiver', 770),
        ]
        for order_id, number, customer, start, end, deposit, option, total in orders:
            db.execute("""INSERT INTO orders (id, order_number, customer_id, collect_branch_id,
                                              return_branch_id, status, payment_status, start_at, end_at,
                                              deposit_total, deposit_option, damage_waiver_amount,
                                              deposit_applied_amount, deposit_refund_amount,
                                              total, due_total, created_at)
                          VALUES (?, ?, ?, 501, 501, 'started', 'paid', ?, ?, ?, ?, 0, 0, 0, ?, 0, '2026-01-01')""",
                       (order_id, number, customer, start, end, deposit, option, total))
        items = [
            (720, 700, 345), (720, 702, 40),
            (683, 701, 255), (683, 702, 40),
            (722, 704, 395),
            (740, 700, 345), (740, 702, 40),
        ]
        for order_id, product_id, price in items:
            db.execute("""INSERT INTO order_items (order_id, product_id, quantity, unit_price, billing_mode,
                                                   line_subtotal, line_tax, line_total)
                          VALUES (?, ?, 1, ?, 'catalog', 0, 0, 0)""", (order_id, product_id, price))
        for order_id, amount in ((720, 385.0), (683, 590.0), (722, 1140.0), (740, 770.0)):
            db.execute("""INSERT INTO payments (order_id, amount, method, status, payment_date, created_at)
                          VALUES (?, ?, 'cash', 'paid', '2026-10-31', '2026-10-31T12:00')""", (order_id, amount))
        db.commit()


@pytest.fixture()
def seeded(app, monkeypatch):
    monkeypatch.setattr(reports_service, 'local_now', lambda: REPORT_MOMENT)
    seed(app)
    return app


def report_rows(app):
    with app.app_context():
        return {row['order_number']: row for row in reports_service.started_orders_report_rows()}


def slots_for(app, order_number):
    return [(label, round(amount, 2)) for label, amount in report_rows(app)[order_number]['balance_slots']]


def block_for(app, order_number):
    with app.app_context():
        blocks = reports_service.started_orders_report_pdf_blocks()
    for block in blocks:
        if any(f'Order No: {order_number}' == line.strip() for line in block):
            return block
    raise AssertionError(f'{order_number} missing from the report')


# --- the ticket's first example -------------------------------------------------

def test_the_noon_return_quotes_r20_through_r100(seeded):
    """ORD-10817, due 12:00: 13:00 R20, 14:00 R40 ... 17:00 R100."""
    assert slots_for(seeded, 'ORD-10817') == [
        ('13:00', 20.0),
        ('14:00', 40.0),
        ('15:00', 60.0),
        ('16:00', 80.0),
        ('17:00', 100.0),
    ]


def test_the_noon_itself_is_the_expected_time_not_a_slot(seeded):
    assert '12:00' not in dict(slots_for(seeded, 'ORD-10817'))


# --- the ticket's second example ------------------------------------------------

def test_the_quarter_to_eleven_return_quotes_r40_through_r120(seeded):
    """ORD-10780, due 10:45: 11:45 is before the window, so 12:45 R40 ... 16:45 R120."""
    assert slots_for(seeded, 'ORD-10780') == [
        ('12:45', 40.0),
        ('13:45', 60.0),
        ('14:45', 80.0),
        ('15:45', 100.0),
        ('16:45', 120.0),
    ]


def test_no_slot_falls_outside_the_twelve_to_five_thirty_window(seeded):
    for order_number in ('ORD-10817', 'ORD-10780', 'ORD-10803'):
        for label, _amount in slots_for(seeded, order_number):
            minutes = int(label[:2]) * 60 + int(label[3:])
            assert 12 * 60 <= minutes <= 17 * 60 + 30, (order_number, label)


# --- the ticket's third example: leave a later hire alone ------------------------

def test_a_hire_due_after_the_report_date_is_left_as_it_was(seeded):
    """ORD-10819, due 02-11: Balance Due -R395 and no time lines at all."""
    row = report_rows(seeded)['ORD-10819']
    assert round(row['balance_due'], 2) == -395.0
    assert row['balance_slots'] == []
    assert row['balance_slots_date'] == ''
    assert row['balance_slots_note'] == ''


def test_the_later_hire_prints_one_balance_due_line_and_nothing_under_it(seeded):
    block = block_for(seeded, 'ORD-10819')
    assert '   Balance Due: -R395.00' in block
    assert [line for line in block if '@' in line] == []


def test_the_pdf_draws_the_ticket_lines_under_balance_due(seeded):
    client = seeded.test_client()
    login(client)
    text = re.sub(r'\n+', '\n', drawn_text(client.get('/orders/started-orders-report.pdf').data))

    assert re.search(
        r'Order No: ORD-10817\n'
        r'   Trailer: 040 - KT 77 JY GP\n'
        r'   Expected Date: 2026-11-01\n'
        r'   Expected Time: 12:00\n'
        r'   Available Deposit: N/A\n'
        r'   Damage Waiver: Yes\n'
        r'   Balance Due: R0\.00\n'
        r'      @13:00 - R20\.00\n'
        r'      @14:00 - R40\.00\n'
        r'      @15:00 - R60\.00\n'
        r'      @16:00 - R80\.00\n'
        r'      @17:00 - R100\.00\n',
        text,
    ), text


def test_the_later_hire_draws_its_balance_without_any_at_lines(seeded):
    client = seeded.test_client()
    login(client)
    text = re.sub(r'\n+', '\n', drawn_text(client.get('/orders/started-orders-report.pdf').data))

    # ORD-10819 is the last order in the report, so the block must simply end.
    assert re.search(
        r'Order No: ORD-10819\n'
        r'   Trailer: 015 - JY 95 XH GP\n'
        r'   Expected Date: 2026-11-02\n'
        r'   Expected Time: 12:45\n'
        r'   Available Deposit: R350\.00\n'
        r'   Damage Waiver: No\n'
        r'   Balance Due: -R395\.00\n\s*$',
        text,
    ), text


# --- the date rule itself -------------------------------------------------------

def test_a_hire_due_before_the_report_date_still_gets_its_times(seeded):
    """Overdue on 31-10: it is costing money today, so today's hours are quoted."""
    row = report_rows(seeded)['ORD-10803']
    assert [label for label, _ in row['balance_slots']] == [
        '12:00', '13:00', '14:00', '15:00', '16:00', '17:00',
    ]
    assert row['balance_slots_date'] == '2026-11-01'
    assert all(amount > 0 for _label, amount in row['balance_slots'])


def test_moving_the_report_a_day_later_moves_the_later_hire_into_the_list(seeded, monkeypatch):
    """On 02-11 ORD-10819 is due that day, so from 03-11 it is a late return."""
    monkeypatch.setattr(reports_service, 'local_now', lambda: datetime(2026, 11, 3, 9, 0))
    row = report_rows(seeded)['ORD-10819']
    # 03-11 is past the 02-11 due date: late hours are quoted again.
    assert [label for label, _ in row['balance_slots']] == [
        '13:45', '14:45', '15:45', '16:45',
    ]


def test_only_the_orders_due_by_today_carry_time_lines(seeded):
    rows = report_rows(seeded)
    with_slots = {number for number, row in rows.items() if row['balance_slots']}
    assert with_slots == {'ORD-10817', 'ORD-10780', 'ORD-10803'}
    # Everything gets a Balance Due figure, with or without a list.
    assert all(row['balance_due'] is not None for row in rows.values())


# --- ABI-341953263: the list is the day's tariff, not a live quote ---------------

REPORT_MOMENTS = [
    datetime(2026, 11, 1, 8, 0),    # before the shop opens
    datetime(2026, 11, 1, 12, 0),   # mid-window
    datetime(2026, 11, 1, 17, 45),  # just after the 17:30 close
    datetime(2026, 11, 1, 20, 0),   # the evening print the client reported
    datetime(2026, 11, 1, 22, 0),   # late at night
    datetime(2026, 11, 2, 6, 0),    # next morning, before the shop opens
]
# The moments that fall on ORD-10819's own due date, for the still-out rule.
REPORT_MOMENTS_ON_DUE_DATE = [moment for moment in REPORT_MOMENTS
                              if moment.date() == datetime(2026, 11, 1).date()]


@pytest.mark.parametrize('moment', REPORT_MOMENTS)
def test_a_hire_due_today_lists_the_same_hours_at_any_clock_time(seeded, monkeypatch, moment):
    """The list is the day's tariff: 20:00 must print what 09:00 prints.

    Before ABI-341953263 every slot past the clock was skipped, so an evening
    print showed no hourly lines at all — the client's report.
    """
    monkeypatch.setattr(reports_service, 'local_now', lambda: moment)
    slots = slots_for(seeded, 'ORD-10817')
    if moment.date() == datetime(2026, 11, 1).date():
        assert slots == [
            ('13:00', 20.0),
            ('14:00', 40.0),
            ('15:00', 60.0),
            ('16:00', 80.0),
            ('17:00', 100.0),
        ]
        assert report_rows(seeded)['ORD-10817']['balance_slots_date'] == '2026-11-01'
    else:
        # On 02-11 the same hire is a late return: still one hour after its own
        # due time, so 13:00 to 17:00, each adding the trailer's R20 rate.
        assert [label for label, _ in slots] == [
            '13:00', '14:00', '15:00', '16:00', '17:00',
        ]
        deltas = [round(second - first, 2) for (_l1, first), (_l2, second) in zip(slots, slots[1:])]
        assert deltas == [20.0] * 4
        assert report_rows(seeded)['ORD-10817']['balance_slots_date'] == '2026-11-02'


@pytest.mark.parametrize('moment', REPORT_MOMENTS)
def test_an_overdue_hire_still_lists_the_window_at_any_clock_time(seeded, monkeypatch, moment):
    monkeypatch.setattr(reports_service, 'local_now', lambda: moment)
    row = report_rows(seeded)['ORD-10803']
    assert [label for label, _ in row['balance_slots']] == [
        '12:00', '13:00', '14:00', '15:00', '16:00', '17:00',
    ]
    assert row['balance_slots_date'] == moment.date().isoformat()


@pytest.mark.parametrize('moment', REPORT_MOMENTS_ON_DUE_DATE)
def test_a_later_hire_stays_single_line_whatever_the_clock_says(seeded, monkeypatch, moment):
    """ABI-341953262's rule is untouched: only the evening skip changed."""
    monkeypatch.setattr(reports_service, 'local_now', lambda: moment)
    row = report_rows(seeded)['ORD-10819']
    assert row['balance_slots'] == []
    assert row['balance_slots_date'] == ''
    assert row['balance_slots_note'] == ''
    assert round(row['balance_due'], 2) == -395.0


def test_the_evening_pdf_still_draws_the_afternoon_hours(seeded, monkeypatch):
    """The client's own case: generating the PDF at 20:00 must show the lines."""
    monkeypatch.setattr(reports_service, 'local_now', lambda: datetime(2026, 11, 1, 20, 0))
    client = seeded.test_client()
    login(client)
    text = re.sub(r'\n+', '\n', drawn_text(client.get('/orders/started-orders-report.pdf').data))

    assert re.search(
        r'Order No: ORD-10817\n'
        r'   Trailer: 040 - KT 77 JY GP\n'
        r'   Expected Date: 2026-11-01\n'
        r'   Expected Time: 12:00\n'
        r'   Available Deposit: N/A\n'
        r'   Damage Waiver: Yes\n'
        r'   Balance Due: R0\.00\n'
        r'      @13:00 - R20\.00\n'
        r'      @14:00 - R40\.00\n'
        r'      @15:00 - R60\.00\n'
        r'      @16:00 - R80\.00\n'
        r'      @17:00 - R100\.00\n',
        text,
    ), text
    # ...and the hire due 02-11 still has none, in the same report.
    assert '@02-11' not in text


def test_a_due_time_late_in_the_window_quotes_the_closing_time(seeded):
    """A 16:45 or 17:15 return leaves no whole hour before 17:30 (ABI-341953263).

    Live examples: ORD-10826/10827 due 16:45 and ORD-10798 due 17:15, which
    used to get no list at any hour of the day.
    """
    with seeded.app_context():
        db = get_db()
        db.execute("UPDATE orders SET end_at = '2026-11-01T16:45' WHERE order_number = 'ORD-10817'")
        db.commit()
    row = report_rows(seeded)['ORD-10817']
    assert [label for label, _ in row['balance_slots']] == ['17:30']
    assert round(row['balance_slots'][0][1], 2) > 0
    assert row['balance_slots_date'] == '2026-11-01'

    with seeded.app_context():
        db = get_db()
        db.execute("UPDATE orders SET end_at = '2026-11-01T17:15' WHERE order_number = 'ORD-10817'")
        db.commit()
    row = report_rows(seeded)['ORD-10817']
    assert [label for label, _ in row['balance_slots']] == ['17:30']


def test_a_hire_due_at_the_close_gets_no_closing_slot(seeded):
    """17:30 is the due time itself, so there is nothing late to quote."""
    with seeded.app_context():
        db = get_db()
        db.execute("UPDATE orders SET end_at = '2026-11-01T17:30' WHERE order_number = 'ORD-10817'")
        db.commit()
    assert report_rows(seeded)['ORD-10817']['balance_slots'] == []


def test_a_hire_due_after_the_close_still_gets_nothing(seeded):
    """An 18:00 return is outside the staffed window — no invented slot."""
    with seeded.app_context():
        db = get_db()
        db.execute("UPDATE orders SET end_at = '2026-11-01T18:00' WHERE order_number = 'ORD-10817'")
        db.commit()
    assert report_rows(seeded)['ORD-10817']['balance_slots'] == []

def test_the_later_hire_starts_quoting_on_its_own_date(seeded, monkeypatch):
    """On 02-11 ORD-10819 is due that day, so its hours appear (unchanged)."""
    monkeypatch.setattr(reports_service, 'local_now', lambda: datetime(2026, 11, 2, 6, 0))
    row = report_rows(seeded)['ORD-10819']
    assert [label for label, _ in row['balance_slots']] == [
        '13:45', '14:45', '15:45', '16:45',
    ]
    assert row['balance_slots_date'] == '2026-11-02'
