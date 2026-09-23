"""Programme phase 7 (feature P / §P1) — the POPIA privacy notice + consent service.

Don's ask: "add a popia privacy terms and click button for client to accept (similar to trailerpro app) …
also make a note on the public booking flow creation jobs for privacy agreement."

What these tests pin, one per line of §P1's acceptance:

* ``/privacy`` is reachable **with the online store switched off** (a customer must be able to read the
  notice even when the store is closed) and without a sign-in;
* it never publishes a raw ``[PLACEHOLDER]`` token to a customer, in *either* state — while
  ``docs/popia/PRIVACY-NOTICE.md`` still carries the five open facts the page is the short interim page,
  and the moment the tokens are gone the same route renders the full notice with every s18(1) element,
  with no code change;
* ``PRIVACY_NOTICE_VERSION`` is pinned to the `Version:` line inside the notice document (and the
  confirmed contact details on the page are pinned to the document too), so the reviewed document and the
  published page cannot drift apart silently;
* the shared consent block (``templates/public/_consent_block.html``) renders **unticked** and is the one
  widget Feature B's portal (§B2) and Feature C's public booking (§C2) must reuse;
* ``record_consent`` refuses a false/absent acceptance (and an unknown customer) and writes nothing;
* a recorded acceptance is retrievable, reads back on the admin customer page as evidence (who, which
  notice version, which channel, when), and dies with the customer — no orphans;
* the table stores **no IP address and no user agent** (asserted on the schema itself, so the data
  minimisation decision cannot be quietly undone).

Nothing here reads or writes the real disc sample, and no test needs a network.
"""

import os
import re
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import render_template

from app import create_app
from app.db import get_db, run_migrations
from app.services import consent, popia_pack
from app.services.customers import create_customer, delete_customer

ROOT = Path(__file__).resolve().parents[1]
NOTICE_PATH = ROOT / "docs" / "popia" / "PRIVACY-NOTICE.md"
CONSENT_BLOCK_PATH = ROOT / "templates" / "public" / "_consent_block.html"
NOTICE_TEMPLATE_PATH = ROOT / "templates" / "public" / "privacy_notice.html"
INTERIM_TEMPLATE_PATH = ROOT / "templates" / "public" / "privacy_notice_interim.html"
STORE_TEMPLATE_PATH = ROOT / "templates" / "public" / "store.html"
CONFIRMATION_TEMPLATE_PATH = ROOT / "templates" / "public" / "confirmation.html"

#: The wording the flag (D11) forbids publishing, and the phrase that proves the
#: page a customer actually sees is the honest interim one.
INTERIM_PHRASE = "being finalised"

#: Every s18(1) element the published notice has to carry, as the strings the
#: rendered page must contain. One tuple per element so a failure names the gap.
S18_ELEMENTS = [
    ("the information collected", "What personal information we collect"),
    ("where it comes from", "given to us by you"),
    ("the responsible party's name", "Sano Trailers"),
    ("the responsible party's address", "229 Summit Road, Midrand"),
    ("the purpose of the collection", "Why we collect it"),
    ("whether supply is voluntary or mandatory", "voluntary"),
    ("the consequences of not supplying it", "we may not be able to hire a trailer"),
    ("the law that requires the collection", "Tax Administration Act"),
    ("the categories of recipients", "categories of recipients"),
    ("cross-border transfer + the protection level", "outside South Africa"),
    ("the recipient jurisdictions", "European Union (Ireland)"),
    ("the right of access", "access"),
    ("the right to correct or delete", "Correct or delete"),
    ("the right to object", "Object"),
    ("the right to complain to the Regulator", "Complain to the Information Regulator"),
    ("the Regulator's contact details", "complaints.IR@justice.gov.za"),
    ("the Regulator's postal address", "JD House, 27 Stiemens Street"),
]

