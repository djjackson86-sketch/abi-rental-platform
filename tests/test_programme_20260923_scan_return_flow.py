"""Programme phase 6 (feature D / D2) — the staff scan-to-return screen.

Don's ask: staff scan a licence disc (the trailer's or the towing car's) and the
rental comes back on the admin side, with the return checklist still there to
finish.

What these tests pin, one per line of the plan's §D2 acceptance:

* the screen is gated by the ``scan_return`` module — a sign-in without it gets
  403 on every route, and the module is what puts the nav link on the page;
* a trailer disc (photo **or** pasted text, or just a typed plate) resolves to the
  started order holding that trailer, with the evidence shown, and one press of
  "Mark returned" moves it to ``returned`` through the existing return flow and
  lands staff on the order page;
* the towing car's disc works the same way and records the vehicle source;
* an ambiguous scan lists every candidate and **returns nothing until a choice is
  posted** — never an auto-pick;
* a scan that matches nothing says so and links to the started-orders list;
* scanning the same disc twice reports "already returned" instead of quietly
  doing nothing;
* a crafted POST for a not-started order, or for another depot's order, is
  refused with the existing flow's message and changes nothing.

Every identifier here is synthetic. The real disc photo and its real plate / VIN /
engine number stay outside the repo (POPIA).
"""

import io
import os
import re
import tempfile
from pathlib import Path

import pytest

from app import create_app
from app.db import get_db
from app.services import returns
from app.services.access import create_additional_user, save_user_modules
from app.services.customers import create_customer
from app.services.documents import create_document, finalize_document
from app.services.orders import create_order, get_order, transition_order, update_return_checklist
from app.services.products import create_product
from app.services.vehicles import create_vehicle

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "disc"

#: The trailer's own disc — the modern 148-char positional payload from the A1
#: fixtures (synthetic identifiers at the real field lengths).
TRAILER_PLATE = "ABC123GP"
TRAILER_NATIS = "ZZ1234Z"
TRAILER_DISC_LICENCE = "T9876543210X"

#: The towing car's disc — a synthetic label-value payload of our own, so the car
#: and the trailer can never be confused with one another.
CAR_PLATE = "JHB789GP"
CAR_NATIS = "NAT5678G"
CAR_DISC_LICENCE = "D1234567890K"
CAR_VIN = "AHTFR22G10L999888"
CAR_ENGINE = "K9K123456"
CAR_TEXT = "\n".join(
    [
        "Registering Authority GAUTENG",
        "Control Number 98765432",
        f"Licence number JHB 789 GP",
        f"Vehicle Registration Number NAT 5678 G",
        "Make NISSAN",
        "Model NP200",
        "Colour White",
        f"Vehicle identification number (VIN) {CAR_VIN}",
        f"Engine number {CAR_ENGINE}",
        "Licence expiry 2027/09/30",
    ]
)


def trailer_disc_text() -> str:
    return (FIXTURE_DIR / "natis_positional.txt").read_text(encoding="utf-8")


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


def _customer(app, name="Charmaine Mokoena", phone="0821234567"):
    with app.app_context():
        return create_customer({"name": name, "customer_type": "individual", "phone": phone})


def _product(app, registration=TRAILER_PLATE, **overrides):
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
        "registration": registration,
        "licence_number": TRAILER_DISC_LICENCE,
        "registration_number": TRAILER_NATIS,
    }
    form.update(overrides)
    with app.app_context():
        return create_product(form)


def _car(app, customer_id):
    form = {
        "registration": CAR_PLATE,
        "registration_number": CAR_NATIS,
        "licence_number": CAR_DISC_LICENCE,
        "make": "NISSAN",
        "model": "NP200",
        "vin": CAR_VIN,
        "engine_number": CAR_ENGINE,
        "source": "scan",
    }
    with app.app_context():
        return create_vehicle(form, customer_id=customer_id)


