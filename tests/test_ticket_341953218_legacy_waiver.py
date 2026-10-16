"""A waiver is not a payment and must agree across customer and statement."""
import pytest
from test_legacy_customer_balances import app, client, login, seed_customer
from app.db import get_db
from app.services.legacy_balances import upsert_legacy_balance, legacy_balance_summary, record_legacy_payment
from app.services.customers import customer_statement


@pytest.mark.parametrize('settlement', [0, 150])
def test_waiver_preserves_import_receipts_and_other_customers(app, client, settlement):
    login(client)
    with app.app_context():
        db = get_db()
        cid = seed_customer(db, 'Add-x Marketing Solutions')
        db.execute("UPDATE customers SET source_id='add-x-source' WHERE id=?", (cid,))
        other = seed_customer(db, 'Unrelated customer')
        upsert_legacy_balance(cid, 'old-cust-1', '8355', 2650)
        upsert_legacy_balance(other, 'old-cust-1', 'OTHER', 99)
        db.commit()
        if settlement:
            record_legacy_payment(cid, {'amount': str(settlement), 'method': 'cash', 'payment_date': '2026-10-04'})
        before_receipts = [tuple(r) for r in db.execute('SELECT * FROM legacy_balance_payments')]
        assert customer_statement(cid)['outstanding_balance'] == 2650 - settlement
        db.execute("UPDATE legacy_customer_balances SET status='waived',note=note || ' ABI-341953218' WHERE customer_id=?", (cid,))
        db.commit()
        summary = legacy_balance_summary(cid)
        assert summary['total'] == summary['active_count'] == 0
        assert summary['imported_total'] == 2650
        assert summary['settled_total'] == settlement
        assert summary['entries'][0]['outstanding'] == 0
        view = customer_statement(cid)
        assert view['outstanding_balance'] == view['closing_balance'] == 0
        assert view['paid_total'] == settlement
        assert view['legacy_total'] == settlement
        assert 'unpaid remainder waived' in view['legacy_balances'][0]['detail']
        assert [tuple(r) for r in db.execute('SELECT * FROM legacy_balance_payments')] == before_receipts
        assert legacy_balance_summary(other)['total'] == 99
        assert customer_statement(other)['outstanding_balance'] == 99
        # Historical ranges/opening balances must also reconcile after waiver.
        assert customer_statement(cid, '2026-10-05')['closing_balance'] == 0
    page = client.get(f'/customers/{cid}')
    assert page.status_code == 200
    panel = page.get_data(as_text=True).split('id="legacy-balance"', 1)[1].split('</article>', 1)[0]
    assert 'R0.00' in panel and 'Waived' in panel
    assert '<b class="balance-owing">' not in panel
    assert 'Record payment' not in panel
    pdf = client.get(f'/customers/{cid}/statement')
    assert pdf.status_code == 200 and pdf.mimetype == 'application/pdf'
    assert b'unpaid remainder waived' in pdf.data
