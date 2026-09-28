"""Ticket ABI-341953066: "Other" as a selectable payment method.

The requested edit was "add 'Other' on payment methods". Two consequences had to
be pinned, because both would quietly misreport money:

* the method a payment is stored with decides which line of the day's figures it
  lands on, so the two forms that write a method must offer Other and the service
  layer must accept it (and lower-case it) while still refusing junk; and
* the dashboard/day-report money invariant is ``revenue == cash + card + EFT``.
  With a fourth method selectable that only stays true if every method that is
  *not* one of the three named cards is summed somewhere — the new "Total other
  payments" line. Legacy rows (``manual``, ``deposit_applied``, imports) land on
  that same line, so no money hides between the cards.

The definition of "other" is deliberately the complement — anything that is not
cash/EFT/card — rather than a list of known values, so a legacy or imported
method can never be dropped from every total.

Fixed dates keep the day assertions independent of the wall clock where the test
is about a seeded day; the two end-to-end tests use the real business day because
they record a payment through the UI (which stamps "now").
"""
import os
import re
import tempfile

import pytest
from flask import session as flask_session

from app import create_app
from app.db import get_db
from app.services import cash
from app.services import reports as reports_service
from app.services.payments import update_payment

TODAY = cash.today_iso()
YESTERDAY = '2026-09-12'

# Every text run the day-report PDF actually draws: (font, size, x, y, text).
_TEXT_RUN = re.compile(r'/(F\d) ([\d.]+) Tf 1 0 0 1 ([\d.]+) ([\d.]+) Tm \(((?:[^()\\]|\\.)*)\) Tj')


def _unescape(value):
    return value.replace('\\(', '(').replace('\\)', ')').replace('\\\\', '\\')


def _runs(pdf_bytes):
    return [(float(x), float(y), float(size), _unescape(text))
            for _font, size, x, y, text in _TEXT_RUN.findall(pdf_bytes.decode('latin-1'))]


def _drawn_text(pdf_bytes):
    return [run[3] for run in _runs(pdf_bytes)]


def _card_values(pdf_bytes):
    """{label: value} as the report's cards draw them (value sits 15pt below)."""
    runs = _runs(pdf_bytes)
    values = {}
    for index, (x, y, _size, text) in enumerate(runs[:-1]):
        next_x, next_y, _next_size, next_text = runs[index + 1]
        if abs(next_x - x) < 0.01 and abs((y - next_y) - 15) < 0.01:
            values[text] = next_text
    return values


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
    assert row is not None
    return client.post('/login', data={'user_id': str(row['id']), 'password': 'admin123'},
                       follow_redirects=True)


def seed_customer_product_order(client):
    """A real order through the app's own forms, so the totals are the app's."""
    client.post('/customers/new', data={
        'customer_type': 'individual',
        'name': 'Other Method Customer',
        'email': 'other-method@example.com',
        'phone': '+270****0660',
    }, follow_redirects=True)
    client.post('/inventory/new', data={
        'name': 'Other Method Trailer',
        'sku': 'OTH-TRL',
        'quantity': '4',
        'description': 'Payment method test trailer.',
        'product_type': 'rental',
        'price_amount': '200',
        'price_unit': 'day',
        'security_deposit': '750',
        'tax_profile_id': '1',
        'active': '1',
        'public_visible': '1',
    }, follow_redirects=True)
    res = client.post('/orders/new', data={
        'customer_id': '1',
        'product_id': '1',
        'quantity': '1',
        'start_date': '2026-07-01',
        'start_time': '09:00',
        'end_date': '2026-07-03',
        'end_time': '15:00',
        'notes': 'Other payment method order',
    }, follow_redirects=False)
    assert res.status_code == 302
    return res.headers['Location'].rstrip('/').split('/')[-1]


def order_row(app, order_id):
    with app.app_context():
        return dict(get_db().execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone())


def payment_rows(app, order_id):
    with app.app_context():
        return [dict(row) for row in get_db().execute(
            'SELECT * FROM payments WHERE order_id = ? ORDER BY id', (order_id,)).fetchall()]


# --- the two forms ---------------------------------------------------------

def test_the_record_payment_form_offers_other(client):
    login(client)
    order_id = seed_customer_product_order(client)
    detail = client.get(f'/orders/{order_id}')
    assert detail.status_code == 200
    assert b'value="other">Other' in detail.data
    # The three original methods are untouched by the addition.
    for option in (b'value="cash">Cash', b'value="eft">EFT', b'value="card">Card'):
        assert option in detail.data, option


