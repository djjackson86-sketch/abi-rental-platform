"""Programme phase 5 (feature D / D1) — trailer identity + the return-matching service.

Don's ask: staff scan a licence disc — the **trailer's** or the **towing car's** — and
the matching rental is marked returned on the admin side.

What these tests pin:

* a rental product carries the same three identifiers a NaTIS disc does
  (``registration`` = number plate, ``registration_number`` = NaTIS number,
  ``licence_number`` = the disc's own licence number — decision D3), stored in one
  shape and unique per plate, so a scan can never be ambiguous;
* a post that never carried the identification panel cannot blank a recorded plate
  (the ``maintenance_panel`` marker trick);
* ``returns.match_open_rentals`` resolves a scan to the live order(s) it could
  belong to — with the evidence, in the documented order (trailer plate → customer
  vehicle → VIN/engine), scope-correct, and **empty rather than guessed**;
* only a picked-up (``started``) order is returnable; a reserved or draft match is
  reported as "not returnable yet";
* ``mark_returned_via_scan`` writes the audit trail and calls the existing
  ``transition_order(order_id, "return")`` — its refusal message is surfaced
  verbatim and nothing at all is written when the return flow says no.

The identifiers used here are the synthetic ones from the A1 fixtures (never the real
disc: that payload carries a real plate/VIN/engine number and stays outside the repo).
"""

import os
import sqlite3
import tempfile

import pytest

from app import create_app
from app.db import get_db, run_migrations
from app.services import returns
from app.services.customers import create_customer
from app.services.documents import create_document, finalize_document
from app.services.orders import (
    create_order,
    get_order,
    transition_order,
    update_return_checklist,
)
from app.services.products import create_product, duplicate_product, update_product
from app.services.vehicles import create_vehicle

PLATE = "TRL123GP"
NATIS = "ZZ1234Z"
DISC_LICENCE = "T9876543210X"
CAR_PLATE = "KP35XKGP"
CAR_NATIS = "SHS812W"
CAR_DISC_LICENCE = "4024048GB8LY"
CAR_VIN = "MMBJNKB40FD123456"
CAR_ENGINE = "4B11LC0187"


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    application = create_app(
        {
            "TESTING": True,
            "DATABASE": path,
            "SECRET_KEY": "test",
            "ADMIN_EMAIL": "admin@abi.local",
            "ADMIN_PASSWORD": "admin123",
        }
    )
    yield application
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


# ── helpers ─────────────────────────────────────────────────────────────────


def _product_form(**overrides):
    form = {
        "name": "6m Trailer",
        "product_type": "rental",
        "tracking_method": "bulk",
        "quantity": "3",
        "price_amount": "200",
        "price_unit": "day",
        "security_deposit": "750",
        "active": "1",
        "public_visible": "1",
        "trailer_identity_panel": "1",
        "registration": PLATE,
        "licence_number": DISC_LICENCE,
        "registration_number": NATIS,
    }
    form.update(overrides)
    return form


def _scan(plate="", natis="", disc_licence="", vin="", engine=""):
    """A ``parse_disc_text()``-shaped result (the parser's key names, not the columns')."""
    return {
        "licence_number": plate,
        "registration_number": natis,
        "disc_licence_number": disc_licence,
        "vin": vin,
        "engine_number": engine,
        "make": "MITSUBISHI",
        "model": "ASX",
        "raw_text": "synthetic fixture text",
    }


def _customer(app, name="Charmaine Mokoena", phone="0821234567"):
    with app.app_context():
        return create_customer({"name": name, "customer_type": "individual", "phone": phone})


def _product(app, **overrides):
    with app.app_context():
        return create_product(_product_form(**overrides))


def _car(app, customer_id, **overrides):
    form = {
        "registration": CAR_PLATE,
        "registration_number": CAR_NATIS,
        "licence_number": CAR_DISC_LICENCE,
        "make": "MITSUBISHI",
        "model": "ASX",
        "vin": CAR_VIN,
        "engine_number": CAR_ENGINE,
        "source": "scan",
    }
    form.update(overrides)
    with app.app_context():
        return create_vehicle(form, customer_id=customer_id)