#: The five facts Sano must supply (docs/popia/BLOCKERS-CHECKLIST.md). While any
#: is missing the notice is not publishable, so the page must be interim.
COMPLETE_FACTS = {
    "version": "1.1",
    "effective_date": "2026-10-01",
    "last_reviewed": "2026-09-23",
    "registered_name": "Sano Trailers (Pty) Ltd",
    "notice_url": "https://sanotrailers.co.za/privacy",
    "cctv": True,
}


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": path,
            "SECRET_KEY": "test",
            "ADMIN_EMAIL": "admin@abi.local",
            "ADMIN_PASSWORD": "admin123",
        }
    )
    yield app
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client, name=None, password="admin123"):
    """Sign in the way the UI does (name -> id, then the password)."""
    with client.application.app_context():
        if name is None:
            row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
        else:
            row = get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
        user_id = row["id"] if row else None
    return client.post("/login", data={"user_id": str(user_id), "password": password}, follow_redirects=True)


def make_customer(app, name="Charmaine Mokoena", phone="0821234567"):
    with app.app_context():
        return create_customer({"customer_type": "individual", "name": name, "phone": phone, "email": ""})


# ---------------------------------------------------------------------------
# popia_pack: reading the notice document honestly
# ---------------------------------------------------------------------------


def test_the_notice_document_is_where_the_manifest_says_it_is():
    paths = popia_pack.document_paths()
    assert "privacy_notice" in paths
    assert paths["privacy_notice"] == NOTICE_PATH
    assert paths["privacy_notice"].exists()


def test_outstanding_fields_finds_every_bracketed_placeholder_in_the_notice():
    fields = popia_pack.outstanding_fields("privacy_notice")
    # The document currently carries the five open facts (header effective date,
    # the registered name, the Information Officer, the CCTV paragraph and the
    # notice URL). An empty list here would mean the gate is broken, not that the
    # notice is finished.
    assert fields, "the notice still carries open items — the gate must see them"
    assert all(token.startswith("[") and token.endswith("]") for token in fields)
    assert popia_pack.is_complete("privacy_notice") is False


def test_outstanding_fields_is_a_general_rule_not_a_fixed_list():
    """A placeholder worded differently must not slip past (phase 14 §Q1's rule)."""
    sample = "Everything here is fine. [SOMETHING NEW] and [To be supplied] and nothing else.\n"
    monkeypatched = popia_pack._outstanding_in_text(sample)
    assert "[SOMETHING NEW]" in monkeypatched
    assert "[To be supplied]" in monkeypatched  # brackets + a capitalised token
    assert popia_pack._outstanding_in_text("No brackets here at all.") == []


def test_notice_metadata_reports_the_open_facts_as_unset_not_as_tokens():
    meta = popia_pack.notice_metadata("privacy_notice")
    assert meta["version"] == consent.PRIVACY_NOTICE_VERSION
    assert meta["last_reviewed"] == consent.PRIVACY_NOTICE_REVIEWED
    # Still open in the document, so they are None — the page must never print the token.
    assert meta["effective_date"] is None
    assert meta["registered_name"] is None
    assert meta["notice_url"] is None
    assert meta["cctv"] is None


# ---------------------------------------------------------------------------
# /privacy — reachable, honest, and placeholder-free
# ---------------------------------------------------------------------------


def test_privacy_page_is_reachable_with_the_store_switched_off(client, app):
    with app.app_context():
        get_db().execute("UPDATE company_settings SET store_enabled = 0 WHERE id = 1")
        get_db().commit()
    # The store itself is closed …
    assert b"Online booking is temporarily unavailable" in client.get("/store").data
    # … and the notice is still readable, without a sign-in.
    res = client.get("/privacy")
    assert res.status_code == 200
    assert b"Privacy" in res.data


def test_privacy_page_never_publishes_a_placeholder_token(client):
    res = client.get("/privacy")
    body = res.get_data(as_text=True)
    assert res.status_code == 200
    assert "[" not in body
    assert "TO CONFIRM" not in body
    assert "____" not in body


def test_privacy_page_is_the_interim_page_while_the_notice_is_unfinished(client):
    res = client.get("/privacy")
    body = res.get_data(as_text=True)
    assert INTERIM_PHRASE in body
    # Even the interim page names the responsible party, how to reach them and how
    # to complain — a customer is never left with nothing to act on.
    assert "Sano Trailers" in body
    assert "229 Summit Road, Midrand" in body
    assert "info@sanotrailers.co.za" in body
    assert "complaints.IR@justice.gov.za" in body


