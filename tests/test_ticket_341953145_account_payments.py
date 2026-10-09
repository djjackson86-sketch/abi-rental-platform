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
    return record_payment(oid, {'amount': str(amount), 'method': 'account', **kw})


def latest(db, oid):
    return db.execute('SELECT id FROM payments WHERE order_id=? ORDER BY id DESC LIMIT 1', (oid,)).fetchone()['id']


def test_no_automatic_borrowing_and_payment_required(client, app):
    cid = helpers.setup_customer(client, app, 20)
    with app.app_context():
        oid = helpers.order(cid, 1000)  # order value may exceed facility, not borrowing
        did = create_document(oid, 'invoice')
        assert outstanding_debt(cid) == 0
        with pytest.raises(ValueError, match='at least one payment'):
            finalize_document(did)
        assert get_document(did)['status'] == 'draft' and get_document(did)['number'] == ''
        record_payment(oid, {'method': 'eft', 'amount': '1'})
        finalize_document(did)
        assert outstanding_debt(cid) == 0
        assert get_document(did)['status'] == 'finalized'
        assert helpers.order(cid, 2000)


@pytest.mark.parametrize('amount', [30, 115])
def test_account_qualifies_without_cash_and_retains_receivables(client, app, amount):
    cid = helpers.setup_customer(client, app, 115)
    with app.app_context():
        oid = helpers.order(cid)
        did = create_document(oid, 'invoice')
        account(oid, amount, payment_date='2026-10-08T10:00:00')
        finalize_document(did)
        assert payment_total(oid) == amount
        assert outstanding_debt(cid) == amount
        assert customer_outstanding_balances([cid])[cid] == 115
        statement = customer_statement(cid)
        assert statement['paid_total'] == 0
        assert statement['closing_balance'] == 115
        assert dashboard_day_metrics('2026-10-08')['revenue'] == 0
        assert dashboard_day_metrics('2026-10-08')['other_payments'] == 0
        assert dashboard_period_metrics()['revenue'] == 0
        assert summary_metrics()['paid'] == 0
        assert not payments_by_method()


@pytest.mark.parametrize('amount', ['0', '-1', 'nan', 'inf', '0.001', 'bad'])
def test_invalid_account_amounts(client, app, amount):
    cid = helpers.setup_customer(client, app)
    with app.app_context():
        oid = helpers.order(cid)
        with pytest.raises(ValueError): account(oid, amount)
        assert outstanding_debt(cid) == 0 and payment_total(oid) == 0


def test_ineligible_and_order_balance_and_exhausted_limit(client, app):
    cid = helpers.setup_customer(client, app, 60)
    with app.app_context():
        db = get_db()
        oid = helpers.order(cid)
        with pytest.raises(ValueError, match='order balance'): account(oid, 116)
        with pytest.raises(ValueError, match='credit limit'): account(oid, 61)
        account(oid, 60)
        other = helpers.order(cid)
        with pytest.raises(ValueError, match='credit limit'): account(other, 1)
        record_payment(other, {'method': 'cash', 'amount': '115'})
        assert outstanding_debt(cid) == 60
        db.execute('UPDATE customers SET credit_allowed=0 WHERE id=?', (cid,)); db.commit()
        with pytest.raises(ValueError, match='enabled'): account(other, 1)
        assert outstanding_debt(cid) == 60


def test_edits_method_switch_archival_and_repayments(client, app):
    cid = helpers.setup_customer(client, app, 115)
    with app.app_context():
        db = get_db(); oid = helpers.order(cid)
        account(oid, 40); pid = latest(db, oid)
        update_payment(pid, {'amount':'60', 'method':'account'})
        assert outstanding_debt(cid) == 60
        update_payment(pid, {'amount':'60', 'method':'eft'})
        assert outstanding_debt(cid) == 0
        update_payment(pid, {'amount':'60', 'method':'account'})
        assert outstanding_debt(cid) == 60
        archive_payment(pid)
        assert outstanding_debt(cid) == 0 and payment_total(oid) == 0
        account(oid, 115)
        record_payment(oid, {'method':'cash', 'amount':'50'})
        assert outstanding_debt(cid) == 65
        assert payment_total(oid) == 115  # no double counting / false overpayment
        account_id=db.execute("SELECT id FROM payments WHERE order_id=? AND method='account' AND status='paid'",(oid,)).fetchone()['id']
        update_payment(account_id, {'amount':'115','method':'account','reference':'Reference after repayment'})
        assert outstanding_debt(cid)==65
        record_payment(oid, {'method':'eft', 'amount':'65'})
        assert outstanding_debt(cid) == 0 and payment_total(oid) == 115
        with pytest.raises(ValueError, match='credit to refund'):
            record_refund(oid, {'refund_method':'cash'})


def test_account_cannot_fund_cash_refund(client, app):
    cid = helpers.setup_customer(client, app)
    with app.app_context():
        db = get_db(); oid = helpers.order(cid)
        account(oid, 115)
        db.execute('UPDATE orders SET total=50 WHERE id=?', (oid,)); db.commit()
        with pytest.raises(ValueError, match='credit to refund'):
            record_refund(oid, {'refund_method':'cash'})


