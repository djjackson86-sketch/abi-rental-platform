"""Refundable security is held money, not revenue or Other Revenue."""
import json
import re
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from app.db import get_db
from app.services import reports
from app.services.cash import cash_received
from app.services.orders import _upsert_deposit_applied_payment, order_counts
from test_ticket_341953282_dashboard import app, client, login, add_customer, add_order, add_depot_user

RECEIVED = '2026-11-10'
APPLIED = '2026-11-17'


def seed(app, *, applied=0, deposit=750, charges=1300, paid=1750,
         branch=1, option='security_deposit', status='returned'):
    with app.app_context():
        db = get_db()
        cid = add_customer(db, 'Deposit revenue client', branch)
        oid = add_order(db, 'DEPOSIT-REVENUE', cid, status, charges + deposit,
                        branch_id=branch, payment_status='paid')
        db.execute('UPDATE orders SET deposit_option=?,deposit_total=?,deposit_applied_amount=? WHERE id=?',
                   (option, deposit, applied, oid))
        db.execute("INSERT INTO payments (order_id,amount,method,status,payment_date,created_at) VALUES (?,?,'cash','paid',?,?)",
                   (oid, paid, RECEIVED, RECEIVED+'T10:00:00'))
        if applied:
            db.execute("INSERT INTO payments (order_id,amount,method,status,payment_date,created_at) VALUES (?,?,'deposit_applied','paid',?,?)",
                       (oid, applied, APPLIED, APPLIED+'T12:00:00'))
        db.commit()
        return oid


def assert_period(start, end, expected, branch=None):
    categories = reports.received_revenue_by_category(start, end, branch)
    assert categories == {'trailer_rentals': expected, 'sales_repairs': 0, 'unattributed': 0}
    assert reports.dashboard_period_metrics(start, end, branch)['revenue'] == expected
    days = reports.received_revenue_by_day(start or '2000-01-01', end or '2099-12-31', branch)
    assert round(sum(days.values()), 2) == expected
    graph = reports.monthly_received_revenue(2026, branch, 'gross', start, end)
    assert round(sum(v or 0 for month in graph for v in month['values']), 2) == expected
    if start == end and start:
        assert reports.dashboard_day_metrics(start, branch)['revenue'] == expected


@pytest.mark.parametrize('paid,expected', [(0,0),(400,0),(750,0),(950,200),(1750,1000)])
def test_held_deposit_reserved_once_across_unallocated_receipts(app, paid, expected):
    seed(app, paid=paid)
    with app.app_context():
        assert_period(RECEIVED, RECEIVED, expected)
        assert order_counts()['revenue'] == expected
        assert reports.dashboard_day_metrics(RECEIVED)['cash_payments'] == paid
        assert cash_received(RECEIVED, None) == paid


@pytest.mark.parametrize('applied,expected', [(0,0),(150,150),(300,300),(750,300)])
def test_full_partial_application_later_day_and_charge_cap(app, applied, expected):
    seed(app, applied=applied)
    with app.app_context():
        assert_period(RECEIVED, RECEIVED, 1000)
        assert_period(APPLIED, APPLIED, expected)
        assert_period(RECEIVED, APPLIED, 1000+expected)
        assert_period(None, None, 1000+expected)
        day = reports.dashboard_day_metrics(APPLIED)
        assert day['other_payments'] == 0  # no new cash was received
        assert day['cash_payments'] == 0
        assert reports.summary_metrics()['revenue'] == 1300  # booked charges, no second application


@pytest.mark.parametrize('method', ['cash','card','eft','customer_credit'])
def test_deposit_refund_and_credit_transfer_never_reduce_revenue(app, method):
    oid = seed(app, applied=300)
    with app.app_context():
        db = get_db()
        db.execute('UPDATE orders SET deposit_refund_amount=450,deposit_process_method=?,deposit_processed_at=? WHERE id=?',
                   (method, APPLIED+'T15:00:00', oid))
        db.commit()
        assert_period(APPLIED, APPLIED, 300)
        assert_period(RECEIVED, APPLIED, 1300)
        if method == 'cash':
            assert reports.dashboard_day_metrics(APPLIED)['cash_payments'] == -450


def test_reversal_and_reprocessing_use_active_application_only(app, monkeypatch):
    oid = seed(app, applied=300)
    with app.app_context():
        db = get_db()
        _upsert_deposit_applied_payment(oid, 0)
        db.execute('UPDATE orders SET deposit_applied_amount=0 WHERE id=?',(oid,))
        db.commit()
        assert_period(APPLIED, APPLIED, 0)
        monkeypatch.setattr('app.services.orders.now', lambda: '2026-11-18T10:00:00')
        _upsert_deposit_applied_payment(oid, 200)
        db.execute('UPDATE orders SET deposit_applied_amount=200 WHERE id=?',(oid,))
        db.commit()
        assert_period('2026-11-18','2026-11-18',200)
        assert_period(RECEIVED,'2026-11-18',1200)


