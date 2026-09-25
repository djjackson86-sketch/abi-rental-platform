"""Ticket ABI-341953057 - edit and delete a deposit refund payout.

Requested edit: "enable edit and delete function for deposit refunds".

A security-deposit refund is not a payments row - it lives on the order
(``orders.deposit_refund_amount`` / ``deposit_process_method`` /
``deposit_processed_at`` / ``deposit_note``) and renders as the "Money payout" row
on the order detail. Before this ticket that row carried an **empty** action cell:
the only way to change a payout was to press "Refund Deposit" again (which recomputes
the split from the balance, so a typo could not be corrected to a different amount)
and there was no way to remove one at all.

These tests pin the new pair of actions and, more importantly, the money invariants
they must not break:

* editing restates the split as ``applied = deposit_total - refund`` and rewrites the
  ``deposit_applied`` payment (same row, no duplicate), so
  ``Paid = non-deposit payments + deposit applied`` still holds and the invoice
  "Less: deposit used" line, the Reports figures and the payment status follow;
* deleting is a **soft** reversal - the payment row is archived (never deleted), the
  deposit goes back to unprocessed, the order returns to the "Process deposit" folder
  and the cash-up/day report stop deducting a payout that never happened;
* both are gated exactly like the settlement buttons (returned/started/canceled, the
  same branch scope, login required) and nothing loosened.
"""
import os
import tempfile

import pytest
from flask import session as flask_session

from app import create_app
from app.db import get_db
from app.services import cash
from app.services.orders import order_filter_counts
from app.services.payments import payment_summary
from app.services.access import create_additional_user

