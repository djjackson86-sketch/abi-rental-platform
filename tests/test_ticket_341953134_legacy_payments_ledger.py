"""Legacy collections are receipts, not order payments (ABI-341953134)."""
import os
import tempfile

import pytest
from flask import session

from app import create_app
from app.db import get_db, run_migrations
from app.services.payments import list_payments, payment_count, payment_method_totals
from app.services.legacy_balances import record_legacy_payment, upsert_legacy_balance
from app.services.reports import dashboard_day_metrics

DAY = '2026-10-06'


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    application = create_app({'TESTING': True, 'DATABASE': path, 'SECRET_KEY': 'test',
                              'ADMIN_EMAIL': 'admin@abi.local', 'ADMIN_PASSWORD': 'admin123',
                              'TURSO_DATABASE_URL': '', 'TURSO_AUTH_TOKEN': ''})
    yield application
    os.unlink(path)


def customer(db, name='Legacy Solar', branch=None):
    return db.execute('INSERT INTO customers (name, branch_id, created_at) VALUES (?, ?, ?)',
                      (name, branch, DAY)).lastrowid


def receipt(db, cid, amount=1174.50, method='card', branch=None, day=DAY,
            status='active', deleted='', created=DAY):
    return db.execute('''INSERT INTO legacy_balance_payments
        (customer_id,amount,method,reference,payment_date,status,deleted_at,created_at,branch_id)
        VALUES (?,?,?,'10009',?,?,?,?,?)''',
        (cid, amount, method, day, status, deleted, created, branch)).lastrowid


def test_combined_listing_count_pagination_sort_and_overlapping_ids(app):
    with app.app_context():
        db = get_db()
        cid = customer(db)
        oid = db.execute("INSERT INTO orders (order_number,customer_id,created_at) VALUES ('NEW-1',?,?)",
                         (cid, DAY)).lastrowid
        regular_id = db.execute('''INSERT INTO payments
            (order_id,amount,method,status,payment_date,created_at)
            VALUES (?,100,'card','paid',?,?)''', (oid, DAY, DAY)).lastrowid
        legacy_id = receipt(db, cid)
        assert legacy_id == regular_id
        for method in ('cash', 'eft'):
            receipt(db, cid, amount=25, method=method)
        db.commit()
        rows = list_payments(date_from=DAY, date_to=DAY)
        assert len(rows) == payment_count(date_from=DAY, date_to=DAY) == 4
        regular = next(r for r in rows if r['synthetic_kind'] is None)
        legacy = next(r for r in rows if r['method'] == 'card' and r['synthetic_kind'])
        assert regular['id'] == regular_id
        assert legacy['id'] is None and legacy['order_id'] is None
        assert legacy['legacy_receipt_id'] == legacy_id
        assert legacy['customer_id'] == cid and legacy['branch_label'] == 'Unassigned'
        pages = [list_payments(limit=1, offset=i)[0] for i in range(4)]
        identity = lambda r: (r['synthetic_kind'], r['id'], r['legacy_receipt_id'])
        assert [identity(r) for r in pages] == [identity(r) for r in rows]
        assert len({identity(r) for r in pages}) == 4
        assert [float(r['amount']) for r in list_payments(sort='amount', direction='asc')] == [25,25,100,1174.5]
        assert payment_method_totals(date_from=DAY,date_to=DAY) == {'card':1274.5,'cash':25,'eft':25}


def test_day_status_deleted_and_created_fallback(app):
    with app.app_context():
        db = get_db()
        cid = customer(db)
        receipt(db,cid,amount=10)
        receipt(db,cid,amount=20,day='',created=DAY+'T12:00:00')
        receipt(db,cid,amount=100,day='2026-10-05')
        receipt(db,cid,amount=200,status='archived')
        receipt(db,cid,amount=300,deleted=DAY)
        receipt(db,cid,amount=400,status='pending')
        db.commit()
        args = {'date_from':DAY,'date_to':DAY}
        assert payment_count(**args) == len(list_payments(**args)) == 2
        assert payment_method_totals(**args)['card'] == 30
        # Preserve historic include_archived semantics: include all real rows.
        assert payment_count(include_archived=True,**args) == 5
        assert payment_method_totals(include_archived=True,**args)['card'] == 930
        assert payment_method_totals(date_from='2026-10-05',date_to='2026-10-05')['card'] == 100


