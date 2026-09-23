"""Ticket ABI-341952990.

Four requested changes, all read-only UI/reporting:

1. The ISO "T" between a date and a time must never reach a human-visible
   surface — it becomes a single space everywhere (admin pages, public
   confirmation, Telegram, PDFs). Raw stored values, ``datetime-local`` inputs
   and JS data attributes deliberately keep their ISO form.
2. The main-profile dashboard gains a custom date range under the quick ranges.
3. Inventory gains a live rented-status column (picked up / reserved).
4. Inventory gains a revenue column whose header filter offers
   current month (default) / last month / all time.
"""

import os
import re
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.db import get_db  # noqa: E402
from app.services.products import live_rental_status, live_rental_status_label  # noqa: E402
from app.services.reports import product_revenue  # noqa: E402

TODAY = date.today()
THIS_MONTH_DAY = TODAY.replace(day=min(2, TODAY.day))
FIRST_OF_THIS_MONTH = TODAY.replace(day=1)
LAST_MONTH_END = FIRST_OF_THIS_MONTH - timedelta(days=1)
LAST_MONTH_DAY = LAST_MONTH_END.replace(day=1)
OLDER_DAY = (LAST_MONTH_END.replace(day=1) - timedelta(days=40))

#: A stored ISO date-time, i.e. exactly what the ticket says must never render.
ISO_T = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


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
        row = get_db().execute(
            "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
        ).fetchone()
        user_id = row["id"] if row else None
    return client.post("/login", data={"user_id": str(user_id), "password": "admin123"},
                       follow_redirects=True)


def visible_text(html):
    """Page text as a reader sees it.

    Script/style bodies and every tag are removed, so a ``datetime-local`` value
    or a ``data-start-at`` attribute (both of which legitimately carry a raw ISO
    "T" by design) can never be mistaken for user-visible output.
    """
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]*>", " ", text)
    return text


# --------------------------------------------------------------------------- #
# Seed
# --------------------------------------------------------------------------- #

def seed(app):
    """One branch, one customer, three products and a spread of orders."""
    ids = {}
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO customers (name, created_at) VALUES ('Status Customer', ?)",
                   (f"{THIS_MONTH_DAY.isoformat()}T09:00:00",))
        ids["customer"] = db.execute(
            "SELECT id FROM customers WHERE name = 'Status Customer'"
        ).fetchone()["id"]

        products = [
            ("Tracked Trailer", "rental", 250),
            ("Spare Wheel", "sale", 90),
            ("Repair Service", "service", 40),
        ]
        for name, ptype, price in products:
            db.execute(
                """INSERT INTO products (name, product_type, active, public_visible, price_amount,
                price_unit, quantity, tracking_method, branch_id, created_at)
                VALUES (?, ?, 1, 1, ?, 'day', 5, 'bulk', 1, ?)""",
                (name, ptype, price, f"{THIS_MONTH_DAY.isoformat()}T08:00:00"),
            )
            ids[name] = db.execute("SELECT id FROM products WHERE name = ?", (name,)).fetchone()["id"]

        # (suffix, status, payment_status, created_at, [(product, qty, line_total)])
        trailer = ids["Tracked Trailer"]
        wheel = ids["Spare Wheel"]
        service = ids["Repair Service"]
        rows = [
            ("STARTED", "started", "paid", THIS_MONTH_DAY, [(trailer, 2, 500.0)]),
            ("RESERVED", "reserved", "payment_due", THIS_MONTH_DAY, [(trailer, 1, 250.0), (wheel, 1, 90.0)]),
            ("RETURNED", "returned", "paid", THIS_MONTH_DAY, [(trailer, 4, 1000.0)]),
            ("DRAFT", "draft", "paid", THIS_MONTH_DAY, [(wheel, 3, 270.0)]),
            ("LASTMONTH", "started", "paid", LAST_MONTH_DAY, [(trailer, 1, 250.0), (service, 1, 40.0)]),
            ("OLDER", "returned", "paid", OLDER_DAY, [(wheel, 2, 180.0)]),
        ]
        for suffix, status, payment_status, when, lines in rows:
            stamp = f"{when.isoformat()}T09:00:00"
            total = sum(line[2] for line in lines)
            db.execute(
                """INSERT INTO orders (order_number, customer_id, booking_type, collect_branch_id,
                return_branch_id, status, payment_status, subtotal, tax_total, deposit_total, total,
                due_total, notes, start_at, end_at, created_at)
                VALUES (?, ?, 'return', 1, 1, ?, ?, ?, 0, 0, ?, ?, '', ?, ?, ?)""",
                (f"ORD-{suffix}", ids["customer"], status, payment_status, total, total,
                 0 if payment_status == "paid" else total, stamp, stamp, stamp),
            )
            order_id = db.execute("SELECT id FROM orders WHERE order_number = ?",
                                  (f"ORD-{suffix}",)).fetchone()["id"]
            for product_id, quantity, line_total in lines:
                db.execute(
                    """INSERT INTO order_items (order_id, product_id, custom_name, quantity,
                    unit_price, line_subtotal, line_tax, line_total, billing_mode)
                    VALUES (?, ?, '', ?, ?, ?, 0, ?, 'catalog')""",
                    (order_id, product_id, quantity, line_total, line_total, line_total),
                )
            ids[f"order_{suffix}"] = order_id
        db.commit()
    return ids


