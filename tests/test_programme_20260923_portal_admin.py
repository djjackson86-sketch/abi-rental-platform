"""Programme phase 10 (feature B / §B3) — the admin links page, the QR sheet and DB↔UI parity.

The acceptance list from ``docs/plans/2026-09-23-branch-public-portal.md`` §B3, one test per line:

* ``/settings/portal`` lists **every** branch with its link, a Copy control, a QR preview, a
  "Print QR sheet" link and an enable/disable toggle plus an editable slug;
* the print sheet renders the QR, the plain link and the branch's address, and carries the
  print-only CSS hooks (``@page { size: A4; margin: 12mm }`` + a ``@media print`` rule that hides
  the screen-only controls);
* a duplicate slug is **refused with 409** and nothing is written;
* the module gate blocks a staff account without the module and lets one through that has it;
* a disabled branch's QR 404s (proven in §B1's file) — here the *admin* side must be honest about
  it: the page marks the branch off and the sheet refuses to print a code that 404s.

Two decisions this file pins on top of the plan:

* **§B3 closes the DB↔UI gap §B1's ledger recorded.** ``public_slug`` / ``portal_enabled`` /
  ``portal_intro`` had no admin UI at all; every one of them is writable from this page now, and
  the write is pinned to exactly those three columns so the ordinary branch form (which writes an
  explicit column list of its own) and this one cannot clobber each other.
* **The POPIA shut state is stated, not hidden.** While the published notice still carries Sano's
  open facts the portal form is closed (D11), so a branch handing out a printed QR would be handing
  out a dead end. The admin page says so, names what is outstanding, and the sheet carries the
  same sentence for the counter — and both are driven by the document, not by a second copy of the
  truth in code.
"""

import os
import sqlite3
import tempfile

import pytest

from app import create_app
from app.db import get_db
from app.services import branches as branches_service
from app.services import portal, portal_intake
from app.services.access import create_additional_user, save_user_modules

BRANCHES = [
    ("Midrand", "229 Summit Road, Midrand", "010 001 0001"),
    ("Roodepoort", "14 Hendrik Potgieter Road, Roodepoort", "011 002 0002"),
    ("Pretoria", "300 Lynnwood Road, Pretoria", "012 003 0003"),
]


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


def seed_branches(app):
    """Three real-name branches through the real service path (the admin form's own code)."""
    ids = {}
    with app.app_context():
        for name, address, phone in BRANCHES:
            ids[name] = branches_service.create_branch(
                {"name": name, "address_line1": address, "phone": phone, "active": 1}
            )
    return ids


def login_owner(client):
    with client.application.app_context():
        row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
    return client.post(
        "/login", data={"user_id": str(row["id"]), "password": "admin123"}, follow_redirects=True
    )


def make_staff(app, name, modules, password="staff1234"):
    with app.app_context():
        user_id, error = create_additional_user(name, password)
        assert error is None, error
        ok, saved = save_user_modules(user_id, list(modules))
        assert ok, saved
    return user_id


def login_staff(client, user_id, password="staff1234"):
    return client.post("/login", data={"user_id": str(user_id), "password": password}, follow_redirects=True)


def branch_row(app, branch_id):
    with app.app_context():
        return get_db().execute("SELECT * FROM branches WHERE id = ?", (branch_id,)).fetchone()


def slugs(app):
    with app.app_context():
        return {
            row["name"]: row["public_slug"]
            for row in get_db().execute("SELECT name, public_slug FROM branches").fetchall()
        }


def text_of(response):
    return response.get_data(as_text=True)


# --- the links page (GET /settings/portal) ------------------------------------------------------

def test_links_page_lists_every_branch_with_its_link(app, client):
    seed_branches(app)
    login_owner(client)
    response = client.get("/settings/portal")
    body = text_of(response)
    assert response.status_code == 200
    for name, address, _phone in BRANCHES:
        assert name in body
        assert address in body
        assert f"/portal/{portal.slugify(name)}" in body
    # Every branch gets its own QR preview and its own print link — counted against the branches
    # that actually exist (the app seeds three of its own on first run), not against the three
    # seeded here.
    with app.app_context():
        branch_count = get_db().execute("SELECT COUNT(*) AS c FROM branches").fetchone()["c"]
    assert body.count("/qr.png") == branch_count
    assert body.count("/print") >= branch_count


