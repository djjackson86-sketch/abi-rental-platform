"""Programme phase 12 (feature C / §C2) — the multi-trailer public booking flow.

One public booking → **one** order with many lines, through the app's existing availability and
customer-blocking checks, the §B2 dedupe flow and the §P1 consent block.

The acceptance list from ``docs/plans/2026-09-23-public-booking-and-store-categories.md`` §C2, one
test per line where it maps cleanly:

* two trailers in one booking produce **one** order with **two** items and the exact money maths
  (subtotal / VAT / refundable deposit / total from a seeded price + VAT rate);
* overbooking one trailer refuses the **whole** booking with a clear message and writes nothing;
* a maintenance-flagged or ``public_visible=0`` trailer cannot be selected even by a crafted POST;
* a duplicate customer phone **links** instead of creating a second row;
* a blocked customer's booking is refused;
* an empty selection is refused;
* the confirmation page lists both lines;
* the customer page shows the order;
* an unticked consent box refuses the whole booking and writes nothing, while a ticked one stores a
  ``consent_records`` row tied to the new customer and the notice version;
* the D11 gate: while the notice is unfinished the whole path is shut, exactly like the portal form.

Fixtures are synthetic throughout: no real licence-disc or client identifier appears in this file.
"""

import os
import tempfile

import pytest

from app import create_app
from app.db import get_db, now
from app.services import branches as branches_service
from app.services import consent, portal_intake
from app.services.customers import get_customer
from app.services.orders import (
    PUBLIC_BOOKING_NOTE,
    PUBLIC_SOURCE_SYSTEM,
    get_order,
    order_items,
)


def build_app(database_path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": database_path,
            "SECRET_KEY": "test",
            "ADMIN_EMAIL": "admin@abi.local",
            "ADMIN_PASSWORD": "admin123",
        }
    )


@pytest.fixture()
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    os.unlink(path)


@pytest.fixture()
def app(db_path):
    portal_intake.reset_rate_limits()
    return build_app(db_path)


@pytest.fixture()
def client(app, monkeypatch):
    # The shipped notice still carries Sano's five open facts, which (correctly) keeps the public
    # write path shut — so the `client` fixture opens the gate explicitly, and the closed behaviour
    # is proven separately against the real document.
    monkeypatch.setattr(portal_intake, "registration_is_open", lambda: True)
    return app.test_client()


def branch(app):
    with app.app_context():
        return branches_service.create_branch({"name": "Midrand Depot", "active": 1})


def tax_profile(app, rate=15.0):
    with app.app_context():
        cur = get_db().execute(
            "INSERT INTO tax_profiles (name, rate, is_default, active, created_at) VALUES (?, ?, 0, 1, ?)",
            (f"VAT {rate:g}%", rate, now()),
        )
        get_db().commit()
        return cur.lastrowid


def make_product(app, name, price=100.0, deposit=0.0, quantity=1, tax_profile_id=None, **extra):
    with app.app_context():
        cur = get_db().execute(
            "INSERT INTO products (name, product_type, description, sku, active, public_visible, "
            "price_amount, price_unit, security_deposit, tax_profile_id, product_group_id, quantity, "
            "tracking_method, under_maintenance, created_at) "
            "VALUES (?, 'rental', '', ?, 1, ?, ?, 'day', ?, ?, NULL, ?, 'bulk', ?, ?)",
            (
                name,
                extra.get("sku", ""),
                extra.get("public_visible", 1),
                price,
                deposit,
                tax_profile_id,
                quantity,
                extra.get("under_maintenance", 0),
                now(),
            ),
        )
        get_db().commit()
        return cur.lastrowid


def booking_payload(**overrides):
    data = {
        "name": "Pieter van Wyk",
        "phone": "083 555 1234",
        "email": "pieter@example.co.za",
        "popia_consent": "1",
        "start_date": "2026-10-01",
        "start_time": "09:00",
        "end_date": "2026-10-03",
        "end_time": "09:00",
        "product_id": [],
        "quantity": [],
    }
    data.update(overrides)
    return data