def test_the_ledger_edit_form_offers_other_and_keeps_manual(client, app):
    """Other is added; the legacy Manual option stays for historic rows."""
    login(client)
    order_id = seed_customer_product_order(client)
    with app.app_context():
        db = get_db()
        db.execute(
            """INSERT INTO payments (order_id, amount, method, reference, status, payment_date,
            deleted_at, created_at) VALUES (?, 5, 'manual', '', 'paid', ?, '', ?)""",
            (order_id, f'{TODAY} 09:00', f'{TODAY} 09:00'))
        payment_id = db.execute('SELECT id FROM payments ORDER BY id DESC LIMIT 1').fetchone()['id']
        db.commit()

    page = client.get(f'/payments/{payment_id}/edit')
    assert page.status_code == 200
    assert b'value="other"' in page.data
    assert b'value="manual" selected' in page.data
    assert b'value="other" selected' not in page.data


# --- recording and editing -------------------------------------------------

def test_a_recorded_other_payment_lands_in_the_ledger_and_on_the_balance(client, app):
    """End to end through the order page: R1 as Other, with its reference note."""
    login(client)
    order_id = seed_customer_product_order(client)
    before = order_row(app, order_id)

    res = client.post(f'/orders/{order_id}/payments', data={
        'amount': '1.00',
        'method': 'other',
        'reference': 'Cash round-up / voucher',
        'payment_date': f'{TODAY}T10:00',
    }, follow_redirects=True)
    assert res.status_code == 200
    assert b'Payment recorded' in res.data

    rows = payment_rows(app, order_id)
    assert len(rows) == 1
    assert rows[0]['method'] == 'other'
    assert rows[0]['amount'] == 1.0
    assert rows[0]['reference'] == 'Cash round-up / voucher'
    # The ledger row on the order page shows the new method and its note.
    assert b'<small>Other</small>' in res.data
    assert b'Cash round-up / voucher' in res.data
    # ...and the money really is on the balance.
    after = order_row(app, order_id)
    assert round(float(after['due_total']), 2) == round(float(before['due_total']) - 1.0, 2)


def test_record_payment_accepts_and_lowercases_other(app):
    """'Other' typed any way stores as 'other' — the reports lower-case anyway."""
    with app.app_context():
        db = get_db()
        order_id = _seed_order(db, 'ORD-341953066-A', 1)
        from app.services.payments import record_payment
        record_payment(order_id, {'amount': '10', 'method': '  Other ', 'payment_date': TODAY})
        row = db.execute('SELECT method, amount FROM payments WHERE order_id = ?',
                         (order_id,)).fetchone()
    assert (row['method'], row['amount']) == ('other', 10.0)


def test_the_edit_page_re_renders_other_as_selected_and_saves_it_back(client, app):
    login(client)
    order_id = seed_customer_product_order(client)
    client.post(f'/orders/{order_id}/payments', data={
        'amount': '1.00', 'method': 'other', 'reference': 'Voucher',
        'payment_date': f'{TODAY}T10:00',
    }, follow_redirects=True)
    payment_id = payment_rows(app, order_id)[0]['id']

    edit = client.get(f'/payments/{payment_id}/edit')
    assert edit.status_code == 200
    assert b'value="other" selected' in edit.data

    res = client.post(f'/payments/{payment_id}/edit', data={
        'amount': '2.50', 'method': 'other', 'reference': 'Voucher (corrected)',
        'payment_date': f'{TODAY}T10:30',
    }, follow_redirects=True)
    assert res.status_code == 200
    saved = payment_rows(app, order_id)[0]
    assert (saved['method'], saved['amount'], saved['reference']) == (
        'other', 2.5, 'Voucher (corrected)')


def test_an_unknown_method_is_refused_when_recording(client, app):
    """Junk must never reach the reports as unlabelled money."""
    login(client)
    order_id = seed_customer_product_order(client)
    before = order_row(app, order_id)
    res = client.post(f'/orders/{order_id}/payments', data={
        'amount': '5', 'method': 'bitcoin', 'payment_date': f'{TODAY}T10:00',
    }, follow_redirects=True)
    assert res.status_code == 200
    assert b'Payment method must be Cash, EFT, Card, Other, Use Customer Credit, or Manual' in res.data
    assert payment_rows(app, order_id) == []
    assert order_row(app, order_id)['due_total'] == before['due_total']


