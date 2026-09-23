"""Programme phase 14 (feature Q / §Q1) — the POPIA pack, routes + templates + PDFs.

This file covers the *UI half* built on the committed service half
(``test_programme_20260923_popia_pack.py``). What it pins:

* the routes are **main profile only**: a staff account gets 403 on every
  ``/popia`` route (and the PDFs), and never sees the notification bar or the nav
  entry;
* ``/popia`` lists every document in the manifest with its status, and a document
  still carrying ``[TO CONFIRM]``-style placeholders is shown as *Pending details*
  with the fields listed and **cannot be accepted** (accepting writes nothing);
* a clean document *can* be accepted over HTTP and records the user, timestamp and
  hash;
* editing a document after adoption flips its status back to *Changed since it was
  adopted*;
* both PDFs return ``%PDF-`` bytes carrying the acceptance certificate, and the
  operator agreement's PDF carries the signature block (the privacy notice's does
  not).

The hard gate (D11) is asserted end-to-end here: the refusal path is a real POST
with the real ``[TO CONFIRM]`` documents, not a synthetic fixture.
"""

import os
import tempfile

import pytest

from app import create_app
from app.db import get_db
from app.services import popia_pack
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


def acceptance_count(app, key=None):
    with app.app_context():
        db = get_db()
        if key is None:
            return db.execute("SELECT COUNT(*) AS c FROM document_acceptances").fetchone()["c"]
        return db.execute(
            "SELECT COUNT(*) AS c FROM document_acceptances WHERE document_key = ?", (key,)
        ).fetchone()["c"]


PACK_TITLES = [
    "Privacy Notice",
    "Retention Policy and Schedule",
    "POPIA Compliance Action Plan",
    "Operator Agreement",
]


# ---------------------------------------------------------------------------
# The pack page
# ---------------------------------------------------------------------------


def test_the_pack_page_lists_every_manifest_document(app, client):
    login_owner(client)
    response = client.get("/popia")
    body = text_of(response)
    assert response.status_code == 200
    for title in PACK_TITLES:
        assert title in body
    # The print-pack control is present.
    assert "/popia/pack.pdf" in body


def test_pending_documents_show_their_outstanding_fields_and_cannot_be_accepted(app, client):
    login_owner(client)
    body = text_of(client.get("/popia"))
    # Today every document still carries placeholders, so the honest state is
    # "Pending details — cannot be adopted yet", with the fields listed.
    assert "Pending details" in body
    assert "must be completed before this can be adopted" in body
    assert "TO CONFIRM" in body, "the outstanding fields are the deliverable — they must be listed"

    before = acceptance_count(app)
    response = client.post("/popia/privacy_notice/accept", follow_redirects=True)
    followed = text_of(response)
    assert "cannot be adopted" in followed, "the refusal reason is flashed"
    assert acceptance_count(app, "privacy_notice") == 0
    assert acceptance_count(app) == before


def test_the_document_page_renders_and_names_the_blockers(app, client):
    login_owner(client)
    response = client.get("/popia/privacy_notice")
    body = text_of(response)
    assert response.status_code == 200
    assert "Pending details" in body
    assert "Privacy Notice" in body
    # The document page lists the exact outstanding fields. Tokens can span two
    # source lines and carry quotes (HTML-escaped by Jinja), so compare the
    # whitespace-collapsed, HTML-unescaped forms.
    import html as _html
    import re as _re

    def norm(text):
        return _re.sub(r"\s+", " ", _html.unescape(text)).strip()

    fields = popia_pack.outstanding_fields("privacy_notice")
    assert fields, "shipped state: the notice really still carries open fields"
    body_norm = norm(body)
    for token in fields:
        assert norm(token) in body_norm, "the document page lists the exact outstanding fields"
    # No Accept button while the document still carries placeholders.
    assert 'action="/popia/privacy_notice/accept"' not in body


# ---------------------------------------------------------------------------
# Acceptance over HTTP: the hard gate, the record, staleness
# ---------------------------------------------------------------------------


