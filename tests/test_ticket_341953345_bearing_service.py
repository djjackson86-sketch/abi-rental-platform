import re
import sqlite3
from datetime import datetime, timedelta

import pytest

from app import create_app
from app.db import get_db, run_migrations
from app.services import orders, trailer_service
from app.services.timezone import local_now


@pytest.fixture
def app(tmp_path):
    return create_app({"TESTING": True, "DATABASE": str(tmp_path / "bearing.db"),
                       "TURSO_DATABASE_URL": "", "SECRET_KEY": "test-only",
                       "ADMIN_PASSWORD": "admin123"})


def sign_in(app, *, staff=False, modules=None):
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=1, user_role="staff" if staff else "owner", user_name="Test",
                       branch_id=1 if staff else None, branch_ids=[1] if staff else [],
                       staff_modules=modules or ["inventory", "dashboard"],
                       can_view_all_branches=not staff)
    return client


def seed(app, interval=3):
    with app.app_context():
        db = get_db()
        group = db.execute("INSERT INTO product_groups (name,created_at,updated_at) VALUES ('1.5m Luggage Trailer','','')").lastrowid
        ids = [db.execute("""INSERT INTO products (name,sku,registration,branch_id,product_group_id,tracking_method,created_at)
                             VALUES ('Luggage trailer',?,?,?,?,'individual','')""",
                          (sku, plate, branch, group)).lastrowid
               for sku, plate, branch in [("UNIT-1", "PLATE-1", 1), ("UNIT-2", "PLATE-2", 2)]]
        trailer_service.save_bearing_interval(group, str(interval))
        return ids


def hire(unit, status, start, end, *, scheduled=None, revised="", scanned="", returned="", archived="", qty=1, branch=1):
    db = get_db()
    oid = db.execute("""INSERT INTO orders (order_number,status,status_before_archive,picked_up_at,start_at,end_at,
                          return_revised_at,return_scan_at,hire_returned_at,collect_branch_id,created_at)
                        VALUES (lower(hex(randomblob(8))),?,?,?,?,?,?,?,?,?,'')""",
                     (status, archived, start, scheduled or start, end, revised, scanned, returned, branch)).lastrowid
    db.execute("INSERT INTO order_items (order_id,product_id,quantity) VALUES (?,?,?)", (oid, unit, qty))
    db.commit()
    return oid


def test_migration_seeds_exact_verified_names_and_preserves_edits(app):
    with app.app_context():
        db = get_db()
        names = [*trailer_service.BEARING_INTERVAL_DEFAULTS, "3m Utility Trailer - 1⁄2 ton"]
        db.executemany("INSERT INTO product_groups (name,created_at,updated_at) VALUES (?,'','')", [(n,) for n in names])
        run_migrations(db)
        sql = "SELECT g.name, i.interval_days FROM bearing_service_intervals i JOIN product_groups g ON g.id=i.product_group_id"
        defaults = dict(db.execute(sql))
        assert len(defaults) == 22
        assert defaults == trailer_service.BEARING_INTERVAL_DEFAULTS
        assert "3m Utility Trailer - 1⁄2 ton" not in defaults
        id_ = db.execute("SELECT id FROM product_groups WHERE name='Piggyback Trailer'").fetchone()[0]
        trailer_service.save_bearing_interval(id_, "47")
        run_migrations(db)
        assert dict(db.execute(sql)) == {**defaults, "Piggyback Trailer": 47}
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            db.execute("UPDATE bearing_service_intervals SET interval_days=1.5")


def test_elapsed_hires_round_up_and_count_each_unit_independently(app):
    a, b = seed(app)
    with app.app_context():
        oid = hire(a, "returned", None, "2025-01-02T10:00", scheduled="2025-01-01T10:00", branch=2)
        get_db().execute("INSERT INTO order_items (order_id,product_id) VALUES (?,?)", (oid, a))
        hire(a, "started", "2025-01-03T10:00", "2025-01-03T11:00")
        for status in ["draft", "reserved", "canceled", "sales_repairs"]:
            hire(a, status, "2025-01-01T10:00", "2025-01-31T10:00")
        hire(a, "archived", "2025-01-01T10:00", "2025-01-31T10:00", archived="sales_repairs")
        hire(b, "archived", "2025-01-01T10:00", "2025-01-31T10:00", archived="returned", scanned="2025-01-02T11:00")
        use = lambda d: trailer_service.bearing_usage(as_of=datetime.fromisoformat(d))
        at_threshold = use("2025-01-04T10:01")
        assert (at_threshold[a]["used_days"], at_threshold[a]["remaining_days"], at_threshold[a]["due"], at_threshold[a]["over_days"]) == (3, 0, True, 0)
        assert (at_threshold[b]["used_days"], at_threshold[b]["remaining_days"], at_threshold[b]["due"]) == (2, 1, False)
        assert use("2025-01-04T10:00")[a]["remaining_days"] == 1
        assert use("2025-01-05T10:01")[a]["over_days"] == 1


