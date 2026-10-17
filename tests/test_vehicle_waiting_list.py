"""Scanned vehicles waiting for a client (ABI-341953230) — service + real SQLite.

``vehicles.customer_id`` is NOT NULL, so a licence disc scanned before the client exists (or
before staff finish verifying a pending portal sign-up) has nowhere to go. This module pins the
separate waiting-list table and its three service entry points:

* ``save_waiting_vehicle(form)`` stores the same validated values a vehicle save would (plate
  normalised, expiry ISO, mass numeric-or-NULL), records ``approved``, and allocates **nobody**;
* the queue holds **three** vehicles, globally shared, and the cap is a property of the table
  (slot NOT NULL, CHECK 1..3, UNIQUE) plus a conditional insert — so simultaneous saves cannot
  write a fourth row;
* a duplicate replay is refused clearly, spacing/case ignored, against the queue **and** the
  recorded vehicles;
* ``allocate_waiting_vehicle`` only serves a client whose portal registration is still
  **pending** inside the session's branch scope and who has no main vehicle (pointer or legacy
  ``vehicle_*`` field); it writes a real ``approved`` vehicle, promotes the main mirror and
  removes the queue row **in one transaction**, so a stale or concurrent allocation is refused
  and can never double-allocate.

Every identifier here is synthetic.
"""

import os
import sqlite3
import threading

import pytest

from app import create_app
from app.db import get_db, run_migrations
from app.services import vehicles
from app.services.customers import create_customer, raw_custom_fields_for
from app.services import portal_registrations


@pytest.fixture()
def app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "waiting.sqlite"),
            "SECRET_KEY": "test",
            "ADMIN_EMAIL": "admin@abi.local",
            "ADMIN_PASSWORD": "admin123",
        }
    )


def scan_form(registration="NB72XMGP", **overrides):
    data = {
        "registration": registration,
        "make": "MITSUBISHI",
        "model": "PAJERO SPORT",
        "vin": "JHTFR22G10L654321",
        "engine_number": "K9K7654321",
        "colour": "WHITE",
        "licence_number": "5120367QP4HD",
        "registration_number": "QWR419V",
        "licence_disk_expiry": "31-03-2027",
        "tare_kg": "1 250",
        "source": "scan",
    }
    data.update(overrides)
    return data


def queue_rows(app):
    with app.app_context():
        return [dict(row) for row in vehicles.list_waiting_vehicles()]


def waiting_id_for(app, registration):
    with app.app_context():
        row = get_db().execute(
            "SELECT id FROM vehicle_waiting_list "
            "WHERE REPLACE(UPPER(registration), ' ', '') = ?",
            (vehicles.registration_key(registration),),
        ).fetchone()
    return int(row["id"]) if row else None


def make_pending_client(app, branch_id=1, name="Charmaine Mokoena"):
    """A customer row that is a *pending* portal registration in ``branch_id``."""
    with app.app_context():
        customer_id = create_customer(
            {"name": name, "customer_type": "individual", "phone": "0821234567"}
        )
        get_db().execute(
            "INSERT INTO portal_registrations "
            "(reference, customer_id, branch_id, name, email, phone, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (f"REG-{customer_id}", customer_id, branch_id, name, "pending@example.co.za",
             "0821234567", "2026-10-17T08:00:00"),
        )
        get_db().commit()
    return customer_id


def make_plain_client(app, name="Pieter van Wyk", **extra):
    with app.app_context():
        return create_customer(
            {"name": name, "customer_type": "individual", "phone": "0837654321", **extra}
        )


def vehicle_count(app, customer_id=None):
    with app.app_context():
        if customer_id is None:
            row = get_db().execute("SELECT COUNT(*) AS c FROM vehicles").fetchone()
        else:
            row = get_db().execute(
                "SELECT COUNT(*) AS c FROM vehicles WHERE customer_id = ?", (int(customer_id),)
            ).fetchone()
    return int(row["c"])


# ── schema ──────────────────────────────────────────────────────────────────


