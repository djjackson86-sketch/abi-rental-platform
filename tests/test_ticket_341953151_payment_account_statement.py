import importlib.util
from pathlib import Path
import pytest
from app.db import get_db
from app.services.payments import record_payment, update_payment, archive_payment, payment_method_totals, list_payments, payment_count
from app.services.prepaid_funding import record_funding
from app.services.customer_credits import customer_credit_balance
from app.services.reports import dashboard_day_metrics, dashboard_period_metrics
from app.services.cash import cash_received
from app.services.account_statements import account_statement

spec=importlib.util.spec_from_file_location('ticket151_helpers',Path(__file__).with_name('test_ticket_341953129_credit_limits.py'))
helpers=importlib.util.module_from_spec(spec);spec.loader.exec_module(helpers)
app,client=helpers.app,helpers.client
DAY='2026-10-08'
USE_DAY='2026-10-09'


def test_prepaid_funding_no_revenue_then_use_and_reverse(client,app):
    cid=helpers.setup_customer(client,app,500)
    with app.app_context():
        db=get_db(); oid=helpers.order(cid)
        fund=record_funding(cid,{'amount':'100','method':'cash','payment_date':DAY,'request_key':'fund-once'})
        assert record_funding(cid,{'amount':'100','method':'cash','request_key':'fund-once'})==fund
        assert customer_credit_balance(cid)==100
        metrics=dashboard_day_metrics(DAY)
        assert metrics['revenue']==0 and metrics['other_payments']==0
        assert metrics['cash_payments']==100
        assert cash_received(DAY,None)==100
        assert dashboard_period_metrics()['revenue']==0
        assert payment_count()==len(list_payments())==1
        assert list_payments()[0]['synthetic_kind']=='prepaid_fundings'
        record_payment(oid,{'amount':'60','method':'customer_credit','payment_date':USE_DAY})
        pid=db.execute("SELECT id FROM payments WHERE method='customer_credit'").fetchone()['id']
        assert customer_credit_balance(cid)==40
        assert dashboard_day_metrics(USE_DAY)['revenue']==60
        assert payment_method_totals(date_from=USE_DAY,date_to=USE_DAY)['other']==60
        with pytest.raises(Exception,match='already used'):
            db.execute("UPDATE prepaid_fundings SET status='archived' WHERE id=?",(fund,))
        with pytest.raises(ValueError,match='Reverse customer-credit'):
            update_payment(pid,{'amount':'50','method':'cash'})
        archive_payment(pid)
        assert customer_credit_balance(cid)==100
        assert dashboard_day_metrics(USE_DAY)['revenue']==0
        db.execute("UPDATE prepaid_fundings SET status='archived',deleted_at='2026-10-09' WHERE id=?",(fund,));db.commit()
        assert customer_credit_balance(cid)==0
        assert dashboard_day_metrics(DAY)['cash_payments']==0


def test_account_repaid_without_double_payment_and_statement(client,app):
    cid=helpers.setup_customer(client,app,500)
    with app.app_context():
        db=get_db(); oid=helpers.order(cid)
        record_payment(oid,{'amount':'115','method':'account','payment_date':DAY})
        assert dashboard_day_metrics(DAY)['revenue']==0
        record_payment(oid,{'amount':'40','method':'eft','payment_date':USE_DAY})
        view=account_statement(cid)
        assert view['facility']['used']==75 and view['facility']['available']==425
        assert view['summary_rows'][3][1]=='R75.00'
        assert any(row['type']=='Repayment' and row['amount']==-40 for row in view['activity'])
        assert dashboard_day_metrics(USE_DAY)['revenue']==40
        assert dashboard_period_metrics(start_date=USE_DAY,end_date=USE_DAY)['revenue']==40
        assert payment_method_totals(date_from=DAY,date_to=DAY)['other']==0
    response=client.get(f'/customers/{cid}/account-statement.pdf')
    assert response.status_code==200 and response.data.startswith(b'%PDF')
    assert b'ACCOUNT STATEMENT' in response.data and b'Closing balance' in response.data and b'R75.00' in response.data
    assert b'Account Statement' in client.get(f'/customers/{cid}').data


def test_choose_method_and_other_card(client,app):
    cid=helpers.setup_customer(client,app,500)
    with app.app_context():
        db=get_db(); oid=helpers.order(cid)
        with pytest.raises(ValueError,match='Choose a payment method'):
            record_payment(oid,{'amount':'10','method':''})
        record_payment(oid,{'amount':'10','method':'cash'})
        pid=db.execute('SELECT id FROM payments WHERE order_id=?',(oid,)).fetchone()['id']
        update_payment(pid,{'amount':'10','reference':'unchanged method'})
        assert db.execute('SELECT method FROM payments WHERE id=?',(pid,)).fetchone()['method']=='cash'
        db.execute("INSERT INTO payments(order_id,amount,method,status,created_at) VALUES(?,20,'deposit_applied','paid',?)",(oid,DAY));db.commit()
        assert payment_method_totals()['other']==20
        db.execute("UPDATE payments SET method='deposit_applied' WHERE id=?",(pid,));db.commit()
    # A stored method with no rendered option must stay preserved on the edit page
    # (no forced re-pick that would rewrite its history); new entries still require a choice.
    edit=client.get(f'/payments/{pid}/edit').data
    assert b'<option value="deposit_applied" selected>' in edit
    assert b'<select name="method" required>' not in edit
    result=client.post(f'/payments/{pid}/edit',data={'amount':'10','method':'','reference':'unchanged'},follow_redirects=True)
    assert result.status_code==200
    with app.app_context():
        row=get_db().execute('SELECT method,amount,reference FROM payments WHERE id=?',(pid,)).fetchone()
        assert row['method']=='deposit_applied' and row['amount']==10.0 and row['reference']=='unchanged'
    html=client.get(f'/orders/{oid}').data
    assert b'<option value="">Choose method</option>' in html
    assert b'OTHER PAYMENTS' in client.get('/payments').data


@pytest.mark.parametrize('amount',['nan','inf','-1','0','bad','0.001'])
def test_invalid_upfront_funds_refused(client,app,amount):
    cid=helpers.setup_customer(client,app)
    with app.app_context(),pytest.raises(ValueError):
        record_funding(cid,{'amount':amount,'method':'cash'})
