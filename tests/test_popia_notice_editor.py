"""Tests for the published-notice editor (``app/services/popia_notice_editor.py``).

These pin the client-facing guarantee: once a notice is published, a client can
correct its *wording* as a new immutable version, without the old wizard gate and
without disturbing history. One test per line of the acceptance:

* ``current_notice`` returns the newest row as a plain dict read by index (works
  with a libSQL SDK row that has no ``.keys()``);
* a publish is a new ``INSERT`` — the existing v1/v2 rows and the consent recorded
  against v1 are byte-for-byte preserved;
* the stored hash is the ``sha256`` of the exact normalised text and the new
  ``notice_version`` is date-stamped and unique;
* a stale ``expected_version`` is refused with ``NoticeConflict`` and writes
  nothing;
* re-publishing the exact same text is a no-op (``unchanged=True``, no new row);
* empty / over-long / control-character text is refused with ``ValueError`` and
  mutates nothing;
* there is no first-publication bypass.

Each test runs against an isolated scratch database created through ``create_app``.
"""

import hashlib
import os
import sqlite3
import tempfile

import pytest

from app import create_app
from app.db import get_db
from app.services import popia_notice_editor as editor
from app.services.customers import create_customer
from app.services.timezone import local_now


V1_TEXT = "Sano customer privacy notice, version 1\nSecond line of v1\n"
V2_TEXT = "Sano customer privacy notice, version 2\nUpdated wording for v2\n"


def _make_app(**overrides):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    config = {
        "TESTING": True,
        "DATABASE": path,
        "TURSO_DATABASE_URL": "",
        "TURSO_AUTH_TOKEN": "",
        "SECRET_KEY": "test",
        "ADMIN_EMAIL": "admin@abi.local",
        "ADMIN_PASSWORD": "admin123",
        "TELEGRAM_NOTIFICATIONS_ENABLED": "",
    }
    config.update(overrides)
    app = create_app(config)
    app._scratch_db_path = path
    return app


@pytest.fixture()
def app():
    application = _make_app()
    yield application
    os.unlink(application._scratch_db_path)


def _seed_history(app):
    """Seed a realistic history: v1, then v2 live, and a consent recorded on v1."""
    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO popia_notice_versions "
            "(notice_version, notice_content_hash, notice_text, published_at, "
            "published_by_user_id) VALUES (?, ?, ?, ?, NULL)",
            (
                "2026-01-01.1",
                hashlib.sha256(V1_TEXT.encode("utf-8")).hexdigest(),
                V1_TEXT,
                "2026-01-01T09:00:00",
            ),
        )
        db.execute(
            "INSERT INTO popia_notice_versions "
            "(notice_version, notice_content_hash, notice_text, published_at, "
            "published_by_user_id) VALUES (?, ?, ?, ?, NULL)",
            (
                "2026-02-02.2",
                hashlib.sha256(V2_TEXT.encode("utf-8")).hexdigest(),
                V2_TEXT,
                "2026-02-02T10:00:00",
            ),
        )
        customer_id = create_customer(
            {"name": "Synthetic Notice Customer", "phone": "0825550100"}
        )
        db.execute(
            "INSERT INTO consent_records "
            "(customer_id, consent_type, notice_version, channel, accepted_at) "
            "VALUES (?, 'popia_privacy', ?, 'in_store', '2026-01-01T09:05:00')",
            (customer_id, "2026-01-01.1"),
        )
        db.commit()


def test_current_notice_returns_newest_as_indexed_dict(app):
    _seed_history(app)
    with app.app_context():
        notice = editor.current_notice()
        assert isinstance(notice, dict)
        assert set(editor.NOTICE_FIELDS).issubset(notice.keys())
        assert notice["notice_version"] == "2026-02-02.2"
        assert notice["notice_text"] == V2_TEXT
        assert notice["notice_content_hash"] == hashlib.sha256(V2_TEXT.encode()).hexdigest()