# --------------------------------------------------------------------------- #
# (1) No ISO "T" anywhere a human reads a date-time
# --------------------------------------------------------------------------- #

def test_no_iso_t_in_visible_text_on_dashboard_inventory_calendar_and_order(app, client):
    ids = seed(app)
    login(client)
    pages = [
        "/dashboard",
        "/inventory",
        "/calendar",
        f"/orders/{ids['order_STARTED']}",
    ]
    for path in pages:
        res = client.get(path)
        assert res.status_code == 200, f"{path} -> {res.status_code}"
        text = visible_text(res.get_data(as_text=True))
        assert not ISO_T.search(text), f"raw ISO date-time still rendered on {path}"


def test_dashboard_and_calendar_show_the_spaced_date_time(app, client):
    ids = seed(app)
    login(client)
    stamp = f"{THIS_MONTH_DAY.isoformat()} 09:00"
    for path in ("/dashboard", "/calendar"):
        res = client.get(path)
        text = visible_text(res.get_data(as_text=True))
        assert stamp in text, f"{path} should render '{stamp}'"


def test_public_confirmation_shows_a_spaced_date_time(app, client):
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO customers (name, created_at) VALUES ('Web Customer', ?)",
                   (f"{THIS_MONTH_DAY.isoformat()}T09:00:00",))
        customer = db.execute("SELECT id FROM customers WHERE name = 'Web Customer'").fetchone()["id"]
        db.execute(
            """INSERT INTO orders (order_number, customer_id, booking_type, collect_branch_id,
            return_branch_id, status, payment_status, subtotal, tax_total, deposit_total, total,
            due_total, notes, start_at, end_at, created_at)
            VALUES ('ORD-WEB', ?, 'return', 1, 1, 'draft', 'payment_due', 100, 0, 0, 100, 100, '',
                    ?, ?, ?)""",
            (customer, f"{THIS_MONTH_DAY.isoformat()}T09:00:00",
             f"{THIS_MONTH_DAY.isoformat()}T17:00:00", f"{THIS_MONTH_DAY.isoformat()}T09:00:00"),
        )
        order_id = db.execute("SELECT id FROM orders WHERE order_number = 'ORD-WEB'").fetchone()["id"]
        db.commit()
    res = client.get(f"/store/booking/{order_id}")
    assert res.status_code == 200
    text = visible_text(res.get_data(as_text=True))
    assert f"{THIS_MONTH_DAY.isoformat()} 09:00" in text
    assert not ISO_T.search(text)