def _order(app, customer_id, product_id, start="2026-07-01", end="2026-07-03", branch="1"):
    with app.app_context():
        return create_order(
            {
                "customer_id": str(customer_id),
                "product_id": str(product_id),
                "quantity": "1",
                "start_date": start,
                "start_time": "09:00",
                "end_date": end,
                "end_time": "15:00",
                "collect_branch_id": str(branch),
            },
            notify=False,
        )


def _started(app, customer_id, product_id, **kwargs):
    order_id = _order(app, customer_id, product_id, **kwargs)
    with app.app_context():
        transition_order(order_id, "start")
    return order_id


def _return_ready(app, order_id):
    """Everything the existing return flow insists on before a return is allowed."""
    with app.app_context():
        document_id = create_document(order_id, "invoice")
        finalize_document(document_id)
        update_return_checklist(order_id, {"no_damages": "1", "no_revision_required": "1"})


def _order_row(app, order_id):
    with app.app_context():
        return get_db().execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def _product_row(app, product_id):
    with app.app_context():
        return get_db().execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()


def _columns(app, table):
    with app.app_context():
        return {row["name"] for row in get_db().execute(f"PRAGMA table_info({table})").fetchall()}


# ── schema (additive only) ──────────────────────────────────────────────────


def test_products_carry_the_three_disc_identifiers(app):
    columns = _columns(app, "products")
    assert {"registration", "licence_number", "registration_number"} <= columns


def test_orders_carry_the_scan_audit_columns(app):
    columns = _columns(app, "orders")
    assert {
        "return_scan_at",
        "return_scan_registration",
        "return_scan_source",
        "return_scan_user_id",
    } <= columns


def test_one_plate_can_only_sit_on_one_trailer_at_the_database_level(app):
    """The partial unique index is the backstop behind the service's refusal."""
    with app.app_context():
        create_product(_product_form())
        with pytest.raises(sqlite3.IntegrityError):
            get_db().execute(
                "INSERT INTO products (name, registration, created_at) VALUES (?, ?, ?)",
                ("Second Trailer", PLATE, "2026-09-23T12:00:00"),
            )


def test_products_without_a_plate_may_repeat(app):
    with app.app_context():
        create_product(_product_form(name="Trailer A", registration="", licence_number="", registration_number=""))
        create_product(_product_form(name="Trailer B", registration="", licence_number="", registration_number=""))
        rows = get_db().execute("SELECT COUNT(*) AS c FROM products WHERE registration = ''").fetchone()
        assert rows["c"] == 2


def test_migration_adds_the_columns_to_an_existing_database_without_touching_rows(app):
    """An older database (no identity/audit columns) only gains empty columns.

    The pre-phase-5 shape is emulated by dropping the columns from a real database
    (SQLite's own ``DROP COLUMN``) and then running the migration again — the same
    code path an existing local database or the live Turso database takes.
    """
    with app.app_context():
        db = get_db()
        product_id = create_product(_product_form())
        customer_id = create_customer({"name": "Legacy Client", "customer_type": "individual"})
        order_id = create_order(
            {
                "customer_id": str(customer_id),
                "product_id": str(product_id),
                "quantity": "1",
                "start_date": "2026-07-01",
                "start_time": "09:00",
                "end_date": "2026-07-03",
                "end_time": "15:00",
            },
            notify=False,
        )
        db.execute("DROP INDEX IF EXISTS idx_products_registration")
        for column in ("registration", "licence_number", "registration_number"):
            db.execute(f"ALTER TABLE products DROP COLUMN {column}")
        for column in (
            "return_scan_at",
            "return_scan_registration",
            "return_scan_source",
            "return_scan_user_id",
        ):
            db.execute(f"ALTER TABLE orders DROP COLUMN {column}")
        db.commit()
        dropped = {row["name"] for row in db.execute("PRAGMA table_info(products)")}
        assert "registration" not in dropped

        run_migrations(db)
        db.commit()

        columns = {row["name"] for row in db.execute("PRAGMA table_info(products)")}
        order_columns = {row["name"] for row in db.execute("PRAGMA table_info(orders)")}
        product = db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()

    assert {"registration", "licence_number", "registration_number"} <= columns
    assert {"return_scan_at", "return_scan_source", "return_scan_user_id"} <= order_columns
    assert product["name"] == "6m Trailer"
    assert product["registration"] == ""
    assert order["order_number"]
    assert order["return_scan_at"] == ""
    assert order["return_scan_registration"] == ""


