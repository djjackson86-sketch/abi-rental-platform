"""Programme phase W1 (feature P) — the POPIA setup wizard, routes + template.

This file covers the *UI half* built on the committed engine
(``test_programme_20260923_popia_wizard.py``). What it pins:

* every ``/settings/popia`` route is main profile only — a staff account gets
  403 on every step, every save, the draft PDF and publish, and never sees the
  POPIA entry in Settings;
* the owner sees each step, with step 1 pre-filled from what the app already
  knows and a fact the app does not know (the trading name) blank, never
  invented;
* saving a step persists it (a closed tab loses nothing) and the progress count
  moves;
* the step-2 "Not yet" answer blocks publication and the refusal says why;
* the nine Step-3 answers round-trip, including "not sure" (blank);
* step 4's preview is exactly ``build_notice()`` and its left-out list matches
  ``left_out()``;
* step 5 surfaces the publish refusal naming the missing fields while the
  officer is unregistered or a required question is unanswered, and on success
  stores the version and the content hash.

No test needs a network; nothing here reads or writes the real disc sample.
"""

import html as _html
import json
import os
import re
import tempfile

import pytest

from app import create_app
from app.db import get_db
from app.services import popia_wizard
from app.services.access import create_additional_user


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


def login_owner(client):
    with client.application.app_context():
        row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
    return client.post(
        "/login", data={"user_id": str(row["id"]), "password": "admin123"}, follow_redirects=True
    )


def make_staff(app, name, password="staff1234"):
    with app.app_context():
        user_id, error = create_additional_user(name, password)
        assert error is None, error
    return user_id


def login_staff(client, user_id, password="staff1234"):
    return client.post("/login", data={"user_id": str(user_id), "password": password}, follow_redirects=True)


def text_of(response):
    return response.get_data(as_text=True)


def unescaped(response):
    """The page body with HTML entities (autoescaped quotes) restored, so a raw
    engine string can be compared against what a browser would render."""
    return _html.unescape(text_of(response))


def seed_company_settings(app):
    """Give the app's company_settings the facts the pre-fill should surface."""
    with app.app_context():
        get_db().execute(
            "UPDATE company_settings SET company_name=?, company_reg_no=?, vat_number=?, phone=?, email=?, "
            "address_line1=?, address_line2=?, city=?, province=?, postcode=?, country=? WHERE id=1",
            (
                "Sano Trailers",
                "2018/521057/07",
                "4880322591",
                "010 221 1723",
                "info@sanotrailers.co.za",
                "229 Summit Road",
                "",
                "Midrand",
                "Gauteng",
                "",
                "South Africa",
            ),
        )
        get_db().commit()


def save_step1(client):
    return client.post("/settings/popia/step/1", data={
        "business_name": "Sano Trailers (Pty) Ltd",
        "registration_number": "2018/521057/07",
        "vat_number": "4880322591",
        "trading_name": "",
        "address": "229 Summit Road, Midrand, Gauteng, South Africa",
        "telephone": "010 221 1723",
        "contact_email": "info@sanotrailers.co.za",
    }, follow_redirects=True)


def save_step2(client, registered="yes", registration_date="2026-09-23"):
    return client.post("/settings/popia/step/2", data={
        "officer_name": "Donovan Jackson",
        "officer_position": "Owner",
        "officer_email": "donovan@sanotrailers.co.za",
        "officer_telephone": "010 221 1723",
        "officer_registered": registered,
        "officer_registration_date": registration_date,
        "officer_registration_ref": "",
    }, follow_redirects=True)


def save_step3(client, **overrides):
    data = {question["key"]: "yes" for question in popia_wizard.QUESTIONS}
    data["cctv_signage"] = "yes"
    data.update(overrides)
    return client.post("/settings/popia/step/3", data=data, follow_redirects=True)


def save_all(app, client, **step3_overrides):
    """Steps 1-3 fully saved, so only the requested overrides remain open."""
    save_step1(client)
    save_step2(client)
    save_step3(client, **step3_overrides)