def test_publish_new_version_preserves_history_and_consents(app):
    _seed_history(app)
    new_text = "Sano customer privacy notice, version 3\nClient-corrected wording\n"
    with app.app_context():
        result = editor.publish_notice(
            new_text, expected_version="2026-02-02.2", user_id=None
        )
        assert result["unchanged"] is False
        assert result["notice_text"] == new_text
        assert result["notice_content_hash"] == hashlib.sha256(new_text.encode()).hexdigest()
        assert result["notice_version"].startswith(local_now().date().isoformat() + ".")

        # The new row is now current.
        assert editor.current_notice()["notice_version"] == result["notice_version"]

        db = get_db()
        # All three rows remain, and v1/v2 are byte-for-byte untouched.
        rows = {
            r["notice_version"]: r["notice_text"]
            for r in db.execute(
                "SELECT notice_version, notice_text FROM popia_notice_versions"
            ).fetchall()
        }
        assert len(rows) == 3
        assert rows["2026-01-01.1"] == V1_TEXT
        assert rows["2026-02-02.2"] == V2_TEXT
        # The consent still points at the exact wording v1 saw.
        consent = db.execute(
            "SELECT notice_version FROM consent_records ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert consent["notice_version"] == "2026-01-01.1"


def test_stale_expected_version_is_refused_and_writes_nothing(app):
    _seed_history(app)
    with app.app_context():
        db = get_db()
        before = db.execute(
            "SELECT COUNT(*) AS c FROM popia_notice_versions"
        ).fetchone()["c"]
        with pytest.raises(editor.NoticeConflict):
            editor.publish_notice("Different text\n", expected_version="2026-01-01.1")
        after = db.execute(
            "SELECT COUNT(*) AS c FROM popia_notice_versions"
        ).fetchone()["c"]
        assert after == before
        assert editor.current_notice()["notice_version"] == "2026-02-02.2"


def test_unchanged_text_is_an_idempotent_no_op(app):
    _seed_history(app)
    with app.app_context():
        db = get_db()
        before = db.execute(
            "SELECT COUNT(*) AS c FROM popia_notice_versions"
        ).fetchone()["c"]
        result = editor.publish_notice(V2_TEXT, expected_version="2026-02-02.2")
        assert result["unchanged"] is True
        assert result["notice_version"] == "2026-02-02.2"
        after = db.execute(
            "SELECT COUNT(*) AS c FROM popia_notice_versions"
        ).fetchone()["c"]
        assert after == before


def test_crlf_is_normalised_to_lf(app):
    _seed_history(app)
    with app.app_context():
        result = editor.publish_notice(
            "Line one\r\nLine two\rLine three", expected_version="2026-02-02.2"
        )
        assert result["notice_text"] == "Line one\nLine two\nLine three"
        assert "\r" not in result["notice_text"]
        assert result["notice_content_hash"] == hashlib.sha256(
            b"Line one\nLine two\nLine three"
        ).hexdigest()


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   \n   ",
        "bad\x07bell",
        "has\x00nul",
        "x" * (editor.MAX_NOTICE_CHARS + 1),
    ],
)
def test_invalid_input_is_refused_and_mutates_nothing(app, bad):
    _seed_history(app)
    with app.app_context():
        db = get_db()
        before = db.execute(
            "SELECT COUNT(*) AS c FROM popia_notice_versions"
        ).fetchone()["c"]
        with pytest.raises(ValueError):
            editor.publish_notice(bad, expected_version="2026-02-02.2")
        after = db.execute(
            "SELECT COUNT(*) AS c FROM popia_notice_versions"
        ).fetchone()["c"]
        assert after == before
        assert editor.current_notice()["notice_version"] == "2026-02-02.2"


def test_no_first_publication_bypass(app):
    with app.app_context():
        assert editor.current_notice() is None
        with pytest.raises(ValueError):
            editor.publish_notice("A brand new notice\n", expected_version="anything")
        db = get_db()
        assert (
            db.execute("SELECT COUNT(*) AS c FROM popia_notice_versions").fetchone()["c"]
            == 0
        )


def test_sdk_rows_without_keys_are_supported():
    """A production libSQL SDK row (no ``.keys()``) must be read by index."""
    from libsql_client.result import Row

    def _sdk_row(cursor, values):
        return Row({item[0]: i for i, item in enumerate(cursor.description)}, tuple(values))

    original = sqlite3.Row
    sqlite3.Row = _sdk_row
    app = _make_app()
    try:
        _seed_history(app)
        with app.app_context():
            row = get_db().execute(
                "SELECT * FROM popia_notice_versions ORDER BY id LIMIT 1"
            ).fetchone()
            assert not hasattr(row, "keys")
            notice = editor.current_notice()
            assert notice["notice_version"] == "2026-02-02.2"
            result = editor.publish_notice(
                "Corrected via SDK row\n", expected_version="2026-02-02.2"
            )
            assert result["notice_text"] == "Corrected via SDK row\n"
            assert result["notice_version"].startswith(local_now().date().isoformat() + ".")
    finally:
        sqlite3.Row = original
        os.unlink(app._scratch_db_path)