def select(data, product_id, quantity):
    data = dict(data)
    data["product_id"] = list(data["product_id"]) + [str(product_id)]
    data["quantity"] = list(data["quantity"]) + [str(quantity)]
    return data


def order_count(app):
    with app.app_context():
        return get_db().execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]


def customer_count(app):
    with app.app_context():
        return get_db().execute("SELECT COUNT(*) AS c FROM customers").fetchone()["c"]


def make_customer(app, **values):
    from app.services.customers import create_customer

    with app.app_context():
        payload = {"customer_type": "individual", "name": "Charmaine Mokoena", "phone": "0821234567"}
        payload.update(values)
        return create_customer(payload)


def make_reserved_order(app, product_id, quantity, start, end, branch_id=None):
    """A live ``reserved`` order holding stock in the window, for the overbooking refusal."""
    with app.app_context():
        db = get_db()
        cur = db.execute(
            "INSERT INTO orders (order_number, customer_id, booking_type, collect_branch_id, return_branch_id, status, payment_status, start_at, end_at, subtotal, tax_total, deposit_total, total, due_total, created_at) "
            "VALUES (?, NULL, 'return', ?, ?, 'reserved', 'payment_due', ?, ?, 0, 0, 0, 0, 0, ?)",
            (f"ORD-RES-{product_id}", branch_id, branch_id, start, end, now()),
        )
        order_id = cur.lastrowid
        db.execute(
            "INSERT INTO order_items (order_id, product_id, quantity, unit_price, line_subtotal, line_tax, line_total, billing_mode) "
            "VALUES (?, ?, ?, 0, 0, 0, 0, 'catalog')",
            (order_id, product_id, quantity),
        )
        db.commit()
        return order_id


# --- two trailers, one order, exact money maths ------------------------------------------------


def test_two_trailers_produce_one_order_with_two_items_and_exact_money(app, client):
    tax = tax_profile(app, 15.0)
    a = make_product(app, "Cage Trailer", price=100.0, deposit=50.0, quantity=3, tax_profile_id=tax)
    b = make_product(app, "Flatbed Trailer", price=250.0, deposit=120.0, quantity=2, tax_profile_id=tax)
    branch_id = branch(app)

    response = client.post(
        "/store/book",
        data=select(select(booking_payload(collect_branch_id=str(branch_id)), a, 1), b, 1),
    )
    assert response.status_code == 302
    assert "/store/booking/" in response.headers["Location"]

    with app.app_context():
        order = get_order(1)
        items = order_items(1)
    assert order is not None
    # 2026-10-01 09:00 -> 2026-10-03 09:00 = 2 rental days.
    assert round(float(order["subtotal"]), 2) == 700.00  # 100*2 + 250*2
    assert round(float(order["tax_total"]), 2) == 105.00   # 15% of 700
    assert round(float(order["deposit_total"]), 2) == 170.00  # 50 + 120
    assert round(float(order["total"]), 2) == 975.00       # 700 + 105 + 170
    assert order["status"] == "draft"
    assert order["booking_type"] == "return"
    assert order["source_system"] == PUBLIC_SOURCE_SYSTEM
    assert order["source_id"].startswith("BOOK-")
    assert PUBLIC_BOOKING_NOTE in (order["notes"] or "")
    assert len(items) == 2
    by_name = {item["product_name"]: item for item in items}
    assert round(float(by_name["Cage Trailer"]["line_total"]), 2) == 230.00
    assert round(float(by_name["Flatbed Trailer"]["line_total"]), 2) == 575.00


def test_duplicate_product_lines_are_deduplicated_by_quantity(app, client):
    tax = tax_profile(app, 15.0)
    a = make_product(app, "Cage Trailer", price=100.0, deposit=50.0, quantity=3, tax_profile_id=tax)
    branch_id = branch(app)
    response = client.post(
        "/store/book",
        data=select(select(booking_payload(collect_branch_id=str(branch_id)), a, 1), a, 1),
    )
    assert response.status_code == 302
    with app.app_context():
        items = order_items(1)
    assert len(items) == 1, "a repeated product merges into one line"
    assert items[0]["quantity"] == 2
    # 2 units * R100 * 2 days = 400 subtotal.
    assert round(float(items[0]["line_subtotal"]), 2) == 400.00


