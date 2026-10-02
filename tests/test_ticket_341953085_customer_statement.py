"""Ticket ABI-341953085: the customer statement (list of finalised invoices and
payments, as a PDF, with an optional inclusive date range).

Guards the two ways a statement can lie:
  * invoiced money that was never invoiced (a draft proforma leaking in, or a
    cancelled booking's lines), and
  * payments that are no longer live (archived / deleted rows).

It also pins the range arithmetic — opening balance + invoiced in range - paid in
range = closing balance, boundaries inclusive — and the PDF itself: every row of
a long statement reaches the page, and the company + customer details are on it.
"""
import os
import tempfile

import pytest

from app import create_app
from app.db import get_db, now
from app.services.customers import customer_statement
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


def seed_customer(db, name="Statement Customer", email="statement@example.com"):
    db.execute(
        """INSERT INTO customers (customer_type, name, email, phone, marketing_opt_in, address_line1,
           country, custom_fields_json, balance_due, standard_discount_percent, created_at)
           VALUES ('individual', ?, ?, '+2700000001', 0, '12 Statement Street', 'South Africa', '{}', 0, 0, ?)""",
        (name, email, now()),
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
    cur = db.execute(
        """INSERT INTO documents (order_id, document_type, status, number, pdf_path, created_at)
           VALUES (?, 'invoice', ?, ?, '', ?)""",
        (order_id, status, number, created_at or now()),
    )
    return cur.lastrowid


def seed_payment(db, order_id, amount, payment_date, status="paid", deleted_at="", method="cash", reference=""):
    db.execute(
        """INSERT INTO payments (order_id, amount, method, reference, status, payment_date, deleted_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (order_id, amount, method, reference, status, payment_date, deleted_at, now()),
    )


def seed_ticket_ledger(db):
    """One customer, two finalised invoices and two live payments."""
    customer_id = seed_customer(db)
    first = seed_order(db, customer_id, "ORD-ST-1", 1150.0)
    second = seed_order(db, customer_id, "ORD-ST-2", 575.0)
    seed_invoice(db, first, "INV-90001", created_at="2026-08-10T09:00:00")
    seed_invoice(db, second, "INV-90002", created_at="2026-09-05T09:00:00")
    seed_payment(db, first, 1150.0, "2026-08-12T10:00:00", reference="EFT ref 88")
    seed_payment(db, second, 300.0, "2026-09-06T10:00:00", method="eft")
    return customer_id


def pdf_source(pdf_bytes):
    """The page streams are uncompressed latin-1 text commands, so the drawn text
    can be read straight off the file (no PDF library in this project)."""
    return pdf_bytes.decode("latin-1", "replace")


def test_statement_lists_finalised_invoices_and_live_payments_and_reconciles(client, app):
    login(client)
    with app.app_context():
        customer_id = seed_ticket_ledger(get_db())
        get_db().commit()
        view = customer_statement(customer_id)

    assert view["customer_id"] == customer_id
    assert view["invoice_count"] == 2
    assert view["payment_count"] == 2
    assert [row["number"] for row in view["invoices"]] == ["INV-90001", "INV-90002"]
    assert [row["order_number"] for row in view["invoices"]] == ["ORD-ST-1", "ORD-ST-2"]
    assert [row["date"] for row in view["invoices"]] == ["2026-08-10", "2026-09-05"]
    assert view["invoices"][0]["amount_display"] == "R1150.00"
    # Payments carry their own reference and payment date.
    assert [row["date"] for row in view["payments"]] == ["2026-08-12", "2026-09-06"]
    assert view["payments"][0]["reference"] == "EFT ref 88"
    assert view["payments"][0]["method"] == "Cash"
    assert view["payments"][1]["method"] == "Eft"
    assert view["payments"][1]["amount_display"] == "R300.00"
    # Full history: nothing before the range, and the closing balance is the
    # customer's live outstanding position on the ledger.
    assert view["invoiced_total"] == 1725.0
    assert view["paid_total"] == 1450.0
    assert view["opening_balance"] == 0.0
    assert view["closing_balance"] == 275.0
    assert view["outstanding_balance"] == 275.0
    assert view["opening_balance_display"] == "R0.00"
    assert view["closing_balance_display"] == "R275.00"
    assert view["period_label"] == "Full history"


def test_statement_excludes_draft_invoices_cancelled_orders_and_archived_payments(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db)
        live = seed_order(db, customer_id, "ORD-ST-LIVE", 500.0)
        seed_invoice(db, live, "INV-91001", created_at="2026-08-01T09:00:00")
        seed_payment(db, live, 200.0, "2026-08-02T10:00:00")
        # A proforma (draft) invoice is not invoiced money.
        draft = seed_order(db, customer_id, "ORD-ST-DRAFT", 999.0, status="draft")
        seed_invoice(db, draft, "", status="draft", created_at="2026-08-03T09:00:00")
        # A cancelled booking's finalised invoice must not inflate the statement.
        cancelled = seed_order(db, customer_id, "ORD-ST-CANCEL", 999.0, status="canceled")
        seed_invoice(db, cancelled, "INV-91002", created_at="2026-08-04T09:00:00")
        # Archived (and soft-deleted) payments are not live money.
        seed_payment(db, live, 300.0, "2026-08-03T10:00:00", status="archived", deleted_at="2026-08-04T00:00:00")
        seed_payment(db, live, 400.0, "2026-08-03T11:00:00", status="paid", deleted_at="2026-08-04T00:00:00")
        db.commit()
        view = customer_statement(customer_id)

    assert view["invoice_count"] == 1
    assert [row["number"] for row in view["invoices"]] == ["INV-91001"]
    assert view["payment_count"] == 1
    assert view["invoiced_total"] == 500.0
    assert view["paid_total"] == 200.0
    assert view["closing_balance"] == 300.0


def test_statement_date_range_is_inclusive_and_carries_the_opening_balance(client, app):
    login(client)
    with app.app_context():
        customer_id = seed_ticket_ledger(get_db())
        get_db().commit()
        # The invoice dated exactly on date_to is inside the range.
        included = customer_statement(customer_id, date_from="2026-08-01", date_to="2026-08-10")
        # The payment dated exactly on date_from is inside the range.
        payment_only = customer_statement(customer_id, date_from="2026-08-12", date_to="2026-08-12")
        # Everything before date_from becomes the opening balance.
        open_ended = customer_statement(customer_id, date_from="2026-08-12")
        # A reversed range is a typo, not an empty statement.
        reversed_range = customer_statement(customer_id, date_from="2026-09-05", date_to="2026-08-01")
        # Junk dates are ignored rather than raising.
        junk = customer_statement(customer_id, date_from="not-a-date", date_to="")

    assert included["period_label"] == "2026-08-01 to 2026-08-10"
    assert [row["number"] for row in included["invoices"]] == ["INV-90001"]
    assert included["payment_count"] == 0
    assert (included["opening_balance"], included["closing_balance"]) == (0.0, 1150.0)

    assert payment_only["invoice_count"] == 0
    assert payment_only["opening_balance"] == 1150.0
    assert payment_only["paid_total"] == 1150.0
    assert payment_only["closing_balance"] == 0.0

    assert open_ended["opening_balance"] == 1150.0
    assert open_ended["invoiced_total"] == 575.0
    assert open_ended["paid_total"] == 1450.0
    # Opening + invoiced - paid lands on the same outstanding figure as full history.
    assert open_ended["closing_balance"] == 275.0
    assert open_ended["period_label"] == "From 2026-08-12"

    assert reversed_range["period_label"] == "2026-08-01 to 2026-09-05"
    # 01-08..05-09 inclusive: both invoices, only the 12-08 payment.
    assert reversed_range["closing_balance"] == 575.0
    assert junk["period_label"] == "Full history"
    assert junk["date_from"] == "" and junk["date_to"] == ""


def test_statement_route_sends_a_pdf_with_no_cache_headers(client, app):
    login(client)
    with app.app_context():
        customer_id = seed_ticket_ledger(get_db())
        get_db().commit()

    response = client.get(f"/customers/{customer_id}/statement")
    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert response.data.startswith(b"%PDF-")
    assert response.headers["Content-Disposition"] == f"inline; filename=CUSTOMER-STATEMENT-{customer_id}.pdf"
    assert "no-store" in response.headers["Cache-Control"]
    assert response.headers["Pragma"] == "no-cache"

    ranged = client.get(f"/customers/{customer_id}/statement?date_from=2026-08-01&date_to=2026-08-31")
    assert ranged.status_code == 200
    assert ranged.data.startswith(b"%PDF-")

    missing = client.get("/customers/999999/statement")
    assert missing.status_code == 302


def test_statement_route_requires_login(client, app):
    with app.app_context():
        customer_id = seed_customer(get_db())
        get_db().commit()
    response = client.get(f"/customers/{customer_id}/statement")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_statement_pdf_carries_the_company_and_customer_details(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_ticket_ledger(db)
        db.commit()
        company_name = db.execute("SELECT company_name FROM company_settings WHERE id = 1").fetchone()["company_name"]
        view = customer_statement(customer_id, generated_at="2026-10-02T08:30:00")
        # The PDF reads the static logo + company settings, so it needs the app context.
        source = pdf_source(customer_statement_pdf_bytes(view))
    assert "CUSTOMER STATEMENT" in source
    assert f"({company_name})" in source          # issuer block, same source as the invoice header
    assert f"Customer no: {customer_id}" in source
    assert "Period: Full history" in source
    assert "Generated: 2026-10-02 08:30" in source
    assert "Bill To:" in source
    assert "(Statement Customer)" in source
    assert "(12 Statement Street)" in source
    assert "INVOICES" in source and "PAYMENTS" in source
    assert "INV-90001" in source and "ORD-ST-1" in source
    assert "EFT ref 88" in source
    assert "Balance outstanding" in source
    assert "(R275.00)" in source


def test_statement_pdf_paginates_and_keeps_every_invoice_and_payment(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_customer(db, name="Long Ledger Customer", email="long@example.com")
        expected_invoices = []
        expected_payments = []
        for index in range(60):
            order_id = seed_order(db, customer_id, f"ORD-LONG-{index:03d}", 100.0 + index,
                                  created_at=f"2026-0{1 + (index // 28)}-{1 + (index % 28):02d}T09:00:00")
            number = f"INV-92{index:03d}"
            seed_invoice(db, order_id, number, created_at=f"2026-0{1 + (index // 28)}-{1 + (index % 28):02d}T09:00:00")
            expected_invoices.append(number)
        for index in range(45):
            order_row = db.execute("SELECT id FROM orders WHERE order_number = ?", (f"ORD-LONG-{index % 60:03d}",)).fetchone()
            reference = f"PAY-REF-{index:03d}"
            seed_payment(db, order_row["id"], 25.0 + index, f"2026-0{1 + (index // 28)}-{1 + (index % 28):02d}T11:00:00",
                         method="eft", reference=reference)
            expected_payments.append(reference)
        db.commit()
        view = customer_statement(customer_id)
        assert view["invoice_count"] == 60
        assert view["payment_count"] == 45
        pdf_bytes = customer_statement_pdf_bytes(view)
    source = pdf_source(pdf_bytes)
    for number in expected_invoices:
        assert f"({number})" in source, f"{number} never reached the statement"
    for reference in expected_payments:
        assert f"({reference})" in source, f"{reference} never reached the statement"
    # The statement runs over more than one page and says so, continuing both
    # sections with repeated headings rather than dropping rows. (Inner
    # parentheses are escaped in the PDF text command, hence the backslashes.)
    assert "Page 1 of" in source
    assert "Page 2 of" in source
    assert "INVOICES \\(CONTINUED\\)" in source
    assert pdf_bytes.count(b"/Type /Page ") >= 2


def test_customer_detail_page_offers_the_statement_button_with_a_date_range(client, app):
    login(client)
    with app.app_context():
        customer_id = seed_ticket_ledger(get_db())
        get_db().commit()

    body = client.get(f"/customers/{customer_id}").data.decode()
    assert "Customer Statement" in body
    assert f'action="/customers/{customer_id}/statement"' in body
    assert 'method="get"' in body
    assert 'name="date_from"' in body
    assert 'name="date_to"' in body
    assert 'target="_blank"' in body
