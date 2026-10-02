"""Programme phase 4 (feature A / A4) — the customer's Vehicles panel.

The acceptance list from ``docs/plans/2026-09-23-staff-vehicle-scan.md`` §A4, one test per line:

* the panel renders on the **Edit customer page** (and has an empty state when there is nothing
  recorded);
* it is **per client** — one client's page never shows another client's vehicles;
* the remove action takes the vehicle away and leaves the **client** intact;
* the edit action saves and the page redisplays what the database holds.

Where the panel lives changed on request: it used to be a section of its own on the customer page,
and it is now part of the Edit customer page, because vehicle details belong with the record the
counter edits. These tests follow it there — the customer page must NOT carry the panel any more,
and that is asserted below so the section cannot creep back.

The panel also carries **only the fields the app uses** (number plate, make, VIN, engine number,
disk expiry, plus NaTIS/disk numbers under the plate). Model, colour, year, vehicle type and the
tare/GVM masses were dropped from the scan screen, the panel and the edit form on request, so the
old column-index assertions are gone; what replaces them is a test that those columns are *absent*
and that a stored mass survives an edit that does not post it.

Two plan items are recorded here as measured facts rather than assumptions:

* **"branch/scope correct"** — this app does **not** branch-scope customers at all
  (``list_customers`` / ``get_customer`` carry no branch filter, unlike ``list_products``), so a
  "depot account sees only its own client's vehicles" test cannot exist: what the panel actually
  has to get right is the *per-client* scope below, plus the module gate on the controls it
  renders. ``test_customer_records_are_not_branch_scoped_today`` pins that observation so the
  next tick does not re-derive it.
* **the towing column** — removed by decision D3b, so a test asserts the word never appears on
  the page (a towing field creeping back into the panel is a regression, not a feature).
"""

import inspect
import os
import re
import tempfile
from pathlib import Path

import pytest

from app import create_app
from app.db import get_db
from app.services import customers as customers_service
from app.services import vehicles
from app.services.access import create_additional_user, save_user_modules
from app.services.customers import create_customer, get_customer
from app.services.vehicle_disk import parse_disc_text

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "disc"


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    application = create_app(
        {
            "TESTING": True,
            "DATABASE": path,
            "SECRET_KEY": "test",
            "ADMIN_EMAIL": "admin@abi.local",
            "ADMIN_PASSWORD": "admin123",
        }
    )
    yield application
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def charmaine(app):
    with app.app_context():
        return create_customer({"name": "Charmaine Mokoena", "customer_type": "individual", "phone": "0821234567"})


@pytest.fixture()
def pieter(app):
    with app.app_context():
        return create_customer({"name": "Pieter van Wyk", "customer_type": "individual", "phone": "0837654321"})


def login_owner(client):
    with client.application.app_context():
        row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
    return client.post("/login", data={"user_id": str(row["id"]), "password": "admin123"}, follow_redirects=True)


def make_staff(app, name="Sipho Nkosi", modules=("dashboard", "customers", "scan_vehicle"), password="staff1234"):
    with app.app_context():
        user_id, error = create_additional_user(name, password)
        assert error is None, error
        if modules is not None:
            saved_ok, saved = save_user_modules(user_id, list(modules))
            assert saved_ok, saved
    return user_id


def login_staff(client, user_id, password="staff1234"):
    return client.post("/login", data={"user_id": str(user_id), "password": password}, follow_redirects=True)


def add_vehicle(app, customer_id, **overrides):
    """Record a vehicle the way the scan screen does (source ``scan``)."""
    data = {
        "registration": "ABC123GP",
        "registration_number": "ZZ1234Z",
        "licence_number": "T9876543210X",
        "make": "MITSUBISHI",
        "model": "ASX",
        "colour": "WHITE",
        "year": "2019",
        "vin": "JMBXTGA2WJZ123456",
        "engine_number": "K9K7654321",
        "vehicle_type": "STATION WAGON",
        "licence_disk_expiry": "2027-07-31",
        "source": "scan",
    }
    data.update(overrides)
    with app.app_context():
        return vehicles.create_vehicle(data, customer_id=customer_id)


