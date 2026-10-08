"""ABI-341953129: owner-only borrowing against finalised invoices."""
import importlib.util
from pathlib import Path
import sqlite3

import pytest

from app.db import get_db, run_migrations
from app.services.credit_limits import outstanding_debt
from app.services.documents import create_document, finalize_document, get_document
from app.services.orders import create_order, update_draft_order

spec = importlib.util.spec_from_file_location('credit_test_helpers', Path(__file__).with_name('test_ticket_341953028_customer_blocking.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
app = helpers.app
client = helpers.client


def setup_customer(client, app, limit=230):
    helpers.login(client)
    cid = helpers.create_customer(client)
    response = helpers.save_customer_form(client, app, cid, credit_panel='1', credit_allowed='1', credit_limit=str(limit))
    assert b'Customer saved' in response.data
    return cid


def order(cid, amount=100):
    form = helpers.order_payload(cid)
    form['custom_unit_price'] = str(amount)
    return create_order(form, notify=False)


def invoice(oid):
    from app.services.payments import record_payment, payment_summary
    record_payment(oid, {'method': 'account', 'amount': str(payment_summary(oid)['due_total'])})
    did = create_document(oid, 'invoice')
    finalize_document(did)
    return did


def test_defaults_and_repeat_migration(client, app):
    helpers.login(client)
    cid = helpers.create_customer(client)
    with app.app_context():
        db = get_db()
        run_migrations(db)
        run_migrations(db)
        row = db.execute('SELECT credit_allowed, credit_limit FROM customers WHERE id=?', (cid,)).fetchone()
        assert tuple(row) == (0, 0)
        assert len(db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'account_%'").fetchall()) == 7


@pytest.mark.parametrize('limit', ['0', '-1', 'nan', 'inf', '', 'bad', '0.001'])
def test_invalid_limits(client, app, limit):
    cid = setup_customer(client, app)
    response = helpers.save_customer_form(client, app, cid, credit_panel='1', credit_allowed='1', credit_limit=limit)
    assert b'Customer saved' not in response.data
    assert helpers.customer_row(app, cid)['credit_limit'] == 230


def test_credit_only_owner_and_inline_cannot_change(client, app):
    cid = setup_customer(client, app)
    with client.session_transaction() as sess:
        sess['user_role'] = 'staff'
    response = helpers.save_customer_form(client, app, cid, credit_panel='1', credit_allowed='1', credit_limit='9999')
    assert response.status_code == 403
    from app.services.customers import update_customer
    with app.test_request_context():
        from flask import session
        session['user_role'] = 'staff'
        update_customer(cid, {'name': 'Credit client', 'credit_panel': '1', 'credit_allowed': '1', 'credit_limit': '9999'})
    assert helpers.customer_row(app, cid)['credit_limit'] == 230


def test_unpaid_finalisation_exact_limit_and_drafts(client, app):
    cid = setup_customer(client, app)
    with app.app_context():
        a = order(cid)
        b = order(cid)
        extra_draft = order(cid)
        assert outstanding_debt(cid) == 0
        did = invoice(a)
        assert outstanding_debt(cid) == 115
        assert get_document(did)['payment_status'] == 'paid'
        invoice(b)
        assert outstanding_debt(cid) == 230
        assert finalize_document(did) == did
        assert outstanding_debt(cid) == 230
        assert order(cid, 0)  # no implicit borrowing on creation
        with pytest.raises(ValueError, match='credit limit'):
            invoice(extra_draft)


def test_new_order_over_limit_and_edit_recheck(client, app):
    cid = setup_customer(client, app, 150)
    with app.app_context():
        oid = order(cid)
        form = helpers.order_payload(cid)
        form['custom_unit_price'] = '200'
        update_draft_order(oid, form)
        assert order(cid, 200)
        assert outstanding_debt(cid) == 0
        from app.services.payments import record_payment
        with pytest.raises(ValueError, match='credit limit'):
            record_payment(oid, {'method':'account','amount':'151'})


def test_allocated_payments_refunds_archived_and_prepaid(client, app):
    cid = setup_customer(client, app)
    with app.app_context():
        db = get_db()
        oid = order(cid)
        invoice(oid)
        draft = order(cid)
        for target, amount, status, deleted in [(oid, 30, 'paid', ''), (oid, -10, 'paid', ''), (oid, 99, 'archived', ''), (oid, 99, 'paid', 'deleted'), (draft, 1000, 'paid', '')]:
            db.execute("INSERT INTO payments (order_id, amount, method, status, deleted_at, created_at) VALUES (?, ?, 'manual', ?, ?, '2026-07-01')", (target, amount, status, deleted))
        db.commit()
        assert outstanding_debt(cid) == 95
        db.execute("UPDATE orders SET status='archived' WHERE id=?", (oid,))
        db.commit()
        assert outstanding_debt(cid) == 95
        from app.services.customer_credits import customer_credit_balance
        before = customer_credit_balance(cid)
        invoice(order(cid, 100))
        assert outstanding_debt(cid) == 210
        assert customer_credit_balance(cid) == before


def test_non_credit_payment_requirement_unchanged(client, app):
    helpers.login(client)
    cid = helpers.create_customer(client)
    with app.app_context():
        did = create_document(order(cid), 'invoice')
        with pytest.raises(ValueError, match='at least one payment'):
            finalize_document(did)


def test_database_atomic_guard_stale_finalisation_and_edits(client, app):
    cid = setup_customer(client, app, 150)
    with app.app_context():
        db = get_db()
        a, b = order(cid), order(cid)
        invoice(a)
        dbid = create_document(b, 'invoice')
        with pytest.raises(sqlite3.IntegrityError, match='at least one payment'):
            db.execute("UPDATE documents SET status='finalized' WHERE id=?", (dbid,))
        assert get_document(dbid)['status'] == 'draft'
        db.execute('UPDATE orders SET total=999 WHERE id=?', (a,))
        assert outstanding_debt(cid) == 115  # total edits are not borrowing


def test_global_debt_not_hidden_by_staff_branch(client, app):
    cid = setup_customer(client, app, 150)
    with app.app_context():
        oid = order(cid)
        invoice(oid)
    with app.test_request_context():
        from flask import session
        session['branch_id'] = 987
        session['user_role'] = 'staff'
        assert outstanding_debt(cid) == 115


def test_repayment_restores_capacity(client, app):
    cid = setup_customer(client, app, 115)
    with app.app_context():
        db = get_db()
        oid = order(cid)
        invoice(oid)
        assert order(cid, 10)  # order itself does not use facility
        db.execute("INSERT INTO payments (order_id, amount, method, status, created_at) VALUES (?, 115, 'eft', 'paid', '2026-07-01')", (oid,))
        db.commit()
        assert outstanding_debt(cid) == 0
        assert order(cid)


def test_staff_can_read_credit_but_form_has_no_controls(client, app):
    cid = setup_customer(client, app)
    with client.session_transaction() as sess:
        sess['user_role'] = 'staff'
        sess['staff_modules'] = ['customers', 'orders', 'documents']
    detail = client.get(f'/customers/{cid}')
    assert detail.status_code == 200 and b'230.00' in detail.data
    form = client.get(f'/customers/{cid}/edit')
    assert form.status_code == 200
    assert b'name="credit_allowed"' not in form.data
    assert b'name="credit_limit"' not in form.data


def test_additive_upgrade_defaults_existing_customer(client, app):
    helpers.login(client)
    cid = helpers.create_customer(client)
    with app.app_context():
        db = get_db()
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'account_%'").fetchall():
            db.execute('DROP TRIGGER ' + row['name'])
        db.execute('ALTER TABLE customers DROP COLUMN credit_allowed')
        db.execute('ALTER TABLE customers DROP COLUMN credit_limit')
        run_migrations(db)
        row = db.execute('SELECT credit_allowed, credit_limit FROM customers WHERE id=?', (cid,)).fetchone()
        assert tuple(row) == (0, 0)


def test_concurrent_finalisation_cannot_double_spend(client, app):
    # Explicit borrowing, not finalisation, is now the concurrency boundary.
    import threading
    cid = setup_customer(client, app, 150)
    with app.app_context():
        ids = [order(cid), order(cid)]
    barrier = threading.Barrier(2)
    outcomes = []
    def worker(oid):
        db = sqlite3.connect(app.config['DATABASE'], timeout=10)
        try:
            barrier.wait()
            db.execute("INSERT INTO payments(order_id,amount,method,status,created_at) VALUES (?,115,'account','paid','2026-10-08')", (oid,))
            db.commit()
            outcomes.append('ok')
        except sqlite3.IntegrityError:
            outcomes.append('blocked')
        finally:
            db.close()
    threads = [threading.Thread(target=worker, args=(oid,)) for oid in ids]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=15)
    assert sorted(outcomes) == ['blocked', 'ok']
    with app.app_context(): assert outstanding_debt(cid) == 115


def test_credit_form_and_invoice_html(client, app):
    cid = setup_customer(client, app)
    form = client.get(f'/customers/{cid}/edit').data
    assert b'name="credit_allowed"' in form and b'name="credit_limit"' in form
    with app.app_context():
        did = create_document(order(cid), 'invoice')
    page = client.get(f'/documents/{did}')
    assert page.status_code == 200
    assert b'Record at least one partial or full payment' in page.data
