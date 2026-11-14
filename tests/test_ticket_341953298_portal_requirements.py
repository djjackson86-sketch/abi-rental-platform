"""Informational portal requirements only: branch identity, type toggle and privacy gates."""
import threading
from pathlib import Path
import os

import pytest
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from app.db import get_db
from app.services import branches as branches_service, portal_intake
from test_client_portal_frontend import app, client, branch, slug_of

EMAILS = {
    "midrand": "mid@sanotrailers.co.za",
    "roodepoort": "cle@sanotrailers.co.za",
    "rooderport": "cle@sanotrailers.co.za",
    "pretoria": "pta@sanotrailers.co.za",
}


def add_branch(app, slug):
    with app.app_context():
        branch_id = branches_service.create_branch({"name": slug.title(), "active": 1})
        db = get_db()
        db.execute("UPDATE branches SET public_slug = ? WHERE id = ?", (slug, branch_id))
        db.commit()
    return branch_id


@pytest.mark.parametrize("slug,email", EMAILS.items())
@pytest.mark.parametrize("company", [False, True])
def test_branch_content_placement_and_postback(client, app, slug, email, company):
    add_branch(app, slug)
    # An administrative branch selection must never choose the recipient.
    with client.session_transaction() as session:
        session["branch_id"] = 9999
    response = client.get(f"/portal/{slug}") if not company else client.post(
        f"/portal/{slug}/register", data={"customer_type": "company", "email": "kept@example.com"}
    )
    assert response.status_code == (400 if company else 200)
    soup = BeautifulSoup(response.data, "html.parser")
    block = soup.select_one(".portal-requirements")
    assert block.find_previous_sibling("section").h2.text == "Alternative contact"
    assert block.h2.text == "REQUIREMENTS"
    assert block.select_one("[data-business-requirements]").has_attr("hidden") != company
    assert [a.text for a in block.select("a")] == [email]
    for text in ["Company Registration Certificate", "Copy of Directors ID (s)",
                 "Letter of Authority (Signed by Director) for the person who will sign the contracts",
                 "The driver must bring the following:", "RSA Citizen: original ID and licence",
                 "Foreign Nationals: original passport, work permit, drivers licence and traffic register",
                 "NB: All first time clients to pay with cards or EFT"]:
        assert text in block.get_text()
    assert not block.select("input,textarea,select")
    if company:
        assert soup.select_one('[name="email"]')["value"] == "kept@example.com"


def test_universal_lists_labelled_recipients_and_gate_stays_closed(client, monkeypatch):
    soup = BeautifulSoup(client.get("/portal").data, "html.parser")
    block = soup.select_one(".portal-requirements")
    assert [a.text for a in block.select("a")] == list(dict.fromkeys(EMAILS.values()))
    assert all(label in block.text for label in ["Midrand:", "Roodepoort:", "Pretoria:"])
    monkeypatch.setattr(portal_intake, "registration_is_open", lambda: False)
    closed = client.get("/portal").get_data(as_text=True)
    assert "Online registration isn" in closed
    assert 'class="form-section portal-requirements"' not in closed
    assert 'name="alternative_contact_name"' not in closed
    assert client.get("/portal/not-a-branch").status_code == 404


@pytest.mark.parametrize("width,height", [(1440, 1000), (390, 844), (320, 740)])
def test_real_browser_type_switch_and_readability(app, client, branch, width, height):
    slug = slug_of(app, branch)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome")
            page = browser.new_page(viewport={"width": width, "height": height})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/portal/{slug}")
            block = page.locator(".portal-requirements")
            business = page.locator("[data-business-requirements]")
            assert block.is_visible() and not business.is_visible()
            page.locator('[name="first_names"]').fill("Preserved")
            page.locator('[name="alternative_contact_name"]').fill("Alternative Tester")
            page.locator('[name="customer_type"][value="company"]').check()
            assert business.is_visible()
            page.locator('.customer-form [name="name"]').fill("Preserved Company")
            assert "The driver must bring" in block.inner_text()
            assert page.locator('[name="alternative_contact_name"]').input_value() == "Alternative Tester"
            assert block.evaluate("el => getComputedStyle(el).color") == "rgb(112, 32, 128)"
            assert block.locator("h2").evaluate("el => getComputedStyle(el).color") == "rgb(112, 32, 128)"
            assert block.locator("a").first.evaluate("el => getComputedStyle(el).color") == "rgb(112, 32, 128)"
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert block.evaluate("el => el.scrollWidth <= el.clientWidth")
            scratch = (Path(os.environ["LOCALAPPDATA"]) / "hermes" / "cache" / "scratch") if os.name == "nt" else Path(os.environ["TMPDIR"])
            shots = scratch / "ABI-341953298"
            shots.mkdir(parents=True, exist_ok=True)
            block.screenshot(path=str(shots / f"company-{width}.png"))
            page.locator('[name="customer_type"][value="individual"]').check()
            assert not business.is_visible()
            assert "Company Registration" not in block.inner_text()
            assert page.locator('[name="first_names"]').input_value() == "Preserved"
            assert page.locator('[name="alternative_contact_name"]').input_value() == "Alternative Tester"
            block.screenshot(path=str(shots / f"personal-{width}.png"))
            page.locator('[name="customer_type"][value="company"]').check()
            assert page.locator('.customer-form [name="name"]').input_value() == "Preserved Company"
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