def html_of(response):
    return response.data.decode("utf-8")


def edit_page_of(client, customer_id):
    """The page the panel now lives on: the customer's own edit screen."""
    return html_of(client.get(f"/customers/{customer_id}/edit"))


def vehicle_row(html, plate):
    """The <tr> block of the read table that carries this number plate."""
    for block in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        if plate in block:
            return block
    return ""


def row_cells(row_html):
    return [re.sub(r"\s+", " ", cell).strip() for cell in re.findall(r"<td>(.*?)</td>", row_html, re.S)]


def panel_html(html):
    """Just the Vehicles panel, so assertions cannot be satisfied by another card."""
    start = html.find("customer-vehicles-card")
    assert start != -1, "the page has no vehicles panel"
    end = html.find("</article>", start)
    return html[start:end if end != -1 else len(html)]


# ── the panel exists on the edit page and is DB-accurate ────────────────────


def test_the_customer_page_no_longer_carries_a_vehicles_panel(app, client, charmaine):
    """The section was moved on request — the customer page must show the record, not vehicles."""
    add_vehicle(app, charmaine)
    login_owner(client)
    page = html_of(client.get(f"/customers/{charmaine}"))
    assert "customer-vehicles-card" not in page
    assert "ABC123GP" not in page


def test_the_edit_page_has_a_vehicles_panel_with_an_empty_state(app, client, charmaine):
    login_owner(client)
    response = client.get(f"/customers/{charmaine}/edit")
    assert response.status_code == 200
    panel = panel_html(html_of(response))
    assert "<h2>Vehicle details</h2>" in panel
    assert "No vehicles recorded" in panel
    assert "Scan a vehicle disk" in panel  # the empty-state call to action


def test_an_allocated_vehicle_appears_in_the_panel(app, client, charmaine):
    vehicle_id = add_vehicle(app, charmaine)
    login_owner(client)
    page = edit_page_of(client, charmaine)
    row = vehicle_row(page, "ABC123GP")
    assert row, "the vehicle row is not on the edit page"
    cells = row_cells(row)
    # only the fields the app uses, in order: plate, make, VIN, engine number, disk expiry
    assert cells[1] == "MITSUBISHI"
    assert cells[2] == "JMBXTGA2WJZ123456"
    assert cells[3] == "K9K7654321"
    assert cells[4] == "2027-07-31"
    assert f"/vehicles/{vehicle_id}/edit" in panel_html(page)


def test_the_panel_only_shows_this_clients_vehicles(app, client, charmaine, pieter):
    add_vehicle(app, charmaine, registration="ABC123GP")
    add_vehicle(app, pieter, registration="XYZ789GP", make="TOYOTA")
    login_owner(client)
    charmaine_html = edit_page_of(client, charmaine)
    pieter_html = edit_page_of(client, pieter)
    assert "ABC123GP" in panel_html(charmaine_html)
    assert "XYZ789GP" not in panel_html(charmaine_html)
    assert "XYZ789GP" in panel_html(pieter_html)
    assert "ABC123GP" not in panel_html(pieter_html)


def test_the_panel_matches_the_json_feed(app, client, charmaine):
    """DB↔UI parity: the rendered rows and `/customers/<id>/vehicles` are the same set."""
    add_vehicle(app, charmaine, registration="ABC123GP")
    add_vehicle(app, charmaine, registration="DEF456GP")
    login_owner(client)
    html = edit_page_of(client, charmaine)
    feed = client.get(f"/customers/{charmaine}/vehicles").get_json()
    assert feed["count"] == 2
    rendered = [v for v in ("ABC123GP", "DEF456GP") if vehicle_row(html, v)]
    assert rendered == ["ABC123GP", "DEF456GP"]
    assert {row["registration"] for row in feed["vehicles"]} == {"ABC123GP", "DEF456GP"}


# ── only the fields the app uses ────────────────────────────────────────────


