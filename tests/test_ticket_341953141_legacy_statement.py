"""Imported previous-order debt and receipts on customer statements."""
from pathlib import Path
import os

from test_ticket_341953085_customer_statement import (
    app, client, login, seed_customer, seed_ticket_ledger, pdf_source,
)
from app.db import get_db
from app.services.customers import customer_statement
from app.services.legacy_balances import upsert_legacy_balance, record_legacy_payment
from app.services.pdf_documents import customer_statement_pdf_bytes


def balance(db, cid, ref, amount, started='2026-08-01', created=''):
    upsert_legacy_balance(cid, 'old', ref, amount, source_total=9000,
                          source_paid=8000, source_started_at=started,
                          source_created_at=created)
    db.execute("UPDATE legacy_customer_balances SET created_at='2026-09-01T12:00:00' WHERE source_order_number=?", (ref,))


def test_mixed_partial_full_multi_order_receipts_and_isolation(app):
    with app.app_context():
        db = get_db()
        cid = seed_ticket_ledger(db)
        other = seed_customer(db, email='other@example.com')
        balance(db, cid, 'OLD-A', 100)
        balance(db, cid, 'OLD-B', 200)
        balance(db, other, 'OTHER', 999)
        db.commit()
        record_legacy_payment(cid, {'amount': '150', 'payment_date': '2026-08-02', 'reference': 'MULTI-150'})
        view = customer_statement(cid)
        assert view['invoiced_total'] == 1725
        assert view['legacy_total'] == 300
        assert view['paid_total'] == 1600
        assert view['closing_balance'] == view['outstanding_balance'] == 425
        assert view['legacy_balance_count'] == 2
        assert view['payment_count'] == 3
        assert sum(r['type'] == 'Legacy payment' for r in view['activity']) == 1
        assert 'OTHER' not in str(view['activity'])
        record_legacy_payment(cid, {'amount': '150', 'payment_date': '2026-08-03', 'reference': 'FINAL-150'})
        settled = customer_statement(cid)
        assert settled['legacy_balance_count'] == 2
        assert settled['closing_balance'] == 275
        assert settled['paid_total'] == 1750
        assert settled['activity'] == customer_statement(cid)['activity']


def test_eligibility_and_payment_date_fallback(app):
    with app.app_context():
        db = get_db()
        cid = seed_customer(db)
        balance(db, cid, 'OLD', 1000)
        for status, deleted, amount in [('active', '', 10), ('active', '', 20),
                                         ('archived', '', 300), ('pending', '', 400),
                                         ('active', '2026-09-02', 500)]:
            db.execute('''INSERT INTO legacy_balance_payments
                (customer_id,amount,method,status,deleted_at,payment_date,created_at)
                VALUES (?,?,'cash',?,?,'','2026-09-01T12:00:00')''',
                (cid, amount, status, deleted))
        db.commit()
        view = customer_statement(cid, '2026-09-01', '2026-09-01')
        assert view['payment_count'] == 2
        assert view['paid_total'] == 30
        assert view['opening_balance'] == 1000
        assert view['closing_balance'] == 970
        assert all(r['date'] == '2026-09-01' for r in view['payments'])


def test_source_dates_import_fallback_inclusive_ranges(app):
    with app.app_context():
        db = get_db()
        cid = seed_customer(db)
        balance(db, cid, 'STARTED', 100, '2026-08-01', '2026-07-01')
        balance(db, cid, 'CREATED', 200, '', '2026-08-02')
        balance(db, cid, 'UNDATED', 300, '', '')
        db.commit()
        full = customer_statement(cid)
        assert [r['date'] for r in full['activity']] == ['2026-08-01','2026-08-02','2026-09-01']
        assert full['activity'][-1]['detail'] == 'UNDATED (import date)'
        one = customer_statement(cid, '2026-08-02', '2026-08-02')
        assert one['opening_balance'] == 100
        assert one['legacy_total'] == 200
        assert one['closing_balance'] == 300
        assert one['outstanding_balance'] == 600
        fallback = customer_statement(cid, '2026-09-01', '2026-09-01')
        assert fallback['opening_balance'] == 300
        assert fallback['legacy_total'] == 300
        assert fallback['closing_balance'] == 600


def test_legacy_pdf_route_pagination_labels_and_geometry(app, client):
    login(client)
    with app.app_context():
        db = get_db()
        cid = seed_customer(db)
        for i in range(65):
            balance(db, cid, f'OLD-REF-{i:03}', 100)
        db.commit()
        record_legacy_payment(cid, {'amount':'500', 'payment_date':'2026-08-02', 'reference':'RECEIPT-500'})
        view = customer_statement(cid)
        data = customer_statement_pdf_bytes(view)
        source = pdf_source(data)
        for i in range(65):
            assert f'OLD-REF-{i:03}' in source
        assert 'Legacy balance' in source and 'Legacy payment' in source
        assert 'RECEIPT-500' in source and 'customer-level' in source
        assert 'Legacy balances' in source and '(R6000.00)' in source
        assert data.count(b'/Type /Page ') >= 2
        import pymupdf
        doc = pymupdf.open(stream=data, filetype='pdf')
        for page in doc:
            for block in page.get_text('dict')['blocks']:
                for line in block.get('lines', []):
                    for span in line['spans']:
                        if span['text'] in ('Legacy balance', 'Legacy payment'):
                            assert span['bbox'][2] < 198
        output = os.environ.get('ABI_STATEMENT_PROOF_DIR')
        if output:
            root = Path(output)
            root.mkdir(parents=True, exist_ok=True)
            (root / 'legacy-statement.pdf').write_bytes(data)
            for i, page in enumerate(doc):
                page.get_pixmap(matrix=pymupdf.Matrix(1.5,1.5)).save(str(root / f'legacy-statement-{i+1}.png'))
    response = client.get(f'/customers/{cid}/statement')
    assert response.status_code == 200 and response.mimetype == 'application/pdf'
    assert 'no-store' in response.headers['Cache-Control']
    assert b'OLD-REF-064' in response.data