def test_links_page_shows_a_readonly_link_and_a_copy_control_per_branch(app, client):
    seed_branches(app)
    login_owner(client)
    body = text_of(client.get("/settings/portal"))
    assert body.count('readonly') >= len(BRANCHES), "the link must be selectable text staff can copy"
    assert body.count("data-copy-target") >= len(BRANCHES)
    assert "navigator.clipboard" in body, "the copy button has to write to the real clipboard"
    assert "execCommand" in body, "…with a fallback for a browser that refuses the clipboard API"


def test_links_page_says_the_form_is_shut_while_the_notice_is_unfinished(app, client):
    """Honesty requirement: a branch must not hand out a QR whose form nobody told them is closed."""
    from app.services import popia_pack

    seed_branches(app)
    login_owner(client)
    body = text_of(client.get("/settings/portal"))
    assert portal_intake.registration_is_open() is False, "shipped state: Sano's facts are still open"
    assert "not open yet" in body.lower()
    assert "privacy notice" in body.lower()
    # …and it says how many facts are open, without quoting a single one of them.
    open_items = popia_pack.outstanding_fields(popia_pack.PRIVACY_NOTICE_KEY)
    assert open_items, "shipped state: the notice really does still carry open facts"
    assert f"{len(open_items)} facts on the notice are still open" in body


def test_links_page_banner_disappears_the_moment_the_notice_is_publishable(app, client, monkeypatch):
    seed_branches(app)
    login_owner(client)
    monkeypatch.setattr(portal_intake, "registration_is_open", lambda: True)
    body = text_of(client.get("/settings/portal"))
    assert "not open yet" not in body.lower()


def test_links_page_carries_no_raw_placeholder_token(app, client):
    """The page names what is outstanding, but never republishes the `[…]` token itself.

    The rule is asserted as "no raw token from the notice appears here" rather than "no bracket
    appears here": the second would false-fail the day a staff page grows a line of JavaScript with
    an array literal, and none of those is what D11 is about. The tokens themselves are the thing
    that must not travel — ``outstanding_summaries`` is what strips them.
    """
    from app.services import popia_pack

    seed_branches(app)
    login_owner(client)
    body = text_of(client.get("/settings/portal"))
    tokens = popia_pack.outstanding_fields(popia_pack.PRIVACY_NOTICE_KEY)
    assert tokens, "shipped state: Sano's facts are still open, which is what makes this test real"
    for token in tokens:
        assert token not in body
    assert "[TO CONFIRM" not in body
    assert "____" not in body


def test_links_page_marks_a_switched_off_branch_and_hides_its_qr(app, client):
    ids = seed_branches(app)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE branches SET portal_enabled = 0 WHERE id = ?", (ids["Pretoria"],))
        db.commit()
    login_owner(client)
    response = client.get("/settings/portal")
    body = text_of(response)
    assert response.status_code == 200
    assert "Portal off" in body
    # The off branch's QR endpoint genuinely 404s, so the page must not render a broken image for it.
    assert f'/portal/{portal.slugify("Pretoria")}/qr.png' not in body
    assert f'/portal/{portal.slugify("Pretoria")}/print' not in body


# --- the A4 sheet (GET /settings/portal/<id>/print) ---------------------------------------------

def test_print_sheet_renders_the_qr_the_link_and_the_address(app, client):
    ids = seed_branches(app)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE company_settings SET public_base_url = 'https://sano-trailers.example' WHERE id = 1")
        db.commit()
    login_owner(client)
    response = client.get(f"/settings/portal/{ids['Roodepoort']}/print")
    body = text_of(response)
    assert response.status_code == 200
    assert "Roodepoort" in body
    assert "14 Hendrik Potgieter Road, Roodepoort" in body
    # The code on the sheet is the same link the customer gets, as selectable text underneath it.
    assert "https://sano-trailers.example/portal/roodepoort" in body
    assert "/portal/roodepoort/qr.png" in body
    assert "register your details" in body.lower()


def test_print_sheet_asks_for_a_bigger_qr_than_the_screen_preview(app, client):
    ids = seed_branches(app)
    login_owner(client)
    body = text_of(client.get(f"/settings/portal/{ids['Midrand']}/print"))
    assert f"box={portal.QR_PRINT_BOX_SIZE_PX}" in body
    assert portal.QR_PRINT_BOX_SIZE_PX > 10, "a sheet printed at 80mm needs more pixels than the screen"


