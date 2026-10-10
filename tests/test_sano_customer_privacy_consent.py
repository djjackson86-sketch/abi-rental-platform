"""The adapted TrailerPro-style customer privacy notice + consent block, for Sano.

Don's ask: the TrailerPro privacy checkbox and its plain-English customer notice read
better than ours, so adapt them for Sano and replace the customer-portal wording.

This is the *publication* test that the combined live release needs. It inserts the new
immutable notice version into ``popia_notice_versions`` exactly the way the parent
release will (version, content SHA-256, the document's own text, a real timestamp) — the
registration gate is opened by a genuinely published notice, never by a bypass or a new
setup step — and then proves, on **both** row types the app really runs on (SQLite and an
actual ``libsql_client.result.Row``):

* the publication opens both portal forms (universal and per-branch) and both link the
  new notice version;
* the shared checkbox arrives unticked (a pre-ticked box is not consent);
* a submission without consent is refused and writes **nothing**;
* the marketing choice is separate, optional and independently unticked;
* the earlier version's row, its hash and the consents recorded against it are untouched.

No credentials, no production data, no network.
"""

import hashlib
import re
import sqlite3
from pathlib import Path

import pytest
from libsql_client.result import Row

from app import create_app
from app.db import get_db
from app.services import consent, portal_intake
from app.services.customers import create_customer

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "popia" / "SANO-CUSTOMER-PRIVACY-NOTICE.md"
BASIC_DOC = ROOT / "docs" / "popia" / "SANO-BASIC-CUSTOMER-PRIVACY-NOTICE.md"
NOTICE_TEMPLATE = ROOT / "templates" / "public" / "_customer_privacy_notice.html"
CONSENT_BLOCK = ROOT / "templates" / "public" / "_consent_block.html"

#: The previously approved, already-published notice. It stays immutable.
V1 = "sano-basic-privacy-v1"
V1_TEXT = BASIC_DOC.read_text(encoding="utf-8")
V1_SHA256 = "6c205e1edd531f2ce2f6d90ec04489a4ca64c1cedbfdc793c9e6b02f5bdaec98"

#: The adapted notice the parent publishes as the next immutable version.
V2 = "sano-customer-privacy-v2"
V2_TEXT = DOC.read_text(encoding="utf-8")

#: The plain-English headings the adapted notice must carry, and the source template must
#: carry in step (so the published DB text and the point-of-collection fallback agree).
NEW_HEADINGS = (
    "Who collects your information?",
    "What information is collected?",
    "Why is it collected?",
    "Who has access to your information?",
    "How long is it kept?",
    "How is it secured?",
    "Where is it processed?",
    "Marketing",
    "Your rights under POPIA",
    "Information Regulator",
)

#: The current Regulator contact — and the two obsolete addresses it replaced.
REGULATOR_CONTACT = "POPIAComplaints@inforegulator.org.za"
OBSOLETE_CONTACT = ("complaints.IR@justice.gov.za", "inforeg@justice.gov.za")

#: TrailerPro claims that are NOT true of Sano's processing and must never be copied in:
#: an unqualified \"no identity document numbers\", AES-256 at rest, an automatic 12-month
#: deletion, a \"no third party\" promise, and the old Regulator email. Each is checked as
#: the exact sentence/fragment a careless copy would have pasted.
FALSE_TRAILERPRO_CLAIMS = (
    "No identity document numbers",
    "AES-256",
    "AES256",
    "retained for the duration of the rental and for up to",
    "not shared with, sold to, or disclosed to any third party",
    "inforeg@justice.gov.za",
)

#: Promises the adapted notice must NOT make, because the app does not (yet) back them:
#: the licence-disc scan records the towing vehicle's details — it does not prove the
#: vehicle is roadworthy; the app does not automatically delete verification copies at
#: settlement; and Sano has no signed operator agreements to point at, so providers are
#: described as processing on Sano's instructions rather than \"under written instruction\".
UNSUPPORTED_PROMISES = (
    "roadworthy",
    "written instruction",
    "Verification copies are deleted",
    "deleted once verification is done",
)

_CONSENT_INPUT_RE = re.compile(r'<input[^>]*name="popia_consent"[^>]*>')
_MARKETING_INPUT_RE = re.compile(r'<input[^>]*name="marketing_opt_in"[^>]*>')