@pytest.mark.parametrize('scope,active,requested,expected', [
    ([1],None,None,10), ([1],None,2,10), ([1,2],None,None,60),
    ([1,2],2,None,50), ([1,2],None,2,50),
])
def test_scope_never_widens_with_customer_fallback_and_receipt_override(app,scope,active,requested,expected):
    with app.test_request_context('/'):
        session.update(user_id=1,user_role='staff',can_view_all_branches=False,
                       branch_ids=scope,branch_id=None,active_branch_id=active)
        db = get_db()
        one = customer(db, 'One',1)
        two = customer(db, 'Two',2)
        unknown = customer(db, 'Unknown')
        receipt(db,one,amount=10)
        receipt(db,two,amount=20)
        receipt(db,one,amount=30,branch=2)
        receipt(db,unknown,amount=40)
        db.commit()
        assert payment_method_totals(branch_id=requested)['card'] == expected
        assert sum(float(r['amount']) for r in list_payments(branch_id=requested)) == expected
        assert payment_count(branch_id=requested) == len(list_payments(branch_id=requested))
        assert dashboard_day_metrics(day=DAY,branch_id=requested)['card_payments'] == expected


def test_receipt_page_has_customer_link_and_no_regular_payment_actions(app):
    with app.app_context():
        db = get_db()
        cid = customer(db, "It's Home Solar Pty Ltd")
        receipt(db,cid)
        db.commit()
    client = app.test_client()
    with client.session_transaction() as s:
        s.update(user_id=1,user_role='owner',can_view_all_branches=True)
    response = client.get('/payments?date_from='+DAY+'&date_to='+DAY)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'Legacy receipt #1' in html and 'R1174.50' in html and 'Unassigned' in html
    assert f'href="/customers/{cid}"' in html
    assert '/payments/1/edit' not in html and '/payments/1/delete' not in html
    assert client.get('/payments?branch=1').status_code == 200
    assert 'Legacy receipt #1' not in client.get('/payments?branch=1').get_data(as_text=True)


def test_additive_migration_keeps_existing_receipt_and_allocation(app):
    with app.app_context():
        db = get_db()
        cid = customer(db)
        rid = receipt(db,cid)
        db.commit()
        db.execute('ALTER TABLE legacy_balance_payments DROP COLUMN branch_id')
        db.commit()
        run_migrations(db)
        run_migrations(db)
        row = db.execute('SELECT * FROM legacy_balance_payments WHERE id=?',(rid,)).fetchone()
        assert row['branch_id'] is None
        assert row['amount'] == 1174.5 and row['customer_id'] == cid
        assert payment_count() == 1


def test_future_receipt_captures_acting_depot_without_changing_customer(app):
    with app.test_request_context('/'):
        session.update(user_id=1,user_role='staff',branch_ids=[1,2],branch_id=1,
                       active_branch_id=2,can_view_all_branches=False)
        db = get_db()
        cid = customer(db, branch=1)
        upsert_legacy_balance(cid,'old','old-134',100)
        db.commit()
        record_legacy_payment(cid,{'amount':'40','method':'card','payment_date':DAY})
        row = db.execute('SELECT * FROM legacy_balance_payments WHERE customer_id=?',(cid,)).fetchone()
        assert row['branch_id'] == 2
        assert db.execute('SELECT branch_id FROM customers WHERE id=?',(cid,)).fetchone()['branch_id'] == 1
        assert db.execute('SELECT settled_amount FROM legacy_customer_balances WHERE customer_id=?',(cid,)).fetchone()['settled_amount'] == 40
        assert payment_method_totals(branch_id=2)['card'] == 40
        # A requested branch outside the active scope is ignored, not widened.
        assert payment_method_totals(branch_id=1)['card'] == 40
        session.update(user_role='owner',active_branch_id=None)
        assert payment_method_totals(branch_id=1)['card'] == 0


def test_empty_effective_scope_returns_no_receipts(app, monkeypatch):
    from app.services import access
    with app.test_request_context('/'):
        db = get_db()
        receipt(db,customer(db))
        db.commit()
        monkeypatch.setattr(access,'session_branch_scope_ids',lambda: [])
        assert payment_count() == 0
        assert payment_method_totals()['card'] == 0