def test_display_helper_uses_a_single_space(app):
    from app.services.timezone import display_local_datetime
    assert display_local_datetime("2026-09-19T08:30:00") == "2026-09-19 08:30"
    assert display_local_datetime("2026-09-19 08:30:00") == "2026-09-19 08:30"
    assert display_local_datetime("") == "—"
    assert display_local_datetime(None) == "—"
    # The template helper is registered app-wide, not per template.
    with app.test_request_context():
        assert app.jinja_env.globals["display_local_datetime"] is display_local_datetime


def test_datetime_local_inputs_and_js_data_attributes_keep_the_raw_t(app, client):
    """The other half of the rule: stored/input values must NOT be rewritten."""
    ids = seed(app)
    login(client)
    html = client.get(f"/orders/{ids['order_STARTED']}").get_data(as_text=True)
    assert 'type="datetime-local"' in html
    assert ISO_T.search(html), "datetime-local inputs must keep the stored ISO value"
    assert 'data-start-at="' in html
    # The extra-days arithmetic still splits on the raw "T".
    assert "split('T')" in html


# --------------------------------------------------------------------------- #
# (2) Dashboard custom date range
# --------------------------------------------------------------------------- #

def _metric(html, label):
    """Read one dashboard headline figure straight out of the rendered card."""
    match = re.search(re.escape(label) + r"</small><b>([\d\.]+)</b>", html)
    assert match, f"dashboard card '{label}' not found"
    return match.group(1)


def test_dashboard_defaults_to_this_month_and_honours_a_custom_range(app, client):
    seed(app)
    login(client)

    default = client.get("/dashboard").get_data(as_text=True)
    assert "Custom dates" in default
    assert 'name="start_date"' in default
    assert 'name="end_date"' in default
    # Default window = this month: 4 orders were created inside it.
    assert _metric(default, "Total orders") == "4"

    older = OLDER_DAY.isoformat()
    res = client.get(f"/dashboard?start_date={older}&end_date={older}")
    assert res.status_code == 200
    custom = res.get_data(as_text=True)
    assert _metric(custom, "Total orders") == "1", "a one-day range must narrow the order count"
    assert f'value="{older}"' in custom, "the submitted range must be echoed back into the form"


def test_dashboard_custom_range_keeps_the_branch_filter(app, client):
    seed(app)
    login(client)
    res = client.get(f"/dashboard?branch=1&start_date={OLDER_DAY.isoformat()}&end_date={OLDER_DAY.isoformat()}")
    html = res.get_data(as_text=True)
    assert res.status_code == 200
    assert 'name="branch" value="1"' in html


# --------------------------------------------------------------------------- #
# (3) Inventory live rented status
# --------------------------------------------------------------------------- #

def test_live_rental_status_counts_started_and_reserved_only(app):
    ids = seed(app)
    with app.app_context():
        live = live_rental_status([ids["Tracked Trailer"], ids["Spare Wheel"], ids["Repair Service"]])
    trailer = live[ids["Tracked Trailer"]]
    # Picked up = started: 2 this month + 1 last month (status, not the date,
    # decides whether a unit is still out).
    assert trailer == {"picked_up": 3, "reserved": 1}
    # The returned (4) and draft (3) lines are not live stock.
    assert live[ids["Spare Wheel"]] == {"picked_up": 0, "reserved": 1}
    assert live[ids["Repair Service"]] == {"picked_up": 1, "reserved": 0}


def test_live_status_label_wording():
    assert live_rental_status_label({"picked_up": 2, "reserved": 1}) == "Picked up 2 · Reserved 1"
    assert live_rental_status_label({"picked_up": 0, "reserved": 3}) == "Reserved 3"
    assert live_rental_status_label({"picked_up": 1, "reserved": 0}) == "Picked up 1"
    assert live_rental_status_label(None) == "—"
    assert live_rental_status_label({"picked_up": 0, "reserved": 0}) == "—"


def test_inventory_renders_the_live_status_column(app, client):
    ids = seed(app)
    login(client)
    html = client.get("/inventory").get_data(as_text=True)
    assert "<th>Live status</th>" in html
    assert "Picked up 3 · Reserved 1" in html
    # Group rows must span the widened table.
    assert 'colspan="13"' in html