def test_the_waiting_table_is_separate_additive_and_needs_no_customer(app):
    with app.app_context():
        columns = {
            row["name"]
            for row in get_db().execute("PRAGMA table_info(vehicle_waiting_list)").fetchall()
        }
    for name in (
        "id", "slot", "registration", "make", "model", "year", "vin", "engine_number",
        "colour", "licence_number", "registration_number", "control_number",
        "registering_authority", "vehicle_type", "tare_kg", "gvm_kg",
        "licence_disk_expiry", "raw_scan_text", "source", "status",
        "created_by_user_id", "created_at", "updated_at",
    ):
        assert name in columns, f"vehicle_waiting_list.{name} is missing"
    # A waiting scan is not allocated, so it carries no owner at all.
    assert "customer_id" not in columns


def test_the_three_slot_cap_is_enforced_by_the_table_itself(app):
    with app.app_context():
        db = get_db()
        # Only slots 1..3 are accepted at all …
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO vehicle_waiting_list (slot, registration, created_at, updated_at) "
                "VALUES (4, 'SLOT4', 'now', 'now')"
            )
        db.rollback()
        # … and each slot only once: a fourth row can never exist.
        for slot, registration in ((1, "AAA 111"), (2, "BBB 222"), (3, "CCC 333")):
            db.execute(
                "INSERT INTO vehicle_waiting_list (slot, registration, created_at, updated_at) "
                "VALUES (?, ?, 'now', 'now')",
                (slot, registration),
            )
        db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO vehicle_waiting_list (slot, registration, created_at, updated_at) "
                "VALUES (1, 'DDD 444', 'now', 'now')"
            )
        db.rollback()
        # Spacing/case-insensitive duplicate guard inside the queue, as a backstop.
        db.execute("DELETE FROM vehicle_waiting_list WHERE slot = 3")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO vehicle_waiting_list (slot, registration, created_at, updated_at) "
                "VALUES (3, 'aaa111', 'now', 'now')"
            )
        db.rollback()


def test_the_migration_adds_the_table_to_an_existing_database(app):
    with app.app_context():
        get_db().execute("DROP TABLE vehicle_waiting_list")
        get_db().commit()
        run_migrations(get_db())
        get_db().commit()
        names = {
            row["name"]
            for row in get_db().execute("PRAGMA table_info(vehicle_waiting_list)").fetchall()
        }
    assert {"id", "slot", "registration", "status"} <= names
    assert queue_rows(app) == []


# ── save ────────────────────────────────────────────────────────────────────


def test_a_scan_is_stored_validated_approved_and_unallocated(app):
    with app.app_context():
        waiting_id = vehicles.save_waiting_vehicle(scan_form("  nb 72   xmgp "))
    rows = queue_rows(app)
    assert [row["id"] for row in rows] == [waiting_id]
    row = rows[0]
    assert row["slot"] == 1
    assert row["registration"] == "NB 72 XMGP"          # stored in one shape
    assert row["licence_disk_expiry"] == "2027-03-31"    # dd-mm-yyyy -> ISO
    assert row["tare_kg"] == 1250.0                      # "1 250" -> REAL
    assert row["gvm_kg"] is None                         # blank stays NULL, never 0
    assert row["source"] == "scan"
    assert row["status"] == "approved"
    assert row["created_at"]
    assert vehicle_count(app) == 0


def test_a_scan_without_a_registration_is_refused(app):
    with app.app_context():
        with pytest.raises(ValueError) as refused:
            vehicles.save_waiting_vehicle(scan_form("   "))
    assert "registration" in str(refused.value).lower()
    assert queue_rows(app) == []


def test_an_impossible_date_or_mass_is_refused_before_anything_waits(app):
    with app.app_context():
        with pytest.raises(ValueError):
            vehicles.save_waiting_vehicle(scan_form("AA 11 BB", licence_disk_expiry="not-a-date"))
        with pytest.raises(ValueError):
            vehicles.save_waiting_vehicle(scan_form("AA 11 BB", tare_kg="heavy"))
    assert queue_rows(app) == []