def test_a_clean_document_can_be_accepted_and_records_user_timestamp_and_hash(app, client, monkeypatch):
    login_owner(client)
    monkeypatch.setattr(popia_pack, "outstanding_fields", lambda key: [])
    response = client.post("/popia/retention_policy/accept", follow_redirects=True)
    assert response.status_code == 200
    assert "Adopted Retention Policy and Schedule" in text_of(response)
    with app.app_context():
        row = popia_pack.acceptance_for("retention_policy")
        assert row["document_key"] == "retention_policy"
        assert row["document_hash"] == popia_pack.document_hash("retention_policy")
        assert row["accepted_at"]
    # The status on the pack page now reads Adopted.
    body = text_of(client.get("/popia"))
    assert "Adopted" in body
    assert "Changed since it was adopted" not in body


def test_editing_a_document_makes_an_existing_acceptance_stale(app, client, monkeypatch):
    login_owner(client)
    monkeypatch.setattr(popia_pack, "outstanding_fields", lambda key: [])
    client.post("/popia/action_plan/accept", follow_redirects=True)
    body = text_of(client.get("/popia"))
    assert "Adopted" in body
    # The document is edited after adoption: its hash changes on disk.
    monkeypatch.setattr(popia_pack, "document_hash", lambda key: "edited-" + key)
    body = text_of(client.get("/popia"))
    assert "Changed since it was adopted" in body


# ---------------------------------------------------------------------------
# Main-profile-only access
# ---------------------------------------------------------------------------


def test_staff_gets_403_on_every_popia_route_and_sees_no_bar_or_nav(app, client):
    staff = make_staff(app, "Counter Staff")
    login_staff(client, staff)

    for path in (
        "/popia",
        "/popia/privacy_notice",
        "/popia/pack.pdf",
        "/popia/privacy_notice.pdf",
    ):
        assert client.get(path).status_code == 403, path
    assert client.post("/popia/privacy_notice/accept").status_code == 403

    dashboard = text_of(client.get("/dashboard"))
    assert "POPIA pack" not in dashboard, "the nav entry must not render for staff"
    assert "popia-nag" not in dashboard, "the notification bar must not render for staff"
    assert 'href="/popia"' not in dashboard


def test_owner_sees_the_notification_bar_and_nav_entry(app, client):
    login_owner(client)
    body = text_of(client.get("/dashboard"))
    assert 'href="/popia"' in body, "the owner's sidebar links to the pack"
    assert "popia-nag" in body, "the compliance nag renders for the main profile"
    # Nothing is adopted in a fresh database, so the count is the whole pack.
    assert f"{len(popia_pack.PACK)} documents need adopting" in body


# ---------------------------------------------------------------------------
# The printable PDFs
# ---------------------------------------------------------------------------


def test_pack_pdf_returns_a4_pdf_with_the_acceptance_certificate(app, client):
    login_owner(client)
    response = client.get("/popia/pack.pdf")
    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    data = response.data
    assert data.startswith(b"%PDF-")
    assert b"POPIA compliance pack" in data
    assert b"Acceptance certificate" in data
    # The certificate names every document in the pack.
    for title in PACK_TITLES:
        assert title.encode() in data or title.split()[0].encode() in data


def test_operator_agreement_pdf_carries_the_signature_block(app, client):
    login_owner(client)
    response = client.get("/popia/operator_agreement.pdf")
    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    data = response.data
    assert data.startswith(b"%PDF-")
    assert b"Acceptance certificate" in data
    assert b"Signature block" in data
    assert b"Signed for Sano Trailers" in data


def test_privacy_notice_pdf_has_no_signature_block(app, client):
    login_owner(client)
    data = client.get("/popia/privacy_notice.pdf").data
    assert data.startswith(b"%PDF-")
    assert b"Acceptance certificate" in data
    assert b"Signature block" not in data


def test_unknown_document_404s(app, client):
    login_owner(client)
    assert client.get("/popia/does_not_exist").status_code == 404
    assert client.get("/popia/does_not_exist.pdf").status_code == 404
    assert client.post("/popia/does_not_exist/accept").status_code == 404