# --------------------------------------------------------------------------- #
# (4) Inventory revenue column + period filter
# --------------------------------------------------------------------------- #

def test_product_revenue_matches_the_reports_rule(app):
    ids = seed(app)
    trailer = ids["Tracked Trailer"]
    with app.app_context():
        this_month = product_revenue(
            [trailer], start_date=FIRST_OF_THIS_MONTH.isoformat(), end_date=TODAY.isoformat()
        )
        all_time = product_revenue([trailer])
    # Paid + real order stage only: this month's started 500 + returned 1000,
    # plus last month's started 250 = 1750 all time. The unpaid reserved 250 and
    # the draft line never count.
    assert this_month[trailer] == 1500.0
    assert all_time[trailer] == 1750.0


def test_product_revenue_window_choices(app):
    ids = seed(app)
    trailer = ids["Tracked Trailer"]
    wheel = ids["Spare Wheel"]
    service = ids["Repair Service"]
    with app.app_context():
        default_window = product_revenue(
            [trailer, wheel, service], start_date=FIRST_OF_THIS_MONTH.isoformat(), end_date=TODAY.isoformat()
        )
        last_month = product_revenue(
            [trailer, wheel, service], start_date=LAST_MONTH_DAY.isoformat(), end_date=LAST_MONTH_END.isoformat()
        )
        all_time = product_revenue([trailer, wheel, service])
    assert default_window[trailer] == 1500.0
    assert default_window[wheel] == 0.0  # its only current-month line is a draft
    assert default_window.get(service, 0.0) == 0.0
    assert last_month[trailer] == 250.0
    assert last_month[service] == 40.0  # services use the same line-revenue rule as stock products
    assert all_time[trailer] == 1750.0
    assert all_time[wheel] == 180.0  # the older returned order
    assert all_time[service] == 40.0


def test_inventory_revenue_column_defaults_to_current_month(app, client):
    ids = seed(app)
    login(client)
    html = client.get("/inventory").get_data(as_text=True)
    assert 'name="revenue_range"' in html
    assert '<option value="current_month" selected>' in html
    assert ">This month</option>" in html
    assert 'value="last_month"' in html and ">Last month</option>" in html
    assert 'value="all_time"' in html and ">All time</option>" in html
    # Rendered figure is the current-month recognised revenue for the trailer.
    cell = re.search(r"<td><b>R([\d,\.]+)</b></td>", html)
    assert cell, "the revenue column should render a money amount"
    assert "R1500.00" in html


def test_inventory_revenue_filter_switches_the_window(app, client):
    seed(app)
    login(client)
    this_month = client.get("/inventory?revenue_range=current_month").get_data(as_text=True)
    last_month = client.get("/inventory?revenue_range=last_month").get_data(as_text=True)
    all_time = client.get("/inventory?revenue_range=all_time").get_data(as_text=True)
    assert "R1500.00" in this_month
    assert '<option value="current_month" selected>' in this_month
    assert '<option value="last_month" selected>' in last_month
    assert '<option value="all_time" selected>' in all_time
    assert "R250.00" in last_month
    # A service row with recognised revenue must show its money value, not a dash.
    service_row = re.search(r"<tr>.*?<strong>Repair Service</strong>.*?</tr>", last_month, re.S)
    assert service_row, "the service product row should render"
    assert "R40.00" in service_row.group(0)
    # All time includes both months for the trailer.
    assert "R1750.00" in all_time


def test_inventory_revenue_range_survives_the_filter_form_and_links(app, client):
    seed(app)
    login(client)
    html = client.get("/inventory?revenue_range=last_month").get_data(as_text=True)
    # The search form carries it so typing a search does not reset the column.
    assert '<input type="hidden" name="revenue_range" value="last_month">' in html
    # The rail's clear links carry it too.
    assert "revenue_range=last_month" in html


def test_inventory_unknown_revenue_range_falls_back_to_the_default(app, client):
    seed(app)
    login(client)
    html = client.get("/inventory?revenue_range=nonsense").get_data(as_text=True)
    assert '<option value="current_month" selected>' in html
    assert "R1500.00" in html