@pytest.fixture(params=[False, True], ids=["sqlite", "libsql-row"])
def app(tmp_path, monkeypatch, request):
    """A fresh app whose published notice is the adapted one, on both row types."""
    if request.param:
        # Same trick the basic-notice activation test uses: make sqlite3 hand back real
        # libSQL Rows, so every query in the portal and notice paths is exercised against
        # the object type the live Turso connection returns.
        monkeypatch.setattr(
            sqlite3,
            "Row",
            lambda c, v: Row({x[0]: i for i, x in enumerate(c.description)}, tuple(v)),
        )
    application = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "customer-privacy.sqlite"),
            "SECRET_KEY": "test",
            "TURSO_DATABASE_URL": "",
            "TURSO_AUTH_TOKEN": "",
            "ADMIN_PASSWORD": "admin123",
            "TELEGRAM_NOTIFICATIONS_ENABLED": "",
        }
    )
    with application.app_context():
        # Nothing is published yet, so collection is shut.
        assert portal_intake.registration_is_open() is False
        db = get_db()
        _publish(db, V1, V1_TEXT, "2026-10-10T20:00:00+02:00")
        _publish(db, V2, V2_TEXT, "2026-10-11T09:00:00+02:00")
        db.commit()
        # The new publication itself opens the gate — no bypass, no new setup step.
        assert consent.notice_is_publishable() is True
        assert portal_intake.registration_is_open() is True
        assert consent.current_notice_version() == V2
    return application


def _publish(db, version, text, published_at):
    db.execute(
        "INSERT INTO popia_notice_versions "
        "(notice_version, notice_content_hash, notice_text, published_at, published_by_user_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (version, hashlib.sha256(text.encode("utf-8")).hexdigest(), text, published_at, None),
    )


def _branch_slug(app):
    with app.app_context():
        return str(get_db().execute("SELECT public_slug FROM branches ORDER BY id LIMIT 1").fetchone()[0])


def _customer_count(app):
    with app.app_context():
        return get_db().execute("SELECT COUNT(*) AS c FROM customers").fetchone()["c"]


def _consent_count(app):
    with app.app_context():
        return get_db().execute("SELECT COUNT(*) AS c FROM consent_records").fetchone()["c"]


# ---------------------------------------------------------------------------
# The publication opens both portal forms and links the new version
# ---------------------------------------------------------------------------


