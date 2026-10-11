"""Direct-to-camera main-vehicle scan opened from the self-registration verification checklist.

The verification panel on ``customers.edit`` links "Scan main vehicle disk" straight at the
scanner with two opt-ins on the query string::

    /scan-vehicle?customer_id=<id>&vehicle_role=main&start_scan=1&return_to_verification=1

* ``start_scan=1`` — open the camera on load (the shared module's own start routine), so the
  checklist button is *the scanner* rather than one more tap on the scanner's landing screen. A
  server response cannot grant camera permission, so the module's existing blocked/permission
  fallback — plus the always-visible scan and manual buttons — is the degradation.
* ``return_to_verification=1`` — after the vehicle is saved, hand control back to
  ``customers.edit#pending-registration`` so staff land on the checklist they came from.

``customer_id`` and ``vehicle_role=main`` keep the client selected and pre-answer "make this the
main vehicle?". Every opt-in must survive the decode POST (through the capture form) and the save
POST (through the review form), and a plain scanner visit must behave exactly as before — this file
pins both halves.

Every identifier here is synthetic; the disc payloads come from ``tests/fixtures/disc``.
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
from app.services.customers import create_customer

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "disc"

AUTOSTART_ATTR = 'data-scan-autostart="1"'


def disc_text(name):
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


def login_owner(client):
    with client.application.app_context():
        row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
    return client.post("/login", data={"user_id": str(row["id"]), "password": "admin123"}, follow_redirects=True)


def make_staff(app, name="Sipho Nkosi", modules=("dashboard", "customers", "scan_vehicle"), password="staff1234"):
    with app.app_context():
        user_id, error = create_additional_user(name, password)
        assert error is None, error
        if modules is not None:
            saved_ok, saved = save_user_modules(user_id, list(modules))
            assert saved_ok, saved
    return user_id


def login_staff(client, user_id, password="staff1234"):
    return client.post("/login", data={"user_id": str(user_id), "password": password}, follow_redirects=True)


def raw_save(client, customer_id, registration, **overrides):
    """POST the review form to ``/scan-vehicle/save`` WITHOUT following the redirect."""
    data = {
        "customer_id": str(customer_id) if customer_id else "",
        "registration": registration,
        "make": "TOYOTA",
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
    return client.post("/scan-vehicle/save", data=data, follow_redirects=False)


# ── start_scan: the camera opens on load, and only on the checklist's GET ──────────────────


def test_the_start_scan_optin_marks_the_capture_form_to_autostart(app, client, customer_id):
    login_owner(client)
    response = client.get(
        f"/scan-vehicle?customer_id={customer_id}&vehicle_role=main&start_scan=1&return_to_verification=1"
    )
    assert response.status_code == 200
    body = response.data.decode()
    assert AUTOSTART_ATTR in body
    # The auto-open is wired to the shared module's own start routine, not a second camera path.
    assert "scanner.open()" in body
    # The client stay selected, and the "make main" default is pre-answered on the review form.
    assert f'value="{customer_id}"' in body
    assert "Charmaine Mokoena" in body


def test_a_plain_scan_visit_does_not_autostart_the_camera(app, client):
    login_owner(client)
    body = client.get("/scan-vehicle").data.decode()
    assert AUTOSTART_ATTR not in body
    # Nothing else about the landing screen changed.
    assert "Scan licence disk" in body
    assert "Add details manually" in body
    assert "js/scan-camera.js" in body


def test_the_camera_is_not_rearmed_after_the_decode_post(app, client, customer_id):
    """start_scan is a GET-only opt-in — the decode POST re-renders the same form, so re-arming
    the camera there would reopen it in a loop after every scan."""
    login_owner(client)
    response = client.post(
        f"/scan-vehicle?customer_id={customer_id}&vehicle_role=main&start_scan=1",
        data={"disc_text": disc_text("natis_positional")},
        follow_redirects=True,
    )
    assert response.status_code == 200
    body = response.data.decode()
    assert "Disc read" in body           # the review panel rendered
    assert AUTOSTART_ATTR not in body    # but the camera was NOT re-armed


# ── return_to_verification: survives decode, and lands on the checklist anchor on save ─────


def test_return_to_verification_survives_the_decode_post(app, client, customer_id):
    login_owner(client)
    response = client.post(
        f"/scan-vehicle?customer_id={customer_id}&vehicle_role=main&return_to_verification=1",
        data={"disc_text": disc_text("natis_positional")},
        follow_redirects=True,
    )
    body = response.data.decode()
    assert "Disc read" in body
    # The review form carries the intent forward to the save POST.
    assert 'name="return_to_verification" value="1"' in body


def test_a_plain_rescan_does_not_carry_the_return_flag(app, client, customer_id):
    login_owner(client)
    response = client.post(
        f"/scan-vehicle?customer_id={customer_id}",
        data={"disc_text": disc_text("natis_positional")},
        follow_redirects=True,
    )
    body = response.data.decode()
    assert "Disc read" in body
    assert 'name="return_to_verification"' not in body


def test_save_returns_to_the_verification_anchor_only_when_opted_in(app, client, customer_id):
    login_owner(client)
    opted_in = raw_save(client, customer_id, "ABC123GP", make_main="yes", return_to_verification="1")
    assert opted_in.status_code == 302
    location = opted_in.headers["Location"]
    assert location.endswith(f"/customers/{customer_id}/edit#pending-registration")
    assert "#pending-registration" in location

    # A different plate, ordinary caller: the historical redirect exactly, no fragment.
    ordinary = raw_save(client, customer_id, "DEF456GP", make_main="no")
    assert ordinary.status_code == 302
    assert ordinary.headers["Location"].endswith(f"/customers/{customer_id}/edit")
    assert "#pending-registration" not in ordinary.headers["Location"]


def test_the_opted_in_save_still_records_the_main_vehicle_and_returns_to_the_checklist(app, client, customer_id):
    """End to end: a real disc payload -> main vehicle on the client -> checklist anchor."""
    login_owner(client)
    # The capture screen's decode POST, carrying the checklist context.
    client.post(
        f"/scan-vehicle?customer_id={customer_id}&vehicle_role=main&return_to_verification=1",
        data={"disc_text": disc_text("natis_positional")},
        follow_redirects=True,
    )
    saved = raw_save(
        client,
        customer_id,
        "ABC123GP",
        make_main="yes",
        return_to_verification="1",
        vehicle_role="main",
    )
    assert saved.status_code == 302
    assert saved.headers["Location"].endswith("#pending-registration")
    with app.app_context():
        rows = vehicles.list_vehicles(customer_id)
        assert len(rows) == 1
        assert rows[0]["registration"] == "ABC123GP"
        # It is the client's MAIN vehicle — the checklist's "Main vehicle" step is now satisfied.
        assert vehicles.get_main_vehicle_id(customer_id) is not None
    # ... and the anchor the redirect names is the checklist panel's own id on that page.
    assert client.get(f"/customers/{customer_id}/edit").status_code == 200


# ── main role preserved through capture -> review -> save ──────────────────────────────────


def test_main_role_defaults_the_make_main_choice_through_the_flow(app, client, customer_id):
    login_owner(client)
    # The capture form echoes the role, so it survives the decode POST.
    capture = client.get(f"/scan-vehicle?customer_id={customer_id}&vehicle_role=main&start_scan=1")
    assert b'name="vehicle_role" value="main"' in capture.data

    review = client.post(
        f"/scan-vehicle?customer_id={customer_id}&vehicle_role=main",
        data={"disc_text": disc_text("natis_positional")},
        follow_redirects=True,
    ).data
    # The main-vehicle scan pre-answers "yes" and the role rides into the save form.
    assert b'name="make_main" value="yes" required checked' in review
    assert b'name="vehicle_role" value="main"' in review


# ── the ordinary scanner is untouched, and the module gate still applies ────────────────────


def test_the_plain_scanner_route_behaves_exactly_as_before(app, client, customer_id):
    login_owner(client)
    landing = client.get("/scan-vehicle")
    assert landing.status_code == 200
    body = landing.data.decode()
    assert AUTOSTART_ATTR not in body
    # A manual submit still opens an empty review form with no checklist context.
    manual = client.post(f"/scan-vehicle?customer_id={customer_id}", data={"action": "manual"}, follow_redirects=True)
    assert b"Type the vehicle details and choose the client" in manual.data
    assert b'name="return_to_verification"' not in manual.data


def test_the_optins_do_not_relax_the_scan_module_gate(app, client, customer_id):
    """Branch/module scoping is enforced app-wide, so a staffer without ``scan_vehicle`` is refused
    the direct-to-camera URL just like the plain one."""
    user_id = make_staff(app, name="No Scan Nomsa", modules=("dashboard", "customers"))
    login_staff(client, user_id)
    assert client.get(
        f"/scan-vehicle?customer_id={customer_id}&vehicle_role=main&start_scan=1&return_to_verification=1"
    ).status_code == 403
    assert client.post("/scan-vehicle/save", data={"registration": "ABC123GP"}).status_code == 403


def test_a_direct_to_camera_visit_without_a_client_still_opens_the_camera(app, client):
    """The opt-ins are honoured on their own — the client id is a convenience, not a precondition."""
    login_owner(client)
    body = client.get("/scan-vehicle?start_scan=1&return_to_verification=1").data.decode()
    assert AUTOSTART_ATTR in body
    # No client is preselected, but the manual-entry fallback is still rendered.
    assert "Add details manually" in body
