"""Geoquest: unassigned customers inherit only an unambiguous invoice issuer."""
import pytest

from app.db import get_db
from app.services.customers import customer_statement
from app.services.account_statements import account_statement
from app.services.pdf_documents import customer_statement_pdf_bytes, document_pdf_bytes
from tests.test_ticket_341953085_customer_statement import (
    app, client, login, seed_customer, seed_order, seed_invoice,
)
from tests.test_ticket_341953293_statement_banking import BANKING, branch, text_of


def invoice(db, cid, bid, number='1', status='finalized', order_status='reserved'):
    oid = seed_order(db, cid, 'ORD-FALLBACK-' + number, 100, status=order_status)
    db.execute('UPDATE orders SET collect_branch_id=? WHERE id=?', (bid, oid))
    return seed_invoice(db, oid, 'INV-FALLBACK-' + number, status=status,
                        created_at='2026-10-01T10:00:00')


def test_geoquest_style_customer_and_both_routes_use_invoice_bank(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        cid = seed_customer(db, 'Geoquest fixture')
        bid = branch(db, 'Invoice issuer', BANKING)
        other = branch(db, 'Logged in elsewhere', {**BANKING, 'bank_account_number': 'WRONG'})
        db.execute('UPDATE users SET branch_id=?, can_view_all_branches=1', (other,))
        db.execute('UPDATE customers SET credit_allowed=1, credit_limit=5000 WHERE id=?', (cid,))
        did = invoice(db, cid, bid)
        invoice_before = document_pdf_bytes(did)
        db.commit()
        for view in (customer_statement(cid), customer_statement(cid, '2026-11-01'), account_statement(cid)):
            assert view['issuer_name'] == 'Invoice issuer'
            assert view['banking_details'] == BANKING
            text = text_of(customer_statement_pdf_bytes(view))
            assert 'Account number: ' + BANKING['bank_account_number'] in text
            assert 'WRONG' not in text
        assert document_pdf_bytes(did) == invoice_before
        assert db.execute('SELECT branch_id FROM customers WHERE id=?', (cid,)).fetchone()['branch_id'] is None
    response = client.get(f'/customers/{cid}/statement')
    assert response.status_code == 200
    assert 'Account number: ' + BANKING['bank_account_number'] in text_of(response.data)
    assert 'no-store' in response.headers['Cache-Control']
    assert client.get(f'/customers/{cid}/statement?date_from=2026-11-01').status_code == 200


@pytest.mark.parametrize('second_branch', ['other', 'null'])
def test_ambiguous_invoice_issuers_never_choose_a_bank(app, second_branch):
    with app.app_context():
        db = get_db()
        cid = seed_customer(db)
        first = branch(db, 'First', BANKING)
        second = branch(db, 'Second', BANKING) if second_branch == 'other' else None
        invoice(db, cid, first)
        invoice(db, cid, second, '2')
        view = customer_statement(cid)
        assert not any(view['banking_details'].values())
        assert 'Banking details' not in text_of(customer_statement_pdf_bytes(view))


def test_assigned_customer_branch_keeps_precedence(app):
    with app.app_context():
        db = get_db()
        cid = seed_customer(db)
        assigned = branch(db, 'Assigned', {**BANKING, 'bank_account_number': 'ASSIGNED'})
        issuer = branch(db, 'Invoice', BANKING)
        db.execute('UPDATE customers SET branch_id=? WHERE id=?', (assigned, cid))
        invoice(db, cid, issuer)
        assert customer_statement(cid)['banking_details']['bank_account_number'] == 'ASSIGNED'


@pytest.mark.parametrize('status,order_status', [('draft', 'reserved'), ('finalized', 'cancelled'), ('finalized', 'canceled')])
def test_non_statement_orders_do_not_change_issuer(app, status, order_status):
    with app.app_context():
        db = get_db()
        cid = seed_customer(db)
        good = branch(db, 'Good', BANKING)
        other = branch(db, 'Excluded', {**BANKING, 'bank_account_number': 'WRONG'})
        invoice(db, cid, good)
        invoice(db, cid, other, '2', status, order_status)
        assert customer_statement(cid)['banking_details'] == BANKING