def test_privacy_page_reports_the_gate_state_the_document_is_actually_in(client):
    """The route is driven by the document, not by a hard-coded decision."""
    res = client.get("/privacy")
    assert (INTERIM_PHRASE in res.get_data(as_text=True)) is (not popia_pack.is_complete("privacy_notice"))


def test_full_notice_renders_every_section_18_element_once_the_facts_land(client, monkeypatch):
    monkeypatch.setattr(popia_pack, "outstanding_fields", lambda key: [])
    monkeypatch.setattr(popia_pack, "notice_metadata", lambda key: dict(COMPLETE_FACTS))
    res = client.get("/privacy")
    body = res.get_data(as_text=True)
    assert res.status_code == 200
    missing = [label for label, needle in S18_ELEMENTS if needle not in body]
    assert missing == [], f"the published notice is missing: {missing}"
    assert INTERIM_PHRASE not in body
    assert "[" not in body


def test_full_notice_shows_the_version_and_the_dated_effective_line(client, monkeypatch):
    monkeypatch.setattr(popia_pack, "outstanding_fields", lambda key: [])
    monkeypatch.setattr(popia_pack, "notice_metadata", lambda key: dict(COMPLETE_FACTS))
    body = client.get("/privacy").get_data(as_text=True)
    assert "Version 1.1" in body
    assert "2026-10-01" in body
    assert "Sano Trailers (Pty) Ltd" in body


def test_full_notice_prints_the_cctv_paragraph_only_when_cctv_is_confirmed(client, monkeypatch):
    monkeypatch.setattr(popia_pack, "outstanding_fields", lambda key: [])
    monkeypatch.setattr(popia_pack, "notice_metadata", lambda key: dict(COMPLETE_FACTS, cctv=False))
    assert "Closed-circuit television" not in client.get("/privacy").get_data(as_text=True)

    monkeypatch.setattr(popia_pack, "notice_metadata", lambda key: dict(COMPLETE_FACTS, cctv=True))
    assert "Closed-circuit television" in client.get("/privacy").get_data(as_text=True)


def test_privacy_notice_version_matches_the_document():
    text = NOTICE_PATH.read_text(encoding="utf-8")
    version = re.search(r"\*\*Version:\*\*\s*([^·\n]+)", text)
    reviewed = re.search(r"\*\*Last reviewed:\*\*\s*([^\n·]+)", text)
    assert version is not None and version.group(1).strip() == consent.PRIVACY_NOTICE_VERSION
    assert reviewed is not None and reviewed.group(1).strip() == consent.PRIVACY_NOTICE_REVIEWED


def test_the_page_carries_the_documents_confirmed_contact_details():
    """Drift guard: the published page and the reviewed document agree on the facts we do have."""
    doc = NOTICE_PATH.read_text(encoding="utf-8")
    for line in (NOTICE_TEMPLATE_PATH.read_text(encoding="utf-8"), INTERIM_TEMPLATE_PATH.read_text(encoding="utf-8")):
        for fact in ("229 Summit Road, Midrand", "010 221 1723", "info@sanotrailers.co.za"):
            if fact in doc:
                assert fact in line, f"{fact} is in the document but not on the published page"


def test_the_notice_page_offers_a_way_back_without_a_foreign_redirect(app, client):
    # A cross-site referrer must not become a redirect target.
    res = client.get("/privacy", headers={"Referer": "https://evil.example/phish"})
    assert b"evil.example" not in res.data
    assert b'href="/store"' in res.data
    res2 = client.get("/privacy", headers={"Referer": "http://localhost/store"})
    assert b'href="http://localhost/store"' in res2.data


# ---------------------------------------------------------------------------
# The shared consent block (§B2 and §C2 must reuse this one widget)
# ---------------------------------------------------------------------------


def _render_consent_block(app, **context):
    with app.test_request_context("/store"):
        return app.jinja_env.get_template("public/_consent_block.html").render(**context)


def test_consent_block_is_unticked_and_required(app):
    html = _render_consent_block(app)
    assert 'name="popia_consent"' in html
    assert 'id="popia-consent"' in html
    assert 'value="1"' in html
    assert "required" in html
    assert "checked" not in html
    assert "in accordance with POPIA" in html
    assert "name, contact details and address" in html