# --- refusals write nothing ----------------------------------------------------------------------


def test_overbooking_one_trailer_refuses_the_whole_booking(app, client):
    tax = tax_profile(app, 15.0)
    a = make_product(app, "Sole Trailer", price=100.0, deposit=50.0, quantity=1, tax_profile_id=tax)
    b = make_product(app, "Free Trailer", price=250.0, deposit=120.0, quantity=2, tax_profile_id=tax)
    branch_id = branch(app)
    make_reserved_order(app, a, 1, "2026-10-01T09:00:00", "2026-10-05T09:00:00", branch_id)

    response = client.post(
        "/store/book",
        data=select(select(booking_payload(collect_branch_id=str(branch_id)), a, 1), b, 1),
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 400
    assert "Sole Trailer" in body
    assert "available" in body
    assert order_count(app) == 1, "only the pre-existing reserved order remains; the booking wrote nothing"
    assert customer_count(app) == 0


def test_a_maintenance_flagged_trailer_is_refused_even_by_a_crafted_post(app, client):
    tax = tax_profile(app, 15.0)
    a = make_product(app, "Off Road Trailer", price=100.0, quantity=2, tax_profile_id=tax, under_maintenance=1)
    branch_id = branch(app)
    response = client.post(
        "/store/book",
        data=select(booking_payload(collect_branch_id=str(branch_id)), a, 1),
    )
    assert response.status_code == 400
    assert "maintenance" in response.get_data(as_text=True).lower()
    assert order_count(app) == 0


def test_a_hidden_trailer_is_refused_even_by_a_crafted_post(app, client):
    tax = tax_profile(app, 15.0)
    a = make_product(app, "Hidden Trailer", price=100.0, quantity=2, tax_profile_id=tax, public_visible=0)
    branch_id = branch(app)
    response = client.post(
        "/store/book",
        data=select(booking_payload(collect_branch_id=str(branch_id)), a, 1),
    )
    assert response.status_code == 400
    assert "not available for online booking" in response.get_data(as_text=True)
    assert order_count(app) == 0


def test_an_empty_selection_is_refused(app, client):
    branch_id = branch(app)
    response = client.post("/store/book", data=booking_payload(collect_branch_id=str(branch_id)))
    assert response.status_code == 400
    assert "at least one trailer" in response.get_data(as_text=True).lower()
    assert order_count(app) == 0


# --- dedupe (D6) ---------------------------------------------------------------------------------


def test_a_duplicate_customer_phone_links_instead_of_creating_a_second_row(app, client):
    tax = tax_profile(app, 15.0)
    a = make_product(app, "Cage Trailer", price=100.0, quantity=2, tax_profile_id=tax)
    branch_id = branch(app)
    existing = make_customer(app, name="Charmaine Mokoena", phone="0821234567")

    response = client.post(
        "/store/book",
        data=select(
            booking_payload(
                collect_branch_id=str(branch_id),
                name="Charmaine Mokoena",
                phone="0821234567",
                decision=f"link:{existing}",
            ),
            a,
            1,
        ),
    )
    assert response.status_code == 302
    assert customer_count(app) == 1, "the phone match links, it does not create a second row"
    with app.app_context():
        order = get_order(1)
    assert order["customer_id"] == existing


def test_a_blocked_customers_booking_is_refused(app, client):
    tax = tax_profile(app, 15.0)
    a = make_product(app, "Cage Trailer", price=100.0, quantity=2, tax_profile_id=tax)
    branch_id = branch(app)
    with app.app_context():
        get_db().execute(
            "INSERT INTO customers (customer_type, name, phone, created_at, is_blocked, blocked_reason) "
            "VALUES ('individual', 'Charmaine Mokoena', '0821234567', ?, 1, 'Unpaid')",
            (now(),),
        )
        get_db().commit()
    response = client.post(
        "/store/book",
        data=select(
            booking_payload(
                collect_branch_id=str(branch_id),
                name="Charmaine Mokoena",
                phone="0821234567",
                decision="create",
            ),
            a,
            1,
        ),
    )
    assert response.status_code == 400
    assert "counter" in response.get_data(as_text=True).lower()
    assert order_count(app) == 0


# --- consent (D10) -------------------------------------------------------------------------------


def test_an_unticked_consent_box_refuses_the_whole_booking_and_writes_nothing(app, client):
    tax = tax_profile(app, 15.0)
    a = make_product(app, "Cage Trailer", price=100.0, quantity=2, tax_profile_id=tax)
    branch_id = branch(app)
    data = select(booking_payload(collect_branch_id=str(branch_id)), a, 1)
    data.pop("popia_consent")
    response = client.post("/store/book", data=data)
    assert response.status_code == 400
    assert consent.consent_required_error() in response.get_data(as_text=True)
    assert order_count(app) == 0
    assert customer_count(app) == 0


def test_a_ticked_consent_records_against_the_customer_and_notice_version(app, client):
    tax = tax_profile(app, 15.0)
    a = make_product(app, "Cage Trailer", price=100.0, quantity=2, tax_profile_id=tax)
    branch_id = branch(app)
    response = client.post(
        "/store/book",
        data=select(booking_payload(collect_branch_id=str(branch_id)), a, 1),
    )
    assert response.status_code == 302
    with app.app_context():
        stored = get_db().execute("SELECT * FROM consent_records").fetchall()
        order = get_order(1)
    assert len(stored) == 1
    assert stored[0]["customer_id"] == order["customer_id"]
    assert stored[0]["channel"] == consent.CHANNEL_PUBLIC_BOOKING
    assert stored[0]["notice_version"] == consent.PRIVACY_NOTICE_VERSION
    with app.app_context():
        summary = consent.consent_summary(order["customer_id"])
    assert summary.startswith("POPIA consent — accepted ")


# --- the order is visible to staff and the customer ----------------------------------------------


def test_the_confirmation_page_lists_both_lines(app, client):
    tax = tax_profile(app, 15.0)
    a = make_product(app, "Cage Trailer", price=100.0, quantity=2, tax_profile_id=tax)
    b = make_product(app, "Flatbed Trailer", price=250.0, quantity=2, tax_profile_id=tax)
    branch_id = branch(app)
    response = client.post(
        "/store/book",
        data=select(select(booking_payload(collect_branch_id=str(branch_id)), a, 1), b, 1),
    )
    assert response.status_code == 302
    confirmation = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Cage Trailer" in confirmation
    assert "Flatbed Trailer" in confirmation
    assert "Booking request received" in confirmation


def test_the_customer_page_shows_the_order(app, client):
    tax = tax_profile(app, 15.0)
    a = make_product(app, "Cage Trailer", price=100.0, quantity=2, tax_profile_id=tax)
    branch_id = branch(app)
    client.post(
        "/store/book",
        data=select(booking_payload(collect_branch_id=str(branch_id)), a, 1),
    )
    with app.app_context():
        order = get_order(1)
    from app.services.customers import customer_orders

    with app.app_context():
        orders = customer_orders(order["customer_id"])
    assert [row["id"] for row in orders] == [order["id"]]


# --- the D11 gate (the shipped notice is unfinished) ---------------------------------------------


def test_the_booking_path_is_closed_while_the_notice_is_unfinished(app):
    tax = tax_profile(app, 15.0)
    a = make_product(app, "Cage Trailer", price=100.0, quantity=2, tax_profile_id=tax)
    branch_id = branch(app)
    raw = app.test_client()  # unpatched: the real shipped document still carries open facts
    body = raw.get("/store/book").get_data(as_text=True)
    assert "counter" in body.lower()
    assert 'id="booking-name"' not in body, "no form is rendered while the notice is unfinished"
    response = raw.post(
        "/store/book",
        data=select(booking_payload(collect_branch_id=str(branch_id)), a, 1),
    )
    assert response.status_code == 200
    assert order_count(app) == 0
    assert customer_count(app) == 0


# --- T3b: the live estimate must agree with what the order actually charges ----------------------

def test_a_taxed_trailer_charges_its_own_profile_rate(app, client):
    """The order side of T3b: per-trailer tax profiles, not one global VAT rate."""
    profile = tax_profile(app, 15.0)
    taxed = make_product(app, "Taxed Trailer", price=200.0, tax_profile_id=profile)
    untaxed = make_product(app, "Untaxed Trailer", price=100.0)
    payload = select(select(booking_payload(), taxed, 1), untaxed, 1)   # 2 days each
    response = client.post("/store/book", data=payload, follow_redirects=True)
    assert response.status_code == 200
    with app.app_context():
        order = get_db().execute("SELECT * FROM orders ORDER BY id DESC LIMIT 1").fetchone()
    assert order["subtotal"] == 600.0, order["subtotal"]      # 200*2 + 100*2
    assert order["tax_total"] == 60.0, order["tax_total"]      # only the 15% trailer
    assert order["total"] == 660.0, order["total"]


def test_the_live_estimate_carries_each_trailers_tax_rate(app, client):
    """The page side of T3b: without the rate on the input the script falls back to guessing."""
    profile = tax_profile(app, 15.0)
    make_product(app, "Taxed Trailer", price=200.0, tax_profile_id=profile)
    make_product(app, "Untaxed Trailer", price=100.0)
    body = client.get("/store/book").get_data(as_text=True)
    assert 'data-tax-rate="15' in body, "the taxed trailer must publish its profile rate"
    assert 'data-tax-rate="0"' in body, "a trailer with no tax profile must publish a zero rate"
    assert "TAX_MODE" in body, "the estimate must know the app's tax mode"


# --- T3b/T3c corrected: what "total" means in this app -------------------------------------------
# orders.total INCLUDES the refundable deposit (the admin order form builds its estimate the same way:
# subtotal + tax + waiver + deposit). These pin the rule so the page's estimate, the stored order and the
# confirmation cannot drift apart again - the drift that shipped a "Total payable" which double-counted it.

def test_a_deposit_is_stored_inside_the_order_total(app, client):
    profile = tax_profile(app, 15.0)
    first = make_product(app, "Deposit Trailer A", price=200.0, deposit=500.0, quantity=3, tax_profile_id=profile)
    second = make_product(app, "Deposit Trailer B", price=100.0, deposit=250.0, quantity=3)
    response = client.post("/store/book", data=select(select(booking_payload(), first, 1), second, 1), follow_redirects=True)
    assert response.status_code == 200
    with app.app_context():
        order = get_db().execute("SELECT * FROM orders ORDER BY id DESC LIMIT 1").fetchone()
    assert order["subtotal"] == 600.0, order["subtotal"]          # 200*2 + 100*2
    assert order["tax_total"] == 60.0, order["tax_total"]          # 15% on the taxed trailer only
    assert order["deposit_total"] == 750.0, order["deposit_total"]  # 500 + 250, refundable
    assert order["total"] == 1410.0, order["total"]                # 600 + 60 + 750: the deposit is INSIDE it


def test_the_confirmation_describes_the_deposit_as_part_of_the_total(app, client):
    product = make_product(app, "Deposit Trailer", price=200.0, deposit=500.0, quantity=2)
    response = client.post("/store/book", data=select(booking_payload(), product, 1), follow_redirects=True)
    assert b"Estimated total incl. refundable deposit" in response.data
    assert b"of which refundable security deposit" in response.data


def test_the_booking_page_will_label_its_total_as_deposit_inclusive(app, client):
    make_product(app, "Deposit Trailer", price=200.0, deposit=500.0, quantity=2)
    body = client.get("/store/book").get_data(as_text=True)
    assert 'id="estimate-total-label"' in body
    assert "incl. refundable deposit" in body
