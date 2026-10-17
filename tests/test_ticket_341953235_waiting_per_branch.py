"""ABI-341953235 — the scanned-vehicle waiting list is PER BRANCH.

The queue introduced by ABI-341953230 held three vehicles for the whole business, so a busy depot
could fill the counter and leave a second depot unable to queue a single scan. This ticket makes
the 3-vehicle cap a property of the **branch** the scan was queued at, limits what each user sees
to the branches they may reach, and refuses allocating a scan to a client of a different branch:

* ``vehicle_waiting_list.branch_id`` records the scanning user's selected (or primary) branch —
  the same rule ``customers.customer_branch_id()`` uses;
* the old global ``UNIQUE(slot)`` index is replaced by ``UNIQUE(branch_id, slot)`` plus a
  branch-less ``UNIQUE(slot)`` backstop, so three vehicles may wait **per branch** while a fourth
  for the same branch is still refused by the table itself;
* a plate stays unique across the whole business (one vehicle cannot wait at two depots);
* ``list_waiting_vehicles()`` is branch-scoped and carries ``branch_name``; groups carry a per
  branch ``n / 3`` counter so an all-branch viewer never reads a combined total as one cap;
* ``allocate_waiting_vehicle()`` refuses a scan waiting at another branch, and the client's
  allocate dialog only offers their own branch's vehicles.

Every identifier here is synthetic.
"""

import sqlite3

import pytest

from app import create_app
from app.db import get_db, run_migrations
from app.services import portal_registrations, vehicles
from app.services.customers import create_customer


@pytest.fixture()
def app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "waiting_per_branch.sqlite"),
            "SECRET_KEY": "test",
            "ADMIN_EMAIL": "admin@abi.local",
            "ADMIN_PASSWORD": "admin123",
            "TELEGRAM_NOTIFICATIONS_ENABLED": "",
        }
    )


@pytest.fixture()
def client(app):
    return app.test_client()


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


def branch_ids(app):
    """The starter branches every fresh test database has."""
    with app.app_context():
        rows = get_db().execute("SELECT id, name FROM branches ORDER BY id").fetchall()
    return {row["name"]: int(row["id"]) for row in rows}


def owner_id(app):
    with app.app_context():
        return int(
            get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()["id"]
        )


def sign_in(client, app, *, role="owner", branch_id=None, branch_ids=(), can_view_all=True, modules=()):
    with client.session_transaction() as session:
        session["user_id"] = owner_id(app)
        session["user_role"] = role
        session["user_name"] = "Synthetic Tester"
        session["initials"] = "ST"
        session["branch_id"] = branch_id
        session["can_view_all_branches"] = can_view_all
        session["branch_ids"] = list(branch_ids)
        session["staff_modules"] = list(modules)
        session["_csrf_tokens"] = {"portal_approval": "synthetic-token"}


def save(app, registration, branch_id=None, **overrides):
    """Queue a scan exactly as the scan screen does, signed in at ``branch_id``."""
    with app.test_request_context("/scan-vehicle"):
        from flask import session

        session["user_id"] = owner_id(app)
        session["user_role"] = "staff" if branch_id else "owner"
        session["branch_id"] = branch_id
        session["can_view_all_branches"] = branch_id is None
        session["branch_ids"] = [branch_id] if branch_id else []
        return vehicles.save_waiting_vehicle(scan_form(registration, **overrides))


def queue(app):
    with app.app_context():
        return [dict(row) for row in vehicles.list_waiting_vehicles()]


def queue_for(app, branch_id):
    with app.app_context():
        return [
            dict(row) for row in vehicles.list_waiting_vehicles(branch_ids=[branch_id])
        ]


def make_pending(app, branch_id, name="Charmaine Mokoena"):
    with app.app_context():
        customer_id = create_customer(
            {"name": name, "customer_type": "individual", "phone": "0821234567"},
            branch_id=branch_id,
        )
        portal_registrations.record_pending_registration(
            customer_id, branch_id, f"REG-{customer_id}", name=name, phone="0821234567"
        )
    return customer_id


# ── schema & migration ──────────────────────────────────────────────────────


def test_the_waiting_row_carries_the_branch_it_was_queued_at(app):
    ids = branch_ids(app)
    save(app, "NB 72 XMGP", branch_id=ids["Branch 2"])
    with app.app_context():
        row = get_db().execute(
            "SELECT branch_id FROM vehicle_waiting_list ORDER BY id LIMIT 1"
        ).fetchone()
    assert int(row["branch_id"]) == ids["Branch 2"]