def _order(app, customer_id, product_id, branch="1"):
    with app.app_context():
        return create_order(
            {
                "customer_id": str(customer_id),
                "product_id": str(product_id),
                "quantity": "1",
                "start_date": "2026-07-01",
                "start_time": "09:00",
                "end_date": "2026-07-03",
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


def _reserved(app, customer_id, product_id, **kwargs):
    order_id = _order(app, customer_id, product_id, **kwargs)
    with app.app_context():
        transition_order(order_id, "reserve")
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


def _login_owner(client):
    with client.application.app_context():
        row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
    return client.post(
        "/login", data={"user_id": str(row["id"]), "password": "admin123"}, follow_redirects=True
    )


def _make_staff(app, name, modules, branch_ids=None, password="staff1234"):
    with app.app_context():
        user_id, error = create_additional_user(name, password, branch_ids=branch_ids)
        assert error is None, error
        ok, saved = save_user_modules(user_id, list(modules))
        assert ok, saved
    return user_id


def _login_staff(client, user_id, password="staff1234"):
    return client.post(
        "/login", data={"user_id": str(user_id), "password": password}, follow_redirects=True
    )


def _scan(client, **data):
    """The capture form: a photo, pasted barcode text, or a typed plate."""
    return client.post("/scan-return", data=data, follow_redirects=True)


def _confirm(client, order_id=None, plate=TRAILER_PLATE, **overrides):
    data = {
        "scan_plate": plate,
        "scan_natis": "",
        "scan_disc_licence": "",
        "scan_vin": "",
        "scan_engine": "",
    }
    if order_id is not None:
        data["order_id"] = str(order_id)
    data.update(overrides)
    return client.post("/scan-return/confirm", data=data, follow_redirects=True)


def _body(response) -> str:
    return " ".join(response.get_data(as_text=True).split())


def _text(response) -> str:
    """The page's **text**, tags stripped and entities decoded.

    Jinja escapes a plain apostrophe to ``&#39;`` in the markup, so an assertion
    about what staff read ("the trailer's number plate") has to look at the text,
    not the source.
    """
    html = response.get_data(as_text=True)
    html = re.sub(r"<[^>]+>", " ", html)
    for entity, character in (("&#39;", "'"), ("&amp;", "&"), ("&quot;", '"'), ("&lt;", "<"), ("&gt;", ">")):
        html = html.replace(entity, character)
    return " ".join(html.split())


def synthetic_disc_png(payload: str) -> bytes:
    """A real PDF417 of *our* text, generated locally — an image, not a stand-in."""
    zxingcpp = pytest.importorskip("zxingcpp")
    from PIL import Image

    barcode = zxingcpp.create_barcode(payload, zxingcpp.BarcodeFormat.PDF417)
    image = zxingcpp.write_barcode_to_image(barcode)
    height, width = image.shape
    pil = Image.frombuffer("L", (width, height), image, "raw", "L", 0, 1)
    buffer = io.BytesIO()
    pil.convert("RGB").resize((width * 2, height * 2)).save(buffer, format="PNG")
    return buffer.getvalue()


# ── module gating and the screen itself ─────────────────────────────────────


def test_scan_return_needs_the_module(app, client):
    user_id = _make_staff(app, "Sipho Nkosi", ("dashboard", "orders"))
    _login_staff(client, user_id)
    assert client.get("/scan-return").status_code == 403
    assert client.post("/scan-return", data={"disc_text": trailer_disc_text()}).status_code == 403
    assert client.post("/scan-return/confirm", data={"order_id": "1"}).status_code == 403


def test_the_module_grants_the_screen_and_the_nav_link(app, client):
    with_module = _make_staff(app, "Sipho Nkosi", ("dashboard", "orders", "scan_return"))
    without = _make_staff(app, "Thabo Mahlangu", ("dashboard", "orders"))
    _login_staff(client, with_module)
    response = client.get("/scan-return")
    body = _body(response)
    assert response.status_code == 200
    assert 'name="disk_image"' in body and 'capture="environment"' in body
    assert 'name="disc_text"' in body and 'name="plate"' in body
    assert "Scan to return" in body

    _login_staff(client, without)
    assert "Scan to return" not in _body(client.get("/dashboard"))


def test_a_pasted_trailer_disc_offers_the_started_order_with_its_evidence(app, client):
    customer_id = _customer(app)
    product_id = _product(app)
    order_id = _started(app, customer_id, product_id)
    expected = _order_row(app, order_id)["order_number"]
    _login_owner(client)

    body = _body(_scan(client, disc_text=trailer_disc_text()))
    assert expected in body
    assert "Charmaine Mokoena" in body
    assert "Mark returned" in body
    assert TRAILER_PLATE in body
    # The confirm form carries the identifiers the scan actually read.
    assert 'name="scan_plate" value="ABC123GP"' in body
    assert 'name="scan_natis" value="ZZ1234Z"' in body
    assert f'name="order_id" value="{order_id}"' in body


def test_a_trailer_disc_photograph_is_read_and_offered(app, client):
    customer_id = _customer(app)
    product_id = _product(app)
    order_id = _started(app, customer_id, product_id)
    expected = _order_row(app, order_id)["order_number"]
    _login_owner(client)

    response = client.post(
        "/scan-return",
        data={"disk_image": (io.BytesIO(synthetic_disc_png(trailer_disc_text())), "disc.png")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    body = _body(response)
    assert response.status_code == 200
    assert expected in body and "Mark returned" in body


def test_a_typed_plate_finds_the_rental_without_any_disc(app, client):
    customer_id = _customer(app)
    product_id = _product(app)
    order_id = _started(app, customer_id, product_id)
    _login_owner(client)

    body = _body(_scan(client, plate="abc 123 gp"))
    assert _order_row(app, order_id)["order_number"] in body
    assert "Mark returned" in body


# ── the loop: scan → confirm → returned ─────────────────────────────────────


def test_one_candidate_is_marked_returned_and_lands_on_the_order(app, client):
    customer_id = _customer(app)
    product_id = _product(app)
    order_id = _started(app, customer_id, product_id)
    _return_ready(app, order_id)
    before = _order_row(app, order_id)
    _login_owner(client)

    _scan(client, disc_text=trailer_disc_text())
    response = _confirm(client, order_id)

    assert response.status_code == 200
    body = _body(response)
    assert f"/orders/{order_id}" in response.request.path or "returned via disc scan" in body
    after = _order_row(app, order_id)
    assert after["status"] == "returned"
    assert after["return_scan_registration"] == TRAILER_PLATE
    assert after["return_scan_source"] == returns.SOURCE_TRAILER_DISC
    assert after["return_scan_at"]
    assert after["return_scan_user_id"] is not None
    # The existing flow is untouched: nothing about the pickup changed.
    assert after["picked_up_at"] == before["picked_up_at"]
    assert "returned via disc scan" in body


def test_the_towing_cars_disc_returns_its_rental_and_records_the_vehicle_source(app, client):
    customer_id = _customer(app)
    product_id = _product(app, registration="")
    _car(app, customer_id)
    order_id = _started(app, customer_id, product_id)
    _return_ready(app, order_id)
    _login_owner(client)

    body = _body(_scan(client, disc_text=CAR_TEXT))
    assert CAR_PLATE in body
    response = _confirm(client, order_id, plate=CAR_PLATE, **{"scan_natis": CAR_NATIS})
    after = _order_row(app, order_id)
    assert after["status"] == "returned"
    assert after["return_scan_source"] == returns.SOURCE_VEHICLE_DISC
    assert after["return_scan_registration"] == CAR_PLATE
    assert "returned via disc scan" in _body(response)


def test_the_order_page_shows_the_disc_scan_line(app, client):
    customer_id = _customer(app)
    product_id = _product(app)
    order_id = _started(app, customer_id, product_id)
    _return_ready(app, order_id)
    _login_owner(client)
    _confirm(client, order_id)

    body = _body(client.get(f"/orders/{order_id}"))
    assert "Returned via disc scan" in body
    assert TRAILER_PLATE in body


def test_the_audit_line_records_the_plate_in_the_house_normalised_shape(app, client):
    """A loose disc payload must not write a lowercase plate into the audit.

    Every plate in the app is stored upper case with single spaces; the order's
    audit line is read by staff on the order page, so it has to match.
    """
    customer_id = _customer(app)
    product_id = _product(app, registration="")
    _car(app, customer_id)
    order_id = _started(app, customer_id, product_id)
    _return_ready(app, order_id)
    _login_owner(client)

    body = _body(_scan(client, disc_text=CAR_TEXT))
    assert "JHB 789 GP" in body  # what the parser read off the disk
    _confirm(client, order_id, plate="jhb 789 gp", **{"scan_natis": "nat 5678 g"})
    row = _order_row(app, order_id)
    assert row["status"] == "returned"
    assert row["return_scan_registration"] == "JHB 789 GP"


def test_the_evidence_is_named_for_the_value_that_actually_matched(app, client):
    """A disk licence number must never be described as the trailer's number plate."""
    customer_id = _customer(app)
    product_id = _product(app)
    order_id = _started(app, customer_id, product_id)
    _login_owner(client)

    body = _body(_scan(client, disc_text=trailer_disc_text()))
    assert _order_row(app, order_id)["order_number"] in body
    text = _text(_scan(client, disc_text=trailer_disc_text()))
    assert f"Matched on the trailer's number plate — {TRAILER_PLATE}" in text
    # The other two identifiers the disk carried are named for what they are.
    assert f"the trailer's disk licence number {TRAILER_DISC_LICENCE}" in text
    assert f"the trailer's NaTIS registration number {TRAILER_NATIS}" in text


def test_a_trailer_matched_only_on_its_natis_or_disk_number_says_so(app, client):
    customer_id = _customer(app)
    product_id = _product(app, registration="")  # plate never captured, NaTIS + disk number are
    order_id = _started(app, customer_id, product_id)
    _login_owner(client)

    body = _body(_scan(client, disc_text=trailer_disc_text()))
    assert _order_row(app, order_id)["order_number"] in body
    text = _text(_scan(client, disc_text=trailer_disc_text()))
    assert f"Matched on the trailer's disk licence number — {TRAILER_DISC_LICENCE}" in text
    assert f"the trailer's NaTIS registration number {TRAILER_NATIS}" in text
    assert "Matched on the trailer's number plate" not in text


# ── ambiguity, nothing-found, and the double scan ───────────────────────────


def test_an_ambiguous_scan_lists_every_candidate_and_returns_nothing_until_a_choice(app, client):
    customer_id = _customer(app)
    _car(app, customer_id)
    first_product = _product(app, registration="")
    second_product = _product(app, name="4m Trailer", registration="")
    first_order = _started(app, customer_id, first_product)
    second_order = _started(app, customer_id, second_product)
    for order_id in (first_order, second_order):
        _return_ready(app, order_id)
    first_number = _order_row(app, first_order)["order_number"]
    second_number = _order_row(app, second_order)["order_number"]
    _login_owner(client)

    body = _body(_scan(client, disc_text=CAR_TEXT))
    assert body.count("Mark returned") == 2
    assert first_number in body and second_number in body
    assert "2 rentals match this disk" in body
    # Nothing was returned by the scan itself.
    assert _order_row(app, first_order)["status"] == "started"
    assert _order_row(app, second_order)["status"] == "started"

    # A confirm without a choice refuses to guess.
    refused = _body(_confirm(client, None, plate=CAR_PLATE))
    assert "Choose which rental" in refused
    assert body.count("Mark returned") == 2
    assert _order_row(app, first_order)["status"] == "started"
    assert _order_row(app, second_order)["status"] == "started"

    # An explicit choice returns exactly that one.
    _confirm(client, second_order, plate=CAR_PLATE)
    assert _order_row(app, first_order)["status"] == "started"
    assert _order_row(app, second_order)["status"] == "returned"


def test_a_scan_that_matches_nothing_says_so_and_links_to_started_orders(app, client):
    _login_owner(client)
    body = _body(_scan(client, plate="ZZZ999ZZ"))
    assert "no open rental" in body.lower() or "Nothing open" in body
    assert "ZZZ999ZZ" in body
    assert "/orders?status=started" in body or "status=started" in body


def test_scanning_the_same_disc_twice_reports_already_returned(app, client):
    customer_id = _customer(app)
    product_id = _product(app)
    order_id = _started(app, customer_id, product_id)
    _return_ready(app, order_id)
    _login_owner(client)
    _confirm(client, order_id)

    body = _body(_scan(client, disc_text=trailer_disc_text()))
    assert "already returned" in body
    assert _order_row(app, order_id)["status"] == "returned"


def test_junk_text_is_a_message_not_a_crash(app, client):
    _login_owner(client)
    response = _scan(client, disc_text="THIS IS NOT A LICENCE DISK")
    assert response.status_code == 200
    body = _body(response)
    assert 'name="disc_text"' in body
    assert "could not" in body.lower() or "no vehicle fields" in body.lower()


# ── crafted posts are refused with the existing message ─────────────────────


def test_a_crafted_post_for_a_not_started_order_is_refused(app, client):
    customer_id = _customer(app)
    product_id = _product(app)
    order_id = _reserved(app, customer_id, product_id)
    _login_owner(client)

    body = _body(_confirm(client, order_id))
    assert "only a picked-up (Started) order can be returned" in body
    row = _order_row(app, order_id)
    assert row["status"] == "reserved"
    assert row["return_scan_at"] == ""


def test_a_crafted_post_for_another_depots_order_is_refused(app, client):
    customer_id = _customer(app)
    product_id = _product(app)
    _return_ready(app, _started(app, customer_id, product_id, branch="1"))
    owner_order = _started(
        app, customer_id, _product(app, name="Depot 2 Trailer", registration=""), branch="1"
    )
    _return_ready(app, owner_order)

    depot_two = _make_staff(app, "Thabo Mahlangu", ("dashboard", "orders", "scan_return"), branch_ids=[2])
    _login_staff(client, depot_two)
    body = _body(_confirm(client, owner_order))
    assert "belongs to another depot" in body
    assert _order_row(app, owner_order)["status"] == "started"
    assert _order_row(app, owner_order)["return_scan_at"] == ""


def test_an_order_that_is_not_return_ready_surfaces_the_flow_message(app, client):
    customer_id = _customer(app)
    product_id = _product(app)
    order_id = _started(app, customer_id, product_id)  # no finalized invoice, no checklist
    _login_owner(client)

    body = _body(_confirm(client, order_id))
    assert "Finalize the invoice before returning this order" in body
    row = _order_row(app, order_id)
    assert row["status"] == "started"
    assert row["return_scan_at"] == ""


def test_the_candidates_the_service_finds_are_the_ones_the_screen_shows(app, client):
    """DB↔UI parity: the page renders exactly what ``match_open_rentals`` returned."""
    customer_id = _customer(app)
    product_id = _product(app)
    order_id = _started(app, customer_id, product_id)
    _login_owner(client)
    with app.app_context():
        expected = returns.match_open_rentals(
            {"licence_number": TRAILER_PLATE, "registration_number": TRAILER_NATIS}
        )
    body = _body(_scan(client, disc_text=trailer_disc_text()))
    assert len(expected) == 1
    assert expected[0]["order_id"] == order_id
    assert expected[0]["evidence"] in body
    assert expected[0]["matched_on"].replace("_", " ") or expected[0]["matched_on"] in body
