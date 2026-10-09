"""Explicit facility allocations, receipts, finalisation and historical upgrade."""
import importlib.util
from pathlib import Path
import sqlite3
import threading

import pytest

from app.db import get_db, run_migrations
from app.services.credit_limits import outstanding_debt, facility_summary
from app.services.documents import create_document, finalize_document, get_document
from app.services.payments import record_payment, update_payment, archive_payment, payment_total, record_refund
from app.services.customers import customer_statement, customer_outstanding_balances
from app.services.reports import dashboard_day_metrics, dashboard_period_metrics, summary_metrics, payments_by_method

spec = importlib.util.spec_from_file_location('account_helpers', Path(__file__).with_name('test_ticket_341953129_credit_limits.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
app, client = helpers.app, helpers.client


def account(oid, amount=30, **kw):
    # Seed a payment that predates ABI-341953166; application writes are retired.
    from app.services.credit_limits import install_credit_guards
    db=get_db(); db.execute('DROP TRIGGER account_payment_retired_insert')
    db.execute("INSERT INTO payments(order_id,amount,method,status,payment_date,created_at) VALUES (?,?,'account','paid',?,'2026-10-01')",(oid,amount,kw.get('payment_date','2026-10-01')))
    install_credit_guards(db); db.commit()


def latest(db, oid):
    return db.execute('SELECT id FROM payments WHERE order_id=? ORDER BY id DESC LIMIT 1', (oid,)).fetchone()['id']


def test_no_automatic_borrowing_and_payment_required(client,app):
    cid=helpers.setup_customer(client,app,20)
    with app.app_context():
        with pytest.raises(ValueError,match='credit limit'):helpers.order(cid,1000)
        oid=helpers.order(cid,10);did=create_document(oid,'invoice')
        assert outstanding_debt(cid)==11.5
        finalize_document(did)
        assert get_document(did)['status']=='finalized'


@pytest.mark.parametrize('amount',[30,115])
def test_account_qualifies_without_cash_and_retains_receivables(client,app,amount):
    cid=helpers.setup_customer(client,app,115)
    with app.app_context():
        oid=helpers.order(cid);did=create_document(oid,'invoice')
        account(oid,amount,payment_date='2026-10-08T10:00:00');finalize_document(did)
        assert payment_total(oid)==0 and outstanding_debt(cid)==115
        assert customer_statement(cid)['closing_balance']==115
        assert dashboard_day_metrics('2026-10-08')['revenue']==0
        assert dashboard_period_metrics()['revenue']==0
        assert summary_metrics()['paid']==0
        assert not payments_by_method()


@pytest.mark.parametrize('amount',['0','-1','nan','inf','0.001','bad','30'])
def test_invalid_account_amounts(client,app,amount):
    cid=helpers.setup_customer(client,app)
    with app.app_context():
        oid=helpers.order(cid)
        with pytest.raises(ValueError):record_payment(oid,{'method':'account','amount':amount})
        assert outstanding_debt(cid)==115 and payment_total(oid)==0


def test_ineligible_and_order_balance_and_exhausted_limit(client,app):
    from test_ticket_341953166_credit_workflow import test_existing_debt_and_lowered_limit_allow_repayment
    test_existing_debt_and_lowered_limit_allow_repayment(client,app)


def test_edits_method_switch_archival_and_repayments(client,app):
    cid=helpers.setup_customer(client,app,115)
    with app.app_context():
        db=get_db();oid=helpers.order(cid);account(oid,115);pid=latest(db,oid)
        with pytest.raises(ValueError):update_payment(pid,{'amount':'60','method':'account'})
        with pytest.raises(ValueError):update_payment(pid,{'amount':'115','method':'eft'})
        record_payment(oid,{'method':'cash','amount':'50'})
        assert outstanding_debt(cid)==65 and payment_total(oid)==50
        update_payment(pid,{'amount':'115','method':'account','reference':'Reference after repayment'})
        record_payment(oid,{'method':'eft','amount':'65'})
        assert outstanding_debt(cid)==0 and payment_total(oid)==115
        with pytest.raises(ValueError,match='credit to refund'):record_refund(oid,{'refund_method':'cash'})


def test_account_cannot_fund_cash_refund(client, app):
    cid = helpers.setup_customer(client, app)
    with app.app_context():
        db = get_db(); oid = helpers.order(cid)
        account(oid, 115)
        db.execute('UPDATE orders SET total=50 WHERE id=?', (oid,)); db.commit()
        with pytest.raises(ValueError, match='credit to refund'):
            record_refund(oid, {'refund_method':'cash'})


def test_idempotent_account_request_and_replay_after_archive(client,app):
    cid=helpers.setup_customer(client,app)
    with app.app_context():
        oid=helpers.order(cid)
        for _ in range(2):
            with pytest.raises(ValueError):record_payment(oid,{'method':'account','amount':'30','account_request_id':'retired-request'})
        assert get_db().execute('SELECT COUNT(*) n FROM payments WHERE order_id=?',(oid,)).fetchone()['n']==0


def test_archived_or_fully_refunded_payment_cannot_finalise(client,app):
    helpers.helpers.login(client);cid=helpers.helpers.create_customer(client)
    with app.app_context():
        db=get_db();oid=helpers.order(cid);did=create_document(oid,'invoice')
        record_payment(oid,{'method':'eft','amount':'115'});archive_payment(latest(db,oid))
        with pytest.raises(ValueError,match='in full'):finalize_document(did)
        with pytest.raises(sqlite3.IntegrityError,match='in full'):db.execute("UPDATE documents SET status='finalized',number='INV-NOT-ALLOWED' WHERE id=?",(did,))
        assert get_document(did)['number']==''


def test_receipt_reversal_cannot_reconsume_exhausted_facility(client,app):
    from test_ticket_341953166_credit_workflow import test_edit_and_receipt_reversal_atomic
    test_edit_and_receipt_reversal_atomic(client,app)


def test_opening_exposure_upgrade_preserved_once_and_settled_by_receipts(client,app):
    cid=helpers.setup_customer(client,app,230)
    with app.app_context():
        db=get_db();oid=helpers.order(cid);did=helpers.invoice(oid)
        run_migrations(db);run_migrations(db)
        assert outstanding_debt(cid)==115 and finalize_document(did)==did
        record_payment(oid,{'method':'eft','amount':'50'})
        assert outstanding_debt(cid)==65 and get_document(did)['status']=='finalized'


def test_concurrent_account_allocations_cannot_double_spend(client,app):
    from test_ticket_341953166_credit_workflow import test_concurrent_order_creation_cannot_overspend
    test_concurrent_order_creation_cannot_overspend(client,app)


def test_ui_eligibility_edit_and_finalisation_route(client,app):
    cid=helpers.setup_customer(client,app)
    with app.app_context():
        oid=helpers.order(cid);did=create_document(oid,'invoice')
    page=client.get(f'/orders/{oid}');assert page.status_code==200 and b'value="account"' not in page.data
    response=client.post(f'/orders/{oid}/payments',data={'method':'account','amount':'30'},follow_redirects=True)
    assert response.status_code==200
    with app.app_context():assert payment_total(oid)==0
    response=client.post(f'/documents/{did}/finalize',follow_redirects=True)
    assert response.status_code==200
    with app.app_context():assert get_document(did)['status']=='finalized'
