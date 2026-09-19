import os
import tempfile

import pytest

from app import create_app
from app.db import get_db
from app.services.orders import order_counts
from app.services.reports import (
    customer_summary,
    dashboard_period_metrics,
    orders_by_status,
    product_performance,
    summary_metrics,
)

DAY = "2026-09-15"


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


def _seed_revenue_orders(app):
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO customers (name, created_at) VALUES ('Revenue Customer', ?)", (DAY,))
        customer_id = db.execute("SELECT id FROM customers WHERE name = 'Revenue Customer'").fetchone()["id"]
        db.execute(
            """INSERT INTO products (name, product_type, active, public_visible, price_amount,
            price_unit, quantity, tracking_method, created_at)
            VALUES ('Revenue Trailer', 'rental', 1, 1, 100, 'day', 1, 'bulk', ?)""",
            (DAY,),
        )
        product_id = db.execute("SELECT id FROM products WHERE name = 'Revenue Trailer'").fetchone()["id"]

        rows = [
            ("SR-UNPAID", "sales_repairs", "payment_due", 100),
            ("SR-PAID", "sales_repairs", "paid", 200),
            ("DRAFT-UNPAID", "draft", "payment_due", 300),
            ("DRAFT-PAID", "draft", "paid", 400),
            ("RESERVED-PAID", "reserved", "paid", 500),
            ("STARTED-PAID", "started", "paid", 600),
            ("RETURNED-PAID", "returned", "paid", 700),
            ("RESERVED-UNPAID", "reserved", "payment_due", 800),
            ("STARTED-UNPAID", "started", "payment_due", 900),
            ("RETURNED-UNPAID", "returned", "payment_due", 1000),
            ("CANCELED-PAID", "canceled", "paid", 1100),
            ("ARCHIVED-PAID", "archived", "paid", 1200),
        ]
        for number, status, payment_status, total in rows:
            db.execute(
                """INSERT INTO orders (order_number, customer_id, booking_type, collect_branch_id,
                return_branch_id, status, payment_status, subtotal, tax_total, deposit_total, total,
                due_total, notes, start_at, end_at, created_at)
                VALUES (?, ?, 'return', 1, 1, ?, ?, ?, 0, 0, ?, ?, '', ?, ?, ?)""",
                (number, customer_id, status, payment_status, total, total, 0 if payment_status == "paid" else total, f"{DAY}T09:00:00", f"{DAY}T17:00:00", f"{DAY}T09:00:00"),
            )
            order_id = db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()["id"]
            db.execute(
                """INSERT INTO order_items (order_id, product_id, custom_name, quantity, unit_price,
                line_subtotal, line_tax, line_total, billing_mode)
                VALUES (?, ?, '', 1, ?, ?, 0, ?, 'catalog')""",
                (order_id, product_id, total, total, total),
            )
            if payment_status == "paid":
                # Ticket ABI-341952993: the money each paid order really took.
                db.execute(
                    """INSERT INTO payments (order_id, amount, method, reference, status, payment_date,
                    deleted_at, created_at) VALUES (?, ?, 'cash', '', 'paid', ?, '', ?)""",
                    (order_id, total, DAY, f"{DAY}T10:00:00"),
                )
        db.commit()


#: Money RECEIVED per paid order — the figure the Orders page Revenue card and
#: the dashboard Gross revenue card report (ticket ABI-341952993).
RECEIVED = {
    "SR-PAID": 200,
    "DRAFT-PAID": 400,
    "RESERVED-PAID": 500,
    "STARTED-PAID": 600,
    "RETURNED-PAID": 700,
    "CANCELED-PAID": 1100,
    "ARCHIVED-PAID": 1200,
}


