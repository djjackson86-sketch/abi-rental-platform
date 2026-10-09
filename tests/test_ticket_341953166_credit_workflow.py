"""Approved outstanding-order credit workflow and atomic guards."""
import importlib.util
from pathlib import Path
import sqlite3
import threading
import pytest
from app.db import get_db, run_migrations
from app.services.credit_limits import facility_summary, outstanding_debt, install_credit_guards
from app.services.documents import create_document, finalize_document, get_document
from app.services.orders import create_order, update_draft_order
from app.services.payments import record_payment, payment_total, archive_payment, update_payment

spec = importlib.util.spec_from_file_location('credit166helpers', Path(__file__).with_name('test_ticket_341953028_customer_blocking.py'))
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
app = h.app
client = h.client


def customer(client, app, limit=230, enabled=True):
    h.login(client)
    cid = h.create_customer(client)
    if enabled:
        assert b'Customer saved' in h.save_customer_form(client, app, cid, credit_panel='1', credit_allowed='1', credit_limit=str(limit)).data
    return cid


def order(cid, amount=100):
    form = h.order_payload(cid)
    form['custom_unit_price'] = str(amount)
    return create_order(form, notify=False)


@pytest.mark.parametrize('enabled,paid,allowed', [(False,0,False),(False,1,False),(False,114,False),(False,115,True),(True,0,True),(True,1,True),(True,115,True)])
def test_invoice_finalisation_requires_facility_or_full_payment(client, app, enabled, paid, allowed):
    cid = customer(client, app, enabled=enabled)
    with app.app_context():
        oid = order(cid)
        if paid:
            record_payment(oid, {'amount':str(paid),'method':'eft'})
        did = create_document(oid, 'invoice')
        if allowed:
            assert finalize_document(did)==did
            assert finalize_document(did)==did
        else:
            with pytest.raises(ValueError, match='in full'):
                finalize_document(did)
            with pytest.raises(sqlite3.IntegrityError, match='in full'):
                get_db().execute("UPDATE documents SET status='finalized' WHERE id=?", (did,))
            assert get_document(did)['status']=='draft'


def test_exact_limit_reserves_once_and_repayment_restores(client, app):
    cid = customer(client, app)
    with app.app_context():
        a,b = order(cid),order(cid)
        assert facility_summary(cid)=={'enabled':True,'limit':230,'used':230,'available':0}
        for oid in (a,b):
            finalize_document(create_document(oid,'invoice'))
        assert outstanding_debt(cid)==230
        with pytest.raises(ValueError,match='credit limit'): order(cid,0)
        with pytest.raises(ValueError,match='credit limit'): order(cid,1)
        record_payment(a,{'method':'cash','amount':'23'})
        assert facility_summary(cid)['available']==23
        assert order(cid,20)
        assert outstanding_debt(cid)==230
        db=get_db()
        count=db.execute('SELECT COUNT(*) n FROM orders').fetchone()['n']
        with pytest.raises(sqlite3.IntegrityError,match='credit limit'):
            db.execute("INSERT INTO orders(order_number,customer_id,total,created_at) VALUES ('RACE',?,1,'2026-10-09')",(cid,))
        assert db.execute('SELECT COUNT(*) n FROM orders').fetchone()['n']==count


def test_edit_and_receipt_reversal_atomic(client,app):
    cid=customer(client,app,150)
    with app.app_context():
        oid=order(cid)
        record_payment(oid,{'method':'eft','amount':'50'})
        order(cid,50)
        db=get_db()
        pid=db.execute('SELECT id FROM payments WHERE order_id=?',(oid,)).fetchone()['id']
        before=outstanding_debt(cid)
        form=h.order_payload(cid);form['custom_unit_price']='200'
        with pytest.raises(ValueError,match='credit limit'):update_draft_order(oid,form)
        with pytest.raises(sqlite3.IntegrityError,match='credit limit'):db.execute('UPDATE orders SET total=200 WHERE id=?',(oid,))
        with pytest.raises(ValueError,match='credit limit'):archive_payment(pid)
        with pytest.raises(ValueError,match='credit limit'):update_payment(pid,{'method':'eft','amount':'1'})
        with pytest.raises(sqlite3.IntegrityError,match='credit limit'):db.execute('DELETE FROM payments WHERE id=?',(pid,))
        assert outstanding_debt(cid)==before
        assert payment_total(oid)==50


