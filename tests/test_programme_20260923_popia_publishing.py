"""T6c — the integration: publishing a notice is what opens the public side.

Before this, the gate read the reviewed document in docs/popia/ and refused while it held
placeholders (decision D11). Now there are two ways a placeholder-free notice can be in force, and
they are the same promise to the customer:

* Sano filled the reviewed document in by hand, or
* the wizard published a generated notice (which cannot contain a placeholder at all).

Nothing else opens the public side: no boolean setting, no branch flag, no separate switch. These
tests pin that, in both directions, and pin that a consent row names the version the customer read.
"""

import os
import tempfile

import pytest

from app import create_app
from app.db import get_db
from app.services import branches as branches_service, consent, popia_wizard, portal_intake


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
    return build_app(db_path)


@pytest.fixture()
def client(app):
    return app.test_client()


def make_branch(app, name="Midrand", portal_enabled=1):
    with app.app_context():
        branch_id = branches_service.create_branch({"name": name, "active": 1})
        db = get_db()
        db.execute("UPDATE branches SET portal_enabled = ? WHERE id = ?", (portal_enabled, branch_id))
        db.commit()
        slug = db.execute("SELECT public_slug FROM branches WHERE id = ?", (branch_id,)).fetchone()["public_slug"]
    return branch_id, slug


def make_product(app, name="6ft Single Axle Flatbed", price=250.0, quantity=3):
    with app.app_context():
        db = get_db()
        cur = db.execute(
            "INSERT INTO products (name, product_type, active, public_visible, price_amount, price_unit,"
            " quantity, created_at) VALUES (?, 'rental', 1, 1, ?, 'day', ?, '2026-09-23T08:00:00')",
            (name, price, quantity),
        )
        db.commit()
        return cur.lastrowid


def publish_a_notice(app):
    """Drive the real engine to a published notice - the same path the wizard uses."""
    with app.app_context():
        popia_wizard.save_step("1", {
            "business_name": "Sano Trailers (Pty) Ltd", "registration_number": "2018/521057/07",
            "vat_number": "4880322591", "trading_name": "Sano Trailers",
            "address": "229 Summit Road, Midrand, 1685", "telephone": "010 221 1723",
            "contact_email": "info@sanotrailers.co.za",
        })
        popia_wizard.save_step("2", {
            "officer_name": "T. Mokoena", "officer_position": "Managing Director",
            "officer_email": "io@sanotrailers.co.za", "officer_telephone": "082 111 2222",
            "officer_registered": "yes", "officer_registration_date": "2026-01-15",
        })
        popia_wizard.save_step("3", {
            "cctv": "yes", "marketing": "no", "id_documents": "yes", "share_info": "no",
            "service_providers": "yes", "card_payments": "yes", "under_18": "no",
            "vehicle_registration": "yes", "credit_checks": "no",
            "cctv_signage": "yes", "cctv_branches": ["Midrand"],
        })
        return popia_wizard.publish(user_id=None)["version"]


BOOKING = {
    "name": "Publishing Proof Client", "phone": "083 444 5566", "email": "publish@example.co.za",
    "popia_consent": "1", "start_date": "2026-10-05", "start_time": "09:00",
    "end_date": "2026-10-07", "end_time": "09:00",
}

REGISTRATION = {
    "name": "Publishing Proof Registrant", "phone": "083 444 5567", "email": "publish2@example.co.za",
    "popia_consent": "1",
}


def booking_payload(app, product_id):
    payload = dict(BOOKING)
    payload.update({"product_id": [str(product_id)], "quantity": ["1"]})
    return payload


# --- shut before anything is published -----------------------------------------------------------

def test_the_public_side_is_shut_until_a_notice_is_published(app, client):
    make_branch(app)
    product_id = make_product(app)
    with app.app_context():
        assert consent.published_notice() is None
        assert portal_intake.registration_is_open() is False

    interim = client.get("/privacy")
    assert interim.status_code == 200
    assert b"being finalised" in interim.data

    booking = client.post("/store/book", data=booking_payload(app, product_id))
    assert booking.status_code in (400, 200)
    assert b"Booking request received" not in booking.data, "a booking must not land while the notice is unpublished"

    _, slug = make_branch(app, "Roodepoort")
    registration = client.post(f"/portal/{slug}/register", data=REGISTRATION)
    assert b"Thank" not in registration.data and b"registered" not in registration.data.lower()

    with app.app_context():
        db = get_db()
        assert db.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"] == 0
        assert db.execute("SELECT COUNT(*) n FROM consent_records").fetchone()["n"] == 0


# --- publishing is what opens it -----------------------------------------------------------------

def test_publishing_opens_the_public_side(app, client):
    make_branch(app)
    product_id = make_product(app)
    version = publish_a_notice(app)

    with app.app_context():
        assert consent.published_notice()["notice_version"] == version
        assert portal_intake.registration_is_open() is True

    page = client.get("/privacy")
    assert page.status_code == 200
    assert version.encode() in page.data
    assert b"being finalised" not in page.data
    assert b"Information Regulator" in page.data

    booking = client.post("/store/book", data=booking_payload(app, product_id), follow_redirects=True)
    assert b"Booking request received" in booking.data


def test_a_consent_row_names_the_published_version(app, client):
    make_branch(app)
    product_id = make_product(app)
    version = publish_a_notice(app)
    client.post("/store/book", data=booking_payload(app, product_id), follow_redirects=True)

    with app.app_context():
        row = get_db().execute(
            "SELECT notice_version, channel FROM consent_records ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None, "a booking while the notice is published must record consent"
    assert row["notice_version"] == version, "the consent must name the wording the customer read"


def test_the_portal_form_opens_with_the_published_notice(app, client):
    _, slug = make_branch(app)
    version = publish_a_notice(app)
    response = client.post(f"/portal/{slug}/register", data=REGISTRATION, follow_redirects=True)
    assert response.status_code == 200
    with app.app_context():
        db = get_db()
        customer = db.execute(
            "SELECT id FROM customers WHERE phone = ?", (REGISTRATION["phone"],)
        ).fetchone()
        assert customer is not None, "registration must create the client once the notice is published"
        consent_row = db.execute(
            "SELECT notice_version FROM consent_records WHERE customer_id = ?", (customer["id"],)
        ).fetchone()
    assert consent_row["notice_version"] == version


# --- and shutting again --------------------------------------------------------------------------

def test_removing_the_published_notice_shuts_it_again(app, client):
    make_branch(app)
    publish_a_notice(app)
    with app.app_context():
        assert portal_intake.registration_is_open() is True
        db = get_db()
        db.execute("DELETE FROM popia_notice_versions")
        db.commit()
        assert consent.published_notice() is None
        assert portal_intake.registration_is_open() is False
    assert b"being finalised" in client.get("/privacy").data


# --- the reviewed document remains the other way in ----------------------------------------------

def test_the_reviewed_document_still_opens_the_side_when_it_is_complete(app, monkeypatch):
    """The original route must keep working: a hand-filled document needs no wizard."""
    from app.services import popia_pack

    monkeypatch.setattr(popia_pack, "document_text", lambda key: "# A hand-filled notice\n\nNo tokens here.\n")
    with app.app_context():
        assert consent.notice_is_publishable() is True
        assert consent.current_notice_version() == consent.PRIVACY_NOTICE_VERSION
