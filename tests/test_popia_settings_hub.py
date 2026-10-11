"""Owner POPIA hub: versioned editing, published print/PDF parity, and access guards."""
import hashlib
import html
import re
from pathlib import Path

import pytest
from app import create_app
from app.db import get_db
from app.services import popia_wizard, consent
from app.services.access import create_additional_user
from app.routes.public import render_markdown_html

ROOT = Path(__file__).resolve().parents[1]
VERSION = "sano-customer-privacy-v2"
TEXT = (ROOT / "docs/popia/SANO-CUSTOMER-PRIVACY-NOTICE.md").read_text(encoding="utf-8")
GET_PATHS = ["/settings/popia", "/settings/popia/notice/print", "/settings/popia/notice.pdf", "/settings/popia/agreement.pdf", "/settings/popia/information-notice.pdf", "/settings/popia/operator-contract.pdf"]


@pytest.fixture
def app(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "popia-hub.sqlite"),
                      "TURSO_DATABASE_URL": "", "TURSO_AUTH_TOKEN": "", "SECRET_KEY": "hub-test",
                      "ADMIN_PASSWORD": "local-test-owner-2026", "TELEGRAM_NOTIFICATIONS_ENABLED": ""})
    with app.app_context():
        db = get_db()
        owner = db.execute("SELECT id FROM users WHERE role='owner'").fetchone()[0]
        db.execute("INSERT INTO popia_notice_versions (notice_version,notice_content_hash,notice_text,published_at,published_by_user_id) VALUES (?,?,?,?,?)",
                   (VERSION, hashlib.sha256(TEXT.encode()).hexdigest(), TEXT, "2026-09-23T09:00:00+02:00", owner))
        db.commit()
    return app


@pytest.fixture
def owner(app):
    client = app.test_client()
    with app.app_context():
        user_id = get_db().execute("SELECT id FROM users WHERE role='owner'").fetchone()[0]
    with client.session_transaction() as session:
        session.update(user_id=user_id, user_role="owner", user_name="Synthetic owner", branch_id=1,
                       can_view_all_branches=True, staff_modules=[], branch_ids=[])
    return client


def form_data(owner, text=None):
    response = owner.get("/settings/popia")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)
    version = re.search(r'name="expected_version" value="([^"]+)"', body).group(1)
    return {"csrf_token": html.unescape(token), "expected_version": html.unescape(version),
            "notice_text": TEXT if text is None else text, "confirm_publish": "1"}


def test_owner_settings_nav_and_three_sections(owner):
    body = owner.get("/settings/general").get_data(as_text=True)
    assert 'href="/settings/popia"' in body
    response = owner.get("/settings/popia")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert all(text in body for text in ["1. Customer privacy notice", "2. Sano–Jackapp operator contract", "3. Register an Information Officer"])
    assert TEXT in html.unescape(body)
    assert 'href="https://eservices.inforegulator.org.za/"' in body
    assert 'href="https://inforegulator.org.za/information-officers/"' in body
    assert "55(2)" in body and "does not register them" in body
    assert "Both authorised representatives" in body
    assert "Downloading does not sign the contract" in body
    assert "Publishing updates the customer-facing notice immediately" in body
    assert "not unsaved edits" in body


def test_publish_updates_public_and_print_not_wizard_or_history(app, owner):
    with app.app_context():
        before = dict(popia_wizard.state())
        old = tuple(get_db().execute("SELECT * FROM popia_notice_versions").fetchone())
    updated = TEXT + "\n## Client amendment\nPlease send privacy questions to Sano.\n"
    response = owner.post("/settings/popia/notice", data=form_data(owner, updated), follow_redirects=True)
    assert response.status_code == 200
    assert "Privacy notice published as version" in response.get_data(as_text=True)
    with app.app_context():
        published = popia_wizard.published_notice()
        assert published["notice_text"] == updated
        assert published["notice_content_hash"] == hashlib.sha256(updated.encode()).hexdigest()
        assert published["notice_version"] != VERSION
        assert consent.current_notice_version() == published["notice_version"]
        assert tuple(get_db().execute("SELECT * FROM popia_notice_versions WHERE notice_version=?", (VERSION,)).fetchone()) == old
        assert dict(popia_wizard.state()) == before
        assert get_db().execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0
    for path in ["/privacy", "/settings/popia/notice/print"]:
        response = owner.get(path)
        assert response.status_code == 200
        assert "Client amendment" in response.get_data(as_text=True)
    print_page = owner.get("/settings/popia/notice/print")
    assert 'id="print-notice"' in print_page.get_data(as_text=True)
    assert "window.print()" in print_page.get_data(as_text=True)
    assert "no-store" in print_page.headers["Cache-Control"]


@pytest.mark.parametrize("missing", ["csrf_token", "expected_version", "confirm_publish", "notice_text"])
def test_publish_requires_token_version_review_and_text(app, owner, missing):
    data = form_data(owner)
    del data[missing]
    response = owner.post("/settings/popia/notice", data=data)
    assert response.status_code in (400, 409)
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM popia_notice_versions").fetchone()[0] == 1