def test_the_session_user_is_recorded_on_the_waiting_row(app):
    with app.app_context():
        user_id = int(get_db().execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"])
    with app.test_request_context("/"):
        from flask import session

        session["user_id"] = user_id
        session["user_role"] = "owner"
        vehicles.save_waiting_vehicle(scan_form())
    assert queue_rows(app)[0]["created_by_user_id"] == user_id


def test_a_duplicate_replay_is_refused_spacing_and_case_ignored(app):
    with app.app_context():
        first = vehicles.save_waiting_vehicle(scan_form("NB72XMGP"))
        with pytest.raises(ValueError) as refused:
            vehicles.save_waiting_vehicle(scan_form("nb 72 xmgp", colour="BLUE"))
    message = str(refused.value)
    assert "already waiting" in message
    assert "slot 1" in message
    rows = queue_rows(app)
    assert len(rows) == 1
    assert rows[0]["id"] == first
    # A refused replay never updates the waiting row.
    assert rows[0]["colour"] == "WHITE"


def test_a_plate_already_recorded_on_a_vehicle_is_refused(app):
    owner_id = make_plain_client(app)
    with app.app_context():
        vehicles.create_vehicle(scan_form("CA 123 456"), customer_id=owner_id)
        with pytest.raises(ValueError) as refused:
            vehicles.save_waiting_vehicle(scan_form("ca123456"))
    assert "already recorded for Pieter van Wyk" in str(refused.value)
    assert queue_rows(app) == []


def test_three_wait_and_the_fourth_is_refused_clearly(app):
    with app.app_context():
        for registration in ("AAA 111", "BBB 222", "CCC 333"):
            vehicles.save_waiting_vehicle(scan_form(registration))
        with pytest.raises(ValueError) as refused:
            vehicles.save_waiting_vehicle(scan_form("DDD 444"))
    assert "full" in str(refused.value)
    rows = queue_rows(app)
    assert [row["slot"] for row in rows] == [1, 2, 3]
    assert [row["registration"] for row in rows] == ["AAA 111", "BBB 222", "CCC 333"]


def test_a_freed_slot_is_reused_lowest_first(app):
    with app.app_context():
        first = vehicles.save_waiting_vehicle(scan_form("AAA 111"))
        vehicles.save_waiting_vehicle(scan_form("BBB 222"))
        assert vehicles.remove_waiting_vehicle(first) is True
        vehicles.save_waiting_vehicle(scan_form("CCC 333"))
    rows = queue_rows(app)
    assert [row["slot"] for row in rows] == [1, 2]
    assert [row["registration"] for row in rows] == ["CCC 333", "BBB 222"]


def test_removing_a_missing_scan_is_a_no_op(app):
    with app.app_context():
        assert vehicles.remove_waiting_vehicle(9999) is False


# ── allocation guards ───────────────────────────────────────────────────────


def test_allocation_requires_a_pending_portal_registration(app):
    plain_id = make_plain_client(app)
    with app.app_context():
        waiting_id = vehicles.save_waiting_vehicle(scan_form())
        with pytest.raises(ValueError) as refused:
            vehicles.allocate_waiting_vehicle(waiting_id, plain_id)
    assert "pending" in str(refused.value).lower()
    assert len(queue_rows(app)) == 1
    assert vehicle_count(app) == 0


def test_allocation_refuses_a_registration_outside_the_session_branch_scope(app):
    pending_id = make_pending_client(app, branch_id=1)
    with app.app_context():
        waiting_id = vehicles.save_waiting_vehicle(scan_form())
    with app.test_request_context("/"):
        from flask import session

        session["user_id"] = 7
        session["user_role"] = "staff"
        session["branch_ids"] = [2]
        with pytest.raises(ValueError) as refused:
            vehicles.allocate_waiting_vehicle(waiting_id, pending_id)
    assert "branches you can access" in str(refused.value)
    assert len(queue_rows(app)) == 1
    assert vehicle_count(app) == 0


def test_allocation_refuses_a_client_that_already_has_a_main_vehicle(app):
    pending_id = make_pending_client(app)
    with app.app_context():
        existing_id = vehicles.create_vehicle(scan_form("OLD 111"), customer_id=pending_id)
        vehicles.promote_main_vehicle(pending_id, existing_id)
        waiting_id = vehicles.save_waiting_vehicle(scan_form("NEW 222"))
        with pytest.raises(ValueError) as refused:
            vehicles.allocate_waiting_vehicle(waiting_id, pending_id)
    assert "main vehicle" in str(refused.value)
    assert len(queue_rows(app)) == 1
    assert vehicle_count(app, pending_id) == 1


def test_allocation_refuses_a_client_with_legacy_main_vehicle_fields(app):
    pending_id = make_pending_client(app)
    with app.app_context():
        # Hand-typed legacy plate, no vehicle row and no pointer at all.
        get_db().execute(
            "UPDATE customers SET custom_fields_json = ? WHERE id = ?",
            ('{"vehicle_reg_no": "LEG 123", "company_reg": "2019/123456/07"}', pending_id),
        )
        get_db().commit()
        waiting_id = vehicles.save_waiting_vehicle(scan_form("NEW 222"))
        with pytest.raises(ValueError) as refused:
            vehicles.allocate_waiting_vehicle(waiting_id, pending_id)
    assert "vehicle details" in str(refused.value)
    assert len(queue_rows(app)) == 1
    assert vehicle_count(app, pending_id) == 0


def test_a_stale_allocation_is_refused(app):
    pending_id = make_pending_client(app)
    with app.app_context():
        with pytest.raises(ValueError) as refused:
            vehicles.allocate_waiting_vehicle(9999, pending_id)
    assert "no longer waiting" in str(refused.value)


# ── allocation ──────────────────────────────────────────────────────────────


def test_allocation_creates_an_approved_main_vehicle_and_clears_the_queue(app):
    pending_id = make_pending_client(app)
    other_id = make_pending_client(app, name="Second Pending")
    with app.app_context():
        waiting_id = vehicles.save_waiting_vehicle(scan_form("nb 72 xmgp"))
        untouched = vehicles.save_waiting_vehicle(scan_form("OTHER 999"))
        vehicle_id = vehicles.allocate_waiting_vehicle(waiting_id, pending_id)

        row = vehicles.get_vehicle(vehicle_id)
        assert row["customer_id"] == pending_id
        assert row["registration"] == "NB 72 XMGP"
        assert row["make"] == "MITSUBISHI"
        assert row["licence_disk_expiry"] == "2027-03-31"
        assert row["tare_kg"] == 1250.0
        assert row["source"] == "scan"
        assert vehicles.vehicle_status(row) == vehicles.STATUS_APPROVED
        # The new vehicle is the client's main (invoiced) vehicle …
        assert vehicles.get_main_vehicle_id(pending_id) == vehicle_id
        assert vehicles.get_main_vehicle(pending_id)["registration"] == "NB 72 XMGP"
        # … the queue row is gone, the other waiting scan is untouched …
        remaining = [dict(r) for r in vehicles.list_waiting_vehicles()]
        assert [r["id"] for r in remaining] == [untouched]
        # … and the portal registration is NOT approved here: the route owns that step.
        pending = portal_registrations.pending_registration_for_customer(pending_id)
        assert pending["status"] == "pending"
        assert vehicle_count(app, other_id) == 0


def test_a_second_allocation_of_the_same_scan_is_refused_without_a_second_vehicle(app):
    pending_id = make_pending_client(app)
    with app.app_context():
        waiting_id = vehicles.save_waiting_vehicle(scan_form())
        first = vehicles.allocate_waiting_vehicle(waiting_id, pending_id)
        with pytest.raises(ValueError) as refused:
            vehicles.allocate_waiting_vehicle(waiting_id, pending_id)
    assert "no longer waiting" in str(refused.value)
    assert vehicle_count(app, pending_id) == 1
    assert queue_rows(app) == []
    assert first


def test_a_plate_recorded_meanwhile_leaves_the_scan_waiting(app):
    pending_id = make_pending_client(app)
    owner_id = make_plain_client(app)
    with app.app_context():
        waiting_id = vehicles.save_waiting_vehicle(scan_form("CA 123 456"))
        # Somebody else records the plate between scanning and allocation.
        vehicles.create_vehicle(scan_form("CA123456"), customer_id=owner_id)
        with pytest.raises(ValueError):
            vehicles.allocate_waiting_vehicle(waiting_id, pending_id)
        # The failed claim rolled back: the scan is still waiting and no vehicle was added.
        remaining = [dict(r) for r in vehicles.list_waiting_vehicles()]
        assert [r["id"] for r in remaining] == [waiting_id]
    assert vehicle_count(app, pending_id) == 0


# ── concurrency ─────────────────────────────────────────────────────────────


def _run_concurrently(app, work, count):
    """Run ``work(index)`` in ``count`` threads released together. Returns outcomes/errors."""
    barrier = threading.Barrier(count)
    outcomes = []
    errors = []
    lock = threading.Lock()

    def target(index):
        barrier.wait()
        with app.app_context():
            try:
                result = work(index)
            except ValueError as exc:
                with lock:
                    errors.append(str(exc))
            except Exception as exc:  # noqa: BLE001 - any other failure must fail the test
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}")
            else:
                with lock:
                    outcomes.append(result)

    threads = [threading.Thread(target=target, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads), "a worker did not finish"
    return outcomes, errors


def test_simultaneous_saves_can_never_write_a_fourth_row(app):
    plates = [f"CON {index:03d}" for index in range(8)]

    def save(index):
        return vehicles.save_waiting_vehicle(scan_form(plates[index]))

    outcomes, errors = _run_concurrently(app, save, len(plates))
    assert len(outcomes) == 3, errors
    assert all("full" in message for message in errors), errors
    rows = queue_rows(app)
    assert len(rows) == 3
    assert sorted(row["slot"] for row in rows) == [1, 2, 3]
    assert len({row["registration"] for row in rows}) == 3


def test_two_threads_cannot_allocate_the_same_scan_twice(app):
    pending_id = make_pending_client(app)
    with app.app_context():
        waiting_id = vehicles.save_waiting_vehicle(scan_form())

    def allocate(index):
        return vehicles.allocate_waiting_vehicle(waiting_id, pending_id)

    outcomes, errors = _run_concurrently(app, allocate, 2)
    assert len(outcomes) == 1, errors
    assert len(errors) == 1 and "no longer waiting" in errors[0]
    assert vehicle_count(app, pending_id) == 1
    assert queue_rows(app) == []
    with app.app_context():
        assert vehicles.get_main_vehicle_id(pending_id) == outcomes[0]


def test_allocation_accepts_turso_row_interface(app, monkeypatch):
    pending_id = make_pending_client(app)
    original = vehicles.get_waiting_vehicle

    class TursoRow:
        def __init__(self, row):
            self.data = dict(row)
        def __getitem__(self, key):
            return self.data[key]
        def asdict(self):
            return self.data

    def read(waiting_id):
        row = original(waiting_id)
        return TursoRow(row) if row is not None else None

    with app.app_context():
        wid = vehicles.save_waiting_vehicle(scan_form("TURSO 230"))
        monkeypatch.setattr(vehicles, "get_waiting_vehicle", read)
        vid = vehicles.allocate_waiting_vehicle(wid, pending_id)
        assert vehicles.get_main_vehicle_id(pending_id) == vid
        assert vehicles.list_waiting_vehicles() == []


def test_two_waiting_scans_cannot_replace_each_other_on_one_customer(app):
    pending_id = make_pending_client(app)
    with app.app_context():
        ids = [vehicles.save_waiting_vehicle(scan_form(f"DIFF {n}")) for n in range(2)]
    outcomes, errors = _run_concurrently(
        app, lambda index: vehicles.allocate_waiting_vehicle(ids[index], pending_id), 2,
    )
    assert len(outcomes) == 1, errors
    assert len(errors) == 1 and "already has a main vehicle" in errors[0], errors
    assert vehicle_count(app, pending_id) == 1
    assert len(queue_rows(app)) == 1


def test_two_threads_saving_the_same_plate_queue_it_once(app):
    def save(index):
        return vehicles.save_waiting_vehicle(scan_form("NB72XMGP"))

    outcomes, errors = _run_concurrently(app, save, 2)
    assert len(outcomes) == 1, errors
    assert len(errors) == 1 and "already waiting" in errors[0], errors
    assert len(queue_rows(app)) == 1