def test_new_publication_opens_both_portal_forms_and_links_the_version(app):
    client = app.test_client()
    slug = _branch_slug(app)
    for path in (
        "/privacy",
        "/portal",
        "/portal/register",
        "/portal/" + slug,
        "/portal/" + slug + "/register",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        body = response.get_data(as_text=True)
        if path == "/privacy":
            # The page a customer reads is the new version, and only the new version.
            assert "Version " + V2 in body
            assert "sano trailers" in body.lower()
            assert "229 summit road, midrand" in body.lower()
            assert "info@sanotrailers.co.za" in body
            assert "010 221 1723" in body
            assert REGULATOR_CONTACT in body
            for heading in NEW_HEADINGS:
                assert heading in body, (path, heading)
            # The obsolete Regulator contact and the old notice's headings are gone.
            for gone in OBSOLETE_CONTACT:
                assert gone not in body
            assert "Overseas processing" not in body
            assert "What we collect and why" not in body
            for falsehood in FALSE_TRAILERPRO_CLAIMS:
                assert falsehood not in body, falsehood
            # Nor may it promise something the app does not actually do.
            for promise in UNSUPPORTED_PROMISES:
                assert promise not in body, promise
        else:
            assert 'name="popia_consent"' in body
            assert 'href="/privacy"' in body


def test_the_shared_checkbox_is_unticked_on_both_forms(app):
    client = app.test_client()
    slug = _branch_slug(app)
    for path in ("/portal", "/portal/register", "/portal/" + slug, "/portal/" + slug + "/register"):
        body = client.get(path).get_data(as_text=True)
        box = _CONSENT_INPUT_RE.search(body)
        assert box is not None, path
        assert "checked" not in box.group(0), path
        assert 'value="1"' in box.group(0)
        assert "required" in box.group(0)
        assert "may use my personal information to process this registration" in body
        assert "in accordance with POPIA" in body
        assert "View Privacy Notice" in body
        assert 'target="_blank"' in body
        # Marketing is a separate, independently unticked choice.
        marketing = _MARKETING_INPUT_RE.search(body)
        assert marketing is not None
        assert "checked" not in marketing.group(0)


# ---------------------------------------------------------------------------
# No consent → refused, nothing written (both channels)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("channel", ["universal", "branch"])
def test_a_submission_without_consent_is_refused_and_writes_nothing(app, channel):
    client = app.test_client()
    slug = _branch_slug(app)
    path = "/portal/register" if channel == "universal" else "/portal/" + slug + "/register"
    data = {"name": "Synthetic Refused Customer", "phone": "0825550118", "email": "refused@example.test"}
    refused = client.post(path, data=data)
    assert refused.status_code == 400
    assert "Please tick the box" in refused.get_data(as_text=True)
    assert _customer_count(app) == 0
    assert _consent_count(app) == 0


# ---------------------------------------------------------------------------
# Consent → the acceptance names the new version
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("channel", ["universal", "branch"])
def test_a_consented_submission_records_the_new_version(app, channel):
    client = app.test_client()
    slug = _branch_slug(app)
    path = "/portal/register" if channel == "universal" else "/portal/" + slug + "/register"
    data = {
        "name": "Synthetic Accepted Customer",
        "phone": "0825550117",
        "email": "accepted@example.test",
        "popia_consent": "1",
    }
    response = client.post(path, data=data)
    assert response.status_code == 200
    with app.app_context():
        row = get_db().execute("SELECT * FROM customers").fetchone()
        assert row["source_system"] == "portal"
        evidence = consent.consent_for(row["id"])
        assert evidence["notice_version"] == V2
        assert evidence["consent_type"] == "popia_privacy"
        assert not row["marketing_opt_in"]


# ---------------------------------------------------------------------------
# Marketing stays independent and optional
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("marketing", ["", "1"])
def test_marketing_is_a_separate_optional_choice(app, marketing):
    client = app.test_client()
    data = {
        "name": "Synthetic Marketing " + ("Yes" if marketing else "No"),
        "phone": "0825550116",
        "email": "",
        "popia_consent": "1",
    }
    if marketing:
        data["marketing_opt_in"] = marketing
    response = client.post("/portal/register", data=data)
    assert response.status_code == 200
    with app.app_context():
        row = get_db().execute("SELECT * FROM customers ORDER BY id DESC LIMIT 1").fetchone()
        assert bool(row["marketing_opt_in"]) is bool(marketing)
        # The privacy acceptance is recorded whatever the marketing choice was.
        assert consent.consent_for(row["id"])["notice_version"] == V2


def test_marketing_alone_does_not_substitute_for_privacy_consent(app):
    client = app.test_client()
    response = client.post(
        "/portal/register",
        data={"name": "Synthetic Marketing Only", "phone": "0825550115", "marketing_opt_in": "1"},
    )
    assert response.status_code == 400
    assert _customer_count(app) == 0
    assert _consent_count(app) == 0


# ---------------------------------------------------------------------------
# History is intact: the earlier version and its consents are untouched
# ---------------------------------------------------------------------------


def test_the_earlier_version_and_its_consents_are_untouched(app):
    with app.app_context():
        rows = get_db().execute(
            "SELECT notice_version, notice_content_hash FROM popia_notice_versions ORDER BY id"
        ).fetchall()
        hashes = {row["notice_version"]: row["notice_content_hash"] for row in rows}
        assert len(rows) == 2
        assert hashes[V1] == V1_SHA256
        assert hashes[V2] == hashlib.sha256(V2_TEXT.encode("utf-8")).hexdigest()

        # A consent taken under the earlier version keeps naming the earlier version: the
        # record still points at the exact wording that customer saw.
        customer_id = create_customer(
            {"customer_type": "individual", "name": "Synthetic Historical", "phone": "0825550114", "email": ""}
        )
        consent.record_consent(customer_id, consent.CHANNEL_PORTAL, True, notice_version=V1)
        assert consent.consent_for(customer_id)["notice_version"] == V1
        # …while a fresh acceptance takes the currently published version.
        assert consent.current_notice_version() == V2


# ---------------------------------------------------------------------------
# The document, the template and the historic document
# ---------------------------------------------------------------------------


def test_the_new_document_and_the_template_agree_heading_for_heading():
    doc = DOC.read_text(encoding="utf-8")
    template = NOTICE_TEMPLATE.read_text(encoding="utf-8")
    for heading in NEW_HEADINGS:
        assert heading in doc, ("document", heading)
        assert heading in template, ("template", heading)
    for text in (doc, template):
        assert "Sano Trailers" in text
        assert "229 Summit Road, Midrand" in text
        assert "info@sanotrailers.co.za" in text
        assert "010 221 1723" in text
        assert "Protection of Personal Information Act" in text
        assert REGULATOR_CONTACT in text
        for gone in OBSOLETE_CONTACT:
            assert gone not in text
        for falsehood in FALSE_TRAILERPRO_CLAIMS:
            assert falsehood not in text, falsehood
        for promise in UNSUPPORTED_PROMISES:
            assert promise not in text, promise


def test_the_historic_basic_notice_document_is_unchanged():
    """The already-published v1 wording is immutable — its bytes may never drift."""
    assert hashlib.sha256(BASIC_DOC.read_bytes()).hexdigest() == V1_SHA256


def test_the_consent_block_is_the_shared_unticked_widget_with_the_adapted_wording():
    template = CONSENT_BLOCK.read_text(encoding="utf-8")
    assert 'name="popia_consent"' in template
    assert 'value="1" required' in template
    assert "checked" not in template
    assert "I agree that" in template
    assert "may use my personal information" in template
    assert "generate invoices" in template
    assert "in accordance with POPIA" in template
    assert '{{ consent_purpose|default(\'booking\') }}' in template
    assert 'target="_blank"' in template and "View Privacy Notice" in template