# ── the inventory form / the marker guard ───────────────────────────────────


def _login(app, client):
    with app.app_context():
        row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
    return client.post(
        "/login", data={"user_id": str(row["id"]), "password": "admin123"}, follow_redirects=True
    )


def test_inventory_form_offers_the_identification_panel(app, client):
    _login(app, client)
    page = client.get("/inventory/new")
    assert page.status_code == 200
    assert b"Trailer identification" in page.data
    assert b'name="trailer_identity_panel"' in page.data
    assert b'name="registration"' in page.data


def test_identity_saves_through_the_real_inventory_form_and_is_normalised(app, client):
    _login(app, client)
    form = _product_form(name="Form Trailer", registration=" kp 35 xkgp ")
    form.pop("quantity")
    form["quantity"] = "2"
    response = client.post("/inventory/new", data=form, follow_redirects=True)
    assert response.status_code == 200
    with app.app_context():
        row = get_db().execute("SELECT * FROM products WHERE name = 'Form Trailer'").fetchone()
    assert row["registration"] == "KP 35 XKGP"
    assert row["registration_number"] == NATIS
    assert row["licence_number"] == DISC_LICENCE


def test_a_post_without_the_panel_keeps_the_stored_plate(app):
    product_id = _product(app)
    with app.app_context():
        form = _product_form(name="6m Trailer")
        for key in ("registration", "licence_number", "registration_number", "trailer_identity_panel"):
            form.pop(key)
        update_product(product_id, form)
    row = _product_row(app, product_id)
    assert row["registration"] == PLATE
    assert row["registration_number"] == NATIS
    assert row["licence_number"] == DISC_LICENCE


def test_a_panel_post_with_blank_boxes_clears_the_identity(app):
    product_id = _product(app)
    with app.app_context():
        update_product(product_id, _product_form(registration="", licence_number="", registration_number=""))
    row = _product_row(app, product_id)
    assert row["registration"] == ""
    assert row["registration_number"] == ""


def test_re_saving_a_trailer_keeps_its_own_plate(app):
    product_id = _product(app)
    with app.app_context():
        update_product(product_id, _product_form(description="Re-saved"))
    row = _product_row(app, product_id)
    assert row["registration"] == PLATE


def test_a_second_trailer_cannot_claim_a_recorded_plate(app):
    first = _product(app, registration="kp 35 xkgp")
    with app.app_context():
        with pytest.raises(ValueError) as error:
            create_product(_product_form(name="Other Trailer", registration="KP35XKGP"))
    assert "already recorded on 6m Trailer" in str(error.value)
    assert _product_row(app, first)["registration"] == "KP 35 XKGP"


def test_moving_a_plate_onto_another_trailer_is_refused_by_name(app):
    first = _product(app)
    second = _product(app, name="Trailer Two", registration="OTH222GP")
    with app.app_context():
        with pytest.raises(ValueError) as error:
            update_product(second, _product_form(name="Trailer Two", registration=PLATE))
    assert "already recorded on 6m Trailer" in str(error.value)
    assert _product_row(app, second)["registration"] == "OTH222GP"
    assert _product_row(app, first)["registration"] == PLATE


def test_duplicating_a_trailer_does_not_copy_its_identity(app):
    product_id = _product(app)
    with app.app_context():
        copy_id = duplicate_product(product_id)
    copy = _product_row(app, copy_id)
    assert copy["registration"] == ""
    assert copy["registration_number"] == ""
    assert copy["licence_number"] == ""


# ── matching ────────────────────────────────────────────────────────────────


def test_scanned_trailer_plate_finds_the_started_order_holding_it(app):
    customer_id = _customer(app)
    product_id = _product(app)
    order_id = _started(app, customer_id, product_id)
    with app.app_context():
        found = returns.match_open_rentals(_scan(plate=PLATE))
    assert [candidate["order_id"] for candidate in found] == [order_id]
    candidate = found[0]
    assert candidate["matched_on"] == returns.MATCH_TRAILER_PLATE
    assert candidate["evidence"] == PLATE
    assert candidate["source"] == returns.SOURCE_TRAILER_DISC
    assert candidate["returnable"] is True
    assert candidate["customer_name"] == "Charmaine Mokoena"
    assert candidate["reason"] == ""