# ---------------------------------------------------------------------------
# Main-profile-only access
# ---------------------------------------------------------------------------

WIZARD_GET_PATHS = [
    "/settings/popia",
    "/settings/popia/1",
    "/settings/popia/2",
    "/settings/popia/3",
    "/settings/popia/4",
    "/settings/popia/5",
    "/settings/popia/draft.pdf",
]
WIZARD_POST_PATHS = [
    "/settings/popia/step/1",
    "/settings/popia/step/2",
    "/settings/popia/step/3",
    "/settings/popia/publish",
]


def test_staff_gets_403_on_every_wizard_route_and_no_settings_entry(app, client):
    staff = make_staff(app, "Counter Staff")
    login_staff(client, staff)

    for path in WIZARD_GET_PATHS:
        assert client.get(path).status_code == 403, path
    for path in WIZARD_POST_PATHS:
        assert client.post(path).status_code == 403, path

    # The Settings tab row must not render a POPIA entry for staff.
    body = text_of(client.get("/settings/general"))
    assert "/settings/popia" not in body
    assert "POPIA" not in body


def test_unknown_step_404s(app, client):
    login_owner(client)
    assert client.get("/settings/popia/9").status_code == 404
    assert client.get("/settings/popia/0").status_code == 404


# ---------------------------------------------------------------------------
# Step 1 pre-fill: known facts filled, unknown facts blank
# ---------------------------------------------------------------------------


def test_owner_sees_step1_prefilled_and_unknown_fields_blank(app, client):
    seed_company_settings(app)
    login_owner(client)
    body = text_of(client.get("/settings/popia"))
    assert "Who you are" in body
    assert "0" in body and "of 5 steps done" in body  # the progress rail
    assert 'value="Sano Trailers"' in body
    assert 'name="business_name" value="Sano Trailers"' in body
    assert 'name="registration_number" value="2018/521057/07"' in body
    assert 'name="vat_number" value="4880322591"' in body
    assert "229 Summit Road" in body and "Midrand" in body
    # The app holds no trading name — it arrives blank, never invented.
    assert 'name="trading_name" value=""' in body


def test_owner_sees_every_step(app, client):
    login_owner(client)
    for step, heading in (
        (1, "Who you are"),
        (2, "Your Information Officer"),
        (3, "How you work"),
        (4, "Check it"),
        (5, "Publish"),
    ):
        body = text_of(client.get(f"/settings/popia/{step}"))
        assert heading in body, step
    # Step 3 renders all nine questions and pre-sets the vehicle-registration answer to yes
    # (the disc scanner already records it).
    body = unescaped(client.get("/settings/popia/3"))
    for question in popia_wizard.QUESTIONS:
        assert question["label"] in body
    assert 'name="vehicle_registration" value="yes" checked' in body


# ---------------------------------------------------------------------------
# Saving a step persists it and moves the progress count
# ---------------------------------------------------------------------------


def test_saving_step1_persists_it_and_moves_the_progress_count(app, client):
    login_owner(client)
    response = save_step1(client)
    assert "Step 1 saved" in text_of(response)
    with app.app_context():
        st = popia_wizard.state()
        assert st["business_name"] == "Sano Trailers (Pty) Ltd"
        assert popia_wizard.progress()["completed"] == 1
    # A fresh GET (like re-opening the tab) shows the saved value, not the pre-fill.
    body = text_of(client.get("/settings/popia/1"))
    assert 'value="Sano Trailers (Pty) Ltd"' in body


# ---------------------------------------------------------------------------
# Step 2: the "Not yet" answer blocks publication and says why
# ---------------------------------------------------------------------------


def test_step2_not_yet_blocks_publication_and_says_why(app, client):
    login_owner(client)
    save_step1(client)
    save_step2(client, registered="not_yet", registration_date="")
    save_step3(client)

    response = client.post("/settings/popia/publish", follow_redirects=True)
    body = text_of(response)
    assert "cannot be published" in body
    assert "officer_registered" in body  # the flash names the exact field
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) AS c FROM popia_notice_versions").fetchone()["c"] == 0
        assert popia_wizard.state()["published"] is False


