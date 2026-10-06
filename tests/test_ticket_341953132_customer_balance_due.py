"""ABI-341953132: live customer debt rather than the imported balance field."""
import csv
import importlib.util
from io import StringIO
from pathlib import Path
import re

import pytest
from app.db import get_db
from app.services.customers import customer_outstanding_balances, get_customer, list_customers
from app.services.payments import payment_summary

spec = importlib.util.spec_from_file_location('balance_helpers', Path(__file__).with_name('test_ticket_341953068_customer_credit.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
app = helpers.app
client = helpers.client


def payment(db, oid, amount, status='paid', deleted='', method='cash'):
    db.execute("INSERT INTO payments (order_id, amount, method, status, deleted_at, created_at) VALUES (?, ?, ?, ?, ?, '2026-10-01')", (oid, amount, method, status, deleted))


def document(db, oid, kind='invoice', status='finalized'):
    db.execute("INSERT INTO documents (order_id, document_type, status, number, created_at) VALUES (?, ?, ?, ?, '2026-10-01')", (oid, kind, status, f'TEST-{oid}-{kind}'))


def test_multiple_orders_partial_full_overpaid_and_other_customer(app):
    with app.app_context():
        db = get_db()
        cid = helpers.seed_customer(db)
        other = helpers.seed_customer(db, 'Other customer')
        for number, total, paid in [('unpaid', 400, 0), ('partial', 300, 100), ('full', 200, 200), ('overpaid', 100, 500)]:
            oid = helpers.seed_order(db, cid, number, total=total, due=9999)
            if paid:
                payment(db, oid, paid)
        helpers.seed_order(db, other, 'other', total=900)
        db.commit()
        assert customer_outstanding_balances([cid, other, -1]) == {cid: 600, other: 900, -1: 0}
        assert customer_outstanding_balances([]) == {}
        assert list_customers(query='Credit Customer')[0]['outstanding_balance'] == 600


@pytest.mark.parametrize('status,kind,doc_status,expected', [
    ('draft', None, None, 0), ('draft', 'invoice', 'draft', 0),
    ('draft', 'invoice', 'finalized', 100),
    ('canceled', 'invoice', 'finalized', 0), ('cancelled', 'invoice', 'finalized', 0),
    ('archived', None, None, 0), ('archived', 'invoice', 'finalized', 100),
    ('sales_repairs', None, None, 0), ('sales_repairs', 'quote', 'accepted', 100),
    ('reserved', None, None, 100), ('started', None, None, 100), ('returned', None, None, 100),
])
def test_order_eligibility(app, status, kind, doc_status, expected):
    with app.app_context():
        db = get_db()
        cid = helpers.seed_customer(db)
        oid = helpers.seed_order(db, cid, 'eligibility', total=100, status=status)
        if kind:
            document(db, oid, kind, doc_status)
        db.commit()
        assert customer_outstanding_balances([cid])[cid] == expected


def test_refunds_archival_deposits_and_allocated_credit_match_order_accounting(app):
    with app.app_context():
        db = get_db()
        cid = helpers.seed_customer(db)
        oid = helpers.seed_order(db, cid, 'accounting', total=1000, deposit=200)
        for amount, status, deleted, method in [(300, 'paid', '', 'cash'), (-50, 'paid', '', 'eft'), (50, 'paid', '', 'deposit_applied'), (100, 'paid', '', 'customer_credit'), (999, 'archived', '', 'cash'), (999, 'paid', 'deleted', 'cash')]:
            payment(db, oid, amount, status, deleted, method)
        # Deposit refunds are order metadata, not another negative payment.
        db.execute("UPDATE orders SET deposit_refund_amount=150, deposit_process_method='cash', deposit_processed_at='2026-10-01' WHERE id=?", (oid,))
        db.commit()
        assert customer_outstanding_balances([cid])[cid] == payment_summary(oid)['due_total'] == 600
        db.execute("UPDATE payments SET status='archived' WHERE order_id=? AND amount=300", (oid,))
        db.commit()
        assert customer_outstanding_balances([cid])[cid] == 900


def test_detail_list_export_separate_legacy_unused_credit_and_stored_balance(app, client):
    helpers.login(client)
    with app.app_context():
        db = get_db()
        cid = helpers.seed_customer(db)
        helpers.seed_order(db, cid, 'route', total=400)
        db.execute('UPDATE customers SET balance_due=777 WHERE id=?', (cid,))
        from app.services.legacy_balances import upsert_legacy_balance
        upsert_legacy_balance(cid, 'test', 'opening', 125)
        db.execute("INSERT INTO customer_credits (customer_id, amount, source_type, status, created_at) VALUES (?, 80, 'order_refund', 'active', '2026-10-01')", (cid,))
        db.commit()
    body = client.get(f'/customers/{cid}').get_data(as_text=True)
    assert re.findall(r'Balance due</(?:small|span)><(?:strong|b)>R([0-9.]+)', body) == ['400.00', '400.00']
    assert 'R125.00' in body and 'R80.00' in body
    listing = client.get('/customers?query=Credit+Customer')
    assert listing.status_code == 200 and b'R400.00' in listing.data
    export = client.get('/customers/export.csv?query=Credit+Customer')
    assert export.status_code == 200
    assert list(csv.DictReader(StringIO(export.get_data(as_text=True))))[0]['balance_due'] == '400.0'
    with app.app_context():
        assert get_customer(cid)['balance_due'] == 777


def test_turso_rows_and_duplicate_invoices_do_not_duplicate_debt(app, monkeypatch):
    from libsql_client.result import Row
    from app.services import customers

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchall(self):
            return [Row({key: i for i, key in enumerate(row.keys())}, tuple(row))
                    for row in self.cursor.fetchall()]

    class RemoteRows:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, params):
            return Cursor(self.db.execute(sql, params))

    with app.app_context():
        db = get_db()
        cid = helpers.seed_customer(db)
        oid = helpers.seed_order(db, cid, 'remote-invoice', total=100, status='draft')
        document(db, oid)
        db.execute("INSERT INTO documents (order_id, document_type, status, number, created_at) VALUES (?, 'invoice', 'finalized', 'DUPLICATE', '2026-10-01')", (oid,))
        db.commit()
        monkeypatch.setattr(customers, 'get_db', lambda: RemoteRows(db))
        assert list_customers()[0]['outstanding_balance'] == 100


def test_batching_more_than_sql_parameter_chunk(app):
    with app.app_context():
        assert customer_outstanding_balances(range(1, 802)) == dict.fromkeys(range(1, 802), 0.0)