def test_actual_returns_revisions_and_unverifiable_records(app):
    a, b = seed(app)
    with app.app_context():
        hire(a, "returned", "2025-01-01T10:00", "2025-01-12T10:00", returned="2025-01-02T10:00")
        hire(a, "returned", "2025-01-03T10:00", "2025-01-04T10:30", returned="2025-01-12T10:00", revised="2025-01-09")
        hire(a, "returned", "2025-01-03T10:00", "2025-01-02T10:30")
        hire(a, "returned", "2025-01-03T10:00", "broken")
        hire(a, "returned", "2025-01-03T10:00", "2025-01-04T10:00", qty=2)
        oid = hire(b, "started", "2025-01-06T10:00", "2025-01-09T10:00")
        db = get_db()
        db.execute("INSERT INTO documents (order_id,document_type,status,created_at) VALUES (?,'invoice','finalized','')", (oid,))
        db.execute("UPDATE orders SET no_damages=1, no_revision_required=1 WHERE id=?", (oid,))
        orders.transition_order(oid, "return")
        row = get_db().execute("SELECT status, hire_returned_at FROM orders WHERE id=?", (oid,)).fetchone()
        assert row["status"] == "returned" and row["hire_returned_at"][:10] == local_now().date().isoformat()
        unit = trailer_service.bearing_usage(as_of=datetime(2025, 2, 1))[a]
        assert unit["used_days"] == 3 and unit["unverified_hires"] == 3


def test_existing_cash_service_route_resets_only_bearings(app):
    a, b = seed(app)
    client = sign_in(app)
    today = local_now()
    with app.app_context():
        for unit in (a, b):
            hire(unit, "returned", (today - timedelta(days=8)).isoformat(), (today - timedelta(days=1)).isoformat())
    partial = "/dashboard/partials/bearing-service"
    data = {"branch": "1", "product_id": a, "day": today.date().isoformat()}
    assert b"PLATE-1" in client.get(partial).data
    assert client.post("/cash-up/trailer-service", data={**data, "service_type": "Tyre change"}).status_code == 302
    assert b"PLATE-1" in client.get(partial).data
    assert client.post("/cash-up/trailer-service", data={**data, "service_type": ["Bearing service", "Brake adjustment"]}).status_code == 302
    assert b"PLATE-1" not in client.get(partial).data and b"PLATE-2" in client.get(partial).data
    assert b"Remaining: 3 rental days" in client.get(f"/inventory/{a}/edit").data
    with app.app_context():
        reset = datetime.fromisoformat(get_db().execute("SELECT created_at FROM trailer_service_history WHERE service_type='Bearing service'").fetchone()[0])
        hire(a, "started", (reset + timedelta(hours=1)).isoformat(), (reset + timedelta(days=3)).isoformat())
        assert trailer_service.bearing_usage(as_of=reset + timedelta(hours=2))[a]["remaining_days"] == 2


def test_backdated_service_only_counts_hire_time_after_service_day(app):
    a, _ = seed(app)
    with app.app_context():
        hire(a, "returned", "2025-01-01T10:00", "2025-01-06T10:00")
        db = get_db()
        db.execute("""INSERT INTO trailer_service_history (product_id,service_type,service_date,created_at)
                      VALUES (?, 'Bearing service', '2025-01-04', '2025-01-07T10:00')""", (a,))
        result = trailer_service.bearing_usage(as_of=datetime(2025, 1, 8))[a]
        assert result["used_days"] == 2


@pytest.mark.parametrize("bad", ["", "0", "-2", "2.5", "1e2", "9999999999"])
def test_bad_interval_does_not_write(app, bad):
    a, b = seed(app)
    client = sign_in(app)
    page = client.get(f"/inventory/{a}/edit").data
    csrf = re.search(rb'name="csrf_token" value="([^"]+)"', page)[1].decode()
    response = client.post(f"/inventory/{a}/bearing-interval", data={"csrf_token": csrf, "interval_days": bad}, follow_redirects=True)
    assert b"Enter a positive whole number of days" in response.data
    assert b"Model interval: 3 days" in client.get(f"/inventory/{b}/edit").data


def test_interval_edit_and_dashboard_routes_enforce_scope(app):
    a, b = seed(app)
    with app.app_context():
        for unit in (a, b):
            hire(unit, "returned", "2025-01-01T10:00", "2025-01-06T10:00")
    owner, staff = sign_in(app), sign_in(app, staff=True)
    route = f"/inventory/{a}/bearing-interval"
    page = owner.get(f"/inventory/{a}/edit").data
    csrf = re.search(rb'name="csrf_token" value="([^"]+)"', page)[1].decode()
    assert owner.post(route, data={"interval_days": "6"}).status_code == 400
    assert staff.post(route, data={"interval_days": "6", "csrf_token": csrf}).status_code == 403
    assert staff.get(f"/inventory/{b}/edit").status_code == 404
    assert b"Save bearing interval" not in staff.get(f"/inventory/{a}/edit").data
    partial = "/dashboard/partials/bearing-service"
    scoped = staff.get(partial + "?branch=2")
    assert scoped.status_code == 200 and b"PLATE-1" in scoped.data and b"PLATE-2" not in scoped.data
    assert app.test_client().get(partial).status_code == 302
    assert sign_in(app, staff=True, modules=["inventory"]).get(partial).status_code == 403
    assert b"PLATE-2" in owner.get("/dashboard").data
    app.config["TESTING"] = False
    assert b'data-dashboard-section="bearing-service"' in owner.get("/dashboard").data
    assert b"PLATE-2" in owner.get(partial + "?branch=2").data
    assert owner.post(route, data={"interval_days": "6", "csrf_token": csrf}).status_code == 302
    assert b"Remaining: 1 rental days" in owner.get(f"/inventory/{b}/edit").data
    assert b"No trailers with a configured interval" in owner.get(partial).data