def test_a_legacy_method_is_never_rejected_on_edit(app):
    """A row the form cannot offer (an import) must still be editable."""
    with app.app_context():
        db = get_db()
        order_id = _seed_order(db, 'ORD-341953066-B', 1)
        db.execute(
            """INSERT INTO payments (order_id, amount, method, reference, status, payment_date,
            deleted_at, created_at) VALUES (?, 40, 'imported_bank', '', 'paid', ?, '', ?)""",
            (order_id, f'{TODAY} 09:00', f'{TODAY} 09:00'))
        db.commit()
        payment_id = db.execute('SELECT id FROM payments ORDER BY id DESC LIMIT 1').fetchone()['id']

        # Its own method passes through untouched...
        update_payment(payment_id, {'amount': '40', 'method': 'imported_bank',
                                    'payment_date': TODAY})
        assert db.execute('SELECT method FROM payments WHERE id = ?',
                          (payment_id,)).fetchone()['method'] == 'imported_bank'
        # ...while a genuinely unknown method is still refused.
        with pytest.raises(ValueError) as excinfo:
            update_payment(payment_id, {'amount': '40', 'method': 'bitcoin',
                                        'payment_date': TODAY})
        assert 'Payment method' in str(excinfo.value)
        assert db.execute('SELECT method FROM payments WHERE id = ?',
                          (payment_id,)).fetchone()['method'] == 'imported_bank'


# --- the money invariant ---------------------------------------------------

def _seed_order(db, number, branch_id, day=TODAY, status='started'):
    db.execute(
        """INSERT INTO orders (order_number, booking_type, collect_branch_id, return_branch_id,
        status, payment_status, start_at, end_at, subtotal, tax_total, deposit_total, total,
        due_total, notes, created_at)
        VALUES (?, 'return', ?, ?, ?, 'paid', ?, ?, 100, 15, 0, 115, 0, '', ?)""",
        (number, branch_id, branch_id, status, f'{day} 08:00', f'{day} 18:00', f'{day} 08:00'))
    return db.execute('SELECT id FROM orders WHERE order_number = ?', (number,)).fetchone()['id']


def _seed_payment(db, order_id, amount, method, day=TODAY, status='paid', deleted_at=''):
    db.execute(
        """INSERT INTO payments (order_id, amount, method, reference, status, payment_date,
        deleted_at, created_at) VALUES (?, ?, ?, '', ?, ?, ?, ?)""",
        (order_id, amount, method, status, f'{day} 09:00', deleted_at, f'{day} 09:00'))


def seeded_day(app):
    """One depot's day: every method, plus the rows that must not count."""
    with app.app_context():
        db = get_db()
        one = _seed_order(db, 'ORD-341953066-C', 1)
        _seed_payment(db, one, 500.0, 'card')
        _seed_payment(db, one, 250.0, 'Cash')          # case-insensitive
        _seed_payment(db, one, 700.0, 'eft')
        _seed_payment(db, one, 100.0, 'other')
        _seed_payment(db, one, 40.0, 'manual')         # legacy row
        _seed_payment(db, one, 60.0, 'deposit_applied')  # applied security deposit
        _seed_payment(db, one, 20.0, '')               # no method at all
        _seed_payment(db, one, 999.0, 'other', status='pending')
        _seed_payment(db, one, 999.0, 'other', deleted_at=f'{TODAY} 09:05')
        _seed_payment(db, one, 999.0, 'other', day=YESTERDAY)
        two = _seed_order(db, 'ORD-341953066-D', 2)
        _seed_payment(db, two, 77.0, 'other')
        db.commit()
    return one, two


def metrics(app, **session_values):
    with app.test_request_context('/dashboard'):
        flask_session.clear()
        flask_session.update(session_values)
        return reports_service.dashboard_day_metrics(day=TODAY)


def test_other_gathers_every_method_that_is_not_cash_eft_or_card(app):
    seeded_day(app)
    day = metrics(app, user_id=1, user_role='owner')

    assert day['card_payments'] == 500.0
    assert day['cash_payments'] == 250.0
    assert day['eft_payments'] == 700.0
    # 100 'other' + 40 legacy 'manual' + 60 applied deposit + 20 with no method
    # + 77 recorded at depot 2. Pending, archived and yesterday's rows are out.
    assert day['other_payments'] == 100.0 + 40.0 + 60.0 + 20.0 + 77.0
    assert day['revenue'] == 500.0 + 250.0 + 700.0 + 100.0 + 40.0 + 60.0 + 20.0 + 77.0
    # The invariant the plan called out: no money hides between the cards.
    assert day['revenue'] == (day['card_payments'] + day['cash_payments']
                              + day['eft_payments'] + day['other_payments'])


