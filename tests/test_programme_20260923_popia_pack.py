"""Programme phase 14 (feature Q / §Q1) — the POPIA document pack, service half.

Don's ask: a notification in the app that pulls up the privacy/operator pack and lets
Sano adopt it (and print it). This file covers the *service half*: the manifest, the
``document_acceptances`` table, and ``app/services/popia_pack.py``. The routes,
templates, notification bar and PDF output are built in a second chunk.

What these tests pin:

* the manifest lists the four pack documents in order, each resolving to a real file;
* ``outstanding_fields`` is the **general rule** (any ``[...]`` with a capitalised
  word), proven on a synthetic fixture *and* against the real files — so it keeps
  working as the blockers get filled, and a new token cannot slip past;
* ``accept_document`` refuses while placeholders remain and writes nothing;
* a clean document is accepted with the user, timestamp and hash recorded;
* editing a document after adoption makes the acceptance **stale** and the status
  flips back to needing adoption;
* accepting twice keeps both rows (audit trail), the newest winning;
* the table stores **no IP address and no user agent** (data minimisation, same rule
  as ``consent_records``), and is created by both ``SCHEMA`` and ``run_migrations``.
"""

import hashlib
import os
import re
import sqlite3
import tempfile
from pathlib import Path

import pytest

from app import create_app
from app.db import get_db, run_migrations
from app.services import popia_pack

ROOT = Path(__file__).resolve().parents[1]
POPIA_DIR = ROOT / "docs" / "popia"
MANIFEST_PATH = POPIA_DIR / "PACK-MANIFEST.md"

#: The four documents in the pack, in manifest order (pinned so a dropped or
#: reordered entry is caught; a FIFTH document added later is allowed).
PACK_KEYS = ["privacy_notice", "retention_policy", "action_plan", "operator_agreement"]


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


def _owner_id(app):
    with app.app_context():
        return get_db().execute(
            "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
        ).fetchone()["id"]


def _acceptance_count(app, key=None):
    with app.app_context():
        db = get_db()
        if key is None:
            return db.execute("SELECT COUNT(*) AS c FROM document_acceptances").fetchone()["c"]
        return db.execute(
            "SELECT COUNT(*) AS c FROM document_acceptances WHERE document_key = ?", (key,)
        ).fetchone()["c"]


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


def test_the_manifest_lists_the_four_documents_in_pack_order():
    manifest = popia_pack.PACK
    assert [entry["key"] for entry in manifest] == PACK_KEYS
    assert all(entry["title"] for entry in manifest)
    assert all(entry["file"] for entry in manifest)
    assert all(isinstance(entry["signature_required"], bool) for entry in manifest)


def test_every_manifest_document_resolves_to_a_real_file():
    paths = popia_pack.document_paths()
    for entry in popia_pack.PACK:
        path = paths[entry["key"]]
        assert path == POPIA_DIR / entry["file"]
        assert path.exists(), f"manifest file missing on disk: {path}"
    assert MANIFEST_PATH.exists()
    assert popia_pack.missing_documents() == []


def test_signature_required_is_true_only_for_the_operator_agreement():
    by_key = {entry["key"]: entry["signature_required"] for entry in popia_pack.PACK}
    assert by_key["operator_agreement"] is True
    for key in ("privacy_notice", "retention_policy", "action_plan"):
        assert by_key[key] is False


# ---------------------------------------------------------------------------
# outstanding_fields: the general rule, proven on a fixture and on the real pack
# ---------------------------------------------------------------------------


def _independent_placeholders(text):
    """The phase-14 rule written independently of the module, so this test is not
    just calling the code it claims to verify: any ``[...]`` whose contents start a
    capitalised word."""
    return [token for token in re.findall(r"\[[^\[\]]*\]", text or "") if re.search(r"\b[A-Z]", token[1:-1])]


def test_outstanding_fields_is_the_general_rule_on_a_synthetic_fixture():
    sample = (
        "Fine text. [TO CONFIRM — the date] and [WEBSITE URL] and "
        "[Confirm per branch before publication] and [TODAY] and [Render] "
        "but not [see section 5] and not [sic] and not []."
    )
    fields = popia_pack._outstanding_in_text(sample)
    assert "[TO CONFIRM — the date]" in fields
    assert "[WEBSITE URL]" in fields
    assert "[Confirm per branch before publication]" in fields
    assert "[TODAY]" in fields
    assert "[Render]" in fields
    assert "[see section 5]" not in fields
    assert "[sic]" not in fields
    assert popia_pack._outstanding_in_text("No brackets here at all.") == []


def test_outstanding_fields_finds_every_placeholder_in_the_real_files():
    """Against the real pack: whatever bracketed-uppercase tokens a file carries,
    outstanding_fields returns exactly them. It stays correct as the blockers get
    filled (it just returns fewer) and a new token cannot slip past."""
    for key in PACK_KEYS:
        expected = _independent_placeholders(popia_pack.document_text(key))
        assert popia_pack.outstanding_fields(key) == expected, f"{key}: the gate missed a token"


# ---------------------------------------------------------------------------
# acceptance: the hard gate, the record, staleness, the audit trail
# ---------------------------------------------------------------------------


def test_accept_is_refused_while_placeholders_remain_and_nothing_is_written(app, monkeypatch):
    user_id = _owner_id(app)
    # The gate is the *rule*, not the specific tokens: force a non-empty field list.
    monkeypatch.setattr(popia_pack, "outstanding_fields", lambda key: ["[TO CONFIRM — something]"])
    with app.app_context():
        with pytest.raises(ValueError):
            popia_pack.accept_document("privacy_notice", user_id)
        assert _acceptance_count(app, "privacy_notice") == 0
        assert _acceptance_count(app) == 0


