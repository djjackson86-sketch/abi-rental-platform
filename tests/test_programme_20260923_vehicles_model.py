"""Programme phase 2 (feature A / A2) — the ``vehicles`` data model and service.

Owner: the customer (``vehicles.customer_id``). One owner per recorded registration,
cascade-deleted with the customer, and **no figure is ever invented**: a mass the disc
did not carry stays NULL, never 0 (master plan D3 / D3b — and no towing capacity column
at all).

Real disc identifiers used below are the *synthetic* ones from the A1 fixtures, with the
D3 mapping: plate -> ``registration``, NaTIS registration number -> ``registration_number``,
disc licence number -> ``licence_number``.
"""

import os
import sqlite3
import tempfile

import pytest

from app import create_app
from app.db import get_db, run_migrations
from app.services import vehicles
from app.services.customers import create_customer, delete_customer


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
def customer_id(app):
    with app.app_context():
        return create_customer({"name": "Charmaine Mokoena", "customer_type": "individual", "phone": "0821234567"})


@pytest.fixture()
def other_customer_id(app):
    with app.app_context():
        return create_customer({"name": "Pieter van Wyk", "customer_type": "individual", "phone": "0837654321"})


def _plate_form(customer_id, **overrides):
    form = {
        "customer_id": str(customer_id),
        "registration": "KP35XKGP",
        "make": "MITSUBISHI",
        "model": "PAJERO SPORT",
        "vin": "MMBJNKB40FD123456",
        "engine_number": "4B11LC0187",
        "colour": "WHITE",
        "licence_number": "4024048GB8LY",
        "registration_number": "SHS812W",
        "licence_disk_expiry": "2027-03-31",
        "source": "scan",
    }
    form.update(overrides)
    return form


def _columns():
    return {row["name"] for row in get_db().execute("PRAGMA table_info(vehicles)").fetchall()}


# ── schema ──────────────────────────────────────────────────────────────────


def test_vehicles_table_exists_with_the_d3_columns(app):
    with app.app_context():
        columns = _columns()
    for name in (
        "id", "customer_id", "registration", "make", "model", "year", "vin", "engine_number",
        "colour", "licence_number", "registration_number", "control_number",
        "registering_authority", "vehicle_type", "tare_kg", "gvm_kg", "licence_disk_expiry",
        "raw_scan_text", "source", "created_by_user_id", "created_at", "updated_at",
    ):
        assert name in columns, f"vehicles.{name} is missing"
    # D3b: towing capacity was removed by Don — the column must not exist at all.
    assert "towing_capacity_kg" not in columns


def test_vehicles_migration_recreates_the_table_on_an_existing_db(app, customer_id):
    with app.app_context():
        vehicle_id = vehicles.create_vehicle(_plate_form(customer_id))
        get_db().execute("DROP TABLE vehicles")
        get_db().commit()
        run_migrations(get_db())
        get_db().commit()
        assert "registration" in _columns()
        # the recreated table is usable and empty — the drop was a destructive test action
        assert vehicles.get_vehicle(vehicle_id) is None
        assert vehicles.list_vehicles(customer_id) == []


# ── create / read / update / delete ─────────────────────────────────────────


def test_create_vehicle_stores_the_disc_fields(app, customer_id):
    with app.app_context():
        vehicle_id = vehicles.create_vehicle(_plate_form(customer_id))
        row = vehicles.get_vehicle(vehicle_id)
    assert row["customer_id"] == customer_id
    assert row["registration"] == "KP35XKGP"
    assert row["make"] == "MITSUBISHI"
    assert row["model"] == "PAJERO SPORT"
    assert row["vin"] == "MMBJNKB40FD123456"
    assert row["engine_number"] == "4B11LC0187"
    assert row["colour"] == "WHITE"
    assert row["licence_number"] == "4024048GB8LY"
    assert row["registration_number"] == "SHS812W"
    assert row["licence_disk_expiry"] == "2027-03-31"
    assert row["source"] == "scan"
    assert row["created_at"]


def test_create_vehicle_defaults_to_manual_and_needs_a_client(app, customer_id):
    with app.app_context():
        vehicle_id = vehicles.create_vehicle({"customer_id": customer_id, "registration": "ABC123"})
        assert vehicles.get_vehicle(vehicle_id)["source"] == "manual"
        with pytest.raises(ValueError):
            vehicles.create_vehicle({"registration": "NOCLIENT"})
        with pytest.raises(ValueError):
            vehicles.create_vehicle({"customer_id": 987654, "registration": "GHOST"})
        assert vehicles.customer_for_vehicle_registration("GHOST") is None


def test_registration_is_normalised(app, customer_id):
    with app.app_context():
        vehicle_id = vehicles.create_vehicle(_plate_form(customer_id, registration="  kp35xkgp  "))
        assert vehicles.get_vehicle(vehicle_id)["registration"] == "KP35XKGP"