def test_consent_block_links_the_notice_in_a_new_tab(app):
    html = _render_consent_block(app)
    assert 'href="/privacy"' in html
    assert 'target="_blank"' in html
    assert 'rel="noopener noreferrer"' in html
    assert "View Privacy Notice" in html


def test_consent_block_wording_follows_the_capture_point(app):
    assert "this booking" in _render_consent_block(app, consent_purpose="booking")
    assert "this registration" in _render_consent_block(app, consent_purpose="registration")


def test_the_capture_points_actually_include_the_shared_block():
    """Feature B/C must reuse the block — pinned here so a second widget cannot appear."""
    assert 'name="popia_consent"' in CONSENT_BLOCK_PATH.read_text(encoding="utf-8")
    for path in sorted((ROOT / "templates" / "public").glob("*.html")):
        if path.name.startswith("_"):
            continue
        text = path.read_text(encoding="utf-8")
        if "popia_consent" in text:
            assert '{% include "public/_consent_block.html" %}' in text, f"{path.name} has its own consent widget"


def test_the_store_footer_and_confirmation_offer_the_notice(client, app):
    with app.app_context():
        get_db().execute("UPDATE company_settings SET store_enabled = 1 WHERE id = 1")
        get_db().commit()
    assert b"View our privacy notice" in client.get("/store").data
    assert b"/privacy" in client.get("/store").data

    with app.test_request_context("/store/booking/1"):
        html = render_template(
            "public/confirmation.html",
            settings={"company_name": "Sano Trailers"},
            order=SimpleNamespace(
                customer_name="Test Client",
                order_number="ORD-10145",
                start_at="2026-09-24T09:00:00",
                end_at="2026-09-25T09:00:00",
                total=100.0,
                deposit_total=0.0,
                deposit_option="security_deposit",
            ),
            items=[],
        )
    assert "View our privacy notice" in html


# ---------------------------------------------------------------------------
# consent service
# ---------------------------------------------------------------------------


def test_record_consent_refuses_an_absent_or_false_acceptance(app):
    customer_id = make_customer(app)
    with app.app_context():
        # "0" is a truthy string in Python but it is not consent — a crafted post of
        # popia_consent=0 must be refused exactly like an unticked box.
        for refused in (None, "", "0", "false", "no", 0, 2, False):
            with pytest.raises(ValueError):
                consent.record_consent(customer_id, "Midrand portal", refused)
        assert consent.consent_for(customer_id) is None
        assert get_db().execute("SELECT COUNT(*) AS c FROM consent_records").fetchone()["c"] == 0
        # … and the values a ticked box really posts are accepted.
        for given in ("1", "on", "true", True, 1):
            assert consent.acceptance_given(given) is True


def test_record_consent_refuses_an_unknown_customer(app):
    with app.app_context():
        with pytest.raises(ValueError):
            consent.record_consent(9999, "Midrand portal", True)
        assert get_db().execute("SELECT COUNT(*) AS c FROM consent_records").fetchone()["c"] == 0


def test_a_recorded_consent_is_retrievable_with_the_version_channel_and_time(app):
    customer_id = make_customer(app)
    with app.app_context():
        row_id = consent.record_consent(customer_id, "Midrand portal", True)
        row = consent.consent_for(customer_id)
    assert row["id"] == row_id
    assert row["customer_id"] == customer_id
    assert row["consent_type"] == "popia_privacy"
    assert row["notice_version"] == consent.PRIVACY_NOTICE_VERSION
    assert row["channel"] == "Midrand portal"
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", row["accepted_at"])


def test_consent_summary_is_the_plain_language_evidence_line(app):
    customer_id = make_customer(app)
    with app.app_context():
        assert consent.consent_summary(customer_id) is None
        consent.record_consent(customer_id, "Midrand portal", True)
        summary = consent.consent_summary(customer_id)
    assert summary.startswith("POPIA consent — accepted ")
    assert f"notice v{consent.PRIVACY_NOTICE_VERSION}" in summary
    assert "via Midrand portal" in summary