def test_print_sheet_carries_the_a4_print_hooks(app, client):
    ids = seed_branches(app)
    login_owner(client)
    body = text_of(client.get(f"/settings/portal/{ids['Midrand']}/print"))
    assert "@page" in body and "size: A4" in body and "margin: 12mm" in body
    assert "@media print" in body
    assert "no-print" in body, "the screen-only controls need a class the print rule can hide"
    # …and no admin chrome on the sheet at all: it is printed and handed to a customer. The template
    # extends base.html directly rather than admin/layout.html, so there is no navigation to hide.
    assert "app-shell" not in body
    assert "Log out" not in body


def test_print_sheet_carries_no_raw_placeholder_token(app, client):
    """A sheet that gets printed, stuck to a counter and scanned must carry no open token either."""
    from app.services import popia_pack

    ids = seed_branches(app)
    login_owner(client)
    body = text_of(client.get(f"/settings/portal/{ids['Midrand']}/print"))
    for token in popia_pack.outstanding_fields(popia_pack.PRIVACY_NOTICE_KEY):
        assert token not in body
    assert "[TO CONFIRM" not in body
    assert "____" not in body


def test_print_sheet_tells_the_counter_when_the_form_is_shut(app, client, monkeypatch):
    from app.services import popia_pack

    ids = seed_branches(app)
    login_owner(client)
    body = text_of(client.get(f"/settings/portal/{ids['Midrand']}/print"))
    assert "not open yet" in body.lower()
    open_items = popia_pack.outstanding_fields(popia_pack.PRIVACY_NOTICE_KEY)
    assert f"{len(open_items)} facts on the notice are still open" in body
    monkeypatch.setattr(portal_intake, "registration_is_open", lambda: True)
    body = text_of(client.get(f"/settings/portal/{ids['Midrand']}/print"))
    assert "not open yet" not in body.lower()


def test_print_sheet_refuses_a_switched_off_branch(app, client):
    ids = seed_branches(app)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE branches SET portal_enabled = 0 WHERE id = ?", (ids["Pretoria"],))
        db.commit()
    login_owner(client)
    response = client.get(f"/settings/portal/{ids['Pretoria']}/print")
    assert response.status_code == 302, "a sheet whose QR 404s must not be printable"
    assert response.headers["Location"].endswith("/settings/portal")
    followed = client.get(f"/settings/portal/{ids['Pretoria']}/print", follow_redirects=True)
    assert "switched off" in text_of(followed).lower()
    assert "/portal/pretoria/qr.png" not in text_of(followed)


def test_print_sheet_and_save_404_for_an_unknown_branch(client):
    login_owner(client)
    assert client.get("/settings/portal/9999/print").status_code == 404
    assert client.post("/settings/portal/9999", data={"public_slug": "x"}).status_code == 404


# --- DB↔UI parity: the three §B1 columns are now editable ----------------------------------------