def test_wrong_token_and_editor_overflow_are_rejected(owner):
    data = form_data(owner)
    data["csrf_token"] = "not-the-session-token"
    assert owner.post("/settings/popia/notice", data=data).status_code == 400
    data = form_data(owner, "x" * 30001)
    assert owner.post("/settings/popia/notice", data=data).status_code == 400
    assert owner.post("/settings/popia/notice", data={"notice_text": "x" * 400001}).status_code == 413


def test_stale_editor_preserves_entered_text_and_cannot_overwrite(app, owner):
    first = form_data(owner, TEXT + "\nFirst user's change.\n")
    stale = dict(first, notice_text=TEXT + "\nStale user's change.\n")
    assert owner.post("/settings/popia/notice", data=first).status_code == 302
    response = owner.post("/settings/popia/notice", data=stale)
    assert response.status_code == 409
    assert "Stale user's change." in html.unescape(response.get_data(as_text=True))
    assert "Reload the current notice" in response.get_data(as_text=True)
    with app.app_context():
        assert popia_wizard.published_notice()["notice_text"] == first["notice_text"]
        assert get_db().execute("SELECT COUNT(*) FROM popia_notice_versions").fetchone()[0] == 2


def test_unchanged_notice_does_not_add_version(app, owner):
    response = owner.post("/settings/popia/notice", data=form_data(owner), follow_redirects=True)
    assert response.status_code == 200
    assert "No wording changed" in response.get_data(as_text=True)
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM popia_notice_versions").fetchone()[0] == 1


@pytest.mark.parametrize("path", GET_PATHS + ["/settings/popia/notice"])
def test_anonymous_is_redirected_to_login(app, path):
    client = app.test_client()
    response = client.post(path) if path.endswith("/notice") else client.get(path)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


@pytest.mark.parametrize("path", GET_PATHS + ["/settings/popia/notice"])
def test_staff_cannot_edit_or_download(app, path):
    with app.app_context():
        user_id, error = create_additional_user("Synthetic counter staff", "synthetic-password-123")
        assert error is None
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=user_id, user_role="additional", user_name="Synthetic counter staff", branch_id=1,
                       can_view_all_branches=False, staff_modules=["settings"], branch_ids=[1])
    response = client.post(path) if path.endswith("/notice") else client.get(path)
    assert response.status_code == 403
    assert 'href="/settings/popia"' not in client.get("/settings/general").get_data(as_text=True)


def test_no_publication_keeps_setup_gate_and_no_print(app, owner):
    with app.app_context():
        # Isolated test fixture only; never remove a published production notice.
        get_db().execute("DELETE FROM popia_notice_versions")
        get_db().commit()
    response = owner.get("/settings/popia")
    assert response.status_code == 200
    assert "No customer notice has been published yet" in response.get_data(as_text=True)
    assert 'name="notice_text"' not in response.get_data(as_text=True)
    assert owner.get("/settings/popia/notice/print").status_code == 404
    assert owner.get("/settings/popia/notice.pdf").status_code == 404
    assert owner.get("/settings/popia/agreement.pdf").status_code == 200


def test_pdf_downloads_have_wrapped_content_signature_and_a4(owner):
    fitz = pytest.importorskip("fitz")
    for path, filename, markers in [
        ("/settings/popia/notice.pdf", "sano-customer-privacy-notice.pdf", [VERSION, "Who collects your information?", "Information Regulator"]),
        ("/settings/popia/agreement.pdf", "sano-jackapp-operator-contract.pdf", ["Jackapp", "Sano Trailers", "11. Electronic signing and copies", "Electronic signing", "contract"]),
    ]:
        response = owner.get(path)
        assert response.status_code == 200
        assert response.mimetype == "application/pdf"
        assert filename in response.headers["Content-Disposition"]
        assert "no-store" in response.headers["Cache-Control"]
        doc = fitz.open(stream=response.data, filetype="pdf")
        text = " ".join(page.get_text() for page in doc)
        text = " ".join(text.split())
        assert all(marker in text for marker in markers), text
        for page in doc:
            assert abs(page.rect.width - 595.276) < .1
            assert abs(page.rect.height - 841.89) < .1
            for word in page.get_text("words"):
                assert word[0] >= 40 and word[2] <= page.rect.width - 40, word
        doc.close()


@pytest.mark.parametrize("url", ["javascript:alert", "data:text/html,attack", "vbscript:attack", "//example.test", "file:///private.txt"])
def test_editable_notice_links_do_not_allow_active_schemes(url):
    rendered = str(render_markdown_html(f"[Read this]({url})"))
    assert "href=" not in rendered
    assert "Read this" in rendered


def test_editable_notice_escapes_raw_html_and_attribute_injection():
    from html.parser import HTMLParser
    class Links(HTMLParser):
        def __init__(self):
            super().__init__(); self.attrs = []
        def handle_starttag(self, tag, attrs):
            self.attrs.extend(attrs)
    rendered = str(render_markdown_html('<script>alert(1)</script>\n\n[Safe](https://example.test/"onmouseover="attack)'))
    assert "<script>" not in rendered
    parser = Links(); parser.feed(rendered)
    assert not any(name.lower().startswith("on") for name, value in parser.attrs)
    rendered = str(render_markdown_html("[Website](https://inforegulator.org.za/) [Email](mailto:info@sanotrailers.co.za)"))
    assert 'href="https://inforegulator.org.za/"' in rendered
    assert 'href="mailto:info@sanotrailers.co.za"' in rendered
