"""ABI-341953199: deposit-payment deletion must undo the settlement."""
import sqlite3
import pytest
from test_ticket_341953068_customer_credit import app, client, login, seed_customer, seed_order, seed_paid, as_owner
from app.db import get_db
from app.services.orders import get_order, reverse_deposit_settlement, settle_return_deposit
from app.services.payments import archive_payment, payment_summary, list_payments
from app.services.customer_credits import customer_credit_balance


def settlement(app, used=600, refund=0, method='card'):
    with app.app_context():
        db = get_db()
        cid = seed_customer(db)
        oid = seed_order(db, cid, 'ORD-REVERSAL', total=1200, deposit=600)
        seed_paid(db, oid, 1200-used)
        seed_paid(db, oid, used, 'deposit_applied')
        pid = db.execute('SELECT MAX(id) AS id FROM payments').fetchone()['id']
        db.execute("UPDATE orders SET deposit_applied_amount=?,deposit_refund_amount=?,deposit_process_method=?,deposit_processed_at='2026-09-28T12:00:00',deposit_note='Original note' WHERE id=?", (used,refund,method,oid))
        if method == 'customer_credit':
            from app.services.customer_credits import create_customer_credit
            create_customer_credit(cid,refund,oid,'deposit_refund')
        db.commit()
        return cid,oid,pid


@pytest.mark.parametrize('used,refund,method', [(600,0,'card'),(250,350,'cash'),(250,350,'customer_credit')])
def test_delete_clears_settlement_and_preserves_history(app,client,used,refund,method):
    cid,oid,pid=settlement(app,used,refund,method)
    login(client)
    before=client.get(f'/orders/{oid}').data
    assert f'Used R{used:.2f}'.encode() in before
    response=client.post(f'/payments/{pid}/delete',follow_redirects=True)
    assert response.status_code==200 and b'Payment deleted' in response.data
    assert f'Used R{used:.2f}'.encode() not in response.data
    with app.app_context():
        db=get_db();order=get_order(oid)
        assert order['deposit_total']==600
        assert order['deposit_applied_amount']==order['deposit_refund_amount']==0
        assert order['deposit_process_method']==order['deposit_processed_at']==''
        assert 'Original note' in order['deposit_note'] and 'deleted' in order['deposit_note']
        row=db.execute('SELECT * FROM payments WHERE id=?',(pid,)).fetchone()
        assert row['status']=='archived' and row['deleted_at'] and row['amount']==used
        assert payment_summary(oid)['due_total']==used
        assert order['due_total']==used
        assert customer_credit_balance(cid)==0
        assert not any(p['order_id']==oid and p['method']=='deposit_refund' for p in list_payments())
    again=client.post(f'/payments/{pid}/delete',follow_redirects=True)
    assert b'Payment not found' in again.data
    with app.app_context():
        assert get_order(oid)['deposit_note'].count('deleted')==1
    ctx=as_owner(app)
    try:
        settle_return_deposit(oid,{'deposit_process_method':'card'})
        assert get_order(oid)['deposit_applied_amount']==used
        assert get_order(oid)['deposit_refund_amount']==refund
        assert get_db().execute("SELECT COUNT(*) AS n FROM payments WHERE order_id=? AND method='deposit_applied' AND deleted_at=''",(oid,)).fetchone()['n']==1
        with pytest.raises(ValueError,match='Payment not found'):
            archive_payment(pid)
        assert get_order(oid)['deposit_applied_amount']==used
    finally:ctx.pop()


def test_invoice_pdf_ledger_and_cashup_follow_reversal(app,client):
    _,oid,pid=settlement(app,250,350,'cash')
    login(client)
    response=client.post(f'/orders/{oid}/documents',data={'document_type':'invoice'},follow_redirects=True)
    assert response.status_code==200
    with app.app_context():
        from app.services.pdf_documents import document_pdf_bytes
        from app.services.cash import deposit_refunds
        did=get_db().execute("SELECT id FROM documents WHERE order_id=? AND document_type='invoice'",(oid,)).fetchone()['id']
        assert b'Less: deposit used' in document_pdf_bytes(did)
        assert deposit_refunds('2026-09-28',1,'cash')==350
    assert b'Less: deposit used' in client.get(f'/documents/{did}').data
    client.post(f'/payments/{pid}/delete',follow_redirects=True)
    assert b'Less: deposit used' not in client.get(f'/documents/{did}').data
    with app.app_context():
        assert b'Less: deposit used' not in document_pdf_bytes(did)
        assert deposit_refunds('2026-09-28',1,'cash')==0
        ledger=list_payments()
        assert not any(p['order_id']==oid and p['method']=='deposit_applied' for p in ledger)
        assert not any(p['order_id']==oid and p['synthetic_kind']=='deposit_refund' for p in ledger)


