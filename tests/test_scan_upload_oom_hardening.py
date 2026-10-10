"""Scan-screen upload hardening — the image-upload OOM surface is gone.

The two scan screens used to accept a phone photo and decode the PDF417 on the server: an
eleven-variant image ladder that expanded a 12 MP photo to hundreds of megabytes of PIL buffers,
which is what took the 512 Mi Render instance down (correlated with ``/scan-return``). The camera
now decodes the barcode **in the browser** and posts the decoded text, so the server must never see
(and never decode) an image.

These tests pin that contract at the level that matters:

* no scan route references the image decoder or ``request.files`` at all;
* a multipart (photo) POST is refused from the request **headers**, before the body is parsed;
* the refusal is a readable manual-entry/retry page — never an error page, never a decode;
* an oversized body and an oversized "barcode text" are both refused (bounded parsing);
* auth and module gating are unchanged;
* the manual fallbacks work end-to-end and nothing is auto-saved after a barcode read.

Every identifier here is synthetic (the A1 fixtures). The real disc photo stays outside the repo.
"""

import io
import os
import tempfile
from pathlib import Path

import pytest

from app import create_app
from app.db import get_db
from app.routes import vehicles as vehicles_route
from app.services import returns, vehicle_disk
from app.services.access import create_additional_user, save_user_modules
from app.services.customers import create_customer
from app.services.orders import create_order, transition_order
from app.services.products import create_product
from app.services.vehicles import list_vehicles

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "disc"
ROUTE_SOURCES = (
    Path(vehicles_route.__file__),
    Path(__file__).resolve().parent.parent / "app" / "routes" / "returns.py",
)

TRAILER_PLATE = "ABC123GP"
TRAILER_NATIS = "ZZ1234Z"
TRAILER_DISC_LICENCE = "T9876543210X"


def trailer_disc_text() -> str:
    return (FIXTURE_DIR / "natis_positional.txt").read_text(encoding="utf-8")


def label_value_text() -> str:
    return (FIXTURE_DIR / "labelvalue.txt").read_text(encoding="utf-8")


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


def _login_owner(client):
    with client.application.app_context():
        row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
    return client.post("/login", data={"user_id": str(row["id"]), "password": "admin123"}, follow_redirects=True)


def _make_staff(app, name, modules, password="staff1234"):
    with app.app_context():
        user_id, error = create_additional_user(name, password)
        assert error is None, error
        ok, saved = save_user_modules(user_id, list(modules))
        assert ok, saved
    return user_id


def _login_staff(client, user_id, password="staff1234"):
    return client.post("/login", data={"user_id": str(user_id), "password": password}, follow_redirects=True)


def _customer(app, name="Charmaine Mokoena", phone="0821234567"):
    with app.app_context():
        return create_customer({"name": name, "customer_type": "individual", "phone": phone})


def _started_order(app, customer_id, registration=TRAILER_PLATE):
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
        "registration": registration,
        "licence_number": TRAILER_DISC_LICENCE,
        "registration_number": TRAILER_NATIS,
    }
    with app.app_context():
        product_id = create_product(form)
        order_id = create_order(
            {
                "customer_id": str(customer_id),
                "product_id": str(product_id),
                "quantity": "1",
                "start_date": "2026-07-01",
                "start_time": "09:00",
                "end_date": "2026-07-03",
                "end_time": "15:00",
                "collect_branch_id": "1",
            },
            notify=False,
        )
        transition_order(order_id, "start")
    return order_id


def _order_status(app, order_id):
    with app.app_context():
        return get_db().execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()["status"]


def _forbid_decoding(monkeypatch):
    """Any call to the image decoder during a route test is a hard failure."""

    def _boom(*args, **kwargs):  # pragma: no cover - only fires on a regression
        raise AssertionError("a scan route invoked the server-side image decoder")

    monkeypatch.setattr(vehicle_disk, "decode_disc_image", _boom)
    monkeypatch.setattr(vehicle_disk, "decode_and_parse", _boom)
    monkeypatch.setattr(vehicle_disk, "decode_payloads", _boom)


# ── the static contract: no route touches an image decoder or request.files ──


