"""ABI-341953234: optional refunds of money received on cancelled drafts."""
import re
from uuid import uuid4

import pytest
from test_app import app, client, login
from app.db import get_db, now, init_db
from app.services.orders import transition_order, get_order, deposit_to_process_amount
from app.services.payments import record_refund, refundable_amount, payment_total, list_payments
from app.services.customer_credits import customer_credit_balance
from app.services.credit_limits import facility_summary


def seed(app, paid=500, status="draft", deposit=200):
    with app.app_context():
        db = get_db()
        customer = db.execute("INSERT INTO customers (name, created_at) VALUES ('Refund fixture', ?)", (now(),)).lastrowid
        oid = db.execute("""INSERT INTO orders (order_number, customer_id, status, subtotal, total, deposit_total, created_at)
            VALUES (?, ?, ?, 800, 1000, ?, ?)""", ('REF-' + str(uuid4()), customer, status, deposit, now())).lastrowid
        if paid:
            db.execute("INSERT INTO payments (order_id, amount, method, status, created_at) VALUES (?, ?, 'cash', 'paid', ?)", (oid, paid, now()))
        db.commit()
        return oid, customer


def refund(amount=100, method="cash", key=None):
    return dict(refund_amount=str(amount), refund_method=method, refund_request_id=key or str(uuid4()),
                refund_date="2026-10-17T10:30", reference="Ticket test refund")


@pytest.mark.parametrize("paid", [0, 250, 1000])
def test_optional_cancel_then_full_refund(client, app, paid):
    oid, _ = seed(app, paid)
    login(client)
    before = client.get(f"/orders/{oid}")
    assert b'class="payment-form refund-form"' not in before.data
    response = client.post(f"/orders/{oid}/cancel", follow_redirects=True)
    assert response.status_code == 200
    with app.app_context():
        order = get_order(oid)
        assert order['status'] == 'canceled'
        assert order['status_before_cancel'] == 'draft'
        assert order['total'] == 1000
        assert payment_total(oid) == paid  # Cancellation alone never pays out.
        assert refundable_amount(oid) == paid
        assert deposit_to_process_amount(order) == 0
    assert (b'Refund cancelled draft payment' in response.data) == bool(paid)
    if paid:
        token = re.search(rb'name="refund_request_id" value="([^"]+)"', response.data)[1].decode()
        data = refund(paid, key=token)
        response = client.post(f"/orders/{oid}/refund", data=data, follow_redirects=True)
        assert b'Refund recorded' in response.data
        assert b'class="payment-form refund-form"' not in response.data
        client.post(f"/orders/{oid}/refund", data=data)
        with app.app_context():
            assert payment_total(oid) == 0
            assert get_order(oid)['total'] == 1000
            assert get_db().execute('SELECT COUNT(*) n FROM payments WHERE order_id=?', (oid,)).fetchone()['n'] == 2
    else:
        with app.app_context(), pytest.raises(ValueError):
            record_refund(oid, refund(1))


def test_partial_credit_and_duplicate_submission(app):
    oid, customer = seed(app, 500)
    with app.app_context():
        transition_order(oid, 'cancel')
        form = refund(100, 'customer_credit')
        record_refund(oid, form)
        with pytest.raises(ValueError):
            record_refund(oid, form)
        assert refundable_amount(oid) == 400
        assert customer_credit_balance(customer) == 100
        from app.services.payments import archive_payment
        payment = get_db().execute('SELECT id FROM payments WHERE refund_request_id=?', (form['refund_request_id'],)).fetchone()
        with pytest.raises(ValueError):
            archive_payment(payment['id'])
        assert customer_credit_balance(customer) == 100
        record_refund(oid, refund(400, 'eft'))
        assert refundable_amount(oid) == 0
        assert payment_total(oid) == 0
        rows = [r for r in list_payments() if r['order_id'] == oid]
        assert len(rows) == 3
        assert sum(r['amount'] for r in rows) == 0
        assert get_order(oid)['total'] == 1000
        assert facility_summary(customer)['used'] == 0


@pytest.mark.parametrize('amount', ['-1', '0', '501', 'nan', 'inf', 'abc'])
def test_invalid_amount_does_not_write(app, amount):
    oid, _ = seed(app)
    with app.app_context():
        transition_order(oid, 'cancel')
        with pytest.raises(ValueError):
            record_refund(oid, refund(amount))
        assert payment_total(oid) == 500


