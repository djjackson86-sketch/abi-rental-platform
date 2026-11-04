"""Tests for scripts/recover_customer_lifecycle.py (ABI-341953272).

The recovery script is maintenance code, so these tests drive it exactly as an
operator would: a throwaway SQLite database, ``main(argv)`` and the printed
counts. No app fixture is needed and nothing touches the app itself.
"""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "recover_customer_lifecycle.py"

LIFECYCLE_DDL = """
    lifecycle_status TEXT NOT NULL DEFAULT 'active',
    lifecycle_changed_at TEXT NOT NULL DEFAULT '',
    lifecycle_reason TEXT NOT NULL DEFAULT '',
    lifecycle_changed_by_user_id INTEGER,
    lifecycle_source TEXT NOT NULL DEFAULT ''
"""


def load_script():
    spec = importlib.util.spec_from_file_location("recover_customer_lifecycle", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def recovery():
    return load_script()


def make_db(tmp_path, with_lifecycle=True):
    path = tmp_path / "abi.db"
    conn = sqlite3.connect(path)
    lifecycle = ("," + LIFECYCLE_DDL) if with_lifecycle else ""
    conn.executescript(
        """
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
            {lifecycle}
        );
        CREATE TABLE portal_registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference TEXT NOT NULL DEFAULT '',
            customer_id INTEGER NOT NULL,
            branch_id INTEGER,
            name TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            approved_at TEXT,
            approved_by_user_id INTEGER,
            archive_reason TEXT NOT NULL DEFAULT '',
            archived_at TEXT,
            archived_by_user_id INTEGER
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT NOT NULL UNIQUE,
            customer_id INTEGER,
            created_at TEXT NOT NULL
        );
        """.format(lifecycle=lifecycle)
    )
    conn.commit()
    conn.close()
    return path


def add_customer(path, name="Customer", created_at="2026-10-01T09:00:00"):
    conn = sqlite3.connect(path)
    cur = conn.execute("INSERT INTO customers (name, created_at) VALUES (?, ?)", (name, created_at))
    conn.commit()
    customer_id = cur.lastrowid
    conn.close()
    return customer_id


def add_registration(
    path,
    customer_id,
    status="deleted",
    reason="",
    archived_at=None,
    archived_by=None,
    created_at="2026-10-02T10:00:00",
):
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO portal_registrations "
        "(customer_id, status, created_at, archive_reason, archived_at, archived_by_user_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (customer_id, status, created_at, reason, archived_at, archived_by),
    )
    conn.commit()
    registration_id = cur.lastrowid
    conn.close()
    return registration_id


def add_order(path, customer_id, number="ORD-1"):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO orders (order_number, customer_id, created_at) VALUES (?, ?, ?)",
        (number, customer_id, "2026-10-03T09:00:00"),
    )
    conn.commit()
    conn.close()


def customer_row(path, customer_id):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def run(recovery, path, *extra, capsys=None):
    code = recovery.main(["--database", str(path), *extra])
    out = capsys.readouterr().out if capsys else ""
    return code, out


def test_dry_run_is_default_and_writes_nothing(tmp_path, recovery, capsys):
    db = make_db(tmp_path)
    customer_id = add_customer(db)
    add_registration(db, customer_id, reason="Booking cancelled", archived_at="2026-10-20T08:00:00", archived_by=5)

    code, out = run(recovery, db, capsys=capsys)

    assert code == 0
    assert "DRY RUN - nothing written" in out
    assert "customers recoverable now       : 1" in out
    assert customer_row(db, customer_id)["lifecycle_status"] == "active"
    assert customer_row(db, customer_id)["lifecycle_source"] == ""