def test_ordinary_payment_delete_does_not_clear_settlement(app):
    _,oid,pid=settlement(app)
    with app.app_context():
        cash_id=get_db().execute("SELECT id FROM payments WHERE order_id=? AND method='cash'",(oid,)).fetchone()['id']
        archive_payment(cash_id)
        assert get_order(oid)['deposit_applied_amount']==600
        assert not get_db().execute('SELECT deleted_at FROM payments WHERE id=?',(pid,)).fetchone()['deleted_at']


def test_consumed_credit_refuses_all_writes(app,client):
    cid,oid,pid=settlement(app,250,350,'customer_credit')
    with app.app_context():
        from app.services.customer_credits import apply_customer_credit
        apply_customer_credit(cid,100,oid)
        get_db().commit()
        before=dict(get_order(oid))
    login(client)
    response=client.post(f'/payments/{pid}/delete',follow_redirects=True)
    assert b'credit has already been used' in response.data
    with app.app_context():
        assert dict(get_order(oid))==before
        assert customer_credit_balance(cid)==250
        assert get_db().execute('SELECT status FROM payments WHERE id=?',(pid,)).fetchone()['status']=='paid'


def test_failure_rolls_back_credit_order_and_payment(app):
    cid,oid,pid=settlement(app,250,350,'customer_credit')
    with app.app_context():
        db=get_db();before=dict(get_order(oid))
        db.execute("CREATE TRIGGER fail_archive BEFORE UPDATE ON payments WHEN NEW.status='archived' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        db.commit()
        with pytest.raises(sqlite3.IntegrityError,match='injected failure'):archive_payment(pid)
        assert dict(get_order(oid))==before
        assert customer_credit_balance(cid)==350
        assert db.execute('SELECT status FROM payments WHERE id=?',(pid,)).fetchone()['status']=='paid'


def test_refund_without_payment_and_legacy_method(app):
    _,oid,pid=settlement(app,250,350)
    with app.app_context():
        db=get_db()
        db.execute("UPDATE payments SET method='Deposit_Applied' WHERE id=?",(pid,));db.commit()
        archive_payment(pid)
        assert get_order(oid)['deposit_applied_amount']==0
        db.execute("UPDATE orders SET deposit_refund_amount=600 WHERE id=?",(oid,));db.commit()
        reverse_deposit_settlement(oid)
        assert get_order(oid)['deposit_refund_amount']==0


def test_archived_repair_is_explicit_and_refuses_replacement(app):
    _,oid,pid=settlement(app)
    with app.app_context():
        db=get_db()
        db.execute("UPDATE payments SET status='archived',deleted_at='2026-10-12T10:08:08' WHERE id=?",(pid,));db.commit()
        with pytest.raises(ValueError):reverse_deposit_settlement(oid,pid)
        seed_paid(db,oid,600,'deposit_applied');db.commit()
        with pytest.raises(ValueError,match='replacement'):reverse_deposit_settlement(oid,pid,repair_archived=True)
        replacement=db.execute('SELECT MAX(id) AS id FROM payments').fetchone()['id']
        db.execute("UPDATE payments SET status='archived',deleted_at='test' WHERE id=?",(replacement,));db.commit()
        reverse_deposit_settlement(oid,pid,repair_archived=True)
        assert get_order(oid)['deposit_applied_amount']==0
        assert get_order(oid)['due_total']==600
        assert db.execute('SELECT deleted_at FROM payments WHERE id=?',(pid,)).fetchone()['deleted_at']=='2026-10-12T10:08:08'