def test_the_migration_turns_an_existing_global_queue_into_a_per_branch_one(app):
    """An existing live database: old table (no branch_id, global slot index) + a queued scan."""
    ids = branch_ids(app)
    with app.app_context():
        db = get_db()
        # Rewind the table to the ABI-341953230 shape, keeping the DB otherwise intact.
        db.execute("DROP INDEX IF EXISTS idx_vehicle_waiting_list_branch_slot")
        db.execute("DROP INDEX IF EXISTS idx_vehicle_waiting_list_unassigned_slot")
        db.execute("ALTER TABLE vehicle_waiting_list DROP COLUMN branch_id")
        db.execute(
            "CREATE UNIQUE INDEX idx_vehicle_waiting_list_slot ON vehicle_waiting_list(slot)"
        )
        for slot, registration in ((1, "LEG 111"), (2, "LEG 222")):
            db.execute(
                "INSERT INTO vehicle_waiting_list "
                "(slot, registration, created_by_user_id, created_at, updated_at) "
                "VALUES (?, ?, ?, 'now', 'now')",
                (slot, registration, owner_id(app)),
            )
        db.commit()

        run_migrations(db)
        db.commit()

        columns = {
            row["name"] for row in db.execute("PRAGMA table_info(vehicle_waiting_list)").fetchall()
        }
        indexes = {
            row["name"] for row in db.execute("PRAGMA index_list(vehicle_waiting_list)").fetchall()
        }
        rows = db.execute(
            "SELECT slot, branch_id FROM vehicle_waiting_list ORDER BY slot"
        ).fetchall()

    assert "branch_id" in columns
    # The global slot index is gone; the per-branch one and the branch-less backstop are in.
    assert "idx_vehicle_waiting_list_slot" not in indexes
    assert "idx_vehicle_waiting_list_branch_slot" in indexes
    assert "idx_vehicle_waiting_list_unassigned_slot" in indexes
    # Legacy rows took their creator's branch, so an existing queue kept its place.
    with app.app_context():
        user_branch = get_db().execute(
            "SELECT branch_id FROM users WHERE id = ?", (owner_id(app),)
        ).fetchone()["branch_id"]
    assert [row["branch_id"] for row in rows] == [user_branch, user_branch]
    assert len(queue(app)) == 2

    # … and the cap really is per branch now: the same slot can be used by another branch.
    with app.app_context():
        db = get_db()
        db.execute(
            "UPDATE vehicle_waiting_list SET branch_id = ? WHERE slot = 2", (ids["Branch 3"],)
        )
        db.execute(
            "INSERT INTO vehicle_waiting_list (slot, registration, branch_id, created_at, updated_at) "
            "VALUES (2, 'LEG 333', ?, 'now', 'now')",
            (ids["Branch 1"],),
        )
        db.commit()
        shared = db.execute(
            "SELECT COUNT(*) AS c FROM vehicle_waiting_list WHERE slot = 2"
        ).fetchone()["c"]
        # … while a fourth go at the SAME branch + slot is refused by the table itself.
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO vehicle_waiting_list (slot, registration, branch_id, created_at, updated_at) "
                "VALUES (2, 'LEG 444', ?, 'now', 'now')",
                (ids["Branch 1"],),
            )
        db.rollback()
    assert shared == 2


# ── the cap is per branch ───────────────────────────────────────────────────


def test_three_wait_per_branch_and_the_fourth_is_refused_for_that_branch_only(app):
    ids = branch_ids(app)
    first, second = ids["Branch 1"], ids["Branch 2"]
    for registration in ("AAA 111", "BBB 222", "CCC 333"):
        save(app, registration, branch_id=first)
    with pytest.raises(ValueError) as refused:
        save(app, "DDD 444", branch_id=first)
    assert "full for this branch" in str(refused.value)

    # The other branch still has its own three slots, even with the first branch full.
    for registration in ("EEE 111", "FFF 222", "GGG 333"):
        save(app, registration, branch_id=second)
    with pytest.raises(ValueError):
        save(app, "HHH 444", branch_id=second)

    rows = queue(app)
    assert len(rows) == 6
    assert [row["slot"] for row in rows if row["branch_id"] == first] == [1, 2, 3]
    assert [row["slot"] for row in rows if row["branch_id"] == second] == [1, 2, 3]


def test_a_plate_waiting_at_one_branch_cannot_wait_at_another(app):
    ids = branch_ids(app)
    save(app, "DUP 111", branch_id=ids["Branch 1"])
    with pytest.raises(ValueError) as refused:
        save(app, "dup111", branch_id=ids["Branch 2"])
    message = str(refused.value)
    assert "already waiting" in message
    assert "Branch 1" in message          # names where it is waiting
    assert len(queue(app)) == 1


# ── what each user sees ─────────────────────────────────────────────────────


def test_a_branch_user_only_sees_their_own_branch_queue(app):
    ids = branch_ids(app)
    save(app, "AAA 111", branch_id=ids["Branch 1"])
    save(app, "BBB 222", branch_id=ids["Branch 2"])

    with app.test_request_context("/customers"):
        from flask import session

        session["user_id"] = owner_id(app)
        session["user_role"] = "staff"
        session["branch_id"] = ids["Branch 1"]
        session["branch_ids"] = [ids["Branch 1"]]
        rows = [dict(row) for row in vehicles.list_waiting_vehicles()]
        groups = vehicles.waiting_vehicle_groups()
    assert [row["registration"] for row in rows] == ["AAA 111"]
    assert rows[0]["branch_name"] == "Branch 1"
    assert [group["branch_name"] for group in groups] == ["Branch 1"]
    assert groups[0]["count"] == 1 and groups[0]["capacity"] == 3


