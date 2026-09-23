"""Programme phase 3 (feature A / A3) — staff scan screen: read a disk, check it, allocate it.

The acceptance list from ``docs/plans/2026-09-23-staff-vehicle-scan.md`` §A3, one test per line:

* staff without the ``scan_vehicle`` module gets 403;
* a good decode plus a chosen client creates the vehicle and it is on that client's record;
* a decode that fails to find a barcode flashes the distinct message (and still offers the form);
* a decoded-but-unparseable payload still lets staff type the fields;
* a registration already owned by another client is refused **with the transfer option**;
* deleting the client still leaves no orphan vehicle rows.

Every fixture payload is the *synthetic* one from A1 (``tests/fixtures/disc/``) — the real disk photo
stays outside the repo (POPIA: it carries a real plate/VIN/engine number).
"""

import io
import os
import tempfile
from pathlib import Path

import pytest

from app import create_app
from app.db import get_db
from app.services import vehicles
from app.services.access import create_additional_user, save_user_modules
from app.services.customers import create_customer, delete_customer, search_customers

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "disc"


def disc_text(name: str) -> str:
    return (FIXTURE_DIR / f"{name}.txt").read_text(encoding="utf-8")


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


@pytest.fixture()
def customer_id(app):
    with app.app_context():
        return create_customer({"name": "Charmaine Mokoena", "customer_type": "individual", "phone": "0821234567"})


@pytest.fixture()
def other_customer_id(app):
    with app.app_context():
        return create_customer({"name": "Pieter van Wyk", "customer_type": "individual", "phone": "0837654321"})


def login_owner(client):
    with client.application.app_context():
        row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
    return client.post("/login", data={"user_id": str(row["id"]), "password": "admin123"}, follow_redirects=True)


def make_staff(app, name="Sipho Nkosi", modules=("dashboard", "customers", "scan_vehicle"), password="staff1234"):
    """An additional account with exactly the modules given (None = inherit the shared default)."""
    with app.app_context():
        user_id, error = create_additional_user(name, password)
        assert error is None, error
        if modules is not None:
            saved_ok, saved = save_user_modules(user_id, list(modules))
            assert saved_ok, saved
    return user_id


def login_staff(client, user_id, password="staff1234"):
    return client.post("/login", data={"user_id": str(user_id), "password": password}, follow_redirects=True)


def synthetic_disc_png(payload: str) -> bytes:
    """A real PDF417 of *our* text, generated locally — an image, not a stand-in for a photo."""
    zxingcpp = pytest.importorskip("zxingcpp")
    from PIL import Image

    barcode = zxingcpp.create_barcode(payload, zxingcpp.BarcodeFormat.PDF417)
    zxing_image = zxingcpp.write_barcode_to_image(barcode)
    height, width = zxing_image.shape
    pil = Image.frombuffer("L", (width, height), zxing_image, "raw", "L", 0, 1)
    buffer = io.BytesIO()
    pil.convert("RGB").resize((width * 2, height * 2)).save(buffer, format="PNG")
    return buffer.getvalue()


def blank_png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (400, 300), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def scan_text(client, text, **extra):
    data = {"action": "decode", "disc_text": text}
    data.update(extra)
    return client.post("/scan-vehicle", data=data, follow_redirects=True)


def save_scan(client, customer_id, registration, **overrides):
    data = {
        "customer_id": str(customer_id) if customer_id else "",
        "registration": registration,
        "make": "TOYOTA",
        "model": "HILUX 2.4 GD-6",
        "vin": "AHTFR22G10L123456",
        "engine_number": "2GD1234567",
        "colour": "WHITE",
        "licence_number": "T9876543210X",
        "registration_number": "ZZ1234Z",
        "licence_disk_expiry": "2027-03-31",
        "source": "scan",
        "tare_kg": "",
        "gvm_kg": "",
    }
    data.update(overrides)
    return client.post("/scan-vehicle/save", data=data, follow_redirects=True)


# ── access ──────────────────────────────────────────────────────────────────