def test_the_panel_carries_no_dropped_columns_or_inputs(app, client, charmaine):
    """Model, colour, year, vehicle type and the masses are not app inputs — they are gone.

    They were trimmed from the scan screen on request ("only show/capture details that the app
    uses as inputs"), so the panel and its edit form must not offer them back.
    """
    vehicle_id = add_vehicle(app, charmaine, tare_kg="1890", gvm_kg="2800", model="ASX", colour="WHITE", year="2019")
    login_owner(client)
    page = edit_page_of(client, charmaine)
    panel = panel_html(page)
    for dropped in ("Model", "Vehicle Color", "Colour", "Year", "Vehicle category", "Tare (kg)", "GVM (kg)"):
        assert dropped not in panel, f"{dropped} is not an app input and must not be on the panel"
    for dropped_field in ('name="tare_kg"', 'name="gvm_kg"', 'name="model"', 'name="colour"', 'name="year"'):
        assert dropped_field not in panel, dropped_field
    # …and the stored masses are untouched by an edit that does not post them
    with app.app_context():
        row = vehicles.get_vehicle(vehicle_id)
    assert row["tare_kg"] == 1890.0 and row["gvm_kg"] == 2800.0


def test_a_disc_that_carries_no_masses_still_records_the_rest(app, client, charmaine):
    """The modern NaTIS payload has no tare/GVM; the fields the app uses must all still land."""
    payload = (FIXTURE_DIR / "natis_positional.txt").read_text(encoding="utf-8")
    parsed = vehicles.fields_from_disc(parse_disc_text(payload))
    assert parsed["tare_kg"] == "" and parsed["gvm_kg"] == ""
    disc_fields = ("tare_kg", "gvm_kg", "make", "vin", "engine_number", "licence_disk_expiry")
    add_vehicle(app, charmaine, **{key: parsed[key] for key in disc_fields})
    login_owner(client)
    row = vehicle_row(edit_page_of(client, charmaine), "ABC123GP")
    assert row, "the vehicle the disc described is missing from the panel"
    cells = row_cells(row)
    assert cells[1] == parsed["make"]
    assert cells[2] == parsed["vin"]
    assert cells[3] == parsed["engine_number"]
    assert cells[4] == parsed["licence_disk_expiry"]
    assert cells[1] and cells[2] and cells[3], "the disc's own fields must render, not a dash"


# ── decision D3b: no towing anywhere ───────────────────────────────────────


def test_the_customer_and_edit_pages_carry_no_towing_field(app, client, charmaine):
    add_vehicle(app, charmaine)
    login_owner(client)
    for page in (html_of(client.get(f"/customers/{charmaine}")), edit_page_of(client, charmaine)):
        lowered = page.lower()
        assert "towing" not in lowered
        assert "gcm" not in lowered


# ── edit and remove from the edit page ─────────────────────────────────────