def test_a_scans_trailer_can_also_be_found_by_its_natis_or_disc_number(app):
    customer_id = _customer(app)
    product_id = _product(app)
    order_id = _started(app, customer_id, product_id)
    with app.app_context():
        by_natis = returns.match_open_rentals(_scan(natis=NATIS))
        by_disc = returns.match_open_rentals(_scan(disc_licence=DISC_LICENCE))
    assert [c["order_id"] for c in by_natis] == [order_id]
    assert by_natis[0]["evidence"] == NATIS
    assert by_natis[0]["matched_on"] == returns.MATCH_TRAILER_PLATE
    assert [c["order_id"] for c in by_disc] == [order_id]


def test_plate_spacing_and_case_do_not_matter(app):
    customer_id = _customer(app)
    product_id = _product(app)
    order_id = _started(app, customer_id, product_id)
    with app.app_context():
        found = returns.match_open_rentals(_scan(plate="  t r l 1 2 3 g p "))
    assert [c["order_id"] for c in found] == [order_id]


def test_the_towing_cars_plate_finds_that_customers_open_rental(app):
    customer_id = _customer(app)
    product_id = _product(app)
    _car(app, customer_id)
    order_id = _started(app, customer_id, product_id)
    with app.app_context():
        found = returns.match_open_rentals(_scan(plate=CAR_PLATE))
    assert [c["order_id"] for c in found] == [order_id]
    assert found[0]["matched_on"] == returns.MATCH_CUSTOMER_VEHICLE_PLATE
    assert found[0]["evidence"] == CAR_PLATE
    assert found[0]["source"] == returns.SOURCE_VEHICLE_DISC


def test_the_cars_natis_number_vin_and_engine_number_all_match(app):
    customer_id = _customer(app)
    product_id = _product(app)
    _car(app, customer_id)
    order_id = _started(app, customer_id, product_id)
    with app.app_context():
        by_natis = returns.match_open_rentals(_scan(natis=CAR_NATIS))
        by_vin = returns.match_open_rentals(_scan(vin=CAR_VIN))
        by_engine = returns.match_open_rentals(_scan(engine=CAR_ENGINE))
    assert [c["order_id"] for c in by_natis] == [order_id]
    assert by_natis[0]["matched_on"] == returns.MATCH_VEHICLE_REGISTRATION_NUMBER
    assert [c["order_id"] for c in by_vin] == [order_id]
    assert by_vin[0]["matched_on"] == returns.MATCH_VEHICLE_VIN
    assert by_vin[0]["evidence"] == CAR_VIN
    assert by_engine[0]["matched_on"] == returns.MATCH_VEHICLE_ENGINE


def test_an_unknown_disc_matches_nothing_at_all(app):
    customer_id = _customer(app)
    product_id = _product(app)
    _started(app, customer_id, product_id)
    with app.app_context():
        assert returns.match_open_rentals(_scan(plate="NOPLATE9", natis="QQ9999Q", vin="X" * 17)) == []


def test_a_blank_scan_matches_nothing(app):
    customer_id = _customer(app)
    product_id = _product(app)
    _started(app, customer_id, product_id)
    with app.app_context():
        assert returns.match_open_rentals(_scan()) == []
        assert returns.match_open_rentals({}) == []


def test_an_ambiguous_scan_returns_every_candidate_and_marks_nothing(app):
    customer_id = _customer(app)
    product_id = _product(app)
    first = _started(app, customer_id, product_id, start="2026-07-01", end="2026-07-03")
    second = _started(app, customer_id, product_id, start="2026-08-01", end="2026-08-03")
    with app.app_context():
        found = returns.match_open_rentals(_scan(plate=PLATE))
        statuses = {order_id: get_order(order_id)["status"] for order_id in (first, second)}
    assert sorted(c["order_id"] for c in found) == sorted([first, second])
    assert all(c["returnable"] for c in found)
    assert statuses == {first: "started", second: "started"}


def test_a_reserved_order_is_reported_as_not_returnable_yet(app):
    customer_id = _customer(app)
    product_id = _product(app)
    order_id = _order(app, customer_id, product_id)
    with app.app_context():
        transition_order(order_id, "reserve")
        found = returns.match_open_rentals(_scan(plate=PLATE))
    assert [c["order_id"] for c in found] == [order_id]
    assert found[0]["returnable"] is False
    assert found[0]["status"] == "reserved"
    assert "must be picked up before it can be returned" in found[0]["reason"]