def test_the_scan_page_opens_for_a_signed_in_staff_member(app, client):
    user_id = make_staff(app)
    login_staff(client, user_id)
    response = client.get("/scan-vehicle")
    assert response.status_code == 200
    assert b"Read the disk" in response.data
    assert b"Scan a vehicle disk" in response.data  # the nav entry, next to "Scan a barcode"


def test_staff_without_the_scan_module_gets_403(app, client):
    user_id = make_staff(app, name="No Scan Nomsa", modules=("dashboard", "customers"))
    login_staff(client, user_id)
    assert client.get("/scan-vehicle").status_code == 403
    assert client.post("/scan-vehicle/save", data={"registration": "ABC123GP"}).status_code == 403
    assert client.get("/api/customers/search?q=cha").status_code == 403
    # ... and the module does not appear in their navigation
    assert b"Scan a vehicle disk" not in client.get("/dashboard").data


def test_the_client_vehicles_feed_only_needs_the_customers_module(app, client, customer_id):
    """The panel feed shows what is already on the client's record, so the client page's own
    permission covers it — a staff member with 'customers' but no scanner still sees the panel."""
    user_id = make_staff(app, name="Read Only Rita", modules=("dashboard", "customers"))
    login_staff(client, user_id)
    response = client.get(f"/customers/{customer_id}/vehicles")
    assert response.status_code == 200
    assert response.get_json()["count"] == 0


# ── reading a disk ──────────────────────────────────────────────────────────


def test_pasting_the_decoded_text_fills_the_review_form(app, client):
    login_owner(client)
    response = scan_text(client, disc_text("labelvalue"))
    assert response.status_code == 200
    assert b"Disc read" in response.data
    assert b'value="ABC 123 GP"' in response.data          # the number plate
    assert b'value="TOYOTA"' in response.data
    assert b'value="AHTFR22G10L123456"' in response.data
    assert b'value="1890.0"' in response.data               # tare from the disk
    assert b"Raw barcode text" in response.data