def test_list_vehicles_is_per_customer_and_newest_first(app, customer_id, other_customer_id):
    with app.app_context():
        first = vehicles.create_vehicle(_plate_form(customer_id, registration="AAA111"))
        second = vehicles.create_vehicle(_plate_form(customer_id, registration="BBB222"))
        vehicles.create_vehicle(_plate_form(other_customer_id, registration="CCC333"))
        listed = vehicles.list_vehicles(customer_id)
    assert [row["registration"] for row in listed] == ["BBB222", "AAA111"]
    assert {row["id"] for row in listed} == {first, second}


def test_update_vehicle_changes_fields_and_refreshes_updated_at(app, customer_id):
    with app.app_context():
        vehicle_id = vehicles.create_vehicle(_plate_form(customer_id))
        before = vehicles.get_vehicle(vehicle_id)
        vehicles.update_vehicle(
            vehicle_id,
            {"registration": "KP35XKGP", "colour": "SILVER", "year": "2019", "tare_kg": "1845"},
        )
        after = vehicles.get_vehicle(vehicle_id)
    assert after["colour"] == "SILVER"
    assert after["year"] == "2019"
    assert after["tare_kg"] == 1845.0
    # untouched fields are not wiped by a partial edit
    assert after["vin"] == "MMBJNKB40FD123456"
    assert after["updated_at"] >= before["updated_at"]
    with app.app_context():
        assert vehicles.update_vehicle(999999, {"registration": "NOPE"}) is False


def test_delete_vehicle_removes_only_that_row(app, customer_id):
    with app.app_context():
        keep = vehicles.create_vehicle(_plate_form(customer_id, registration="KEEP01"))
        drop = vehicles.create_vehicle(_plate_form(customer_id, registration="DROP01"))
        assert vehicles.delete_vehicle(drop) is True
        assert vehicles.delete_vehicle(drop) is False
        remaining = [row["id"] for row in vehicles.list_vehicles(customer_id)]
    assert remaining == [keep]


def test_vehicle_writes_leave_the_customers_table_untouched(app, customer_id):
    with app.app_context():
        before = dict(get_db().execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone())
        vehicle_id = vehicles.create_vehicle(_plate_form(customer_id))
        vehicles.update_vehicle(vehicle_id, {"registration": "KP35XKGP", "colour": "BLUE"})
        vehicles.delete_vehicle(vehicle_id)
        after = dict(get_db().execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone())
    assert before == after


# ── cascade / orphans ───────────────────────────────────────────────────────


def test_the_database_itself_cascades_when_a_customer_row_is_removed(app, customer_id):
    """The schema is right as well as the service: with foreign keys on (which is how
    get_db opens a SQLite connection) removing the customer row takes its vehicles too."""
    with app.app_context():
        vehicles.create_vehicle(_plate_form(customer_id, registration="FK0001"))
        assert get_db().execute("PRAGMA foreign_keys").fetchone()[0] == 1
        get_db().execute("DELETE FROM customers WHERE id = ?", (customer_id,))
        get_db().commit()
        left = get_db().execute(
            "SELECT COUNT(*) AS c FROM vehicles WHERE customer_id = ?", (customer_id,)
        ).fetchone()["c"]
    assert left == 0


def test_a_duplicate_registration_is_blocked_by_the_database_too(app, customer_id, other_customer_id):
    with app.app_context():
        vehicles.create_vehicle(_plate_form(customer_id))
        with pytest.raises(sqlite3.IntegrityError):
            get_db().execute(
                "INSERT INTO vehicles (customer_id, registration, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (other_customer_id, "KP35XKGP", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
            )


def test_deleting_a_customer_leaves_zero_orphan_vehicle_rows(app, customer_id, other_customer_id):
    with app.app_context():
        vehicles.create_vehicle(_plate_form(customer_id, registration="ORPH01"))
        vehicles.create_vehicle(_plate_form(customer_id, registration="ORPH02"))
        survivors = vehicles.create_vehicle(_plate_form(other_customer_id, registration="KEEP02"))
        assert delete_customer(customer_id) is True
        orphans = get_db().execute(
            "SELECT COUNT(*) AS c FROM vehicles WHERE customer_id NOT IN (SELECT id FROM customers)"
        ).fetchone()["c"]
        left = get_db().execute("SELECT id FROM vehicles").fetchall()
    assert orphans == 0
    assert [row["id"] for row in left] == [survivors]


# ── one owner per registration ──────────────────────────────────────────────


def test_a_second_customer_cannot_claim_an_owned_registration(app, customer_id, other_customer_id):
    with app.app_context():
        vehicles.create_vehicle(_plate_form(customer_id))
        with pytest.raises(ValueError) as excinfo:
            vehicles.create_vehicle(_plate_form(other_customer_id))
        assert "already" in str(excinfo.value).lower()
        # the same plate typed with different case/spacing is the same vehicle
        with pytest.raises(ValueError):
            vehicles.create_vehicle(_plate_form(other_customer_id, registration=" kp 35 xkgp "))
        # ... and the owner may still re-save its own vehicle
        owner_vehicle = vehicles.list_vehicles(customer_id)[0]
        vehicles.update_vehicle(owner_vehicle["id"], {"registration": "KP35XKGP"})
        assert len(vehicles.list_vehicles(customer_id)) == 1


