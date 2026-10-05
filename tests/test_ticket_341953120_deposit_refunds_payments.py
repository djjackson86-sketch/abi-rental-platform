"""ABI-341953120: processed deposit refunds must show in Payments."""
import os
import tempfile

import pytest

from app import create_app
from app.db import get_db
from app.services.payments import list_payments, payment_count, payment_summary


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    application = create_app({
        "TESTING": True,
        "DATABASE": path,
        "SECRET_KEY": "test",
        "ADMIN_EMAIL": "admin@abi.local",
        "ADMIN_PASSWORD": "admin123",
    })
    yield application
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client):
    with client.application.app_context():
        row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
        assert row is not None
    return client.post("/login", data={"user_id": str(row["id"]), "password": "admin123"}, follow_redirects=True)


def seed_customer(db, name="Refund Ledger Customer"):
    db.execute("""INSERT INTO customers (customer_type, name, email, phone, marketing_opt_in,
        address_line1, address_line2, suburb, city, province, postal_code, country,
        custom_fields_json, balance_due, standard_discount_percent, client_verified,
        is_blocked, blocked_reason, created_by_user_id, branch_id, created_at)
        VALUES ('individual', ?, '', '', 0, '', '', '', '', '', '', 'South Africa', '{}', 0, 0, NULL, 0, '', 1, 1, ?)""",
        (name, "2026-09-28T08:00:00"))
    return db.execute("SELECT id FROM customers WHERE name = ?", (name,)).fetchone()["id"]


def seed_refunded_order(db, number="ORD-10444", *, method="card", processed_at="2026-09-29T14:30:00", branch_id=1):
    customer_id = seed_customer(db)
    db.execute("""INSERT INTO orders (order_number, customer_id, booking_type, collect_branch_id,
        return_branch_id, status, payment_status, start_at, end_at, subtotal, tax_total,
        deposit_total, deposit_applied_amount, deposit_refund_amount, deposit_process_method,
        deposit_processed_at, total, due_total, notes, created_at)
        VALUES (?, ?, 'return', ?, ?, 'returned', 'paid', '2026-09-25T09:00:00',
        '2026-09-28T17:00:00', 1000, 0, 750, 455, 295, ?, ?, 1750, 0, '', '2026-09-25T09:00:00')""",
        (number, customer_id, branch_id, branch_id, method, processed_at))
    order_id = db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()["id"]
    db.execute("""INSERT INTO payments (order_id, amount, method, reference, status, payment_date, created_at)
        VALUES (?, 1295, 'card', 'Rental paid', 'paid', '2026-09-25T10:00:00', '2026-09-25T10:00:00')""", (order_id,))
    db.execute("""INSERT INTO payments (order_id, amount, method, reference, status, payment_date, created_at)
        VALUES (?, 455, 'deposit_applied', 'Deposit Amount Utilised', 'paid', '', '2026-09-29T12:00:00')""", (order_id,))
    db.commit()
    return order_id


def test_deposit_refund_order_fields_appear_as_read_only_payment_row(app):
    with app.app_context():
        db = get_db()
        order_id = seed_refunded_order(db)

        rows = list_payments(date_from="2026-09-29", date_to="2026-09-29")
        refund_rows = [row for row in rows if row["synthetic_kind"] == "deposit_refund"]

        assert len(refund_rows) == 1
        refund = refund_rows[0]
        assert refund["id"] is None
        assert refund["order_id"] == order_id
        assert refund["order_number"] == "ORD-10444"
        assert refund["amount"] == -295
        assert refund["method"] == "card"
        assert refund["reference"] == "Refunded deposit"
        assert refund["payment_date"] == "2026-09-29T14:30:00"
        assert payment_count(date_from="2026-09-29", date_to="2026-09-29") == 2
        assert payment_count(include_archived=True, date_from="2026-09-29", date_to="2026-09-29") == 1
        assert not [row for row in list_payments(include_archived=True, date_from="2026-09-29", date_to="2026-09-29") if row["synthetic_kind"] == "deposit_refund"]

        summary = payment_summary(order_id)
        assert summary["paid_total"] == 1750
        assert summary["due_total"] == 0
        assert summary["payment_status"] == "paid"


def test_payments_page_shows_deposit_refund_and_filters_by_date_and_branch(app, client):
    login(client)
    with app.app_context():
        db = get_db()
        seed_refunded_order(db, "ORD-10444", branch_id=1)
        seed_refunded_order(db, "ORD-10452", method="eft", processed_at="2026-09-30T09:15:00", branch_id=2)

    page = client.get("/payments?date_from=2026-09-29&date_to=2026-09-29&branch=1")
    assert page.status_code == 200
    assert b"ORD-10444" in page.data
    assert b"-R295.00" in page.data
    assert b"Card" in page.data
    assert b"Refunded deposit" in page.data
    assert b"Deposit refund" in page.data
    assert b"ORD-10452" not in page.data
    assert b"/payments/None" not in page.data

    archived = client.get("/payments?status=archived&date_from=2026-09-29&date_to=2026-09-30")
    assert archived.status_code == 200
    assert b"Deposit refund" not in archived.data


def test_order_detail_still_shows_one_deposit_refund_row(app, client):
    login(client)
    with app.app_context():
        order_id = seed_refunded_order(get_db())

    detail = client.get(f"/orders/{order_id}")
    assert detail.status_code == 200
    assert detail.data.count(b"Refunded deposit") == 1
    assert detail.data.count(b"-R295.00") == 1
