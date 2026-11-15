"""ABI-341953300 — the portal REQUIREMENTS block is numbered and sits tighter.

The owner's change, in the customer's words: number the requirements, and close the gaps between
them. The words themselves are not to change, and the company-only documents must still be there
only for a company.

What this file pins:

* the requirements are real ``<ol>`` lists; company and driver documents each start at 1
  (the driver restart was requested in ABI-341953302);
* the copy is word-for-word what it was: no "1." typed into a sentence, the email line and "The
  driver must bring the following:" stay prose, and the NB note stays a bold note;
* the conditional lists survive: the company block (with its documents and its branch email) is
  hidden for an individual, and the universal portal still lists the three labelled branch
  addresses;
* the spacing is tight — 5px between blocks, no list margins (the email has breathing room), and list items that sit
  on consecutive lines — at desktop and phone widths.

Fixtures are synthetic, notifications are off, and nothing is sent anywhere.
"""

import os
import threading
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from app.db import get_db
from app.services import branches as branches_service
from test_client_portal_frontend import app, branch, client, slug_of  # noqa: F401 — pytest fixtures

COMPANY_DOCUMENTS = [
    "Company Registration Certificate",
    "Copy of Directors ID (s)",
    "Letter of Authority (Signed by Director) for the person who will sign the contracts",
]
DRIVER_DOCUMENTS = [
    "RSA Citizen: original ID and licence",
    "Foreign Nationals: original passport, work permit, drivers licence and traffic register",
]
NOTE = "NB: All first time clients to pay with cards or EFT"
BRANCH_EMAILS = {
    "midrand": "mid@sanotrailers.co.za",
    "roodepoort": "cle@sanotrailers.co.za",
    "pretoria": "pta@sanotrailers.co.za",
}


def add_branch(app, slug):
    with app.app_context():
        branch_id = branches_service.create_branch({"name": slug.title(), "active": 1})
        db = get_db()
        db.execute("UPDATE branches SET public_slug = ? WHERE id = ?", (slug, branch_id))
        db.commit()
    return branch_id


def requirement_block(soup):
    block = soup.select_one(".portal-requirements")
    assert block is not None, "the REQUIREMENTS block must be on the portal"
    return block


def lines_of(block):
    return [line for line in block.get_text("\n", strip=True).split("\n") if line]


def section_text(block):
    """The section's copy as one normalised string, with the hidden conditional block removed."""
    clone = BeautifulSoup(str(block), "html.parser")
    for hidden in clone.select("[hidden]"):
        hidden.decompose()
    return " ".join(clone.get_text(" ", strip=True).split())


def li_texts(list_element):
    return [li.get_text(" ", strip=True) for li in list_element.find_all("li", recursive=False)]


def driver_list(block):
    lists = block.select("[data-driver-requirements]")
    assert len(lists) == 1, "the driver's documents are one list"
    return lists[0]


def business_block(block):
    return block.select_one("[data-business-requirements]")


def business_list(block):
    return block.select_one("[data-business-requirements] ol")


# ---------------------------------------------------------------- the markup, as served


def test_company_renders_independent_numbered_document_lists(client, app):
    add_branch(app, "midrand")
    # A validation re-render is where the server draws the company labels and lists.
    response = client.post("/portal/midrand/register", data={"customer_type": "company", "email": "kept@example.com"})
    assert response.status_code == 400
    block = requirement_block(BeautifulSoup(response.get_data(as_text=True), "html.parser"))

    business = business_list(block)
    assert business is not None, "a company keeps its documents list"
    assert business.name == "ol", "the company's documents are numbered, not plain paragraphs"
    assert li_texts(business) == COMPANY_DOCUMENTS
    assert "start" not in business.attrs, "the company's documents start at 1"

    driver = driver_list(block)
    assert driver.name == "ol"
    assert li_texts(driver) == DRIVER_DOCUMENTS
    assert driver["start"] == "1", "driver documents restart independently of company documents"

    # The email line and the "must bring" line stay prose, in order, inside the conditional block.
    business_panel = business_block(block)
    assert [p.get_text(" ", strip=True) for p in business_panel.find_all("p")] == [
        "To be email at " + BRANCH_EMAILS["midrand"],
        "The driver must bring the following:",
    ]
    assert [a.get_text(strip=True) for a in business_panel.select("a")] == [BRANCH_EMAILS["midrand"]]

    # The copy is untouched, word for word — the numbers are list markers, never typed into a line.
    assert block.select_one("h2").get_text(strip=True) == "REQUIREMENTS"
    assert block.select_one("p strong").get_text(" ", strip=True) == NOTE
    assert section_text(block) == " ".join([
        "REQUIREMENTS", *COMPANY_DOCUMENTS, "To be email at " + BRANCH_EMAILS["midrand"],
        "The driver must bring the following:", *DRIVER_DOCUMENTS, NOTE,
    ])