def test_every_real_document_with_open_fields_is_refused_today(app):
    """The gate refuses the real pack for any document that still has placeholders,
    and a refusal writes nothing. Fill-proof: a finished document is simply not
    refused, so this stays green as the blockers land."""
    user_id = _owner_id(app)
    with app.app_context():
        before = _acceptance_count(app)
        for key in PACK_KEYS:
            if popia_pack.outstanding_fields(key):
                with pytest.raises(ValueError):
                    popia_pack.accept_document(key, user_id)
        assert _acceptance_count(app) == before


def test_accept_succeeds_on_a_clean_document_and_records_user_timestamp_and_hash(app, monkeypatch):
    user_id = _owner_id(app)
    monkeypatch.setattr(popia_pack, "outstanding_fields", lambda key: [])
    with app.app_context():
        row_id = popia_pack.accept_document("retention_policy", user_id)
        row = popia_pack.acceptance_for("retention_policy")
        expected_hash = hashlib.sha256(
            (POPIA_DIR / "RETENTION-POLICY-AND-SCHEDULE.md").read_bytes()
        ).hexdigest()
    assert row["id"] == row_id
    assert row["document_key"] == "retention_policy"
    assert row["document_hash"] == expected_hash
    assert row["document_hash"] == popia_pack.document_hash("retention_policy")
    assert row["accepted_by_user_id"] == user_id
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", row["accepted_at"])


def test_editing_a_document_makes_the_acceptance_stale_and_status_flips_back(app, monkeypatch):
    user_id = _owner_id(app)
    monkeypatch.setattr(popia_pack, "outstanding_fields", lambda key: [])
    with app.app_context():
        popia_pack.accept_document("action_plan", user_id)
        assert popia_pack.is_stale("action_plan") is False
        # The document is edited after adoption: its hash changes on disk.
        monkeypatch.setattr(popia_pack, "document_hash", lambda key: "edited-" + key)
        assert popia_pack.is_stale("action_plan") is True
        status = {entry["key"]: entry for entry in popia_pack.pack_status()}["action_plan"]
    assert status["stale"] is True
    assert status["accepted_at"] is not None
    assert status["accepted_by"] is not None


def test_accepting_twice_keeps_both_rows_and_the_newest_wins(app, monkeypatch):
    user_id = _owner_id(app)
    monkeypatch.setattr(popia_pack, "outstanding_fields", lambda key: [])
    with app.app_context():
        first = popia_pack.accept_document("action_plan", user_id)
        second = popia_pack.accept_document("action_plan", user_id)
        latest = popia_pack.acceptance_for("action_plan")
        rows = get_db().execute(
            "SELECT id FROM document_acceptances WHERE document_key = 'action_plan' ORDER BY id"
        ).fetchall()
    assert [row["id"] for row in rows] == [first, second]
    assert latest["id"] == second


def test_pack_status_reports_every_document_in_pack_order_with_title_required_and_fields(app):
    with app.app_context():
        status = popia_pack.pack_status()
    assert [entry["key"] for entry in status] == PACK_KEYS
    titles = {entry["key"]: entry["title"] for entry in status}
    assert titles["privacy_notice"] == "Privacy Notice"
    assert titles["operator_agreement"] == "Operator Agreement (ABI Solutions ↔ Sano Trailers)"
    by_key = {entry["key"]: entry for entry in status}
    assert by_key["operator_agreement"]["required"] is True
    assert by_key["privacy_notice"]["required"] is False
    assert isinstance(by_key["privacy_notice"]["outstanding_fields"], list)
    # Nothing adopted yet in a fresh database.
    assert by_key["privacy_notice"]["accepted_at"] is None
    assert by_key["privacy_notice"]["accepted_by"] is None
    assert by_key["privacy_notice"]["stale"] is False


# ---------------------------------------------------------------------------
# schema: data minimisation and the additive migration
# ---------------------------------------------------------------------------


def test_document_acceptances_stores_no_ip_address_and_no_user_agent(app):
    with app.app_context():
        columns = {
            row["name"]
            for row in get_db().execute("PRAGMA table_info(document_acceptances)").fetchall()
        }
    assert columns == {
        "id",
        "document_key",
        "document_version",
        "document_hash",
        "accepted_by_user_id",
        "accepted_at",
        "note",
    }
    for banned in ("ip", "ip_address", "user_agent", "fingerprint", "device", "channel"):
        assert banned not in columns


def test_document_acceptances_is_declared_in_the_schema_constant():
    from app.db import SCHEMA

    assert "CREATE TABLE IF NOT EXISTS document_acceptances" in SCHEMA
    assert "idx_document_acceptances_key" in SCHEMA
    assert "accepted_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL" in SCHEMA


def test_document_acceptances_is_created_by_run_migrations_on_an_existing_database(app):
    with app.app_context():
        db = get_db()
        db.execute("DROP INDEX IF EXISTS idx_document_acceptances_key")
        db.execute("DROP TABLE document_acceptances")
        db.commit()
        with pytest.raises(sqlite3.OperationalError):
            db.execute("SELECT COUNT(*) FROM document_acceptances").fetchone()
        run_migrations(db)
        db.commit()
        columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(document_acceptances)").fetchall()
        }
        assert "document_key" in columns
        assert "document_hash" in columns
        assert "accepted_at" in columns
        indexes = {
            row["name"] for row in db.execute("PRAGMA index_list(document_acceptances)").fetchall()
        }
        assert "idx_document_acceptances_key" in indexes