DAY = '2026-09-18'


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    application = create_app({
        'TESTING': True,
        'DATABASE': path,
        'SECRET_KEY': 'test',
        'ADMIN_EMAIL': 'admin@abi.local',
        'ADMIN_PASSWORD': 'admin123',
    })
    yield application
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client, name=None, password='admin123'):
    with client.application.app_context():
        if name is None:
            row = get_db().execute(
                "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
        else:
            row = get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
    return client.post('/login', data={'user_id': str(row['id']), 'password': password},
                       follow_redirects=True)


def body(response):
    return response.data.decode('utf-8')


def add_branch_staff(app, name, branch_ids):
    with app.app_context():
        user_id, error = create_additional_user(name, 'staff123',
                                                branch_ids=[str(b) for b in branch_ids])
        assert error is None
    return name


def _insert_order(db, number, status='returned', *, deposit=1000, applied=0, refund=0,
                  method='', processed='', note='', total=2000, due=None, branch_id=1):
    """One returned order with a deposit; no app writes, so tests control the state."""
    due = total if due is None else due
    payment_status = 'payment_due' if due > 0 else 'paid'
    db.execute(
        """INSERT INTO orders (order_number, booking_type, collect_branch_id, return_branch_id,
        status, payment_status, start_at, end_at, subtotal, tax_total, deposit_total,
        deposit_applied_amount, deposit_refund_amount, deposit_process_method,
        deposit_processed_at, deposit_note, total, due_total, notes, created_at)
        VALUES (?, 'return', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)""",
        (number, branch_id, branch_id, status, payment_status,
         f"{DAY}T09:00:00", f"{DAY}T17:00:00", total - deposit, deposit,
         applied, refund, method, processed, note, total, due, f"{DAY}T09:00:00"),
    )
    return db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()['id']


def _insert_payment(db, order_id, amount, *, method='cash', reference='PART-PAY',
                    payment_date=DAY, status='paid', deleted_at=''):
    db.execute(
        """INSERT INTO payments (order_id, amount, method, reference, status, payment_date,
        deleted_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (order_id, amount, method, reference, status, payment_date, deleted_at,
         f"{DAY}T10:00:00"),
    )
    return db.execute("SELECT id FROM payments ORDER BY id DESC LIMIT 1").fetchone()['id']


def settle(app, client, *, total=2000, deposit=1000, paid=1500, method='eft',
           number='ORD-57001', date=f'{DAY}T16:00:00', note='Refund to customer'):
    """Insert an order, part-pay the rental, then press Refund Deposit for real.

    R2000 total, R1000 deposit, R1500 already paid -> R500 of rental money still due,
    so the settlement applies R500 and pays R500 out: paid == R2000, due == 0.
    """
    with app.app_context():
        order_id = _insert_order(get_db(), number, 'returned', deposit=deposit, total=total)
        _insert_payment(get_db(), order_id, paid, payment_date=DAY)
        get_db().commit()
    response = client.post(f'/orders/{order_id}/settle-return', data={
        'deposit_process_method': method,
        'deposit_processed_at': date,
        'deposit_note': note,
    }, follow_redirects=True)
    assert b'Deposit settled' in response.data
    return order_id


def post_edit(client, order_id, **overrides):
    data = {
        'refund_amount': '300.00',
        'deposit_process_method': 'eft',
        'deposit_processed_at': f'{DAY}T16:00:00',
        'deposit_note': 'Corrected payout',
    }
    data.update(overrides)
    return client.post(f'/orders/{order_id}/deposit-refund/edit', data=data, follow_redirects=True)


def order_row(app, order_id):
    with app.app_context():
        return dict(get_db().execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())


def payment_rows(app, order_id):
    with app.app_context():
        return [dict(row) for row in get_db().execute(
            "SELECT * FROM payments WHERE order_id = ? ORDER BY id", (order_id,)).fetchall()]


def deposit_payment(app, order_id):
    with app.app_context():
        row = get_db().execute(
            "SELECT * FROM payments WHERE order_id = ? AND method = 'deposit_applied'",
            (order_id,)).fetchone()
    return dict(row) if row else None


def summary(app, order_id):
    with app.app_context():
        return payment_summary(order_id)


def cashup(app, day=DAY, branch_id=1):
    ctx = app.test_request_context('/dashboard')
    ctx.push()
    flask_session['user_id'] = 1
    flask_session['user_role'] = 'owner'
    try:
        return cash.day_summary(day=day, branch_id=branch_id)
    finally:
        ctx.pop()


def deposit_refunds(app, day=DAY, branch_id=1):
    ctx = app.test_request_context('/dashboard')
    ctx.push()
    flask_session['user_id'] = 1
    flask_session['user_role'] = 'owner'
    try:
        return cash.cash_deposit_refunds(day, branch_id)
    finally:
        ctx.pop()


def process_deposit_count(app):
    ctx = app.test_request_context('/orders')
    ctx.push()
    flask_session['user_id'] = 1
    flask_session['user_role'] = 'owner'
    try:
        return order_filter_counts()['payment_status']['process_deposit']
    finally:
        ctx.pop()


def insert_invoice(app, order_id, number='INV-57001'):
    with app.app_context():
        db = get_db()
        db.execute(
            """INSERT INTO documents (order_id, document_type, status, number, pdf_path,
            revision_of_id, revision_number, revised_at, created_at)
            VALUES (?, 'invoice', 'finalized', ?, '', NULL, 0, '', ?)""",
            (order_id, number, f'{DAY}T12:00:00'))
        db.commit()
        return db.execute("SELECT id FROM documents WHERE order_id = ?", (order_id,)).fetchone()['id']


def invoice_html(client, document_id):
    return body(client.get(f'/documents/{document_id}'))


# ---------------------------------------------------------------------------
# The order detail row offers edit + delete (ticket's own surface)
# ---------------------------------------------------------------------------

def test_payout_row_offers_edit_and_delete_only_for_a_recorded_refund(app, client):
    login(client)
    order_id = settle(app, client)

    html = body(client.get(f'/orders/{order_id}'))
    payout_row = html.split('Money payout', 1)[1].split('</div>', 1)[0]
    assert f'/orders/{order_id}/deposit-refund/edit' in payout_row
    assert f'/orders/{order_id}/deposit-refund/delete' in payout_row
    assert 'Delete' in payout_row

    # A refund that has never been processed has no payout row and therefore no
    # buttons: nothing is offered where there is nothing to edit.
    with app.app_context():
        unpaid = _insert_order(get_db(), 'ORD-57099', 'returned', refund=400)
        get_db().commit()
    html = body(client.get(f'/orders/{unpaid}'))
    assert 'Money payout' not in html
    assert f'/orders/{unpaid}/deposit-refund/delete' not in html


def test_edit_form_prefills_the_current_payout(app, client):
    login(client)
    order_id = settle(app, client, method='cash', note='Paid cash at the depot')

    html = body(client.get(f'/orders/{order_id}/deposit-refund/edit'))

    assert f'value="500.00"' in html                      # current payout amount
    assert 'name="refund_amount"' in html
    assert 'name="deposit_process_method"' in html
    assert '<option value="cash" selected' in html        # current method
    assert 'Paid cash at the depot' in html               # current note
    assert '2026-09-18T16:00' in html                     # current payout date


# ---------------------------------------------------------------------------
# Editing the amount keeps the money maths honest
# ---------------------------------------------------------------------------

def test_edit_amount_recalculates_the_split_paid_due_status_and_invoice(app, client):
    login(client)
    order_id = settle(app, client)
    invoice_id = insert_invoice(app, order_id)

    # R500 paid out, R500 applied out of the R1000 deposit.
    assert order_row(app, order_id)['deposit_applied_amount'] == 500
    assert summary(app, order_id)['paid_total'] == 2000
    assert summary(app, order_id)['due_total'] == 0
    assert '-R500.00' in invoice_html(client, invoice_id)

    response = post_edit(client, order_id, refund_amount='300.00',
                         deposit_process_method='card', deposit_note='Corrected to R300')
    assert b'Deposit refund updated: R300.00 paid out; R700.00 used' in response.data

    order = order_row(app, order_id)
    assert order['deposit_refund_amount'] == 300
    assert order['deposit_applied_amount'] == 700
    assert order['deposit_process_method'] == 'card'
    assert order['deposit_processed_at'] == f'{DAY}T16:00:00'
    assert order['deposit_note'] == 'Corrected to R300'

    # the deposit still adds up, and Paid = non-deposit (1500) + applied (700)
    with app.app_context():
        payment = get_db().execute(
            "SELECT * FROM payments WHERE order_id = ? AND method = 'deposit_applied'",
            (order_id,)).fetchone()
    assert payment['amount'] == 700 and payment['status'] == 'paid'
    assert payment['reference'] == 'Deposit Amount Utilised'
    assert len([row for row in payment_rows(app, order_id)
                if row['method'] == 'deposit_applied']) == 1        # rewritten, not duplicated
    assert summary(app, order_id)['paid_total'] == 2200
    assert summary(app, order_id)['due_total'] == -200
    assert summary(app, order_id)['payment_status'] == 'overpaid'

    # the invoice's deposit deduction follows the order
    assert '-R700.00' in invoice_html(client, invoice_id)
    assert '-R500.00' not in invoice_html(client, invoice_id)
    # the payout row on the order detail shows the corrected money
    detail = body(client.get(f'/orders/{order_id}'))
    assert '-R300.00' in detail and 'Payout date' in detail


def test_edit_can_move_the_whole_deposit_back_to_used(app, client):
    """A payout of the full deposit leaves nothing applied and archives the payment."""
    login(client)
    order_id = settle(app, client)

    post_edit(client, order_id, refund_amount='1000.00')

    order = order_row(app, order_id)
    assert order['deposit_refund_amount'] == 1000 and order['deposit_applied_amount'] == 0
    payment = deposit_payment(app, order_id)
    assert payment['status'] == 'archived' and payment['deleted_at']
    assert payment['amount'] == 500      # archived, never deleted — but no longer counted
    # Paid is now just the R1500 the customer actually paid
    assert summary(app, order_id)['paid_total'] == 1500
    assert summary(app, order_id)['due_total'] == 500
    assert summary(app, order_id)['payment_status'] == 'partially_paid'


def test_edit_refuses_a_payout_bigger_than_the_deposit(app, client):
    login(client)
    order_id = settle(app, client)

    response = post_edit(client, order_id, refund_amount='1000.01')

    assert b'Deposit refund cannot be more than the R1000.00 deposit' in response.data
    order = order_row(app, order_id)
    assert order['deposit_refund_amount'] == 500 and order['deposit_applied_amount'] == 500


@pytest.mark.parametrize('amount,message', [
    ('0', 'Deposit refund amount must be more than zero'),
    ('-50', 'Deposit refund amount must be more than zero'),
    ('abc', 'Deposit refund amount must be a number'),
    ('', 'Deposit refund amount must be more than zero'),
])
def test_edit_refuses_a_bad_amount(app, client, amount, message):
    login(client)
    order_id = settle(app, client)

    response = post_edit(client, order_id, refund_amount=amount)

    assert message.encode() in response.data
    assert order_row(app, order_id)['deposit_refund_amount'] == 500


def test_edit_refuses_bad_method_and_future_date(app, client):
    login(client)
    order_id = settle(app, client)

    response = post_edit(client, order_id, deposit_process_method='bank')
    assert b'Deposit process method must be EFT, Card, or Cash' in response.data

    response = post_edit(client, order_id, deposit_processed_at='2099-01-01T10:00')
    assert b'Deposit refund date cannot be in the future' in response.data

    assert order_row(app, order_id)['deposit_refund_amount'] == 500
    assert order_row(app, order_id)['deposit_process_method'] == 'eft'


def test_edit_needs_a_recorded_refund_and_rejects_the_settlement_states(app, client):
    login(client)
    order_id = settle(app, client)

    # nothing recorded
    with app.app_context():
        plain = _insert_order(get_db(), 'ORD-57002', 'returned')
        get_db().commit()
    response = post_edit(client, plain)
    assert b'There is no deposit refund recorded on this order to edit' in response.data
    redirected = client.get(f'/orders/{plain}/deposit-refund/edit', follow_redirects=True)
    assert b'There is no deposit refund recorded on this order to edit' in redirected.data

    # an order that was never picked up or canceled cannot be edited either
    with app.app_context():
        draft = _insert_order(get_db(), 'ORD-57003', 'draft', refund=400, method='eft',
                              processed=f'{DAY}T16:00:00')
        get_db().commit()
    response = post_edit(client, draft)
    assert b'Return and deposit settlement is available after pickup or cancelation' in response.data
    assert order_row(app, draft)['deposit_refund_amount'] == 400
    assert 'Return and deposit settlement is available after pickup or cancelation' in body(
        client.get(f'/orders/{draft}/deposit-refund/edit', follow_redirects=True))


# ---------------------------------------------------------------------------
# Deleting reverses the payout (soft) and puts the order back in the folder
# ---------------------------------------------------------------------------

def test_delete_reverses_the_payout_and_returns_the_order_to_process_deposit(app, client):
    login(client)
    order_id = settle(app, client, method='cash', note='Paid cash at the depot',
                      number='ORD-57011')
    assert process_deposit_count(app) == 0
    assert 'ORD-57011' not in body(client.get('/orders?payment_status=process_deposit'))

    response = client.post(f'/orders/{order_id}/deposit-refund/delete', follow_redirects=True)

    assert b'Deposit refund of R500.00 deleted' in response.data
    order = order_row(app, order_id)
    assert order['deposit_refund_amount'] == 0
    assert order['deposit_applied_amount'] == 0
    assert order['deposit_process_method'] == ''
    assert order['deposit_processed_at'] == ''
    assert 'Deposit refund of R500.00 deleted' in order['deposit_note']   # audit line kept
    assert 'Paid cash at the depot' in order['deposit_note']              # old note kept

    # nothing was hard deleted: the deposit_applied payment is archived
    payment = deposit_payment(app, order_id)
    assert payment is not None
    assert payment['status'] == 'archived' and payment['deleted_at']

    # money falls back to what the customer actually paid
    assert summary(app, order_id)['paid_total'] == 1500
    assert summary(app, order_id)['due_total'] == 500
    assert summary(app, order_id)['payment_status'] == 'partially_paid'

    # back in the Process deposit folder, badge and order page
    assert process_deposit_count(app) == 1
    assert 'ORD-57011' in body(client.get('/orders?payment_status=process_deposit'))
    detail = body(client.get(f'/orders/{order_id}'))
    assert 'Money payout' not in detail
    assert 'Refund Deposit' in detail                      # the payout can be re-recorded
    assert 'Deposit used/refund' not in detail


def test_delete_drops_the_cash_payout_from_the_cashup_and_day_report(app, client):
    login(client)
    order_id = settle(app, client, method='cash')

    before = cashup(app)
    assert before['cash_received'] == 1500
    assert before['deposit_refund_total'] == 500
    assert before['expected'] == 1000

    client.post(f'/orders/{order_id}/deposit-refund/delete', follow_redirects=True)

    after = cashup(app)
    assert after['deposit_refund_total'] == 0
    assert after['cash_received'] == 1500
    assert after['expected'] == 1500
    # the day report reads the same function, so it cannot disagree
    assert deposit_refunds(app) == 0

    # and putting the payout back (for the right depot) restores the deduction
    assert b'Deposit settled' in client.post(f'/orders/{order_id}/settle-return', data={
        'deposit_process_method': 'cash',
        'deposit_processed_at': f'{DAY}T16:00:00',
        'deposit_note': 'Re-recorded',
    }, follow_redirects=True).data
    assert cashup(app)['expected'] == 1000


def test_delete_is_idempotent_and_refuses_when_nothing_is_recorded(app, client):
    login(client)
    order_id = settle(app, client, number='ORD-57004')

    client.post(f'/orders/{order_id}/deposit-refund/delete', follow_redirects=True)
    again = client.post(f'/orders/{order_id}/deposit-refund/delete', follow_redirects=True)

    assert b'There is no deposit refund recorded on this order to delete' in again.data
    assert order_row(app, order_id)['deposit_note'].count('deleted') == 1

    with app.app_context():
        plain = _insert_order(get_db(), 'ORD-57005', 'returned')
        get_db().commit()
    response = client.post(f'/orders/{plain}/deposit-refund/delete', follow_redirects=True)
    assert b'There is no deposit refund recorded on this order to delete' in response.data


def test_delete_is_gated_to_settled_statuses(app, client):
    login(client)
    with app.app_context():
        draft = _insert_order(get_db(), 'ORD-57006', 'draft', refund=400, method='eft',
                              processed=f'{DAY}T16:00:00')
        get_db().commit()

    response = client.post(f'/orders/{draft}/deposit-refund/delete', follow_redirects=True)

    assert b'Return and deposit settlement is available after pickup or cancelation' in response.data
    order = order_row(app, draft)
    assert order['deposit_refund_amount'] == 400 and order['deposit_process_method'] == 'eft'


# ---------------------------------------------------------------------------
# Same guards as the settlement buttons: login, branch scope, staff
# ---------------------------------------------------------------------------

def test_both_actions_require_login(app, client):
    login(client)
    order_id = settle(app, client)
    client.post('/logout')

    assert client.post(f'/orders/{order_id}/deposit-refund/delete', follow_redirects=False).status_code == 302
    assert client.post(f'/orders/{order_id}/deposit-refund/edit', data={'refund_amount': '10'},
                       follow_redirects=False).status_code == 302
    assert order_row(app, order_id)['deposit_refund_amount'] == 500


def test_staff_cannot_touch_another_branch_payout(app, client):
    login(client)
    order_id = settle(app, client, number='ORD-57007')          # branch 1 (Midrand)
    staff = add_branch_staff(app, 'Pretoria Deposit Staff', (2,))
    client.post('/logout')
    login(client, name=staff, password='staff123')

    assert client.get(f'/orders/{order_id}/deposit-refund/edit').status_code == 404
    assert client.post(f'/orders/{order_id}/deposit-refund/delete',
                       follow_redirects=False).status_code == 404

    order = order_row(app, order_id)
    assert order['deposit_refund_amount'] == 500 and order['deposit_applied_amount'] == 500
    assert order['deposit_process_method'] == 'eft'


def test_staff_in_the_orders_branch_can_edit_and_delete(app, client):
    login(client)
    order_id = settle(app, client, number='ORD-57008')          # branch 1 (Midrand)
    staff = add_branch_staff(app, 'Midrand Deposit Staff', (1,))
    client.post('/logout')
    login(client, name=staff, password='staff123')

    assert b'Deposit refund updated' in post_edit(client, order_id, refund_amount='250.00').data
    assert order_row(app, order_id)['deposit_refund_amount'] == 250
    assert b'Deposit refund of R250.00 deleted' in client.post(
        f'/orders/{order_id}/deposit-refund/delete', follow_redirects=True).data
    assert order_row(app, order_id)['deposit_refund_amount'] == 0