def test_save_switches_a_portal_off_and_the_public_page_stops_serving_it(app, client):
    ids = seed_branches(app)
    login_owner(client)
    assert client.get("/portal/midrand").status_code == 200
    response = client.post(
        f"/settings/portal/{ids['Midrand']}",
        data={"public_slug": "midrand", "portal_intro": ""},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "switched off" in text_of(response).lower()
    assert branch_row(app, ids["Midrand"])["portal_enabled"] == 0
    assert client.get("/portal/midrand").status_code == 404
    assert client.get("/portal/midrand/qr.png").status_code == 404
    # …and back on again, so the toggle is not a one-way door.
    client.post(
        f"/settings/portal/{ids['Midrand']}",
        data={"public_slug": "midrand", "portal_enabled": "1", "portal_intro": ""},
        follow_redirects=True,
    )
    assert branch_row(app, ids["Midrand"])["portal_enabled"] == 1
    assert client.get("/portal/midrand").status_code == 200


def test_save_normalises_a_typed_slug(app, client):
    ids = seed_branches(app)
    login_owner(client)
    response = client.post(
        f"/settings/portal/{ids['Pretoria']}",
        data={"public_slug": "  North  Gate  ", "portal_enabled": "1", "portal_intro": ""},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert branch_row(app, ids["Pretoria"])["public_slug"] == "north-gate"
    assert "north-gate" in text_of(response)


def test_save_refuses_a_duplicate_slug_with_409_and_writes_nothing(app, client):
    ids = seed_branches(app)
    login_owner(client)
    response = client.post(
        f"/settings/portal/{ids['Pretoria']}",
        data={"public_slug": "roodepoort", "portal_enabled": "1", "portal_intro": "Bring your ID."},
    )
    assert response.status_code == 409
    body = text_of(response)
    assert "Roodepoort" in body, "the refusal names the branch that already owns the link"
    row = branch_row(app, ids["Pretoria"])
    assert row["public_slug"] == "pretoria", "a refused save must leave the slug alone"
    assert row["portal_intro"] == "", "…and must not write the rest of the form either"


def test_save_refuses_a_blank_slug_with_400_and_writes_nothing(app, client):
    ids = seed_branches(app)
    login_owner(client)
    response = client.post(
        f"/settings/portal/{ids['Midrand']}",
        data={"public_slug": "   ", "portal_enabled": "1", "portal_intro": "Hello"},
    )
    assert response.status_code == 400
    row = branch_row(app, ids["Midrand"])
    assert row["public_slug"] == "midrand"
    assert row["portal_intro"] == ""


def test_save_refuses_an_over_long_welcome_line(app, client):
    ids = seed_branches(app)
    login_owner(client)
    response = client.post(
        f"/settings/portal/{ids['Midrand']}",
        data={"public_slug": "midrand", "portal_enabled": "1", "portal_intro": "x" * 400},
    )
    assert response.status_code == 400
    assert branch_row(app, ids["Midrand"])["portal_intro"] == ""


def test_the_welcome_line_reaches_the_public_form(app, client, monkeypatch):
    """DB↔UI parity for ``portal_intro``: what the admin types is what the customer reads."""
    ids = seed_branches(app)
    login_owner(client)
    client.post(
        f"/settings/portal/{ids['Midrand']}",
        data={
            "public_slug": "midrand",
            "portal_enabled": "1",
            "portal_intro": "Bring your ID and a copy of the licence.",
        },
        follow_redirects=True,
    )
    monkeypatch.setattr(portal_intake, "registration_is_open", lambda: True)
    body = text_of(client.get("/portal/midrand"))
    # No apostrophe in the fixture text: Jinja escapes it on the customer page, which would make
    # this assertion test the escaping rather than the DB↔UI link.
    assert "Bring your ID and a copy of the licence" in body


def test_changing_the_slug_moves_the_link_and_the_qr_with_it(app, client):
    ids = seed_branches(app)
    login_owner(client)
    client.post(
        f"/settings/portal/{ids['Roodepoort']}",
        data={"public_slug": "roodepoort-west", "portal_enabled": "1", "portal_intro": ""},
        follow_redirects=True,
    )
    assert client.get("/portal/roodepoort").status_code == 404, "the old link stops resolving"
    assert client.get("/portal/roodepoort-west").status_code == 200
    assert client.get("/portal/roodepoort-west/qr.png").status_code == 200
    body = text_of(client.get("/settings/portal"))
    assert "/portal/roodepoort-west" in body
    assert "/portal/roodepoort" not in body.replace("/portal/roodepoort-west", "")


def test_saving_the_portal_form_leaves_the_rest_of_the_branch_alone(app, client):
    """The page owns exactly three columns; the branch's own form keeps owning the rest."""
    ids = seed_branches(app)
    login_owner(client)
    before = dict(branch_row(app, ids["Midrand"]))
    client.post(
        f"/settings/portal/{ids['Midrand']}",
        data={"public_slug": "midrand-depot", "portal_enabled": "1", "portal_intro": "Hi"},
        follow_redirects=True,
    )
    after = dict(branch_row(app, ids["Midrand"]))
    changed = {key for key in after if after[key] != before[key]}
    # ``updated_at`` is second-resolution, so a save in the same second leaves it identical — it is
    # allowed to change, never required to.
    assert {"public_slug", "portal_intro"} <= changed, changed
    assert changed <= {"public_slug", "portal_intro", "updated_at"}, changed
    assert after["name"] == before["name"]
    assert after["address_line1"] == before["address_line1"]
    # …and a normal branch edit still cannot clobber the portal settings (§B1's pinned rule).
    with app.app_context():
        branches_service.update_branch(ids["Midrand"], {"name": "Midrand Depot", "active": 1})
    row = branch_row(app, ids["Midrand"])
    assert (row["public_slug"], row["portal_intro"], row["portal_enabled"]) == ("midrand-depot", "Hi", 1)


def test_service_refuses_a_duplicate_slug_directly(app):
    ids = seed_branches(app)
    with app.app_context():
        with pytest.raises(portal.DuplicateSlugError):
            portal.update_portal_settings(ids["Pretoria"], {"public_slug": "midrand", "portal_enabled": "1"})
        assert portal.DuplicateSlugError.__mro__[1] is ValueError, "kept a ValueError for existing callers"


def test_database_still_backstops_the_slug_uniqueness(app):
    ids = seed_branches(app)
    with app.app_context():
        db = get_db()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE branches SET public_slug = 'midrand' WHERE id = ?", (ids["Pretoria"],)
            )


# --- access --------------------------------------------------------------------------------------

def test_module_gate_blocks_a_staff_account_without_the_module(app, client):
    ids = seed_branches(app)
    staff = make_staff(app, "Counter Staff", ["dashboard", "orders", "customers"])
    login_staff(client, staff)
    assert client.get("/settings/portal").status_code == 403
    assert client.get(f"/settings/portal/{ids['Midrand']}/print").status_code == 403
    assert client.post(f"/settings/portal/{ids['Midrand']}", data={"public_slug": "midrand"}).status_code == 403
    assert "Customer portal" not in text_of(client.get("/dashboard"))


def test_module_gate_lets_a_staff_account_with_the_module_through(app, client):
    ids = seed_branches(app)
    staff = make_staff(app, "Branch Manager", ["dashboard", "settings"])
    login_staff(client, staff)
    assert client.get("/settings/portal").status_code == 200
    assert client.get(f"/settings/portal/{ids['Midrand']}/print").status_code == 200
    assert "Customer portal" in text_of(client.get("/settings/portal"))


def test_the_sidebar_carries_the_customer_portal_entry_behind_the_module_gate(app, client):
    """§B3 asks for a nav entry; the gate is the same one the routes use, not a second decision."""
    seed_branches(app)
    login_owner(client)
    body = text_of(client.get("/dashboard"))
    assert 'href="/settings/portal"' in body, "the owner's sidebar must link to the portal page"
    assert "Customer portal" in body

    with app.app_context():
        staff = make_staff(app, "Counter Only", ["dashboard", "customers"])
    client.get("/logout")
    login_staff(client, staff)
    body = text_of(client.get("/dashboard"))
    assert 'href="/settings/portal"' not in body, "an account without settings must not be offered it"
    assert "Customer portal" not in body


# --- the QR size knob the sheet needs ------------------------------------------------------------

def test_qr_box_parameter_returns_a_bigger_code_that_still_decodes(app, client):
    ids = seed_branches(app)
    plain = client.get("/portal/midrand/qr.png").get_data()
    bigger = client.get(f"/portal/midrand/qr.png?box={portal.QR_PRINT_BOX_SIZE_PX}").get_data()
    import io

    import zxingcpp
    from PIL import Image

    plain_image = Image.open(io.BytesIO(plain))
    bigger_image = Image.open(io.BytesIO(bigger))
    # The byte length is not monotonic (PNG compresses a bigger code better), so the size claim is
    # made on the pixels — which is what decides how many dots land on the paper.
    assert bigger_image.width > plain_image.width
    assert bigger_image.width >= 300, "the sheet needs a code with real dots behind it"
    for image in (plain_image, bigger_image):
        results = zxingcpp.read_barcodes(image)
        assert results and results[0].text == "http://localhost/portal/midrand"


def test_qr_box_parameter_is_clamped_and_junk_ignored(app, client):
    ids = seed_branches(app)
    # Junk and out-of-range values must not 500 — the QR is a printed artefact staff rely on.
    assert client.get("/portal/midrand/qr.png?box=abc").status_code == 200
    assert client.get("/portal/midrand/qr.png?box=-3").status_code == 200
    assert client.get("/portal/midrand/qr.png?box=9999").status_code == 200
    huge = client.get("/portal/midrand/qr.png?box=9999").get_data()
    capped = client.get(f"/portal/midrand/qr.png?box={portal.QR_BOX_SIZE_MAX}").get_data()
    assert huge == capped