def test_reports_only_recognize_paid_real_order_stages_as_revenue(app):
    _seed_revenue_orders(app)

    expected_revenue = 200 + 500 + 600 + 700
    with app.app_context():
        assert summary_metrics(DAY, DAY)["revenue"] == expected_revenue

        by_status = {row["status"]: row for row in orders_by_status(DAY, DAY)}
        assert by_status["sales_repairs"]["count"] == 2
        assert by_status["sales_repairs"]["total"] == 200
        assert by_status["draft"]["count"] == 2
        assert by_status["draft"]["total"] == 0
        assert by_status["reserved"]["total"] == 500
        assert by_status["started"]["total"] == 600
        assert by_status["returned"]["total"] == 700
        assert by_status["canceled"]["total"] == 0
        assert by_status["archived"]["total"] == 0

        product_rows = product_performance(DAY, DAY)
        assert product_rows[0]["product_name"] == "Revenue Trailer"
        assert product_rows[0]["total"] == expected_revenue

        customer_rows = customer_summary(DAY, DAY)
        assert customer_rows[0]["customer_name"] == "Revenue Customer"
        assert customer_rows[0]["total"] == expected_revenue


def test_the_dashboard_gross_revenue_card_is_money_received(app):
    """Ticket ABI-341952993(2): the Gross revenue card reports money RECEIVED in
    the window — the same basis as "Revenue for the day" — not the booked value
    of the orders raised inside it. The two are deliberately different numbers
    and the card says so ("Received · <range>")."""
    _seed_revenue_orders(app)

    received = sum(RECEIVED.values())        # 4700 — every paid payment, by date
    booked = 200 + 500 + 600 + 700           # 1800 — the recognised basis
    with app.app_context():
        assert dashboard_period_metrics(DAY, DAY)["revenue"] == received
        assert dashboard_period_metrics(DAY, DAY)["revenue"] != booked
        # A window that excludes the day holds none of it.
        assert dashboard_period_metrics("2026-09-20", "2026-09-21")["revenue"] == 0


def test_orders_page_metrics_report_money_received(app):
    """Ticket ABI-341952993(2): the Orders page Revenue card uses the same
    received basis, restricted to the orders the page is showing."""
    _seed_revenue_orders(app)

    received = sum(RECEIVED.values())
    with app.app_context():
        counts = order_counts()
        assert counts["total"] == 12
        assert counts["revenue"] == received
        # The due card still excludes draft/Sales-Repairs quotes that have not
        # been accepted, while keeping active reserved/started/returned balances.
        assert counts["due"] == 800 + 900 + 1000

        sales_repairs = order_counts(status="sales_repairs")
        assert sales_repairs["total"] == 2
        assert sales_repairs["revenue"] == RECEIVED["SR-PAID"]

        # A paid order that was reverted to draft keeps its money (the client
        # asked for exactly that in ticket item 1), so the draft folder still
        # shows what was received against it.
        drafts = order_counts(status="draft")
        assert drafts["total"] == 2
        assert drafts["revenue"] == RECEIVED["DRAFT-PAID"]

        unpaid = order_counts(payment_status="payment_due")
        assert unpaid["total"] == 5
        assert unpaid["revenue"] == 0

        paid = order_counts(payment_status="paid")
        assert paid["total"] == 7
        assert paid["revenue"] == received


def test_orders_page_revenue_keeps_existing_branch_and_date_filters(app):
    _seed_revenue_orders(app)

    received = sum(RECEIVED.values())
    with app.app_context():
        db = get_db()
        db.execute("UPDATE orders SET collect_branch_id = 2, return_branch_id = 2 WHERE order_number = 'RETURNED-PAID'")
        db.execute("UPDATE orders SET start_at = '2026-09-16T09:00:00' WHERE order_number = 'STARTED-PAID'")
        db.commit()

        # Branch: money received on the orders collected at that depot. Date: the
        # window narrows the order set (pickup date) AND the payments counted.
        assert order_counts(branch_id=1)["revenue"] == received - RECEIVED["RETURNED-PAID"]
        assert order_counts(branch_id=2)["revenue"] == RECEIVED["RETURNED-PAID"]
        assert order_counts(start_date=DAY, end_date=DAY)["revenue"] == received - RECEIVED["STARTED-PAID"]
        assert order_counts(branch_id=1, start_date=DAY, end_date=DAY)["revenue"] == (
            received - RECEIVED["RETURNED-PAID"] - RECEIVED["STARTED-PAID"]
        )
