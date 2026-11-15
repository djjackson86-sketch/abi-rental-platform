"""ABI-341953299 — the public portal's contact details, WhatsApp and directors block.

The owner's change, in the customer's words:

* the individual's phone is labelled **Cellphone No.**; an optional **WhatsApp No. (if different
  from the above)** sits directly below it;
* a **company** gets an optional **Telephone No** and an optional **Contact Person Name & Surname**
  above the cellphone, with a **Director** tick beside the contact person, and its cellphone is
  labelled **Contact Person Cellphone No.**;
* for a company the alternative-contact block is replaced by **Directors Name & Surname** and
  **Directors Contact**, both required — unless the contact person was ticked as a director, in
  which case the directors block is hidden/disabled and neither field is required (JavaScript
  **and** the server agree);
* an individual's alternative contact is unchanged, and an old alternative contact is **never**
  copied into the director fields.

The contact panel travels with the rest of the submission: ``submission_values`` marks every
portal payload with ``contact_details_panel='1'`` and carries ``whatsapp_number``,
``telephone_number``, ``contact_person_name``, ``contact_person_is_director`` (``'1'``/``'0'``),
``director_name`` and ``director_contact``, so a validation re-render (form_values) and a
re-parse keep every one of them, and the shared customers service persists them in the customer's
custom details.

Fixtures are synthetic, notifications are off, and no message is sent anywhere.
"""

import json
import os
import threading
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from werkzeug.datastructures import MultiDict
from werkzeug.serving import make_server

from app.db import get_db
from app.services import portal_intake
from test_client_portal_frontend import app, branch, client, slug_of  # noqa: F401 — pytest fixtures


@pytest.fixture(autouse=True)
def fresh_lookup_budget():
    """Every test starts with a full lookup budget — registration charges it, 10 per window."""
    portal_intake.reset_rate_limits()
    yield
    portal_intake.reset_rate_limits()


INDIVIDUAL = {
    "customer_type": "individual",
    "first_names": "Pieter",
    "surname": "van Wyk",
    "email": "pieter.contact@example.co.za",
    "phone": "083 555 1234",
    "whatsapp_number": "083 555 9999",
    "address_line1": "12 Kerk Street",
    "suburb": "Randburg",
    "city": "Johannesburg",
    "province": "Gauteng",
    "postal_code": "2194",
    "alternative_contact_name": "Jane Smith",
    "alternative_contact_number": "082 111 2222",
    "alternative_contact_relationship": "Sister",
    "popia_consent": "1",
    "own_use_declaration": "individual",
}

COMPANY = {
    "customer_type": "company",
    "name": "ABC Trading (Pty) Ltd.",
    "email": "accounts.contact@example.co.za",
    "phone": "011 555 1234",
    "telephone_number": "011 555 0000",
    "contact_person_name": "Jane Smith",
    "whatsapp_number": "082 999 8888",
    "address_line1": "12 Kerk Street",
    "suburb": "Randburg",
    "city": "Johannesburg",
    "province": "Gauteng",
    "postal_code": "2194",
    "director_name": "Thandi Mokoena",
    "director_contact": "084 765 4321",
    "popia_consent": "1",
    "own_use_declaration": "company",
}


def register(client, app, branch_id, data):
    return client.post(f"/portal/{slug_of(app, branch_id)}/register", data=dict(data))