def test_existing_debt_and_lowered_limit_allow_repayment(client,app):
    cid=customer(client,app,enabled=False)
    with app.app_context():
        a,b=order(cid),order(cid)
        db=get_db()
        db.execute('UPDATE customers SET credit_allowed=1,credit_limit=100 WHERE id=?',(cid,));db.commit()
        assert facility_summary(cid)['used']==230
        assert facility_summary(cid)['available']==0
        unchanged=h.order_payload(cid)
        update_draft_order(a,unchanged)  # harmless edit while existing exposure exceeds limit
        with pytest.raises(ValueError,match='credit limit'):order(cid)
        with pytest.raises(ValueError,match='credit limit'):finalize_document(create_document(a,'invoice'))
        record_payment(a,{'method':'eft','amount':'115'})
        record_payment(b,{'method':'cash','amount':'50'})
        assert facility_summary(cid)['available']==35
        assert order(cid,30)


def test_account_rejected_but_historical_record_preserved(client,app):
    cid=customer(client,app)
    with app.app_context():
        oid=order(cid)
        with pytest.raises(ValueError):record_payment(oid,{'method':'account','amount':'115'})
        db=get_db()
        with pytest.raises(sqlite3.IntegrityError,match='no longer'):db.execute("INSERT INTO payments(order_id,amount,method,status,created_at) VALUES (?,115,'account','paid','2026-10-01')",(oid,))
        db.execute('DROP TRIGGER account_payment_retired_insert')
        db.execute("INSERT INTO payments(order_id,amount,method,status,created_at) VALUES (?,115,'account','paid','2026-10-01')",(oid,))
        install_credit_guards(db);db.commit()
        pid=db.execute('SELECT id FROM payments WHERE order_id=?',(oid,)).fetchone()['id']
        assert payment_total(oid)==0
        assert outstanding_debt(cid)==115
        update_payment(pid,{'method':'account','amount':'115','reference':'Historical annotation'})
        with pytest.raises(ValueError):update_payment(pid,{'method':'cash','amount':'115'})
        with pytest.raises(ValueError):update_payment(pid,{'method':'account','amount':'116'})
        row=db.execute('SELECT * FROM payments WHERE id=?',(pid,)).fetchone()
        assert row['method']=='account' and row['amount']==115
        assert row['reference']=='Historical annotation'
    assert b'value="account"' not in client.get(f'/orders/{oid}').data


def test_duplicate_invoice_is_not_double_counted(client,app):
    cid=customer(client,app)
    with app.app_context():
        oid=order(cid)
        a=create_document(oid,'invoice')
        db=get_db()
        b=db.execute("INSERT INTO documents(order_id,document_type,status,created_at) VALUES (?,'invoice','draft','2026-10-09')",(oid,)).lastrowid
        finalize_document(a)
        with pytest.raises(ValueError,match='already exists'):finalize_document(b)
        with pytest.raises(sqlite3.IntegrityError,match='already exists'):get_db().execute("UPDATE documents SET status='finalized' WHERE id=?",(b,))
        assert outstanding_debt(cid)==115


def test_concurrent_order_creation_cannot_overspend(client,app):
    cid=customer(client,app,150)
    barrier=threading.Barrier(2);outcomes=[]
    def worker(i):
        db=sqlite3.connect(app.config['DATABASE'],timeout=10)
        try:
            barrier.wait()
            db.execute("INSERT INTO orders(order_number,customer_id,total,created_at) VALUES (?, ?,115,'2026-10-09')",(f'CONCURRENT-{i}',cid));db.commit();outcomes.append('ok')
        except sqlite3.IntegrityError:outcomes.append('blocked')
        finally:db.close()
    threads=[threading.Thread(target=worker,args=(i,)) for i in range(2)]
    for t in threads:t.start()
    for t in threads:t.join(15)
    assert sorted(outcomes)==['blocked','ok']
    with app.app_context():assert outstanding_debt(cid)==115


def test_repeat_migration_and_prepaid_is_separate(client,app):
    cid=customer(client,app)
    with app.app_context():
        db=get_db();run_migrations(db);run_migrations(db)
        oid=order(cid)
        from app.services.customer_credits import customer_credit_balance
        before=customer_credit_balance(cid)
        finalize_document(create_document(oid,'invoice'))
        assert customer_credit_balance(cid)==before
        assert outstanding_debt(cid)==115