def test_idempotent_account_request_and_replay_after_archive(client, app):
    cid = helpers.setup_customer(client, app)
    with app.app_context():
        db = get_db(); oid = helpers.order(cid)
        account(oid, 30, account_request_id='same-request')
        account(oid, 30, account_request_id='same-request')
        assert outstanding_debt(cid) == 30
        assert db.execute('SELECT COUNT(*) FROM payments WHERE order_id=?', (oid,)).fetchone()[0] == 1
        with pytest.raises(ValueError, match='already been used'):
            account(oid, 31, account_request_id='same-request')
        archive_payment(latest(db, oid))
        account(oid, 30, account_request_id='same-request')
        assert outstanding_debt(cid) == 0


def test_archived_or_fully_refunded_payment_cannot_finalise(client, app):
    cid = helpers.setup_customer(client, app)
    with app.app_context():
        db = get_db(); oid = helpers.order(cid); did = create_document(oid, 'invoice')
        account(oid); archive_payment(latest(db, oid))
        with pytest.raises(ValueError, match='at least one payment'): finalize_document(did)
        with pytest.raises(sqlite3.IntegrityError, match='at least one payment'):
            db.execute("UPDATE documents SET status='finalized',number='INV-NOT-ALLOWED' WHERE id=?", (did,))
        assert get_document(did)['number'] == ''


def test_receipt_reversal_cannot_reconsume_exhausted_facility(client, app):
    cid = helpers.setup_customer(client, app, 115)
    with app.app_context():
        db = get_db(); a,b = helpers.order(cid), helpers.order(cid)
        account(a,115); record_payment(a, {'method':'cash','amount':'115'}); receipt=latest(db,a)
        account(b,115)
        with pytest.raises(ValueError, match='credit limit'): archive_payment(receipt)
        assert outstanding_debt(cid) == 115
        assert db.execute('SELECT deleted_at FROM payments WHERE id=?',(receipt,)).fetchone()[0] in ('',None)


def test_opening_exposure_upgrade_preserved_once_and_settled_by_receipts(client, app):
    cid = helpers.setup_customer(client, app, 230)
    with app.app_context():
        db = get_db(); oid = helpers.order(cid); did = create_document(oid, 'invoice')
        # Simulate a pre-ticket finalised unpaid invoice, then first upgrade.
        db.execute('DROP TRIGGER invoice_payment_required_update')
        db.execute("UPDATE documents SET status='finalized', number='INV-HISTORICAL' WHERE id=?", (did,))
        db.execute("DELETE FROM credit_facility_opening"); db.commit()
        run_migrations(db)
        assert outstanding_debt(cid) == 0
        assert get_document(did)['status'] == 'finalized'
        assert finalize_document(did) == did  # no retrospective block
        new = helpers.order(cid); record_payment(new, {'method':'cash','amount':'1'})
        finalize_document(create_document(new,'invoice'))
        run_migrations(db); run_migrations(db)
        assert outstanding_debt(cid) == 0  # no new implicit exposure
        account(oid,30)
        assert outstanding_debt(cid) == 30  # only the saved Account allocation counts
        record_payment(oid, {'method':'eft','amount':'50'})
        assert outstanding_debt(cid) == 30
        assert get_document(did)['number'] == 'INV-HISTORICAL'


def test_concurrent_account_allocations_cannot_double_spend(client, app):
    cid = helpers.setup_customer(client, app, 115)
    with app.app_context(): ids=[helpers.order(cid),helpers.order(cid)]
    barrier=threading.Barrier(2); outcomes=[]
    def worker(oid):
        db=sqlite3.connect(app.config['DATABASE'], timeout=10)
        try:
            barrier.wait()
            db.execute("INSERT INTO payments(order_id,amount,method,status,created_at) VALUES (?,115,'account','paid','2026-10-08')",(oid,))
            db.commit(); outcomes.append('ok')
        except sqlite3.IntegrityError: outcomes.append('blocked')
        finally: db.close()
    threads=[threading.Thread(target=worker,args=(oid,)) for oid in ids]
    for t in threads:t.start()
    for t in threads:t.join(15)
    assert sorted(outcomes)==['blocked','ok']
    with app.app_context():assert outstanding_debt(cid)==115


def test_ui_eligibility_edit_and_finalisation_route(client, app):
    cid=helpers.setup_customer(client,app)
    with app.app_context():
        db=get_db(); oid=helpers.order(cid); did=create_document(oid,'invoice')
    page=client.get(f'/orders/{oid}')
    assert page.status_code==200 and b'value="account"' in page.data
    assert b'facility available' in page.data and b'account_request_id' in page.data
    result=client.post(f'/documents/{did}/finalize',follow_redirects=True)
    assert b'Record at least one payment' in result.data
    result=client.post(f'/orders/{oid}/payments',data={'method':'account','amount':'30','account_request_id':'route-request'},follow_redirects=True)
    # Route spelling deliberately discovered/verified below if this assertion fails.
    assert result.status_code==200
    with app.app_context():
        assert outstanding_debt(cid)==30
        pid=latest(get_db(),oid)
    assert b'value="account"' in client.get(f'/payments/{pid}/edit').data
    result=client.post(f'/documents/{did}/finalize',follow_redirects=True)
    assert result.status_code==200
    with app.app_context():
        assert get_document(did)['status']=='finalized'
        get_db().execute('UPDATE customers SET credit_allowed=0 WHERE id=?',(cid,));get_db().commit()
    assert b'value="account"' not in client.get(f'/orders/{oid}').data