def stored_customer(app, email):
    with app.app_context():
        row = get_db().execute("SELECT * FROM customers WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None


def custom_details(row):
    return json.loads((row or {}).get("custom_fields_json") or "{}")


def portal_body(client, app, branch_id):
    return client.get(f"/portal/{slug_of(app, branch_id)}/register").get_data(as_text=True)


def label_of(soup, name):
    field = soup.select_one(f'.customer-form [name="{name}"]')
    assert field is not None, f"{name} must be on the form"
    parent = field.find_parent("label")
    assert parent is not None, f"{name} must sit inside its label"
    return parent.get_text(" ", strip=True)


# ---------------------------------------------------------------- the markup: labels and order


def test_individual_phone_label_and_whatsapp_placement(client, app, branch):  # noqa: F811
    soup = BeautifulSoup(portal_body(client, app, branch), "html.parser")
    assert "Cellphone No." in label_of(soup, "phone")
    assert "Contact Person Cellphone No." not in label_of(soup, "phone")
    assert soup.select_one('.customer-form [name="phone"]').has_attr("required")
    whatsapp = soup.select_one('.customer-form [name="whatsapp_number"]')
    assert whatsapp is not None, "the optional WhatsApp number belongs on the form"
    assert not whatsapp.has_attr("required"), "WhatsApp is optional"
    assert label_of(soup, "whatsapp_number") == "WhatsApp No."
    body = str(soup.select_one(".customer-form"))
    assert body.index('name="phone"') < body.index('name="whatsapp_number"'), (
        "WhatsApp sits directly below the cellphone"
    )


def test_company_contact_fields_are_above_the_cellphone_and_optional(client, app, branch):  # noqa: F811
    # A company re-render (the validation path) is where the server renders the company labels.
    response = register(client, app, branch, {**COMPANY, "director_name": ""})
    assert response.status_code == 400
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    assert soup.select_one('[name="customer_type"][value="company"]').has_attr("checked")
    assert "Contact Person Cellphone No." in label_of(soup, "phone")
    assert "Directors Name & Surname" in label_of(soup, "director_name")
    assert "Directors Contact" in label_of(soup, "director_contact")
    for name in ("telephone_number", "contact_person_name"):
        field = soup.select_one(f'[name="{name}"]')
        assert field is not None
        assert not field.has_attr("required"), f"{name} is optional"
    assert "Telephone No" in label_of(soup, "telephone_number")
    assert "Contact Person Name & Surname" in label_of(soup, "contact_person_name")
    director_tick = soup.select_one('[name="contact_person_is_director"]')
    assert director_tick is not None and director_tick.get("type") == "checkbox"
    assert director_tick.get("value") == "1"
    assert label_of(soup, "contact_person_is_director") == "Tick if Director"
    body = str(soup.select_one(".customer-form"))
    assert body.index('name="telephone_number"') < body.index('name="contact_person_name"')
    assert body.index('name="contact_person_name"') < body.index('name="phone"'), (
        "the contact person sits above the cellphone"
    )
    assert body.index('name="phone"') < body.index('name="whatsapp_number"')
    # Company replaces the alternative contact with the directors block; the section heading is
    # kept so the page still reads the same way (and an existing requirements test pins it).
    heading = soup.select_one(".portal-requirements").find_previous_sibling("section").find("h2")
    assert heading.get_text(strip=True) == "Directors contact details"
    alternative = soup.select_one("[data-individual-alternative]")
    directors = soup.select_one("[data-company-director]")
    assert alternative.has_attr("hidden")
    assert not directors.has_attr("hidden")
    assert soup.select_one('[name="director_name"]').has_attr("required")
    assert soup.select_one('[name="director_contact"]').has_attr("required")
    for name in ("alternative_contact_name", "alternative_contact_number", "alternative_contact_relationship"):
        assert soup.select_one(f'[name="{name}"]').has_attr("disabled")


def test_a_director_contact_person_hides_and_disables_the_directors_block(client, app, branch):  # noqa: F811
    response = register(
        client, app, branch, {**COMPANY, "contact_person_is_director": "1", "director_name": "", "director_contact": ""}
    )
    assert response.status_code == 200, "a ticked director needs no separate directors block"
    soup = BeautifulSoup(portal_body(client, app, branch), "html.parser")
    assert soup.select_one("[data-company-director]").has_attr("hidden")
    assert soup.select_one('[name="director_name"]').has_attr("disabled")


# ---------------------------------------------------------------- submission_values / service


def test_submission_values_marks_the_panel_and_keeps_every_contact_key():
    individual = portal_intake.submission_values(MultiDict(INDIVIDUAL))
    assert individual["contact_details_panel"] == "1"
    assert individual["whatsapp_number"] == "083 555 9999"
    assert individual["contact_person_is_director"] == "0"
    assert individual["telephone_number"] == ""
    assert individual["director_name"] == ""

    company = portal_intake.submission_values(MultiDict(COMPANY))
    assert company["contact_details_panel"] == "1"
    assert company["whatsapp_number"] == "082 999 8888"
    assert company["telephone_number"] == "011 555 0000"
    assert company["contact_person_name"] == "Jane Smith"
    assert company["contact_person_is_director"] == "0"
    assert company["director_name"] == "Thandi Mokoena"
    assert company["director_contact"] == "084 765 4321"

    ticked = portal_intake.submission_values(
        MultiDict({**COMPANY, "contact_person_is_director": "1", "director_name": "", "director_contact": ""})
    )
    assert ticked["contact_person_is_director"] == "1"


def test_the_contact_panel_survives_a_service_reparse():
    """Validated values are passed through intake again on some paths — nothing may be lost."""
    for payload in (INDIVIDUAL, COMPANY):
        parsed = portal_intake.submission_values(MultiDict(payload))
        reparsed = portal_intake.submission_values(MultiDict(parsed))
        for key in ("whatsapp_number", "telephone_number", "contact_person_name",
                    "contact_person_is_director", "director_name", "director_contact"):
            assert reparsed[key] == parsed[key], key
        assert reparsed["contact_details_panel"] == "1"


def test_company_requires_the_directors_block(client, app, branch):  # noqa: F811
    data = dict(COMPANY)
    del data["director_name"]
    response = register(client, app, branch, data)
    assert response.status_code == 400
    assert "director" in response.get_data(as_text=True).lower()
    assert stored_customer(app, COMPANY["email"]) is None, "a refused company writes nothing"


def test_company_needs_no_alternative_contact(client, app, branch):  # noqa: F811
    response = register(client, app, branch, COMPANY)
    assert response.status_code == 200
    assert stored_customer(app, COMPANY["email"]) is not None


def test_individual_still_requires_the_alternative_contact(client, app, branch):  # noqa: F811
    data = {k: v for k, v in INDIVIDUAL.items() if not k.startswith("alternative_")}
    response = register(client, app, branch, data)
    assert response.status_code == 400
    assert "alternative contact" in response.get_data(as_text=True).lower()
    assert stored_customer(app, INDIVIDUAL["email"]) is None


def test_an_old_alternative_contact_is_not_turned_into_a_director(client, app, branch):  # noqa: F811
    response = register(
        client, app, branch,
        {
            **COMPANY,
            "director_name": "Real Director",
            "director_contact": "084 111 2222",
            "alternative_contact_name": "Old Alternative",
            "alternative_contact_number": "082 333 4444",
            "alternative_contact_relationship": "Friend",
        },
    )
    assert response.status_code == 200
    custom = custom_details(stored_customer(app, COMPANY["email"]))
    assert custom["director_name"] == "Real Director"
    assert custom["director_contact"] == "084 111 2222"


# ---------------------------------------------------------------- persistence through a POST


def test_company_submission_persists_the_contact_details_panel(client, app, branch):  # noqa: F811
    response = register(client, app, branch, COMPANY)
    assert response.status_code == 200
    row = stored_customer(app, COMPANY["email"])
    assert row["customer_type"] == "company"
    assert row["phone"] == "011 555 1234"
    custom = custom_details(row)
    assert custom["whatsapp_number"] == "082 999 8888"
    assert custom["telephone_number"] == "011 555 0000"
    assert custom["contact_person_name"] == "Jane Smith"
    assert custom["contact_person_is_director"] == "0"
    assert custom["director_name"] == "Thandi Mokoena"
    assert custom["director_contact"] == "084 765 4321"


def test_company_with_a_director_contact_person_persists_the_tick(client, app, branch):  # noqa: F811
    path = f"/portal/{slug_of(app, branch)}/register"
    response = client.post(
        path,
        data={**COMPANY, "contact_person_is_director": "1", "director_name": "", "director_contact": ""},
    )
    assert response.status_code == 200
    custom = custom_details(stored_customer(app, COMPANY["email"]))
    assert custom["contact_person_is_director"] == "1"
    assert not custom.get("director_name")


def test_individual_submission_persists_whatsapp_and_keeps_company_fields_off(client, app, branch):  # noqa: F811
    response = register(
        client, app, branch,
        {**INDIVIDUAL, "telephone_number": "011 000 0000", "director_name": "Forged Director"},
    )
    assert response.status_code == 200
    custom = custom_details(stored_customer(app, INDIVIDUAL["email"]))
    assert custom["whatsapp_number"] == "083 555 9999"
    assert custom["alternative_contact_name"] == "Jane Smith"
    assert not custom.get("telephone_number")
    assert not custom.get("director_name")
    assert custom.get("contact_person_is_director") in (None, "", "0")


# ---------------------------------------------------------------- every validation re-render


def test_a_missing_director_rerender_keeps_the_contact_panel(client, app, branch):  # noqa: F811
    response = register(
        client, app, branch,
        {**COMPANY, "director_name": "", "director_contact": ""},
    )
    assert response.status_code == 400
    body = response.get_data(as_text=True)
    assert 'value="082 999 8888"' in body, "the WhatsApp number is re-shown"
    assert 'value="011 555 0000"' in body, "the company telephone is re-shown"
    assert 'value="Jane Smith"' in body, "the contact person is re-shown"
    assert 'value="ABC Trading (Pty) Ltd."' in body


def test_a_missing_consent_rerender_keeps_the_contact_panel(client, app, branch):  # noqa: F811
    data = dict(COMPANY)
    del data["popia_consent"]
    response = register(client, app, branch, data)
    assert response.status_code == 400
    body = response.get_data(as_text=True)
    assert 'value="082 999 8888"' in body
    assert 'value="011 555 0000"' in body
    assert 'value="Jane Smith"' in body
    assert 'value="Thandi Mokoena"' in body


def test_a_wrong_own_use_rerender_keeps_the_whatsapp_number(client, app, branch):  # noqa: F811
    response = register(client, app, branch, {**INDIVIDUAL, "own_use_declaration": "company"})
    assert response.status_code == 400
    assert 'value="083 555 9999"' in response.get_data(as_text=True)


def test_the_director_tick_rerenders_ticked_with_its_block_disabled(client, app, branch):  # noqa: F811
    response = register(
        client, app, branch,
        {**COMPANY, "contact_person_is_director": "1", "telephone_number": "011 555 0101",
         "phone": "", "director_name": "", "director_contact": ""},
    )
    assert response.status_code == 400, "the blank cellphone forces a validation re-render"
    body = response.get_data(as_text=True)
    soup = BeautifulSoup(body, "html.parser")
    assert soup.select_one('[name="contact_person_is_director"]').has_attr("checked")
    assert soup.select_one("[data-company-director]").has_attr("hidden")
    assert 'value="011 555 0101"' in body


# ---------------------------------------------------------------- the real browser


@pytest.mark.parametrize("width,height", [(1440, 1000), (390, 844)])
def test_real_browser_company_individual_and_director_toggle(app, client, branch, width, height):  # noqa: F811
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

            phone = page.locator('.customer-form [name="phone"]')
            phone_label = page.locator('[data-phone-label]')
            whatsapp = page.locator('.customer-form [name="whatsapp_number"]')
            telephone = page.locator('.customer-form [name="telephone_number"]')
            contact_person = page.locator('.customer-form [name="contact_person_name"]')
            director_box = page.locator('.customer-form [name="contact_person_is_director"]')
            director_name = page.locator('.customer-form [name="director_name"]')
            director_contact = page.locator('.customer-form [name="director_contact"]')
            alternative = page.locator('.customer-form [name="alternative_contact_name"]')

            # An individual: Cellphone No., WhatsApp below it, no company/director controls.
            assert phone_label.inner_text() == "Cellphone No."
            assert phone.is_visible() and whatsapp.is_visible()
            assert whatsapp.bounding_box()["y"] > phone.bounding_box()["y"]
            assert phone.evaluate("el => el.required") is True
            assert whatsapp.evaluate("el => el.required") is False
            assert not telephone.is_visible() and not contact_person.is_visible()
            assert not director_box.is_visible()
            assert alternative.is_visible() and alternative.evaluate("el => el.required") is True
            assert not director_name.is_visible()

            # Company: the company contact fields appear above the cellphone, the directors block
            # replaces the alternative contact, and the cellphone is the contact person's.
            page.locator('[name="customer_type"][value="company"]').check()
            page.locator('.customer-form [name="name"]').fill("Preserved Company")
            assert phone_label.inner_text() == "Contact Person Cellphone No."
            for field in (telephone, contact_person, director_box):
                assert field.is_visible(), "company contact controls are visible for a company"
            assert telephone.bounding_box()["y"] < phone.bounding_box()["y"]
            assert contact_person.bounding_box()["y"] < phone.bounding_box()["y"]
            assert whatsapp.is_visible()
            assert not alternative.is_visible()
            assert alternative.evaluate("el => el.disabled") is True
            assert director_name.is_visible() and director_contact.is_visible()
            assert director_name.evaluate("el => el.required") is True
            assert director_contact.evaluate("el => el.required") is True

            # Typed values survive the switch, and ticking Director hides + disables the block.
            telephone.fill("011 555 0000")
            contact_person.fill("Jane Smith")
            whatsapp.fill("082 999 8888")
            director_name.fill("Thandi Mokoena")
            shots = Path(os.environ["LOCALAPPDATA"]) / "hermes/cache/scratch/ABI-341953299"
            shots.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(shots / f"public-company-{width}.png"), full_page=True)
            director_box.check()
            assert not director_name.is_visible() and not director_contact.is_visible()
            assert director_name.evaluate("el => el.disabled") is True
            assert director_name.evaluate("el => el.required") is False
            assert telephone.input_value() == "011 555 0000"
            assert contact_person.input_value() == "Jane Smith"
            assert whatsapp.input_value() == "082 999 8888"

            # Unticking brings the required block back; switching to individual restores theirs.
            director_box.uncheck()
            assert director_name.is_visible()
            assert director_name.evaluate("el => el.required") is True
            assert director_name.input_value() == "Thandi Mokoena"
            page.locator('[name="customer_type"][value="individual"]').check()
            assert phone_label.inner_text() == "Cellphone No."
            assert alternative.is_visible() and alternative.evaluate("el => el.required") is True
            assert not director_name.is_visible()
            assert not telephone.is_visible() and not contact_person.is_visible()
            assert whatsapp.is_visible() and whatsapp.input_value() == "082 999 8888"

            # Small screens must not scroll sideways because of the extra fields.
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