def test_an_all_branch_user_sees_every_queue_grouped_and_labelled(app):
    ids = branch_ids(app)
    for registration in ("AAA 111", "BBB 222"):
        save(app, registration, branch_id=ids["Branch 1"])
    save(app, "CCC 333", branch_id=ids["Branch 2"])

    with app.test_request_context("/customers"):
        from flask import session

        session["user_id"] = owner_id(app)
        session["user_role"] = "owner"
        session["can_view_all_branches"] = True
        rows = [dict(row) for row in vehicles.list_waiting_vehicles()]
        groups = vehicles.waiting_vehicle_groups()
    assert len(rows) == 3
    assert [group["count"] for group in groups] == [2, 1]
    assert [group["branch_name"] for group in groups] == ["Branch 1", "Branch 2"]


# ── screens ─────────────────────────────────────────────────────────────────


def test_the_customers_page_labels_the_branch_and_counts_per_branch(app, client):
    ids = branch_ids(app)
    save(app, "AAA 111", branch_id=ids["Branch 1"])
    save(app, "BBB 222", branch_id=ids["Branch 2"])

    sign_in(client, app)                                    # all branches
    html = client.get("/customers").get_data(as_text=True)
    assert "Vehicles awaiting allocation" in html
    assert "Branch 1" in html and "Branch 2" in html
    assert "1 / 3 vehicles" in html                         # per branch, never a combined n / 3
    assert "2 vehicles · 2 branches" in html
    assert "Maximum 3 vehicles per branch" in html
    assert "Branch: Branch 2" in html

    # A Branch 1 user sees that depot's single scan, labelled, and nothing from Branch 2.
    sign_in(client, app, role="staff", branch_id=ids["Branch 1"], branch_ids=[ids["Branch 1"]],
            can_view_all=False, modules=["customers"])
    html = client.get("/customers").get_data(as_text=True)
    assert "AAA 111" in html and "Branch: Branch 1" in html
    assert "BBB 222" not in html
    assert "1 / 3 vehicles" in html


def test_the_allocate_dialog_only_offers_the_clients_own_branch_vehicles(app, client):
    ids = branch_ids(app)
    customer_id = make_pending(app, ids["Branch 1"])
    save(app, "OWN 111", branch_id=ids["Branch 1"])
    save(app, "OTHER 222", branch_id=ids["Branch 2"])

    sign_in(client, app)                                    # all branches: still own-branch only
    html = client.get(f"/customers/{customer_id}/edit").get_data(as_text=True)
    assert "OWN 111" in html
    assert "OTHER 222" not in html
    assert "Branch 1 branch" in html


# ── allocation ──────────────────────────────────────────────────────────────


def test_allocating_a_scan_to_another_branchs_client_is_refused(app):
    ids = branch_ids(app)
    customer_id = make_pending(app, ids["Branch 1"])
    save(app, "CROSS 111", branch_id=ids["Branch 2"])

    with app.test_request_context("/customers"):
        from flask import session

        session["user_id"] = owner_id(app)
        session["user_role"] = "owner"
        session["can_view_all_branches"] = True
        waiting_id = int(vehicles.list_waiting_vehicles()[0]["id"])
        with pytest.raises(ValueError) as refused:
            vehicles.allocate_waiting_vehicle(waiting_id, customer_id)
    message = str(refused.value)
    assert "waiting at Branch 2" in message and "belongs to Branch 1" in message
    assert len(queue(app)) == 1                              # still waiting, nothing written
    with app.app_context():
        assert vehicles.list_vehicles(customer_id) == []


def test_allocation_still_works_inside_the_same_branch(app):
    ids = branch_ids(app)
    customer_id = make_pending(app, ids["Branch 1"])
    save(app, "SAME 111", branch_id=ids["Branch 1"])

    with app.test_request_context("/customers"):
        from flask import session

        session["user_id"] = owner_id(app)
        session["user_role"] = "owner"
        session["can_view_all_branches"] = True
        waiting_id = int(vehicles.list_waiting_vehicles()[0]["id"])
        vehicle_id = vehicles.allocate_waiting_vehicle(waiting_id, customer_id)
        assert vehicles.get_main_vehicle_id(customer_id) == vehicle_id
    assert queue(app) == []


def test_allocation_branch_check_reads_a_turso_style_row(app, monkeypatch):
    ids = branch_ids(app)
    customer_id = make_pending(app, ids["Branch 1"])
    save(app, "TURSO 235", branch_id=ids["Branch 2"])
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

    with app.test_request_context("/customers"):
        from flask import session

        session["user_id"] = owner_id(app)
        session["user_role"] = "owner"
        session["can_view_all_branches"] = True
        monkeypatch.setattr(vehicles, "get_waiting_vehicle", read)
        waiting_id = int(vehicles.list_waiting_vehicles()[0]["id"])
        with pytest.raises(ValueError) as refused:
            vehicles.allocate_waiting_vehicle(waiting_id, customer_id)
    assert "waiting at Branch 2" in str(refused.value)
    assert len(queue(app)) == 1