def test_a_draft_order_is_listed_but_not_offered(app):
    customer_id = _customer(app)
    product_id = _product(app)
    order_id = _order(app, customer_id, product_id)
    with app.app_context():
        found = returns.match_open_rentals(_scan(plate=PLATE))
    assert [c["order_id"] for c in found] == [order_id]
    assert found[0]["returnable"] is False
    assert found[0]["status_label"] == "Draft"


def test_a_returnable_match_is_listed_before_a_not_returnable_one(app):
    customer_id = _customer(app)
    product_id = _product(app)
    returnable = _started(app, customer_id, product_id, start="2026-07-01", end="2026-07-03")
    parked = _order(app, customer_id, product_id, start="2026-09-01", end="2026-09-03")
    with app.app_context():
        found = returns.match_open_rentals(_scan(plate=PLATE))
    assert [c["order_id"] for c in found] == [returnable, parked]
    assert [c["returnable"] for c in found] == [True, False]


def test_one_order_matched_two_ways_is_returned_once_with_the_strongest_evidence(app):
    customer_id = _customer(app)
    product_id = _product(app)
    # The same customer's car carries the trailer's plate (a real data-entry
    # possibility): the trailer match is the stronger evidence and wins.
    _car(app, customer_id, registration=PLATE)
    order_id = _started(app, customer_id, product_id)
    with app.app_context():
        found = returns.match_open_rentals(_scan(plate=PLATE))
    assert len(found) == 1
    assert found[0]["matched_on"] == returns.MATCH_TRAILER_PLATE
    assert len(found[0]["also_matched_on"]) == 1
    assert found[0]["also_matched_on"][0]["matched_on"] == returns.MATCH_CUSTOMER_VEHICLE_PLATE


def test_another_depots_rental_is_out_of_scope_for_a_branch_limited_account(app):
    customer_id = _customer(app)
    product_id = _product(app)
    order_id = _started(app, customer_id, product_id, branch="2")
    with app.app_context():
        own_branch = returns.match_open_rentals(_scan(plate=PLATE), session_scope=[2])
        other_branch = returns.match_open_rentals(_scan(plate=PLATE), session_scope=[1])
        no_branch = returns.match_open_rentals(_scan(plate=PLATE), session_scope=[])
    assert [c["order_id"] for c in own_branch] == [order_id]
    assert other_branch == []
    assert no_branch == []


def test_a_customer_vehicle_scan_is_also_scope_correct(app):
    customer_id = _customer(app)
    product_id = _product(app)
    _car(app, customer_id)
    _started(app, customer_id, product_id, branch="3")
    with app.app_context():
        assert returns.match_open_rentals(_scan(plate=CAR_PLATE), session_scope=[1]) == []
        assert len(returns.match_open_rentals(_scan(plate=CAR_PLATE), session_scope=[3])) == 1


# ── the guard the screen calls before posting ───────────────────────────────


def test_returnable_order_refuses_a_not_started_order(app):
    customer_id = _customer(app)
    product_id = _product(app)
    order_id = _order(app, customer_id, product_id)
    with app.app_context():
        with pytest.raises(ValueError) as error:
            returns.returnable_order(order_id)
    assert "only a picked-up (Started) order can be returned" in str(error.value)


def test_returnable_order_refuses_an_already_returned_order(app):
    customer_id = _customer(app)
    product_id = _product(app)
    order_id = _started(app, customer_id, product_id)
    _return_ready(app, order_id)
    with app.app_context():
        returns.mark_returned_via_scan(order_id, user_id=None, parsed=_scan(plate=PLATE))
        with pytest.raises(ValueError) as error:
            returns.returnable_order(order_id)
    assert "is already returned (returned via disc scan on" in str(error.value)


def test_returnable_order_refuses_another_depot(app):
    customer_id = _customer(app)
    product_id = _product(app)
    order_id = _started(app, customer_id, product_id, branch="2")
    with app.app_context():
        with pytest.raises(ValueError) as error:
            returns.returnable_order(order_id, session_scope=[1])
    assert "belongs to another depot" in str(error.value)


