"""Received category cards, calendar comparisons and oldest-first late hires."""
import re
import pytest
from app.db import get_db
from app.routes import admin
from app.services.orders import dashboard_schedule, list_orders
from app.services.reports import received_revenue_by_category, dashboard_period_metrics, previous_month_period
from test_ticket_341953282_dashboard import app, client, login, add_customer, add_order, add_depot_user


@pytest.mark.parametrize('start,end,expected', [
    ('2026-03-01','2026-03-31',('2026-02-01','2026-02-28')),
    ('2024-03-01','2024-03-31',('2024-02-01','2024-02-29')),
    ('2026-01-01','2026-01-15',('2025-12-01','2025-12-15')),
    ('2026-05-31','2026-05-31',('2026-04-30','2026-04-30')),
    ('2026-11-01','2026-11-17',('2026-10-01','2026-10-17')),
    ('','',(None,None)), ('bad','2026-01-01',(None,None)),
    ('2026-02-01','2026-01-01',(None,None)),
])
def test_comparison_bounds(start,end,expected):
    assert previous_month_period(start,end)==expected


def seed_revenue(app):
    with app.app_context():
        db=get_db()
        cid=add_customer(db,'Revenue client')
        ids={}
        for number,status,branch in [('HIRE','started',1),('SALE','sales_repairs',2),('DRAFT','draft',1),('ARCHIVED','archived',1)]:
            ids[number]=add_order(db,number,cid,status,1000,branch_id=branch)
        def payment(order,amount,day='2026-11-17',method='cash',status='paid',deleted=''):
            db.execute('INSERT INTO payments (order_id,amount,method,status,payment_date,created_at,deleted_at) VALUES (?,?,?,?,?,?,?)',
                       (ids[order],amount,method,status,day,'2026-11-17',deleted))
        payment('HIRE',300); payment('HIRE',50); payment('HIRE',-20)
        payment('SALE',200); payment('SALE',-10)
        payment('DRAFT',40); payment('ARCHIVED',15)
        # Seed historical account transfers (new ones are now rejected by a guard).
        db.execute('DROP TRIGGER account_payment_retired_insert')
        payment('HIRE',99,method='account'); payment('HIRE',99,status='pending')
        from app.services.credit_limits import install_credit_guards
        install_credit_guards(db)
        payment('HIRE',99,deleted='2026-11-18'); payment('HIRE',99,day='2026-11-18')
        payment('HIRE',120,day='2026-10-17'); payment('HIRE',999,day='2026-10-18')
        payment('SALE',70,day='2026-10-01')
        db.execute("UPDATE orders SET deposit_refund_amount=30,deposit_processed_at='2026-11-17' WHERE id=?",(ids['HIRE'],))
        db.execute("INSERT INTO legacy_balance_payments (customer_id,amount,method,status,payment_date,created_at,branch_id) VALUES (?,25,'cash','active','2026-11-17','2026-11-17',1)",(cid,))
        db.commit()
    return ids


@pytest.mark.parametrize('branch,expected', [(None,{'trailer_rentals':330,'sales_repairs':190,'unattributed':80}), (1,{'trailer_rentals':330,'sales_repairs':0,'unattributed':80}), (2,{'trailer_rentals':0,'sales_repairs':190,'unattributed':0})])
def test_received_rules_and_reconciliation(app,branch,expected):
    seed_revenue(app)
    with app.app_context():
        actual=received_revenue_by_category('2026-11-01','2026-11-17',branch)
        assert actual==expected
        assert sum(actual.values())==dashboard_period_metrics('2026-11-01','2026-11-17',branch)['revenue']