def test_no_scan_route_references_the_image_decoder_or_request_files():
    """The strongest form of the guarantee: the routes cannot reach the image pipeline at all."""
    for path in ROUTE_SOURCES:
        source = Path(path).read_text(encoding="utf-8")
        assert "decode_disc_image" not in source, path
        assert "decode_and_parse" not in source, path
        assert "decode_payloads" not in source, path
        assert "request.files" not in source, path
        assert "disk_image" not in source, path


# ── the guard reads headers only ─────────────────────────────────────────────


class _HeadersOnlyRequest:
    """A request object that only exposes the headers, to prove the guard never reads the body."""

    def __init__(self, mimetype, content_length=None):
        self.mimetype = mimetype
        self.content_length = content_length

    def __getattr__(self, name):  # only fires for attributes the guard should not touch
        raise AssertionError(f"scan_request_rejection touched request.{name}")


def test_the_upload_guard_reads_only_the_request_headers():
    assert vehicles_route.scan_request_rejection(_HeadersOnlyRequest("multipart/form-data", 16)) is not None
    # A normal urlencoded text post passes untouched.
    assert vehicles_route.scan_request_rejection(_HeadersOnlyRequest("application/x-www-form-urlencoded", 16)) is None
    # An oversized body is refused on the declared length alone.
    big = vehicles_route.MAX_SCAN_REQUEST_BYTES + 1
    assert vehicles_route.scan_request_rejection(_HeadersOnlyRequest("application/x-www-form-urlencoded", big)) is not None
    # A chunked/unknown-length stream must not evade the body bound.
    assert vehicles_route.scan_request_rejection(_HeadersOnlyRequest("application/x-www-form-urlencoded", None)) is not None


@pytest.mark.parametrize('url', ['/scan-vehicle', '/scan-return'])
def test_chunked_scan_rejected_before_reading_stream(app, client, url):
    _login_owner(client)
    class UnreadableStream:
        def read(self, *args):
            raise AssertionError('unbounded request body was read')
        def readline(self, *args):
            raise AssertionError('unbounded request body was read')
    response = client.open(url, method='POST', environ_overrides={
        'CONTENT_TYPE': 'application/x-www-form-urlencoded',
        'CONTENT_LENGTH': '', 'wsgi.input_terminated': True,
        'wsgi.input': UnreadableStream(),
    })
    assert response.status_code == 200
    assert b'far too long to be a licence disk' in response.data


# ── a photo POST is refused on both screens, without decoding ────────────────