def test_edit_does_not_move_application_capture_date(app, monkeypatch):
    oid = seed(app, applied=300)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE payments SET payment_date='' WHERE method='deposit_applied'")
        monkeypatch.setattr('app.services.orders.now', lambda: '2026-12-20T10:00:00')
        _upsert_deposit_applied_payment(oid, 150)
        db.execute('UPDATE orders SET deposit_applied_amount=150 WHERE id=?',(oid,))
        db.commit()
        assert_period(APPLIED, APPLIED, 150)
        assert_period('2026-12-20','2026-12-20',0)
        assert db.execute("SELECT created_at FROM payments WHERE method='deposit_applied'").fetchone()['created_at'] == APPLIED+'T12:00:00'


@pytest.mark.parametrize('option', ['damage_waiver','no_deposit'])
def test_waiver_and_no_deposit_keep_existing_signed_receipt_basis(app, option):
    oid = seed(app, deposit=0, charges=1000, paid=1000, option=option)
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO payments (order_id,amount,method,status,payment_date,created_at) VALUES (?,-50,'cash','paid',?,?)", (oid, APPLIED, APPLIED))
        db.commit()
        assert_period(RECEIVED, APPLIED, 950)


def test_branch_filters_and_missing_category_application(app, client):
    seed(app, applied=300, branch=2, status='draft')
    with app.app_context():
        # The applied deposit belongs to its rental order, never Other Revenue,
        # even when historical status data has lost the rental category.
        assert_period(APPLIED, APPLIED, 300, 2)
        assert_period(APPLIED, APPLIED, 0, 1)
    add_depot_user(app, 'Deposit staff', 1)
    login(client, 'Deposit staff', 'staff123')
    with client.session_transaction() as session:
        session_copy = dict(session)
    with app.test_request_context('/dashboard?branch=2'):
        from flask import session
        session.update(session_copy)
        assert_period(APPLIED, APPLIED, 0, 2)


def test_multiple_receipts_reserve_deposit_once_and_ignore_archived_pending(app):
    oid = seed(app, paid=500, applied=300)
    with app.app_context():
        db = get_db()
        for amount,status,deleted in [(1250,'paid',''),(100,'pending',''),(100,'paid','2026-11-11')]:
            db.execute('INSERT INTO payments (order_id,amount,method,status,payment_date,created_at,deleted_at) VALUES (?,?,?,?,?,?,?)',
                       (oid,amount,'eft',status,'2026-11-11','2026-11-11',deleted))
        db.commit()
        assert_period(RECEIVED,RECEIVED,0)
        assert_period('2026-11-11','2026-11-11',1000)
        assert_period(APPLIED,APPLIED,300)


def test_real_browser_cards_chart_and_mobile(app, client):
    seed(app, applied=300)
    login(client)
    cookie = client.get_cookie('session')
    server = make_server('127.0.0.1',0,app,threaded=True)
    thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    output = Path('C:/Users/djjac/AppData/Local/hermes/cache/scratch/ABI-341953330')
    output.mkdir(parents=True,exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome',headless=True)
            for width,height in [(1440,1000),(390,844)]:
                context = browser.new_context(viewport={'width':width,'height':height})
                context.add_cookies([{'name':'session','value':cookie.value,'domain':'127.0.0.1','path':'/'}])
                page = context.new_page(); errors=[]
                page.on('pageerror',lambda err: errors.append(str(err)))
                query = '?start_date='+APPLIED+'&end_date='+APPLIED
                page.goto(f'http://127.0.0.1:{server.server_port}/dashboard'+query,wait_until='networkidle')
                cards = page.locator('.dashboard-revenue-cards')
                # Card HTML is also checked on the lazy route below.
                assert 'Trailer rentals revenue' in page.locator('body').text_content()
                assert 'R300.00' in page.locator('body').inner_text()
                partial = client.get('/dashboard/partials/revenue'+query).get_data(as_text=True)
                assert 'R300.00' in partial and 'OTHER REVENUE' in partial
                page.screenshot(path=str(output/f'dashboard-{width}.png'),full_page=True)
                page.goto(f'http://127.0.0.1:{server.server_port}/reports'+query+'&chart_year=2026',wait_until='networkidle')
                chart = page.locator('.monthly-revenue')
                assert 'Rentals · November 17: R300.00' in chart.locator('circle title').all_text_contents()
                with page.expect_navigation(wait_until='networkidle'):
                    chart.locator('[name=revenue_type]').select_option('unattributed')
                assert 'Other Revenue · November 17: R0.00' in chart.locator('circle title').all_text_contents()
                chart.scroll_into_view_if_needed()
                page.screenshot(path=str(output/f'reports-{width}.png'))
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert not errors, errors
                context.close()
            browser.close()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=10)
        assert not thread.is_alive()