def test_apply_recovers_only_tombstoned_customers(tmp_path, recovery, capsys):
    db = make_db(tmp_path)
    untouched = add_customer(db, "Plain client")
    archived = add_customer(db, "Archived client")
    legacy = add_customer(db, "Legacy tombstone")
    approved = add_customer(db, "Approved client")
    add_registration(db, approved, status="approved", created_at="2026-10-02T10:00:00")
    archived_reg = add_registration(
        db, archived, reason="Customer never came", archived_at="2026-10-20T08:15:00", archived_by=5
    )
    legacy_reg = add_registration(db, legacy)
    add_order(db, archived, "ORD-9")

    code, out = run(recovery, db, "--apply", "--backup-dir", str(tmp_path / "backups"), capsys=capsys)

    assert code == 0
    assert "Rows updated: 2 / 2" in out
    assert "Verified rows: 2, mismatches: 0" in out

    row = customer_row(db, archived)
    assert row["lifecycle_status"] == "deleted"
    assert row["lifecycle_reason"] == "Customer never came"
    assert row["lifecycle_changed_at"] == "2026-10-20T08:15:00"
    assert row["lifecycle_changed_by_user_id"] == 5
    assert row["lifecycle_source"] == "portal_registration_tombstone:%d" % archived_reg

    legacy_row = customer_row(db, legacy)
    assert legacy_row["lifecycle_status"] == "deleted"
    assert legacy_row["lifecycle_reason"] == recovery.LEGACY_REASON
    assert legacy_row["lifecycle_changed_at"] == ""
    assert legacy_row["lifecycle_changed_by_user_id"] is None
    assert legacy_row["lifecycle_source"] == "portal_registration_tombstone:%d" % legacy_reg

    for untouched_id in (untouched, approved):
        row = customer_row(db, untouched_id)
        assert row["lifecycle_status"] == "active"
        assert row["lifecycle_source"] == ""


def test_second_apply_is_idempotent(tmp_path, recovery, capsys):
    db = make_db(tmp_path)
    customer_id = add_customer(db)
    add_registration(db, customer_id, reason="No longer want to rent", archived_at="2026-10-21T07:00:00", archived_by=4)

    first, _ = run(recovery, db, "--apply", "--backup-dir", str(tmp_path / "backups"), capsys=capsys)
    state_after_first = customer_row(db, customer_id)
    second, out = run(recovery, db, "--apply", "--backup-dir", str(tmp_path / "backups"), capsys=capsys)

    assert first == 0
    assert second == 0
    assert "customers recoverable now       : 0" in out
    assert "Nothing to recover - already idempotent." in out
    assert customer_row(db, customer_id) == state_after_first


def test_existing_lifecycle_state_is_never_overwritten(tmp_path, recovery, capsys):
    db = make_db(tmp_path)
    customer_id = add_customer(db)
    add_registration(db, customer_id, reason="Staff archived", archived_at="2026-10-20T08:00:00", archived_by=5)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE customers SET lifecycle_status = 'archived', lifecycle_reason = 'manual archive', "
        "lifecycle_source = 'manual:7' WHERE id = ?",
        (customer_id,),
    )
    conn.commit()
    conn.close()

    code, out = run(recovery, db, "--apply", "--backup-dir", str(tmp_path / "backups"), capsys=capsys)

    assert code == 0
    assert "already non-active (skipped)    : 1" in out
    row = customer_row(db, customer_id)
    assert row["lifecycle_status"] == "archived"
    assert row["lifecycle_reason"] == "manual archive"
    assert row["lifecycle_source"] == "manual:7"


def test_sourced_active_row_is_not_double_stamped(tmp_path, recovery, capsys):
    db = make_db(tmp_path)
    customer_id = add_customer(db)
    add_registration(db, customer_id, reason="Staff archived", archived_at="2026-10-20T08:00:00", archived_by=5)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE customers SET lifecycle_status = 'active', lifecycle_source = 'manual:9' WHERE id = ?",
        (customer_id,),
    )
    conn.commit()
    conn.close()

    code, out = run(recovery, db, "--apply", "--backup-dir", str(tmp_path / "backups"), capsys=capsys)

    assert code == 0
    assert "already sourced (skipped)       : 1" in out
    assert customer_row(db, customer_id)["lifecycle_source"] == "manual:9"