def test_a_photographed_disk_is_read_in_memory_and_fills_the_form(app, client):
    login_owner(client)
    response = client.post(
        "/scan-vehicle",
        data={"action": "decode", "disk_image": (io.BytesIO(synthetic_disc_png(disc_text("labelvalue"))), "disc.png")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Disc read" in response.data
    assert b'value="ABC 123 GP"' in response.data
    assert b'value="2GD1234567"' in response.data           # engine number off the barcode


def test_a_photo_without_a_barcode_flashes_the_distinct_message_and_still_offers_the_form(app, client):
    login_owner(client)
    response = client.post(
        "/scan-vehicle",
        data={"action": "decode", "disk_image": (io.BytesIO(blank_png()), "disc.png")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"No barcode found on that photo" in response.data
    assert b"Type the details instead" in response.data     # the capture form is back
    assert b"Number plate" in response.data                 # ... and the review form is offered


def test_a_barcode_with_no_vehicle_fields_still_lets_staff_type_the_fields(app, client):
    login_owner(client)
    payload = "THIS IS NOT A LICENCE DISK - please rescan @@##$$"
    response = client.post(
        "/scan-vehicle",
        data={"action": "decode", "disk_image": (io.BytesIO(synthetic_disc_png(payload)), "disc.png")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"holds no vehicle fields" in response.data
    assert payload.encode() in response.data                # the raw text is shown for typing
    assert b'name="registration"' in response.data


def test_a_file_that_is_not_an_image_is_named_as_such(app, client):
    login_owner(client)
    response = client.post(
        "/scan-vehicle",
        data={"action": "decode", "disk_image": (io.BytesIO(b"this is definitely not an image"), "notes.txt")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"not an image" in response.data


def test_an_oversize_photo_is_refused_without_reading_it(app, client):
    login_owner(client)
    response = client.post(
        "/scan-vehicle",
        data={"action": "decode", "disk_image": (io.BytesIO(b"x" * (9 * 1024 * 1024)), "huge.png")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"larger than 8 MB" in response.data


def test_submitting_nothing_asks_for_a_scan_or_the_fields(app, client):
    login_owner(client)
    response = scan_text(client, "")
    assert b"Photograph the disc, or paste the barcode text" in response.data


def test_the_manual_button_opens_an_empty_review_form(app, client):
    login_owner(client)
    response = client.post("/scan-vehicle", data={"action": "manual"}, follow_redirects=True)
    assert b"Type the vehicle details and choose the client" in response.data
    assert b'name="registration"' in response.data
    assert b'value="ABC 123 GP"' not in response.data


def test_the_page_preselects_the_client_it_was_opened_from(app, client, customer_id):
    login_owner(client)
    response = client.get(f"/scan-vehicle?customer_id={customer_id}")
    assert response.status_code == 200
    assert b"Charmaine Mokoena" in response.data
    assert f'value="{customer_id}"'.encode() in response.data


# ── allocating: save, refusal, transfer ─────────────────────────────────────


def test_saving_allocates_the_vehicle_to_the_chosen_client(app, client, customer_id):
    login_owner(client)
    response = save_scan(client, customer_id, "ABC123GP")
    assert response.status_code == 200  # followed the redirect to the client page
    assert b"allocated to Charmaine Mokoena" in response.data
    with app.app_context():
        rows = vehicles.list_vehicles(customer_id)
    assert len(rows) == 1
    assert rows[0]["registration"] == "ABC123GP"
    assert rows[0]["registration_number"] == "ZZ1234Z"
    assert rows[0]["licence_number"] == "T9876543210X"
    assert rows[0]["source"] == "scan"
    # ... and it is on that client's page feed (the panel A4 renders)
    feed = client.get(f"/customers/{customer_id}/vehicles").get_json()
    assert feed["count"] == 1
    assert feed["vehicles"][0]["registration"] == "ABC123GP"


def test_a_disk_that_carries_no_masses_stores_them_blank_not_zero(app, client, customer_id):
    """The modern NaTIS payload has no tare/GVM at all — the column must stay NULL."""
    login_owner(client)
    scan_text(client, disc_text("natis_positional"))
    save_scan(client, customer_id, "ABC123GP", registration_number="ZZ1234Z")
    with app.app_context():
        row = vehicles.list_vehicles(customer_id)[0]
    assert row["tare_kg"] is None and row["gvm_kg"] is None


def test_saving_without_picking_a_client_is_refused(app, client):
    login_owner(client)
    before = None
    with app.app_context():
        before = get_db().execute("SELECT COUNT(*) AS c FROM vehicles").fetchone()["c"]
    response = save_scan(client, None, "ABC123GP")
    assert b"Choose the client this vehicle belongs to" in response.data
    with app.app_context():
        after = get_db().execute("SELECT COUNT(*) AS c FROM vehicles").fetchone()["c"]
    assert after == before == 0


def test_a_plate_owned_by_another_client_is_refused_with_the_transfer_option(app, client, customer_id, other_customer_id):
    login_owner(client)
    save_scan(client, customer_id, "ABC123GP")
    response = save_scan(client, other_customer_id, "ABC123GP")
    body = response.data.decode()
    assert "already recorded for Charmaine Mokoena" in body
    assert 'name="transfer"' in body                        # the explicit transfer option is offered
    assert "Transfer ABC123GP from Charmaine Mokoena" in body
    with app.app_context():
        assert len(vehicles.list_vehicles(customer_id)) == 1
        assert vehicles.list_vehicles(other_customer_id) == []   # nothing was created or moved


def test_the_transfer_action_moves_the_vehicle_and_applies_the_new_disk_details(app, client, customer_id, other_customer_id):
    login_owner(client)
    save_scan(client, customer_id, "ABC123GP", colour="WHITE")
    response = save_scan(client, other_customer_id, "ABC123GP", transfer="1", colour="SILVER")
    assert b"transferred to Pieter van Wyk" in response.data
    with app.app_context():
        assert vehicles.list_vehicles(customer_id) == []
        moved = vehicles.list_vehicles(other_customer_id)
        assert len(moved) == 1                               # moved, never duplicated
        assert moved[0]["customer_id"] == other_customer_id
        assert moved[0]["colour"] == "SILVER"                # the freshly scanned disk won


def test_a_transfer_with_nothing_to_transfer_creates_no_record(app, client, customer_id):
    login_owner(client)
    response = save_scan(client, customer_id, "NOPE123", transfer="1")
    assert b"nothing to transfer" in response.data
    with app.app_context():
        assert vehicles.list_vehicles(customer_id) == []


def test_a_second_save_of_the_same_plate_does_not_create_a_second_row(app, client, customer_id):
    login_owner(client)
    save_scan(client, customer_id, "ABC123GP")
    response = save_scan(client, customer_id, "ABC123GP")
    assert b"already recorded for Charmaine Mokoena" in response.data
    with app.app_context():
        assert len(vehicles.list_vehicles(customer_id)) == 1


# ── edit / remove / cascade ─────────────────────────────────────────────────


def test_editing_and_removing_a_vehicle_leaves_the_client_intact(app, client, customer_id):
    login_owner(client)
    save_scan(client, customer_id, "ABC123GP")
    with app.app_context():
        vehicle_id = vehicles.list_vehicles(customer_id)[0]["id"]
        before = dict(get_db().execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone())

    edited = client.post(f"/vehicles/{vehicle_id}/edit", data={"registration": "ABC123GP", "tare_kg": "1845"}, follow_redirects=True)
    assert b"Vehicle saved" in edited.data
    with app.app_context():
        assert vehicles.get_vehicle(vehicle_id)["tare_kg"] == 1845.0

    removed = client.post(f"/vehicles/{vehicle_id}/delete", follow_redirects=True)
    assert b"Vehicle removed" in removed.data
    with app.app_context():
        assert vehicles.get_vehicle(vehicle_id) is None
        after = dict(get_db().execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone())
    assert before == after


def test_deleting_the_client_leaves_no_orphan_vehicles(app, client, customer_id):
    login_owner(client)
    save_scan(client, customer_id, "ABC123GP")
    with app.app_context():
        assert delete_customer(customer_id) is True
        orphans = get_db().execute(
            "SELECT COUNT(*) AS c FROM vehicles WHERE customer_id NOT IN (SELECT id FROM customers)"
        ).fetchone()["c"]
    assert orphans == 0


def test_removing_a_vehicle_that_is_already_gone_is_handled(app, client):
    login_owner(client)
    response = client.post("/vehicles/999999/delete", follow_redirects=True)
    assert b"no longer on file" in response.data


# ── the allocate-step typeahead ─────────────────────────────────────────────


def test_the_client_search_returns_name_and_phone_only(app, client, customer_id, other_customer_id):
    login_owner(client)
    payload = client.get("/api/customers/search?q=char").get_json()
    assert [row["name"] for row in payload["customers"]] == ["Charmaine Mokoena"]
    assert set(payload["customers"][0]) == {"id", "name", "phone"}
    by_phone = client.get("/api/customers/search?q=0837").get_json()
    assert [row["name"] for row in by_phone["customers"]] == ["Pieter van Wyk"]
    assert client.get("/api/customers/search?q=").get_json()["customers"] == []


def test_the_search_service_never_returns_an_email(app, customer_id):
    with app.app_context():
        get_db().execute("UPDATE customers SET email = 'charmaine@example.co.za' WHERE id = ?", (customer_id,))
        get_db().commit()
        rows = search_customers("charmaine")
    assert rows and "email" not in rows[0]


# ── the mapping that A1/A2 flagged (parser key names vs column names) ───────


def test_fields_from_disc_maps_the_plate_and_the_disc_licence_number_separately(app):
    from app.services import vehicle_disk as disk

    parsed = disk.parse_disc_text(disc_text("natis_positional"))
    fields = vehicles.fields_from_disc(parsed)
    assert fields["registration"] == "ABC123GP"              # the plate
    assert fields["registration_number"] == "ZZ1234Z"        # the NaTIS registration number
    assert fields["licence_number"] == "T9876543210X"        # the disk's own licence number
    assert fields["source"] == "scan"
    assert fields["tare_kg"] == "" and fields["gvm_kg"] == ""   # blank, never "0"