def test_step2_page_shows_the_registration_link_and_consequence(app, client):
    login_owner(client)
    body = text_of(client.get("/settings/popia/2"))
    assert "https://eservices.inforegulator.org.za" in body
    assert "s55(2)" in body
    assert "free" in body


# ---------------------------------------------------------------------------
# Step 3: the nine answers round-trip
# ---------------------------------------------------------------------------


def test_the_nine_answers_round_trip_through_step3(app, client):
    login_owner(client)
    answers = {
        "cctv": "yes",
        "marketing": "no",
        "id_documents": "yes",
        "share_info": "no",
        "service_providers": "yes",
        "card_payments": "no",
        "under_18": "",  # "not sure" = blank = not answered
        "vehicle_registration": "yes",
        "credit_checks": "no",
    }
    data = dict(answers)
    data["cctv_signage"] = "yes"
    client.post(
        "/settings/popia/step/3",
        data={**data, "cctv_branches": ["Midrand", "Roodepoort"]},
        follow_redirects=True,
    )
    with app.app_context():
        st = popia_wizard.state()
        for key, value in answers.items():
            assert st[key] == value, key
        assert json.loads(st["cctv_branches_json"]) == ["Midrand", "Roodepoort"]
        assert st["cctv_signage"] == "yes"


# ---------------------------------------------------------------------------
# Step 4: the preview matches build_notice and the left-out list matches
# ---------------------------------------------------------------------------


def test_step4_preview_matches_build_notice_and_left_out_list_matches(app, client):
    login_owner(client)
    save_step1(client)
    save_step2(client)
    save_step3(client, cctv="no", marketing="no")

    with app.app_context():
        expected_text = popia_wizard.build_notice()
        expected_left_out = popia_wizard.left_out()

    body = unescaped(client.get("/settings/popia/4"))
    assert expected_text in body, "the preview is the exact generated notice text"
    for entry in expected_left_out:
        assert entry["paragraph"] in body
    # The skipped paragraphs really are not in the rendered notice.
    assert "closed-circuit television (CCTV)" not in expected_text
    assert "## Marketing" not in expected_text
    # The draft PDF action is present.
    assert "/settings/popia/draft.pdf" in body


def test_draft_pdf_returns_pdf_bytes(app, client):
    login_owner(client)
    save_all(app, client)
    response = client.get("/settings/popia/draft.pdf")
    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert response.data.startswith(b"%PDF-")


# ---------------------------------------------------------------------------
# Step 5: the publish gate, surfaced
# ---------------------------------------------------------------------------


def test_step5_surfaces_the_refusal_naming_missing_fields(app, client):
    login_owner(client)
    save_step1(client)
    save_step2(client, registered="not_yet", registration_date="")
    save_step3(client, share_info="")

    body = text_of(client.get("/settings/popia/5"))
    assert "cannot be published" in body
    # The officer gate and the unanswered required question are both named, in
    # plain wording.
    assert "Information Officer registration" in body
    assert "Do you share customer information" in body
    with app.app_context():
        assert popia_wizard.state()["published"] is False


def test_step5_publish_success_stores_version_and_hash(app, client):
    login_owner(client)
    save_all(app, client)

    response = client.post("/settings/popia/publish", follow_redirects=True)
    body = text_of(response)
    assert "published as version" in body
    assert "The notice is live" in body

    with app.app_context():
        published = popia_wizard.published_notice()
        assert published is not None
        version = published["notice_version"]
        content_hash = published["notice_content_hash"]
        assert re.match(r"^\d{4}-\d{2}-\d{2}\.\d+$", version)
        assert popia_wizard.state()["published"] is True

    # The page shows the version and the hash it recorded.
    assert version in body
    assert content_hash in body