@pytest.mark.parametrize('closed,prior,column,category', [
    ('archived','returned','status_before_archive','trailer_rentals'),
    ('archived','sales_repairs','status_before_archive','sales_repairs'),
    ('canceled','started','status_before_cancel','trailer_rentals'),
    ('cancelled','sales_repairs','status_before_cancel','sales_repairs'),
])
def test_closed_order_receipts_keep_explicit_stored_category(app,closed,prior,column,category):
    with app.app_context():
        db=get_db(); cid=add_customer(db,'Closed client')
        oid=add_order(db,'CLOSED',cid,closed,100)
        db.execute(f'UPDATE orders SET {column}=?,deposit_refund_amount=10,deposit_processed_at=? WHERE id=?',(prior,'2026-11-17',oid))
        db.execute("INSERT INTO payments (order_id,amount,method,status,payment_date,created_at) VALUES (?,100,'cash','paid','2026-11-17','2026-11-17')",(oid,))
        db.commit()
        totals=received_revenue_by_category('2026-11-17','2026-11-17')
        assert totals[category]==100 and totals['unattributed']==0


def comparisons(body):
    return re.findall(r'class="dashboard-last-month revenue-last-month">This time last month <strong>(.*?)</strong>',body)


@pytest.mark.parametrize('lazy',[False,True])
def test_cards_initial_and_lazy_with_elapsed_month(app,client,lazy):
    seed_revenue(app); login(client)
    app.config['TESTING']=not lazy
    query='?start_date=2026-11-01&end_date=2026-11-17'
    shell=client.get('/dashboard'+query)
    assert shell.status_code==200
    body=shell.get_data(as_text=True)
    partial=client.get('/dashboard/partials/revenue'+query).get_data(as_text=True)
    assert comparisons(partial)==['R120.00','R70.00','R0.00']
    assert 'R330.00' in partial and 'R190.00' in partial
    assert 'Trailer rentals revenue' in partial and 'Sales/repairs revenue' in partial
    if lazy:
        assert 'data-dashboard-section="revenue"' in body
    else:
        assert comparisons(body)==comparisons(partial)
    branch=client.get('/dashboard/partials/revenue'+query+'&branch=1').get_data(as_text=True)
    assert comparisons(branch)==['R120.00','R0.00','R0.00']
    alltime=client.get('/dashboard/partials/revenue?range=all_time').get_data(as_text=True)
    assert len(comparisons(alltime))==3 and 'Not applicable' in alltime


def test_staff_and_anonymous_cannot_access_revenue(app,client):
    assert client.get('/dashboard/partials/revenue').status_code==302
    add_depot_user(app,'Revenue staff',1)
    login(client,'Revenue staff','staff123')
    assert client.get('/dashboard/partials/revenue?branch=2').status_code==403
    assert 'Trailer rentals revenue' not in client.get('/dashboard').get_data(as_text=True)


def test_oldest_due_first_before_limit_and_in_both_renders(app,client):
    with app.app_context():
        db=get_db(); cid=add_customer(db,'Late client')
        # Creation order is deliberately unrelated to due-date order.
        for number,due,branch in [('NEWER','2026-03-01',1),('OLDEST','2026-01-01',1),('TIE','2026-01-01',1),('OTHER-BRANCH','2025-01-01',2),('FUTURE','2099-01-01',1)]:
            oid=add_order(db,number,cid,'started',0,branch_id=branch)
            db.execute('UPDATE orders SET end_at=? WHERE id=?',(due+'T10:00:00',oid))
        db.commit()
        rows=dashboard_schedule(branch_id=1)['coming_back']
        assert [r['order_number'] for r in rows]==['OLDEST','TIE','NEWER']
        rows=list_orders(status='started',return_status='late',branch_id=1,oldest_return_first=True,limit=1,offset=1)
        assert [r['order_number'] for r in rows]==['TIE']
    login(client)
    for path in ['/dashboard?branch=1','/dashboard/partials/movement?branch=1']:
        body=client.get(path).get_data(as_text=True).split('<h2>Late trailers</h2>')[1].split('</article>')[0]
        assert body.index('OLDEST')<body.index('TIE')<body.index('NEWER')
        assert 'OTHER-BRANCH' not in body and 'FUTURE' not in body
    body=client.get('/orders?status=started&return_status=late&branch=1').get_data(as_text=True)
    assert body.index('OLDEST')<body.index('TIE')<body.index('NEWER')
    assert 'OTHER-BRANCH' not in body and 'FUTURE' not in body