def test_returnable_order_refuses_an_order_that_does_not_exist(app):
    with app.app_context():
        with pytest.raises(ValueError) as error:
            returns.returnable_order(9999)
    assert str(error.value) == "Order not found"


# ── marking it returned, through the existing flow ──────────────────────────


def test_a_trailer_disc_scan_marks_the_rental_returned_and_records_the_audit(app):
    customer_id = _customer(app)
    product_id = _product(app)
    order_id = _started(app, customer_id, product_id)
    _return_ready(app, order_id)
    before = _order_row(app, order_id)
    with app.app_context():
        result = returns.mark_returned_via_scan(order_id, user_id=None, parsed=_scan(plate=PLATE))
    after = _order_row(app, order_id)
    assert result["message"] == "Order returned"
    assert after["status"] == "returned"
    assert after["return_scan_registration"] == PLATE
    assert after["return_scan_source"] == returns.SOURCE_TRAILER_DISC
    assert after["return_scan_at"]
    assert after["picked_up_at"] == before["picked_up_at"]


def test_a_car_disc_scan_records_the_vehicle_source_and_the_staff_member(app):
    customer_id = _customer(app)
    product_id = _product(app)
    _car(app, customer_id)
    order_id = _started(app, customer_id, product_id)
    _return_ready(app, order_id)
    with app.app_context():
        user_id = get_db().execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
        result = returns.mark_returned_via_scan(order_id, user_id=user_id, parsed=_scan(plate=CAR_PLATE))
    after = _order_row(app, order_id)
    assert result["source"] == returns.SOURCE_VEHICLE_DISC
    assert result["registration"] == CAR_PLATE
    assert after["return_scan_source"] == returns.SOURCE_VEHICLE_DISC
    assert after["return_scan_registration"] == CAR_PLATE
    assert after["return_scan_user_id"] == user_id


def test_a_normal_return_leaves_the_scan_audit_empty(app):
    customer_id = _customer(app)
    product_id = _product(app)
    order_id = _started(app, customer_id, product_id)
    _return_ready(app, order_id)
    with app.app_context():
        transition_order(order_id, "return")
    row = _order_row(app, order_id)
    assert row["status"] == "returned"
    assert row["return_scan_at"] == ""
    assert row["return_scan_registration"] == ""
    assert row["return_scan_source"] == ""
    assert row["return_scan_user_id"] is None


def test_the_existing_return_flow_refusal_is_surfaced_verbatim_and_writes_nothing(app):
    customer_id = _customer(app)
    product_id = _product(app)
    order_id = _started(app, customer_id, product_id)
    # No finalized invoice, no checklist: the return flow refuses, and the scan path
    # must not have written anything (no audit, no status change).
    with app.app_context():
        with pytest.raises(ValueError) as error:
            returns.mark_returned_via_scan(order_id, user_id=7, parsed=_scan(plate=PLATE))
    assert str(error.value) == "Finalize the invoice before returning this order"
    row = _order_row(app, order_id)
    assert row["status"] == "started"
    assert row["return_scan_at"] == ""
    assert row["return_scan_registration"] == ""


def test_scanning_the_same_disc_twice_says_already_returned(app):
    customer_id = _customer(app)
    product_id = _product(app)
    order_id = _started(app, customer_id, product_id)
    _return_ready(app, order_id)
    with app.app_context():
        returns.mark_returned_via_scan(order_id, user_id=None, parsed=_scan(plate=PLATE))
        with pytest.raises(ValueError) as error:
            returns.mark_returned_via_scan(order_id, user_id=None, parsed=_scan(plate=PLATE))
    assert "is already returned" in str(error.value)


def test_a_scan_cannot_return_an_order_belonging_to_another_depot(app):
    customer_id = _customer(app)
    product_id = _product(app)
    order_id = _started(app, customer_id, product_id, branch="2")
    _return_ready(app, order_id)
    with app.app_context():
        with pytest.raises(ValueError) as error:
            returns.mark_returned_via_scan(order_id, parsed=_scan(plate=PLATE), session_scope=[1])
        also = None
        try:
            returns.returnable_order(order_id, session_scope=[1])
        except ValueError as exc:
            also = str(exc)
    assert "belongs to another depot" in str(error.value)
    assert "belongs to another depot" in also
    row = _order_row(app, order_id)
    assert row["status"] == "started"
    assert row["return_scan_at"] == ""