@pytest.mark.parametrize('field,value', [('refund_method', 'account'), ('refund_date', 'bad-date'), ('refund_request_id', ''), ('refund_request_id', 'x'*101)])
def test_invalid_fields_do_not_write(app, field, value):
    oid, _ = seed(app)
    with app.app_context():
        transition_order(oid, 'cancel')
        form = refund()
        form[field] = value
        with pytest.raises(ValueError):
            record_refund(oid, form)
        assert payment_total(oid) == 500


def test_prior_deposit_payout_synthetic_and_archived_rows(app, client):
    oid, _ = seed(app, 500)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE orders SET deposit_applied_amount=100, deposit_refund_amount=100, deposit_process_method='cash' WHERE id=?", (oid,))
        for amount, method, status in [(100, 'deposit_applied', 'paid'), (300, 'cash', 'archived')]:
            db.execute('INSERT INTO payments (order_id,amount,method,status,created_at) VALUES (?,?,?,?,?)', (oid,amount,method,status,now()))
        db.commit()
        transition_order(oid, 'cancel')
        assert refundable_amount(oid) == 400
        record_refund(oid, refund(400))
        assert refundable_amount(oid) == 0
        from app.services.orders import reverse_deposit_settlement
        with pytest.raises(ValueError):
            reverse_deposit_settlement(oid)
        assert refundable_amount(oid) == 0
    login(client)
    for endpoint in ['settle-return', 'use-deposit', 'deposit-refund/delete']:
        client.post(f'/orders/{oid}/{endpoint}', data={'deposit_process_method':'cash','deposit_refund_amount':'200'})
    response = client.post(f'/orders/{oid}/revert-draft', follow_redirects=True)
    with app.app_context():
        assert get_order(oid)['status'] == 'canceled'
        assert refundable_amount(oid) == 0


@pytest.mark.parametrize('status', ['reserved', 'returned', 'canceled'])
def test_other_workflows_not_given_new_entitlement(app, status):
    oid, _ = seed(app, 500, status)
    with app.app_context():
        if status == 'reserved':
            transition_order(oid, 'cancel')
        assert refundable_amount(oid) == 0
        with pytest.raises(ValueError):
            record_refund(oid, refund(100))


def test_customer_credit_failure_rolls_back_payment(app, monkeypatch):
    oid, _ = seed(app)
    with app.app_context():
        transition_order(oid, 'cancel')
        def fail(*args, **kwargs):
            raise ValueError('forced credit failure')
        monkeypatch.setattr('app.services.customer_credits.create_customer_credit', fail)
        with pytest.raises(ValueError):
            record_refund(oid, refund(100, 'customer_credit'))
        assert refundable_amount(oid) == 500
        assert payment_total(oid) == 500


def test_refund_access_requires_login(client, app):
    oid, _ = seed(app)
    response = client.post(f'/orders/{oid}/refund', data=refund())
    assert response.status_code == 302 and '/login' in response.location


def test_concurrent_refunds_cannot_exceed_received(app):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    oid, _ = seed(app, 500)
    with app.app_context():
        transition_order(oid, 'cancel')
    barrier = Barrier(2)
    def attempt():
        with app.app_context():
            barrier.wait()
            try:
                record_refund(oid, refund(400))
                return True
            except ValueError:
                return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(results) == [False, True]
    with app.app_context():
        assert refundable_amount(oid) == 100


def test_cancelled_draft_refund_does_not_use_credit_facility(app):
    oid, customer = seed(app, 500)
    with app.app_context():
        db = get_db()
        db.execute('UPDATE customers SET credit_allowed=1,credit_limit=600 WHERE id=?', (customer,))
        db.commit()
        assert facility_summary(customer)['used'] == 500
        transition_order(oid, 'cancel')
        record_refund(oid, refund(500))
        assert facility_summary(customer)['used'] == 0


def test_additive_migration_is_idempotent(app):
    with app.app_context():
        init_db()
        init_db()
        assert 'status_before_cancel' in {r['name'] for r in get_db().execute('PRAGMA table_info(orders)')}