def test_editing_a_vehicle_from_the_edit_page_saves_and_redisplays(app, client, charmaine):
    vehicle_id = add_vehicle(app, charmaine)
    login_owner(client)
    response = client.post(
        f"/vehicles/{vehicle_id}/edit",
        data={
            "registration": "abc 123 gp",  # normalised on write
            "registration_number": "ZZ1234Z",
            "licence_number": "T9876543210X",
            "make": "MITSUBISHI",
            "vin": "JMBXTGA2WJZ123456",
            "engine_number": "K9K7654321",
            "licence_disk_expiry": "2028-01-31",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    with app.app_context():
        row = vehicles.get_vehicle(vehicle_id)
    assert row["registration"] == "ABC 123 GP"
    assert row["licence_disk_expiry"] == "2028-01-31"
    assert row["vin"] == "JMBXTGA2WJZ123456"  # untouched fields survive the edit
    page = html_of(response)
    cells = row_cells(vehicle_row(page, "ABC 123 GP"))
    assert cells[1] == "MITSUBISHI"
    assert cells[2] == "JMBXTGA2WJZ123456"
    assert cells[3] == "K9K7654321"
    assert cells[4] == "2028-01-31"
    # the save keeps the user on the page the panel lives on
    assert "Vehicle saved." in page and "Vehicle details" in page


def test_removing_a_vehicle_leaves_the_client_intact(app, client, charmaine):
    vehicle_id = add_vehicle(app, charmaine)
    login_owner(client)
    response = client.post(f"/vehicles/{vehicle_id}/delete", follow_redirects=True)
    assert response.status_code == 200
    page = html_of(response)
    assert "Vehicle removed — the client record is untouched." in page
    assert "No vehicles recorded" in panel_html(page)
    assert "Charmaine Mokoena" in page  # still the client's own record
    with app.app_context():
        assert vehicles.get_vehicle(vehicle_id) is None
        assert vehicles.list_vehicles(charmaine) == []
        assert get_customer(charmaine) is not None


def test_deleting_the_client_still_leaves_no_orphan_vehicles(app, charmaine):
    """The panel's rows are real vehicle rows, so the phase 2 cascade rule must still hold."""
    add_vehicle(app, charmaine)
    with app.app_context():
        assert vehicles.delete_vehicles_for_customer(charmaine) == 1
        row = get_db().execute("SELECT COUNT(*) AS c FROM vehicles WHERE customer_id = ?", (charmaine,)).fetchone()
        assert row["c"] == 0


# ── access: the panel follows the modules ──────────────────────────────────


def test_the_add_button_links_the_client_into_the_scan_screen(app, client, charmaine):
    login_owner(client)
    page = edit_page_of(client, charmaine)
    assert f'/scan-vehicle?customer_id={charmaine}' in page


def test_an_account_without_the_scan_module_sees_the_list_but_no_controls(app, client, charmaine):
    vehicle_id = add_vehicle(app, charmaine)
    user_id = make_staff(app, "Nomsa Dlamini", modules=("dashboard", "customers"))
    login_staff(client, user_id)
    response = client.get(f"/customers/{charmaine}/edit")
    assert response.status_code == 200
    page = html_of(response)
    # the list is the client's own record, so an account that may open the page may read it …
    assert "ABC123GP" in panel_html(page)
    # … but every control that would 403 is absent rather than rendered and broken
    assert 'href="/scan-vehicle' not in page.replace("&amp;", "&")
    assert "Add vehicle" not in page
    assert ">Remove vehicle<" not in page
    assert f"/vehicles/{vehicle_id}/edit" not in page
    assert client.post(f"/vehicles/{vehicle_id}/delete").status_code == 403  # …and the route agrees
    # the read-only feed stays open to this account (it is gated on `customers`, not `scan_vehicle`)
    feed = client.get(f"/customers/{charmaine}/vehicles")
    assert feed.status_code == 200 and feed.get_json()["count"] == 1


def test_an_account_without_the_customers_module_cannot_open_the_client_page(app, charmaine):
    user_id = make_staff(app, "Thabo Mokoena", modules=("dashboard", "orders"))
    client = app.test_client()
    login_staff(client, user_id)
    assert client.get(f"/customers/{charmaine}").status_code == 403
    assert client.get(f"/customers/{charmaine}/vehicles").status_code == 403


def test_customer_records_are_not_branch_scoped_today(app, client, charmaine):
    """Measured fact, not a wish: customers have no branch filter (unlike products).

    The plan's A4 line ("a depot account sees its own client's vehicles only") cannot be true while
    ``list_customers``/``get_customer`` carry no branch clause. Pinned here so the panel is not
    "fixed" against a scope the rest of the customer module does not have.
    """
    add_vehicle(app, charmaine)
    user_id = make_staff(app, "Depot Dlamini", modules=("dashboard", "customers"))
    login_staff(client, user_id)
    page = edit_page_of(client, charmaine)
    assert "ABC123GP" in panel_html(page)
    listing = inspect.getsource(customers_service.list_customers).lower()
    single = inspect.getsource(customers_service.get_customer).lower()
    assert "branch" not in listing, "customers are branch-scoped now — the panel needs the same filter"
    # get_customer joins the branch for its NAME only; the row it returns is not filtered by branch.
    assert "b.name as branch_name" in single and "branch_id in" not in single and "branch_id =" not in single
