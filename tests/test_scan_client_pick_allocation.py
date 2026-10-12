"""Regression: the scan screen must keep the client staff picked out of the typeahead.

The reported failure (Sano staff, screen recording): the client was picked from the suggestions,
"not the main vehicle" was chosen, Save vehicle answered *"Choose the client from the list…"* and
no vehicle was allocated. Two things were wrong, and both are pinned here:

* the picker's own debounced refetch runs for the label it just wrote into the box, and a label
  (``Name · 0748848822``) matches no name or phone, so the search answers with an **empty** list —
  the save then re-resolved the unchanged box against that empty list and blanked the hidden id;
* the client whose number is on file in two formats (``+27748848822`` and ``0748848822``) is
  digit-for-digit the same as the other record of that name, so the server's digit-normalised
  resolution was ambiguous and refused even though the label identifies exactly one record.

``find_customer_id_by_text`` therefore resolves the *exact label the typeahead rendered* first; a
text that fits several clients still refuses. The template's script (run in Node against a fake DOM
by ``tests/js/scan_customer_picker.mjs``) keeps a picked client's id across the refetch, and never
turns ambiguous **typed** text into "first in the list".

The asymmetry is deliberate: an explicit ``customer_id`` in the posted form is the staff member's
choice and the save trusts it. That is exactly why the picker must never manufacture one from
ambiguous typing — the client-side and server-side regressions below cover both ends.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from app import create_app
from app.db import get_db
from app.services import vehicles
from app.services.customers import find_customer_id_by_text

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
HARNESS = ROOT / "tests" / "js" / "scan_customer_picker.mjs"

#: The live cluster's shapes: the same cellphone number stored two ways, plus a different Sandile.
SANDILE_NKOSI = [
    ("Sandile Nkosi", "+27748848822"),     # country-code format (live id 2312 shape)
    ("Sandile Nkosi", "0748848822"),       # local format, the label actually picked (live 3060)
    ("Sandile Nkosi", "+2764187062"),      # a different Sandile Nkosi (live id 2271 shape)
]
PICKED_LABEL = "Sandile Nkosi \u00b7 0748848822"
OTHER_LABEL = "Sandile Nkosi \u00b7 +27748848822"
THIRD_LABEL = "Sandile Nkosi \u00b7 +2764187062"


@pytest.fixture()
def app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "scan-client-pick.db"),
            "SECRET_KEY": "test",
            "ADMIN_EMAIL": "admin@abi.local",
            "ADMIN_PASSWORD": "admin123",
        }
    )


@pytest.fixture()
def client(app):
    c = app.test_client()
    with app.app_context():
        owner = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
        assert owner is not None
    with c.session_transaction() as session:
        session["user_id"] = owner["id"]
        session["user_role"] = "owner"
        session["user_name"] = "Owner"
    return c


def add_customer(app, name, phone):
    """Insert exactly the strings given (no normalisation), as the Booqable import left them."""
    with app.app_context():
        db = get_db()
        cursor = db.execute(
            "INSERT INTO customers (name, phone, source_system, created_at, client_verified)"
            " VALUES (?, ?, 'booqable', ?, 1)",
            (name, phone, datetime.now().isoformat(timespec="seconds")),
        )
        db.commit()
        return cursor.lastrowid


@pytest.fixture()
def cluster(app):
    """The three Sandile Nkosi records, by id."""
    same_code, picked, other_number = (add_customer(app, name, phone) for name, phone in SANDILE_NKOSI)
    return {"code": same_code, "picked": picked, "other": other_number}


def save_scan(client, customer_pick="", customer_id="", registration="ABC123GP", **extra):
    data = {
        "source": "scan",
        "customer_pick": customer_pick,
        "customer_id": customer_id,
        "registration": registration,
        "registration_number": "",
        "licence_number": "",
        "vin": "",
        "engine_number": "",
        "make": "",
        "colour": "",
        "licence_disk_expiry": "",
        "make_main": "no",
    }
    data.update(extra)
    return client.post("/scan-vehicle/save", data=data, follow_redirects=True)


# ── the server's resolution of the picked label ──────────────────────────────


def test_the_picked_label_identifies_one_of_two_clients_who_share_a_number(app, cluster):
    """The label the suggestion list rendered is exact, so it picks out that one record."""
    with app.app_context():
        assert find_customer_id_by_text(PICKED_LABEL) == cluster["picked"]
        assert find_customer_id_by_text(OTHER_LABEL) == cluster["code"]
        assert find_customer_id_by_text(THIRD_LABEL) == cluster["other"]


def test_an_ambiguous_number_or_name_still_refuses(app, cluster):
    """The same number is on file twice, once in each format, and the name is on file three times."""
    with app.app_context():
        assert find_customer_id_by_text("0748848822") is None          # digits alone fit two clients
        assert find_customer_id_by_text("+27748848822") is None        # ... in either format
        assert find_customer_id_by_text("Sandile Nkosi") is None       # a shared name
        assert find_customer_id_by_text("Sandile") is None             # a shared partial name


def test_a_label_two_clients_genuinely_share_is_refused(app):
    """Two records rendering the SAME label cannot be told apart by the text — refuse, never guess."""
    first = add_customer(app, "Lindiwe Dube", "0748848822")
    second = add_customer(app, "Lindiwe Dube", "0748848822")
    assert first != second
    with app.app_context():
        assert find_customer_id_by_text("Lindiwe Dube \u00b7 0748848822") is None
        assert find_customer_id_by_text("Lindiwe Dube") is None


# ── the save route for the reported case ─────────────────────────────────────


def test_the_save_route_allocates_from_the_picked_label_alone(app, client, cluster):
    """The hidden id can be lost on the way out; the picked label alone must still allocate."""
    response = save_scan(client, customer_pick=PICKED_LABEL)
    body = response.data.decode()
    assert "allocated to Sandile Nkosi" in body, body[:400]
    with app.app_context():
        assert len(vehicles.list_vehicles(cluster["picked"])) == 1
        assert vehicles.list_vehicles(cluster["code"]) == []
        assert vehicles.list_vehicles(cluster["other"]) == []


def test_the_save_route_refuses_an_ambiguous_number_or_name(app, client, cluster):
    with app.app_context():
        client.post("/scan-vehicle", data={"action": "decode", "disc_text": ""}, follow_redirects=True)
        before = get_db().execute("SELECT COUNT(*) AS n FROM vehicles").fetchone()["n"]

    for ambiguous in ("0748848822", "+27748848822", "Sandile Nkosi"):
        response = save_scan(client, customer_pick=ambiguous, registration="XYZ987GP")
        assert "Choose the client from the list" in response.data.decode(), ambiguous
    with app.app_context():
        after = get_db().execute("SELECT COUNT(*) AS n FROM vehicles").fetchone()["n"]
    assert after == before == 0


# ── the picker script itself (the part a server test cannot see) ─────────────


@pytest.mark.skipif(NODE is None, reason="Node is not available")
def test_the_picker_script_regressions_hold():
    """Picked id survives the refetch; ambiguous typed text never becomes "first in the list"."""
    result = subprocess.run(
        [NODE, str(HARNESS), str(ROOT / "templates" / "admin" / "scan_vehicle.html")],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "checks passed" in result.stdout
    assert "FAIL" not in result.stdout
