"""Ticket ABI-341953166: customer-facing statement/facility presentation.

The Account Statement is retired as a customer-facing feature: the dedicated
route is gone (404) and the customer page no longer offers the action. The
Customer Statement is retained and must never count historical Account payment
rows as money received — an Account draw is an internal facility allocation, not
a receipt — and it reports the shared credit facility summary
(``app.services.credit_limits.facility_summary``). Used / available credit is
shown on the customer page, the new-order form and the order detail, always
beside the prepaid Customer Credit that is a separate pot.

The tests read the facility figures back from the shared service rather than
hard-coding them, so they stay valid as the parent retunes that definition.
"""
import os
import tempfile

import pytest

from app import create_app
from app.db import get_db, now
from app.services.customers import customer_statement
from app.services.credit_limits import facility_summary
from app.services.pdf_documents import customer_statement_pdf_bytes


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


def seed_customer(db, name, email, credit_allowed=0, credit_limit=0):
    db.execute(
        """INSERT INTO customers (customer_type, name, email, phone, marketing_opt_in, address_line1,
           country, custom_fields_json, balance_due, standard_discount_percent, credit_allowed,
           credit_limit, created_at)
           VALUES ('individual', ?, ?, '+270****0003', 0, '7 Facility Road', 'South Africa', '{}', 0, 0, ?, ?, ?)""",
        (name, email, credit_allowed, credit_limit, now()),
    )
    row = db.execute("SELECT id FROM customers WHERE email = ?", (email,)).fetchone()
    assert row is not None
    return row["id"]


def seed_order(db, customer_id, number, total, status="reserved", created_at=None):
    db.execute(
        """INSERT INTO orders (order_number, customer_id, booking_type, status, payment_status,
           subtotal, tax_total, deposit_total, total, due_total, notes, created_at)
           VALUES (?, ?, 'return', ?, 'payment_due', ?, 0, 0, ?, ?, '', ?)""",
        (number, customer_id, status, total, total, total, created_at or now()),
    )
    row = db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()
    assert row is not None
    return row["id"]


def seed_invoice(db, order_id, number, status="finalized", created_at=None):
    db.execute(
        """INSERT INTO documents (order_id, document_type, status, number, pdf_path, created_at)
           VALUES (?, 'invoice', ?, ?, '', ?)""",
        (order_id, status, number, created_at or now()),
    )


