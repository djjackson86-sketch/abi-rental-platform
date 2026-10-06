"""ABI-341953133: actual legacy receipts and independently visible notes."""
import pytest
from flask import session
from app.db import get_db
from app.services import cash, reports
from app.services.legacy_balances import upsert_legacy_balance, record_legacy_payment
from test_cash_up import app, client, login, _seed_payment, _seed_deposit_refund, TODAY, YESTERDAY


def customer(app, branch=1):
    with app.app_context():
        db = get_db()
        result = db.execute("INSERT INTO customers (name, branch_id, created_at) VALUES ('Legacy test', ?, ?)", (branch, TODAY))
        db.commit()
        return result.lastrowid


def receipt(app, cid, amount, method='cash', day=TODAY, status='active', deleted=''):
    with app.app_context():
        db = get_db()
        db.execute("""INSERT INTO legacy_balance_payments
            (customer_id, amount, method, payment_date, status, deleted_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""", (cid, amount, method, day, status, deleted, TODAY + 'T09:00:00'))
        db.commit()


def metrics(app, day=TODAY, branch=1):
    with app.test_request_context('/dashboard'):
        session['user_id'] = 1
        session['user_role'] = 'owner'
        return reports.dashboard_day_metrics(day, branch), cash.day_summary(day, branch)


def test_receipts_methods_refunds_and_report_parity(app, client):
    cid = customer(app)
    for amount, method in [(120, 'cash'), (230, 'CARD'), (340, 'eft'), (45, 'other'), (5, 'manual')]:
        receipt(app, cid, amount, method)
    order = _seed_payment(app, 80, 'cash')
    _seed_deposit_refund(app, order, 20, 'cash')
    card_order = _seed_payment(app, 70, 'card', number='ORD-CARD')
    _seed_deposit_refund(app, card_order, 30, 'card')
    m, s = metrics(app)
    assert m['revenue'] == 890
    assert (m['cash_payments'], m['card_payments'], m['eft_payments'], m['other_payments']) == (200, 270, 340, 50)
    assert (s['cash_received'], s['expected'], s['card_received']) == (200, 180, 270)
    login(client)
    for route in ['/dashboard?branch=1', '/cash-up/export.csv?branch=1&day=' + TODAY, '/cash-up/report.pdf?branch=1&day=' + TODAY]:
        response = client.get(route)
        assert response.status_code == 200, route
    csv = client.get('/cash-up/export.csv?branch=1&day=' + TODAY).get_data(as_text=True)
    assert 'R200.00' in csv and 'R270.00' in csv


def test_payment_date_fallback_and_inactive_deleted_exclusion(app):
    cid = customer(app)
    receipt(app, cid, 10, day=YESTERDAY)
    receipt(app, cid, 20, day='')
    receipt(app, cid, 100, status='archived')
    receipt(app, cid, 200, deleted=TODAY)
    receipt(app, cid, 300, status='pending')
    assert metrics(app)[0]['cash_payments'] == 20
    assert metrics(app, YESTERDAY)[1]['cash_received'] == 10


def test_one_receipt_settling_multiple_imports_counts_once(app):
    cid = customer(app)
    with app.test_request_context('/'):
        session['user_id'] = 1
        session['user_role'] = 'owner'
        for number in ('OLD-ONE', 'OLD-TWO'):
            upsert_legacy_balance(cid, 'old-customer', number, 100)
        get_db().commit()
        record_legacy_payment(cid, {'amount': '200', 'method': 'cash', 'payment_date': TODAY})
    assert metrics(app)[0]['cash_payments'] == 200
    assert metrics(app)[1]['cash_received'] == 200


def test_branch_isolation_unassigned_and_aggregate_no_double_count(app):
    for branch, amount in [(1, 10), (2, 20), (None, 30)]:
        receipt(app, customer(app, branch), amount)
    assert metrics(app, branch=1)[0]['cash_payments'] == 10
    assert metrics(app, branch=2)[0]['cash_payments'] == 20
    assert metrics(app, branch=None)[0]['cash_payments'] == 60
    with app.test_request_context('/'):
        session['user_id'] = 1
        session['user_role'] = 'owner'
        assert cash.aggregate_day_summary(TODAY)['cash_received'] == 30
    # A forged requested depot cannot widen a depot-scoped sign-in.
    from app.services.access import create_additional_user
    with app.app_context():
        user_id, error = create_additional_user('Legacy scoped', 'test12345', branch_id=1)
        assert user_id and not error
    scoped = app.test_client()
    login(scoped, 'Legacy scoped', 'test12345')
    body = scoped.get('/dashboard?branch=2').get_data(as_text=True)
    assert 'R10.00' in body and 'R20.00' not in body and 'R30.00' not in body


@pytest.mark.parametrize('day', [TODAY, YESTERDAY])
@pytest.mark.parametrize('branch', ['1', 'all'])
def test_recorded_notes_labeled_escaped_and_exported(app, client, day, branch):
    with app.test_request_context('/'):
        session['user_id'] = 1
        session['user_role'] = 'owner'
        cash.save_cash_up(day, '0', branch_id=1, cash_up_notes='Drawer <script>\nSecond cash line')
        cash.save_pos_cash_up(day, '0', branch_id=1, pos_cash_up_notes='Terminal <b>\nSecond POS line')
        cash.save_notes(day, 'End of day kept', branch_id=1)
    login(client)
    body = client.get('/dashboard?branch=' + branch + '&day=' + day).get_data(as_text=True)
    assert body.count('recorded-cashup-note') == 2
    assert '<strong>Cash up notes</strong>' in body and '<strong>POS Cashup notes</strong>' in body
    assert 'Drawer &lt;script&gt;\nSecond cash line' in body
    assert 'Terminal &lt;b&gt;\nSecond POS line' in body
    assert 'white-space: pre-wrap' in body
    csv = client.get('/cash-up/export.csv?branch=' + branch + '&day=' + day)
    assert csv.status_code == 200
    text = csv.get_data(as_text=True)
    assert 'Cash up notes,Notes,' in text and 'POS Cashup notes,Notes,' in text
    assert 'Second cash line' in text and 'Second POS line' in text and 'End of day kept' in text
    pdf = client.get('/cash-up/report.pdf?branch=' + branch + '&day=' + day)
    assert pdf.status_code == 200 and pdf.data.startswith(b'%PDF')
    assert b'CASH UP NOTES' in pdf.data and b'POS CASHUP NOTES' in pdf.data
    assert b'Second cash line' in pdf.data and b'Second POS line' in pdf.data


def test_blank_notes_do_not_create_summary_or_export_sections(app, client):
    with app.test_request_context('/'):
        session['user_id'] = 1
        session['user_role'] = 'owner'
        cash.save_cash_up(TODAY, '0', branch_id=1, cash_up_notes='  ')
        cash.save_pos_cash_up(TODAY, '0', branch_id=1, pos_cash_up_notes='\n')
    login(client)
    assert 'recorded-cashup-note' not in client.get('/dashboard?branch=1').get_data(as_text=True)
    text = client.get('/cash-up/export.csv?branch=1&day=' + TODAY).get_data(as_text=True)
    assert 'Cash up notes,Notes,' not in text and 'POS Cashup notes,Notes,' not in text