def test_individual_starts_at_one_and_drops_the_company_documents(client, app, branch):  # noqa: F811
    block = requirement_block(BeautifulSoup(
        client.get(f"/portal/{slug_of(app, branch)}").get_data(as_text=True), "html.parser"
    ))

    assert block.select_one("[data-business-requirements]").has_attr("hidden")
    assert business_list(block) is not None, "the conditional company list stays in the markup"
    assert driver_list(block)["start"] == "1"

    assert section_text(block) == " ".join(["REQUIREMENTS", *DRIVER_DOCUMENTS, NOTE])
    assert not block.select("input,textarea,select")


def test_universal_keeps_the_labelled_branch_list_numbered_from_one(client):
    block = requirement_block(BeautifulSoup(client.get("/portal").get_data(as_text=True), "html.parser"))
    assert block.select_one("[data-business-requirements]").has_attr("hidden")
    assert driver_list(block)["start"] == "1"
    assert [a.get_text(strip=True) for a in block.select("a")] == list(BRANCH_EMAILS.values())
    assert [li.get_text(" ", strip=True) for li in block.select("ul li")] == [
        f"Midrand: {BRANCH_EMAILS['midrand']}",
        f"Roodepoort: {BRANCH_EMAILS['roodepoort']}",
        f"Pretoria: {BRANCH_EMAILS['pretoria']}",
    ]
    assert section_text(block) == " ".join(["REQUIREMENTS", *DRIVER_DOCUMENTS, NOTE])


# ---------------------------------------------------------------- the real browser


def serve(app):
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.mark.parametrize("width,height", [(1440, 1000), (390, 844), (320, 740)])
def test_real_browser_numbering_toggle_and_tight_spacing(app, client, branch, width, height):  # noqa: F811
    slug = slug_of(app, branch)
    server, thread = serve(app)
    shots = Path(os.environ["LOCALAPPDATA"]) / "hermes/cache/scratch/ABI-341953300" if os.name == "nt" else Path(os.environ["TMPDIR"]) / "ABI-341953300"
    shots.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome")
            page = browser.new_page(viewport={"width": width, "height": height})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/portal/{slug}")

            block = page.locator(".portal-requirements")
            driver = page.locator("[data-driver-requirements]")
            business = page.locator("[data-business-requirements]")
            business_items = business.locator("ol > li")

            def numbers_are(lst, first):
                """A <ol> whose items are decimal markers starting at ``first``."""
                assert lst.evaluate("el => el.tagName") == "OL"
                assert lst.evaluate("el => el.start") == first
                assert lst.evaluate("el => getComputedStyle(el).listStyleType") == "decimal"
                assert lst.locator("li").first.evaluate("el => getComputedStyle(el).display") == "list-item"

            def section_lines():
                return [line for line in block.inner_text().splitlines() if line.strip()]

            # An individual: two numbered driver documents, no company documents.
            assert not business.is_visible()
            numbers_are(driver, 1)
            assert driver.locator("li").count() == 2
            assert section_lines() == ["REQUIREMENTS", *DRIVER_DOCUMENTS, NOTE]

            # The spacing is tight: 5px between blocks, nothing added by paragraph or list margins,
            # and list items on consecutive lines.
            assert block.evaluate("el => getComputedStyle(el).rowGap") == "5px"
            for selector in (".portal-requirements p:not(.portal-requirement-email)", ".portal-requirements li"):
                margins = page.eval_on_selector_all(
                    selector, "els => els.map(el => [getComputedStyle(el).marginTop, getComputedStyle(el).marginBottom])"
                )
                assert margins, selector
                assert all(m == ["0px", "0px"] for m in margins), (selector, margins)
            first, second = driver.locator("li").nth(0).bounding_box(), driver.locator("li").nth(1).bounding_box()
            assert second["y"] - (first["y"] + first["height"]) <= 3, "list items sit on consecutive lines"
            heading = block.locator("h2").bounding_box()
            assert driver.bounding_box()["y"] - (heading["y"] + heading["height"]) <= 8
            block.screenshot(path=str(shots / f"individual-{width}.png"))

            # Switching to company adds the three documents and keeps the driver's list at 1-2.
            page.locator('[name="customer_type"][value="company"]').check()
            assert business.is_visible()
            numbers_are(driver, 1)
            assert business_items.count() == 3
            assert [business_items.nth(i).inner_text() for i in range(3)] == COMPANY_DOCUMENTS
            assert [driver.locator("li").nth(i).inner_text() for i in range(2)] == DRIVER_DOCUMENTS
            assert "The driver must bring the following:" in business.inner_text()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            block.screenshot(path=str(shots / f"company-{width}.png"))

            # Back to an individual: the company documents go, numbering returns to 1-2, copy intact.
            page.locator('[name="customer_type"][value="individual"]').check()
            assert not business.is_visible()
            numbers_are(driver, 1)
            assert section_lines() == ["REQUIREMENTS", *DRIVER_DOCUMENTS, NOTE]
            assert not errors, errors
            browser.close()
    finally:
        stop(server, thread)