def seed_payment(db, order_id, amount, payment_date, method="cash", reference="", status="paid", deleted_at=""):
    if method == "account":
        db.execute("DROP TRIGGER account_payment_retired_insert")
    db.execute(
        """INSERT INTO payments (order_id, amount, method, reference, status, payment_date, deleted_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (order_id, amount, method, reference, status, payment_date, deleted_at, now()),
    )
    if method == "account":
        from app.services.credit_limits import install_credit_guards
        install_credit_guards(db)


def pdf_source(pdf_bytes):
    return pdf_bytes.decode("latin-1", "replace")


# --- Statement: historical Account rows are not receipts -------------------------


def test_customer_statement_excludes_historical_account_payments(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db, "Statement Facility", "stmt-fac@example.com",
                                    credit_allowed=1, credit_limit=1000)
        order_id = seed_order(db, customer_id, "ORD-166-1", 500.0)
        seed_invoice(db, order_id, "INV-166-1", created_at="2026-09-01T09:00:00")
        # An Account draw is an internal facility allocation, not money received.
        seed_payment(db, order_id, 300.0, "2026-09-02T10:00:00", method="account", reference="FACILITY-DRAW-XYZ")
        # A real receipt on the same order.
        seed_payment(db, order_id, 200.0, "2026-09-03T10:00:00", method="cash", reference="CASH-OK")
        db.commit()
        view = customer_statement(customer_id)
        expected_facility = facility_summary(customer_id)

    assert view["invoiced_total"] == 500.0
    # Only the cash receipt counts; the Account draw never does.
    assert view["paid_total"] == 200.0
    assert view["closing_balance"] == 300.0
    assert view["payment_count"] == 1
    assert [row["reference"] for row in view["payments"]] == ["CASH-OK"]
    assert all(row["type"] != "Account" for row in view["activity"])
    assert [row["detail"] for row in view["activity"] if row["type"] == "Payment"] == ["Cash · CASH-OK"]
    # The statement reports the shared facility summary, not a private copy.
    assert view["facility"] == expected_facility


def test_customer_statement_pdf_reports_the_facility_summary(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db, "Statement Facility Pdf", "stmt-fac-pdf@example.com",
                                    credit_allowed=1, credit_limit=600)
        order_id = seed_order(db, customer_id, "ORD-166-2", 200.0)
        seed_invoice(db, order_id, "INV-166-2", created_at="2026-09-07T09:00:00")
        seed_payment(db, order_id, 200.0, "2026-09-08T10:00:00", method="account", reference="FACILITY-DRAW-XYZ")
        seed_payment(db, order_id, 50.0, "2026-09-09T10:00:00", method="cash", reference="CASH-OK")
        db.commit()
        facility = facility_summary(customer_id)
        source = pdf_source(customer_statement_pdf_bytes(customer_statement(customer_id)))

    assert "Credit used" in source
    assert "Credit available" in source
    assert f"(R{facility['used']:.2f})" in source
    assert f"(R{facility['available']:.2f})" in source
    # The receipt is printed; the historical Account draw is not.
    assert "CASH-OK" in source
    assert "FACILITY-DRAW-XYZ" not in source


# --- Account Statement route + action retired ------------------------------------


def test_account_statement_route_and_action_are_retired(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db, "Retired Route", "retired-route@example.com",
                                    credit_allowed=1, credit_limit=500)
        seed_order(db, customer_id, "ORD-166-3", 100.0)
        db.commit()

    # The dedicated route is gone: the URL 404s, with or without a date range.
    assert client.get(f"/customers/{customer_id}/account-statement.pdf").status_code == 404
    assert client.get(f"/customers/{customer_id}/account-statement.pdf?date_from=2026-09-01&date_to=2026-09-30").status_code == 404

    detail = client.get(f"/customers/{customer_id}")
    assert detail.status_code == 200
    assert b"Account Statement" not in detail.data

    # The Customer Statement is retained.
    statement = client.get(f"/customers/{customer_id}/statement")
    assert statement.status_code == 200
    assert statement.mimetype == "application/pdf"
    assert statement.data.startswith(b"%PDF-")


# --- Used / available credit on the customer-facing screens ----------------------


def test_customer_detail_shows_facility_used_and_available(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db, "Detail Facility", "detail-fac@example.com",
                                    credit_allowed=1, credit_limit=800)
        order_id = seed_order(db, customer_id, "ORD-166-4", 300.0)
        seed_payment(db, order_id, 300.0, "2026-09-04T10:00:00", method="account")
        db.commit()
        facility = facility_summary(customer_id)

    body = client.get(f"/customers/{customer_id}").data.decode()
    assert "Credit used" in body
    assert "Credit available" in body
    assert f"R{facility['used']:.2f}" in body
    assert f"R{facility['available']:.2f}" in body
    # The prepaid Customer Credit presentation is preserved.
    assert "Customer credit" in body


def test_new_order_shows_facility_used_and_available(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db, "New Order Facility", "new-order-fac@example.com",
                                    credit_allowed=1, credit_limit=900)
        order_id = seed_order(db, customer_id, "ORD-166-5", 400.0)
        seed_payment(db, order_id, 400.0, "2026-09-05T10:00:00", method="account")
        db.commit()
        facility = facility_summary(customer_id)

    body = client.get(f"/orders/new?customer_id={customer_id}").data.decode()
    assert 'id="account-credit-used"' in body
    assert f'id="account-credit-used">R{facility["used"]:.2f}' in body
    assert f'id="account-credit-available">R{facility["available"]:.2f}' in body
    # The prepaid Customer Credit card is preserved.
    assert 'id="customer-credit-card"' in body


def test_order_detail_shows_facility_used_and_available(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db, "Order Detail Facility", "order-detail-fac@example.com",
                                    credit_allowed=1, credit_limit=700)
        order_id = seed_order(db, customer_id, "ORD-166-6", 250.0)
        seed_payment(db, order_id, 250.0, "2026-09-06T10:00:00", method="account")
        db.commit()
        facility = facility_summary(customer_id)

    response = client.get(f"/orders/{order_id}")
    assert response.status_code == 200
    body = response.data.decode()
    assert "Credit facility" in body
    assert f"R{facility['used']:.2f} used" in body
    assert f"R{facility['available']:.2f} available" in body