def test_scan_vehicle_photo_upload_is_refused_without_decoding(app, client, monkeypatch):
    _forbid_decoding(monkeypatch)
    _login_owner(client)
    response = client.post(
        "/scan-vehicle",
        data={"action": "decode", "disk_image": (io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 4096), "disc.png")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    body = response.data
    assert b"Photo uploads are not accepted" in body
    assert b'name="registration"' in body  # manual review form still offered


def test_scan_return_photo_upload_is_refused_without_decoding(app, client, monkeypatch):
    _forbid_decoding(monkeypatch)
    _login_owner(client)
    response = client.post(
        "/scan-return",
        data={"disk_image": (io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 4096), "disc.png")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Photo uploads are not accepted" in response.data
    assert b'name="plate"' in response.data  # manual plate box still offered


# ── bounded parsing: oversized body / oversized text are refused ─────────────


def test_an_oversized_scan_body_is_refused_on_both_screens(app, client):
    _login_owner(client)
    blob = "x" * (vehicles_route.MAX_SCAN_REQUEST_BYTES + 5000)
    for url in ("/scan-vehicle", "/scan-return"):
        response = client.post(url, data={"disc_text": blob}, follow_redirects=True)
        assert response.status_code == 200, url
        assert b"far too long to be a licence disk" in response.data, url


def test_oversized_disc_text_is_refused_on_both_screens(app, client):
    _login_owner(client)
    text = "8" * (vehicles_route.MAX_DISC_TEXT_CHARS + 1)
    for url in ("/scan-vehicle", "/scan-return"):
        response = client.post(url, data={"disc_text": text}, follow_redirects=True)
        assert response.status_code == 200, url
        assert b"far too long to be a licence disk" in response.data, url


# ── auth and module gating unchanged ─────────────────────────────────────────


def test_anonymous_scan_posts_are_redirected_to_login(app, client):
    for url in ("/scan-vehicle", "/scan-return"):
        response = client.post(url, data={"disc_text": "ABC 123 GP"})
        assert response.status_code in (301, 302, 303), url
        assert "/login" in response.headers.get("Location", ""), url


def test_module_gating_still_403s_the_scan_posts(app, client):
    staff = _make_staff(app, "No Scan Nomsa", ("dashboard", "customers"))
    _login_staff(client, staff)
    assert client.post("/scan-vehicle", data={"disc_text": "ABC 123 GP"}).status_code == 403
    assert client.post("/scan-return", data={"disc_text": "ABC 123 GP"}).status_code == 403


# ── manual fallbacks work, and nothing is auto-saved ─────────────────────────


def test_manual_vehicle_review_renders_then_saves(app, client):
    _login_owner(client)
    customer_id = _customer(app)

    # 1. The manual action opens an empty review form (no scan needed).
    opened = client.post("/scan-vehicle", data={"action": "manual", "customer_id": str(customer_id)}, follow_redirects=True)
    assert opened.status_code == 200
    assert b"Type the vehicle details and choose the client" in opened.data
    assert b'name="registration"' in opened.data

    # 2. Saving the typed-in vehicle allocates it to the chosen client.
    saved = client.post(
        "/scan-vehicle/save",
        data={
            "customer_id": str(customer_id),
            "registration": "ABC123GP",
            "make": "TOYOTA",
            "source": "scan",
            "tare_kg": "",
            "gvm_kg": "",
        },
        follow_redirects=True,
    )
    assert b"allocated to Charmaine Mokoena" in saved.data
    with app.app_context():
        rows = list_vehicles(customer_id)
    assert len(rows) == 1 and rows[0]["registration"] == "ABC123GP"


def test_manual_return_plate_lookup_finds_the_rental(app, client):
    _login_owner(client)
    customer_id = _customer(app)
    order_id = _started_order(app, customer_id)

    body = client.post("/scan-return", data={"plate": "abc 123 gp"}, follow_redirects=True).get_data(as_text=True)
    assert "Mark returned" in " ".join(body.split())
    assert _order_status(app, order_id) == "started"


def test_a_decoded_barcode_text_never_auto_saves_a_vehicle(app, client):
    _login_owner(client)
    customer_id = _customer(app)

    response = client.post(
        "/scan-vehicle",
        data={"disc_text": trailer_disc_text(), "customer_id": str(customer_id)},
        follow_redirects=True,
    )
    assert b"Disc read" in response.data
    with app.app_context():
        assert list_vehicles(customer_id) == []  # nothing saved until staff submit the review form


def test_a_decoded_barcode_text_never_auto_returns_an_order(app, client):
    _login_owner(client)
    customer_id = _customer(app)
    order_id = _started_order(app, customer_id)

    response = client.post("/scan-return", data={"disc_text": trailer_disc_text()}, follow_redirects=True)
    assert b"Mark returned" in response.data
    assert _order_status(app, order_id) == "started"  # the scan only offers; the confirm returns


# ── the genuine disc grammar still round-trips through the text path ─────────


def test_both_generation_grammars_still_parse_from_the_posted_text(app, client):
    """The caps are generous enough that a real modern *and* legacy payload are untouched."""
    _login_owner(client)

    modern = client.post("/scan-vehicle", data={"disc_text": trailer_disc_text()}, follow_redirects=True)
    assert b'value="ABC123GP"' in modern.data  # number plate, modern 148-char positional record
    assert b'value="ZZ1234Z"' in modern.data

    legacy = client.post("/scan-vehicle", data={"disc_text": label_value_text()}, follow_redirects=True)
    assert b'value="ABC 123 GP"' in legacy.data  # number plate, legacy label-value record
    assert b'value="TOYOTA"' in legacy.data


def test_the_parser_still_reports_the_rows_the_matcher_will_use(app):
    """A text parse handed to the return matcher is unchanged (DB↔parser contract intact)."""
    with app.app_context():
        parsed = vehicle_disk.parse_disc_text(trailer_disc_text())
    assert parsed["licence_number"] == TRAILER_PLATE
    assert parsed["registration_number"] == TRAILER_NATIS
    assert parsed["disc_licence_number"] == TRAILER_DISC_LICENCE
    assert returns.scanned_identifiers(parsed)["registration"] == TRAILER_PLATE