def test_the_other_line_follows_the_branch_and_day_scope(app):
    seeded_day(app)
    depot_one = metrics(app, user_id=3, user_role='staff', can_view_all_branches=False,
                        branch_id=1, branch_ids=[1])
    depot_two = metrics(app, user_id=2, user_role='staff', can_view_all_branches=False,
                        branch_id=2, branch_ids=[2])
    assert depot_one['other_payments'] == 100.0 + 40.0 + 60.0 + 20.0
    assert depot_two['other_payments'] == 77.0
    assert depot_one['other_payments'] + depot_two['other_payments'] == metrics(
        app, user_id=1, user_role='owner')['other_payments']


def test_the_cash_drawer_is_untouched_by_an_other_payment(app):
    """Other money is not in the drawer and not on the card machine."""
    with app.app_context():
        db = get_db()
        order_id = _seed_order(db, 'ORD-341953066-E', 1)
        _seed_payment(db, order_id, 500.0, 'cash')
        db.commit()

    with app.test_request_context('/dashboard'):
        flask_session.clear()
        flask_session.update({'user_id': 1, 'user_role': 'owner'})
        before = cash.day_summary(day=TODAY, branch_id=1)

    with app.app_context():
        db = get_db()
        _seed_payment(db, order_id, 300.0, 'other')
        db.commit()

    with app.test_request_context('/dashboard'):
        flask_session.clear()
        flask_session.update({'user_id': 1, 'user_role': 'owner'})
        after = cash.day_summary(day=TODAY, branch_id=1)

    assert after['cash_received'] == before['cash_received'] == 500.0
    assert after['card_received'] == before['card_received'] == 0.0
    assert after['expected'] == before['expected']
    assert after['expected_card'] == before['expected_card']


# --- what the client actually sees ----------------------------------------

def test_the_dashboard_card_and_the_day_report_lines_carry_other(client, app):
    seeded_day(app)
    login(client)

    page = client.get('/dashboard').get_data(as_text=True)
    assert re.search(r'Total other payments</small><b>R297\.00</b>', page), \
        'the dashboard is missing the other-payments card'

    # The day report's one source of truth (PDF *and* CSV) carries the line too.
    with app.test_request_context('/dashboard'):
        flask_session.clear()
        flask_session.update({'user_id': 1, 'user_role': 'owner'})
        report = cash.day_report(day=TODAY, branch_id=1)
    rows = {row[1]: row[2] for row in cash.day_report_rows(report) if row[0] == 'Dashboard'}
    assert rows['Total other payments'] == 'R297.00'
    assert rows['Revenue for the day'] == 'R1747.00'

    csv_body = client.get('/cash-up/export.csv?branch=1').get_data(as_text=True)
    assert 'Total other payments,R297.00' in csv_body

    pdf = client.get('/cash-up/report.pdf?branch=1')
    assert pdf.status_code == 200 and pdf.data.startswith(b'%PDF-')
    assert 'Total other payments' in _drawn_text(pdf.data), \
        'the day-report PDF never draws the other-payments line'


def test_the_reports_and_the_ledger_group_other_automatically(client, app):
    """No grouping code was touched: the ledger and Reports pick the value up."""
    seeded_day(app)
    login(client)

    with app.test_request_context('/dashboard'):
        flask_session.clear()
        flask_session.update({'user_id': 1, 'user_role': 'owner'})
        all_time = {row['method']: row['total'] for row in reports_service.payments_by_method()}
        today = {row['method']: row['total'] for row in reports_service.payments_by_method(
            start_date=TODAY, end_date=TODAY)}
    # Every day at once: the Other rows are the 100 + 77 shown for the day plus
    # yesterday's 999, while pending and archived rows are excluded as always.
    assert all_time['other'] == 1176.0
    assert all_time['manual'] == 40.0
    assert today['other'] == 177.0
    # The three named methods are untouched by the new one. Reports groups the
    # raw stored value (pre-existing: 'Cash' and 'cash' are two rows there),
    # while the dashboard cards compare case-insensitively, so sum both cases.
    summed = {}
    for method, total in today.items():
        key = (method or '').lower()
        summed[key] = summed.get(key, 0.0) + total
    assert (summed['card'], summed['cash'], summed['eft']) == (500.0, 250.0, 700.0)

    ledger = client.get('/payments').get_data(as_text=True)
    assert 'Other' in ledger
    reports_page = client.get('/reports').get_data(as_text=True)
    assert 'other' in reports_page.lower()
