"""Explicit Account charges, repayment, statement and signed revenue regressions."""
import importlib.util
from pathlib import Path
import sqlite3
import threading
import pytest
from app.db import get_db
from app.services.credit_limits import facility_summary, outstanding_debt
from app.services.payments import record_payment, payment_summary, archive_payment
from app.services.account_statements import account_statement
from app.services.reports import dashboard_day_metrics, dashboard_period_metrics
from app.services.documents import create_document, get_document
spec=importlib.util.spec_from_file_location('facility_helpers',Path(__file__).with_name('test_ticket_341953129_credit_limits.py'))
h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
app=h.app
client=h.client


def pay(oid, amount, method='account', key=None, day='2026-10-08'):
    return record_payment(oid,{'amount':str(amount),'method':method,'payment_date':day+'T10:00','account_request_id':key or ''})


def test_account_only_draws_on_save_and_repayments_restore(client,app):
    cid=h.setup_customer(client,app,150)
    with app.app_context():
        oid=h.order(cid)
        assert facility_summary(cid)['used']==0
        pay(oid,100,key='once')
        pay(oid,100,key='once')
        assert facility_summary(cid)=={'enabled':True,'limit':150.0,'used':100.0,'available':50.0}
        pay(oid,30,'eft')
        assert facility_summary(cid)['used']==85  # remaining receivable: 115 - 30
        pay(oid,20,'cash')
        assert facility_summary(cid)['used']==65
        assert payment_summary(oid)['paid_total']==115
        assert payment_summary(oid)['due_total']==0
        did=create_document(oid,'quote')
        assert get_document(did)['paid_total']==115
        pid=get_db().execute("SELECT id FROM payments WHERE method='account'").fetchone()['id']
        archive_payment(pid)
        assert outstanding_debt(cid)==0


def test_atomic_capacity_and_disabled_customer(client,app):
    cid=h.setup_customer(client,app,150)
    with app.app_context():
        a,b=h.order(cid),h.order(cid)
        pay(a,100)
        with pytest.raises(ValueError,match='credit limit'):pay(b,100)
        assert outstanding_debt(cid)==100
        get_db().execute('UPDATE customers SET credit_allowed=0 WHERE id=?',(cid,));get_db().commit()
        with pytest.raises(ValueError,match='enabled'):pay(b,20)


def test_concurrent_account_saves(client,app):
    cid=h.setup_customer(client,app,150)
    with app.app_context():a,b=h.order(cid),h.order(cid)
    barrier=threading.Barrier(2);out=[]
    def worker(oid):
        db=sqlite3.connect(app.config['DATABASE'],timeout=10)
        try:
            barrier.wait()
            db.execute("INSERT INTO payments(order_id,amount,method,status,created_at) VALUES(?,100,'account','paid','2026-10-08')",(oid,));db.commit();out.append('ok')
        except sqlite3.IntegrityError:out.append('blocked')
        finally:db.close()
    ts=[threading.Thread(target=worker,args=(oid,)) for oid in (a,b)]
    for t in ts:t.start()
    for t in ts:t.join(15)
    assert sorted(out)==['blocked','ok']
    with app.app_context():assert outstanding_debt(cid)==100


def test_statement_eligibility_period_layout_and_repayment(client,app):
    cid=h.setup_customer(client,app,150)
    with app.app_context():
        oid=h.order(cid)
        pay(oid,115,day='2026-10-07')
        pay(oid,30,'eft')
        v=account_statement(cid,'2026-10-08','2026-10-08')
        assert v['summary_rows'][0][1]=='R115.00'
        assert v['summary_rows'][3][1]=='R85.00'
        assert [r['amount'] for r in v['activity']]==[-30]
    r=client.get(f'/customers/{cid}/account-statement.pdf?date_from=2026-10-08&date_to=2026-10-08')
    assert r.status_code==200 and r.data.startswith(b'%PDF')
    assert b'ACCOUNT STATEMENT' in r.data and b'Available credit' in r.data
    with app.app_context():get_db().execute('UPDATE customers SET credit_allowed=0 WHERE id=?',(cid,));get_db().commit()
    assert client.get(f'/customers/{cid}/account-statement.pdf').status_code==404
    assert b'Account Statement' not in client.get(f'/customers/{cid}').data
    assert client.get(f'/customers/{cid}/statement').status_code==200


def test_revenue_signed_receipts_deposit_refund_and_account_exclusion(client,app):
    cid=h.setup_customer(client,app,1000)
    with app.app_context():
        db=get_db();oid=h.order(cid,1000)
        pay(oid,830,'card');pay(oid,80,'other')
        db.execute("INSERT INTO payments(order_id,amount,method,status,payment_date,created_at) VALUES(?,-420,'card','paid','2026-10-08','2026-10-08')",(oid,))
        db.execute("UPDATE orders SET deposit_refund_amount=420,deposit_process_method='card',deposit_processed_at='2026-10-08T11:00' WHERE id=?",(oid,));db.commit()
        pay(h.order(cid),100,'account')
    with app.test_request_context():
        from flask import session
        session['user_role']='owner';session['user_id']=1
        d=dashboard_day_metrics('2026-10-08')
        assert d['revenue']==70
        assert d['card_payments']==-10
        assert d['other_payments']==80
        assert sum(d[k] for k in ['card_payments','cash_payments','eft_payments','other_payments'])==70
        assert dashboard_period_metrics('2026-10-08','2026-10-08')['revenue']==70