def test_the_latest_acceptance_is_the_one_reported(app):
    customer_id = make_customer(app)
    with app.app_context():
        consent.record_consent(customer_id, "Midrand portal", True, notice_version="1.0")
        consent.record_consent(customer_id, "Public booking page", True)
        latest = consent.consent_for(customer_id)
        assert latest["channel"] == "Public booking page"
        assert latest["notice_version"] == consent.PRIVACY_NOTICE_VERSION


def test_consent_required_error_is_one_stable_sentence(app):
    with app.app_context():
        message = consent.consent_required_error()
    assert message and message == consent.consent_required_error()
    assert "privacy notice" in message.lower()


def test_notice_is_publishable_follows_the_document(app):
    with app.app_context():
        assert consent.notice_is_publishable() is (not popia_pack.outstanding_fields("privacy_notice"))


def test_the_customer_page_shows_the_consent_evidence_line(client, app, monkeypatch):
    customer_id = make_customer(app)
    login(client)
    body = client.get(f"/customers/{customer_id}").get_data(as_text=True)
    assert "No POPIA consent recorded" in body

    with app.app_context():
        consent.record_consent(customer_id, "Midrand portal", True)
    body = client.get(f"/customers/{customer_id}").get_data(as_text=True)
    assert "POPIA consent — accepted" in body
    assert "via Midrand portal" in body


# ---------------------------------------------------------------------------
# schema, cascade and data minimisation
# ---------------------------------------------------------------------------


def test_consent_records_stores_no_ip_address_and_no_user_agent(app):
    with app.app_context():
        columns = {row["name"] for row in get_db().execute("PRAGMA table_info(consent_records)").fetchall()}
    assert columns == {"id", "customer_id", "consent_type", "notice_version", "channel", "accepted_at"}
    for banned in ("ip", "ip_address", "user_agent", "fingerprint", "device"):
        assert banned not in columns


def test_deleting_the_customer_removes_the_consent_rows(app):
    customer_id = make_customer(app)
    with app.app_context():
        consent.record_consent(customer_id, "Midrand portal", True)
        assert delete_customer(customer_id) is True
        db = get_db()
        assert db.execute("SELECT COUNT(*) AS c FROM consent_records").fetchone()["c"] == 0
        orphans = db.execute(
            "SELECT COUNT(*) AS c FROM consent_records c LEFT JOIN customers cu ON cu.id = c.customer_id "
            "WHERE cu.id IS NULL"
        ).fetchone()["c"]
        assert orphans == 0


def test_the_database_itself_cascades_a_raw_customer_delete(app):
    """Belt and braces: the FK declares ON DELETE CASCADE, so a raw DELETE cannot orphan a row."""
    customer_id = make_customer(app)
    with app.app_context():
        consent.record_consent(customer_id, "Midrand portal", True)
        db = get_db()
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        db.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
        db.commit()
        assert db.execute("SELECT COUNT(*) AS c FROM consent_records").fetchone()["c"] == 0


def test_consent_records_is_created_by_run_migrations_on_an_existing_database(app):
    """Additive migration: dropping the table and re-migrating brings it back empty."""
    with app.app_context():
        db = get_db()
        db.execute("DROP INDEX IF EXISTS idx_consent_records_customer")
        db.execute("DROP TABLE consent_records")
        db.commit()
        with pytest.raises(sqlite3.OperationalError):
            db.execute("SELECT COUNT(*) FROM consent_records").fetchone()
        # An existing database with real customers in it (the upgrade path on live).
        customer_id = make_customer(app)
        run_migrations(db)
        db.commit()
        columns = {row["name"] for row in db.execute("PRAGMA table_info(consent_records)").fetchall()}
        assert "customer_id" in columns and "accepted_at" in columns
        assert db.execute("SELECT COUNT(*) AS c FROM consent_records").fetchone()["c"] == 0
        # The pre-existing customer survived the migration untouched.
        assert db.execute("SELECT name FROM customers WHERE id = ?", (customer_id,)).fetchone()["name"]


def test_the_scoped_consent_delete_is_idempotent(app):
    customer_id = make_customer(app)
    with app.app_context():
        consent.record_consent(customer_id, "Midrand portal", True)
        assert consent.delete_consents_for_customer(customer_id) == 1
        assert consent.delete_consents_for_customer(customer_id) == 0