def test_a_vehicle_cannot_be_moved_onto_a_registration_another_customer_owns(app, customer_id, other_customer_id):
    with app.app_context():
        vehicles.create_vehicle(_plate_form(customer_id, registration="OWNED01"))
        mine = vehicles.create_vehicle(_plate_form(other_customer_id, registration="MINE01"))
        with pytest.raises(ValueError):
            vehicles.update_vehicle(mine, {"registration": "OWNED01"})
        assert vehicles.get_vehicle(mine)["registration"] == "MINE01"


def test_blank_registration_is_allowed_more_than_once(app, customer_id, other_customer_id):
    with app.app_context():
        a = vehicles.create_vehicle({"customer_id": customer_id, "make": "TOYOTA"})
        b = vehicles.create_vehicle({"customer_id": customer_id, "make": "FORD"})
        c = vehicles.create_vehicle({"customer_id": other_customer_id, "make": "ISUZU"})
        blank = [row["id"] for row in vehicles.list_vehicles(customer_id) if row["registration"] == ""]
    assert blank and {a, b} <= set(blank)
    assert c


def test_customer_for_vehicle_registration_finds_the_owner(app, customer_id, other_customer_id):
    with app.app_context():
        vehicles.create_vehicle(_plate_form(customer_id))
        owner = vehicles.customer_for_vehicle_registration("kp35xkgp")
        nobody = vehicles.customer_for_vehicle_registration("ZZZ999")
        also_nobody = vehicles.customer_for_vehicle_registration("")
        other_owner = vehicles.customer_for_vehicle_registration("ORPH99")
    assert owner["id"] == customer_id
    assert owner["name"] == "Charmaine Mokoena"
    assert nobody is None and also_nobody is None and other_owner is None


# ── masses: blank is NULL, never 0 (D3) ─────────────────────────────────────


def test_masses_accept_blank_as_null_and_never_default_to_zero(app, customer_id):
    with app.app_context():
        blank_id = vehicles.create_vehicle({"customer_id": customer_id, "registration": "MASS01"})
        blank = vehicles.get_vehicle(blank_id)
        assert blank["tare_kg"] is None and blank["gvm_kg"] is None
        real_id = vehicles.create_vehicle(
            {"customer_id": customer_id, "registration": "MASS02", "tare_kg": "1 845", "gvm_kg": "2500.5"}
        )
        real = vehicles.get_vehicle(real_id)
    assert real["tare_kg"] == 1845.0
    assert real["gvm_kg"] == 2500.5
    with app.app_context():
        # blanking a stored mass writes NULL, not 0
        vehicles.update_vehicle(real_id, {"registration": "MASS02", "tare_kg": "", "gvm_kg": "   "})
        cleared = vehicles.get_vehicle(real_id)
    assert cleared["tare_kg"] is None and cleared["gvm_kg"] is None


def test_nonsense_masses_are_refused_rather_than_invented(app, customer_id):
    with app.app_context():
        for bad in ("abc", "-5", "0x12"):
            with pytest.raises(ValueError):
                vehicles.create_vehicle({"customer_id": customer_id, "registration": "BAD", "tare_kg": bad})


# ── other validated fields ──────────────────────────────────────────────────


def test_source_and_expiry_are_validated(app, customer_id):
    with app.app_context():
        with pytest.raises(ValueError):
            vehicles.create_vehicle({"customer_id": customer_id, "source": "telepathy"})
        assert vehicles.create_vehicle({"customer_id": customer_id, "source": "import"}) is not None
        # the app's date shapes are accepted and stored as ISO; junk is refused
        normalised = vehicles.create_vehicle(
            {"customer_id": customer_id, "registration": "DATE01", "licence_disk_expiry": "31/03/2027"}
        )
        assert vehicles.get_vehicle(normalised)["licence_disk_expiry"] == "2027-03-31"
        with pytest.raises(ValueError):
            vehicles.create_vehicle({"customer_id": customer_id, "registration": "DATE02", "licence_disk_expiry": "soon"})


def test_vehicle_counts_reports_what_is_on_file(app, customer_id, other_customer_id):
    with app.app_context():
        vehicles.create_vehicle(_plate_form(customer_id))
        vehicles.create_vehicle({"customer_id": other_customer_id, "make": "TOYOTA"})
        counts = vehicles.vehicle_counts()
    assert counts["total"] == 2
    assert counts["scan"] == 1
    assert counts["manual"] == 1
    assert counts["blank_registration"] == 1