def test_orphan_tombstone_is_evidence_only(tmp_path, recovery, capsys):
    db = make_db(tmp_path)
    add_registration(db, 999, reason="Booking cancelled", archived_at="2026-10-20T08:00:00", archived_by=5)

    code, out = run(recovery, db, "--apply", "--backup-dir", str(tmp_path / "backups"), capsys=capsys)

    assert code == 0
    assert "tombstone without live customer : 1" in out
    assert "no longer exists" in out
    assert "Nothing to recover" in out
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0
    conn.close()


def test_duplicate_tombstones_use_the_newest_and_report_the_conflict(tmp_path, recovery, capsys):
    db = make_db(tmp_path)
    customer_id = add_customer(db)
    older = add_registration(db, customer_id, reason="first attempt", archived_at="2026-10-15T08:00:00", archived_by=3)
    newer = add_registration(db, customer_id, reason="second attempt", archived_at="2026-10-25T08:00:00", archived_by=5)

    code, out = run(recovery, db, "--apply", "--backup-dir", str(tmp_path / "backups"), capsys=capsys)

    assert code == 0
    assert "multiple tombstones (conflict)  : 1" in out
    row = customer_row(db, customer_id)
    assert row["lifecycle_reason"] == "second attempt"
    assert row["lifecycle_source"] == "portal_registration_tombstone:%d" % newer
    assert row["lifecycle_source"] != "portal_registration_tombstone:%d" % older


def test_missing_lifecycle_columns_plan_but_refuse_apply(tmp_path, recovery, capsys):
    db = make_db(tmp_path, with_lifecycle=False)
    customer_id = add_customer(db)
    add_registration(db, customer_id, reason="Booking cancelled", archived_at="2026-10-20T08:00:00", archived_by=5)

    dry_code, dry_out = run(recovery, db, capsys=capsys)
    apply_code, apply_out = run(recovery, db, "--apply", "--backup-dir", str(tmp_path / "backups"), capsys=capsys)

    assert dry_code == 0
    assert "customers recoverable now       : 1" in dry_out
    assert "missing lifecycle columns" in apply_out
    assert apply_code == 0
    assert not (tmp_path / "backups").exists()
    conn = sqlite3.connect(db)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(customers)")}
    assert "lifecycle_status" not in columns
    conn.close()


def test_backup_is_written_before_the_first_row_changes(tmp_path, recovery):
    db = make_db(tmp_path)
    customer_id = add_customer(db)
    add_registration(db, customer_id, reason="Booking cancelled", archived_at="2026-10-20T08:00:00", archived_by=5)

    from booqable_db import connect

    db_conn = connect(database=str(db))
    plan, counts, _ = recovery.build_plan(recovery.evidence_rows(db_conn, True))
    assert counts["recoverable"] == 1
    backup_dir = tmp_path / "backups"
    outcomes, backup_path = recovery.apply_plan(db_conn, plan, str(backup_dir))
    db_conn.close()

    assert outcomes[0]["changed"] == 1
    payload = json.loads(Path(backup_path).read_text(encoding="utf-8"))
    assert payload["ticket"] == "ABI-341953272"
    assert payload["planned_rows"][0]["customer_id"] == customer_id
    assert payload["planned_rows"][0]["lifecycle_reason"] == "Booking cancelled"
    assert Path(backup_path).parent == backup_dir


def test_blocked_conditional_write_reports_zero_changed(tmp_path, recovery):
    """If the row changed between plan and write, the guard must not clobber it."""
    db = make_db(tmp_path)
    customer_id = add_customer(db)
    add_registration(db, customer_id, reason="Booking cancelled", archived_at="2026-10-20T08:00:00", archived_by=5)

    from booqable_db import connect

    db_conn = connect(database=str(db))
    plan, _, _ = recovery.build_plan(recovery.evidence_rows(db_conn, True))
    conn = sqlite3.connect(db)
    conn.execute("UPDATE customers SET lifecycle_status = 'archived' WHERE id = ?", (customer_id,))
    conn.commit()
    conn.close()

    outcomes, _ = recovery.apply_plan(db_conn, plan, str(tmp_path / "backups"))
    mismatches, checked = recovery.verify_plan(db_conn, plan)
    db_conn.close()

    assert outcomes[0]["changed"] == 0
    assert checked == 1
    assert mismatches and mismatches[0]["customer_id"] == customer_id
