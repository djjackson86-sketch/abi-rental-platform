"""ABI-341953272 — recover customer lifecycle state from retained portal tombstones.

What this does
--------------
The customer lifecycle feature (active / deleted / archived) is additive: existing
rows stay ``active`` until something marks them otherwise. The only reliable,
ID-bearing evidence of a customer having already been *removed* in production is
a retained ``portal_registrations`` row with ``status = 'deleted'`` — the
verification-phase tombstone written by tickets ABI-341953232/341953210. That row
points at the customer by primary key and, for archives made through the current
UI, carries ``archive_reason``, ``archived_at`` and ``archived_by_user_id``.

This script turns that evidence into lifecycle fields, never the other way round:

* it only *reads* ``portal_registrations``; it never edits or revives one;
* it only writes the ``customers.lifecycle_*`` columns, conditionally, on a
  customer that is still ``active`` and has no recorded source yet;
* the registration id is the provenance, so a second run (or a run after the app
  itself archived the client) changes nothing.

Names, emails and "customer is absent from a list" are deliberately NOT evidence.
A customer row that no longer exists cannot be recovered from here; the script
reports those ids as unrecoverable evidence only.

Column contract (the app migration owns these; this script refuses to write
without all five, so it can never invent a half-schema):

    ALTER TABLE customers ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'active';
    ALTER TABLE customers ADD COLUMN lifecycle_changed_at TEXT NOT NULL DEFAULT '';
    ALTER TABLE customers ADD COLUMN lifecycle_reason TEXT NOT NULL DEFAULT '';
    ALTER TABLE customers ADD COLUMN lifecycle_changed_by_user_id INTEGER;
    ALTER TABLE customers ADD COLUMN lifecycle_source TEXT NOT NULL DEFAULT '';

``lifecycle_source`` is ``portal_registration_tombstone:<registration id>``.

Usage
-----
    python scripts/recover_customer_lifecycle.py                     # dry run, local default DB
    python scripts/recover_customer_lifecycle.py --database /tmp/x.db
    python scripts/recover_customer_lifecycle.py --live               # dry run against live Turso
    python scripts/recover_customer_lifecycle.py --live --apply       # owner-approved write

Dry run is the default. ``--apply`` snapshots every row it is about to change to
``backups/tickets/`` before the first write (libSQL autocommits; there is no
rollback inside the database).
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from booqable_db import ROOT, connect  # noqa: E402

#: The lifecycle columns the app migration adds to ``customers``.
LIFECYCLE_COLUMNS = (
    "lifecycle_status",
    "lifecycle_changed_at",
    "lifecycle_reason",
    "lifecycle_changed_by_user_id",
    "lifecycle_source",
)

STATUS_ACTIVE = "active"
STATUS_DELETED = "deleted"
REGISTRATION_TOMBSTONE = "deleted"
SOURCE_PREFIX = "portal_registration_tombstone:"

#: Reason recorded when the tombstone predates the reason column (the legacy
#: ``delete_pending_registration`` path). Explicitly says the wording was never
#: captured rather than pretending the archive had no cause.
LEGACY_REASON = "Portal sign-up removed before archive reasons were recorded"

BACKUP_DIR = os.path.join(str(ROOT), "backups", "tickets")
PAUSE_SECONDS = 0.3

_EVIDENCE_BASE = """
SELECT pr.id                AS registration_id,
       pr.customer_id       AS customer_id,
       pr.created_at        AS registered_at,
       pr.archived_at       AS archived_at,
       pr.archive_reason    AS archive_reason,
       pr.archived_by_user_id AS archived_by_user_id,
       c.id                 AS customer_exists,
       c.name               AS customer_name,
       c.created_at         AS customer_created_at,
       (SELECT COUNT(*) FROM orders o WHERE o.customer_id = pr.customer_id) AS order_count
       {extra_columns}
FROM portal_registrations pr
LEFT JOIN customers c ON c.id = pr.customer_id
WHERE pr.status = ?
ORDER BY pr.id
"""


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Recover customer lifecycle state from retained portal tombstones (ABI-341953272)."
    )
    parser.add_argument("--database", help="local sqlite database to work on")
    parser.add_argument("--live", action="store_true", help="target the live Turso database")
    parser.add_argument("--apply", action="store_true", help="write the changes (default is a dry run)")
    parser.add_argument("--backup-dir", default=BACKUP_DIR, help="where --apply snapshots rows first")
    parser.add_argument("--plan-out", help="optional path to dump the planned rows as JSON")
    parser.add_argument("--show-names", action="store_true", help="include customer names in the sample output")
    return parser.parse_args(argv)


def columns(db, table):
    return {row["name"] for row in db.query("PRAGMA table_info(%s)" % table)}


def _value(row, key, default=None):
    """Read a column from a sqlite3.Row, a dict or a libSQL Row."""
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        pass
    try:
        return row.asdict().get(key, default)
    except (AttributeError, TypeError):
        return default


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def evidence_rows(db, has_lifecycle):
    """Every retained tombstone, joined to its customer when the row still exists."""
    extra = ""
    if has_lifecycle:
        extra = ",\n       c.lifecycle_status AS lifecycle_status, c.lifecycle_source AS lifecycle_source"
    sql = _EVIDENCE_BASE.format(extra_columns=extra)
    return [_as_dict(row, has_lifecycle) for row in db.query(sql, (REGISTRATION_TOMBSTONE,))]


def _as_dict(row, has_lifecycle):
    keys = [
        "registration_id",
        "customer_id",
        "registered_at",
        "archived_at",
        "archive_reason",
        "archived_by_user_id",
        "customer_exists",
        "customer_name",
        "customer_created_at",
        "order_count",
    ]
    if has_lifecycle:
        keys += ["lifecycle_status", "lifecycle_source"]
    return {key: _value(row, key) for key in keys}


def _newest(rows):
    """Newest tombstone first: an archive timestamp wins, then the registration id."""
    return sorted(
        rows,
        key=lambda row: (str(row.get("archived_at") or ""), _as_int(row.get("registration_id")) or 0),
        reverse=True,
    )


def build_plan(evidence, has_lifecycle=True):
    """Pure decision step: which customers get which lifecycle values, and why not.

    Returns ``(plan, counts, notes)``. ``plan`` is ordered by customer id so two
    runs over the same database produce the same JSON and the same write order.
    """
    counts = {
        "tombstones": len(evidence),
        "recoverable": 0,
        "already_non_active": 0,
        "already_sourced": 0,
        "missing_customer": 0,
        "conflicting_evidence": 0,
        "legacy_without_reason": 0,
        "with_orders": 0,
    }
    notes = []
    by_customer = {}
    for row in evidence:
        if row.get("customer_exists") is None:
            counts["missing_customer"] += 1
            notes.append(
                "registration #%s points at customer #%s, which no longer exists - "
                "evidence retained, record not recoverable from here"
                % (row.get("registration_id"), row.get("customer_id"))
            )
            continue
        by_customer.setdefault(row["customer_id"], []).append(row)

    plan = []
    for customer_id in sorted(by_customer):
        group = by_customer[customer_id]
        if len(group) > 1:
            counts["conflicting_evidence"] += 1
            notes.append(
                "customer #%s has %d tombstones; using registration #%s and leaving the rest as evidence"
                % (customer_id, len(group), _newest(group)[0]["registration_id"])
            )
        chosen = _newest(group)[0]
        status = chosen.get("lifecycle_status") if has_lifecycle else STATUS_ACTIVE
        source = chosen.get("lifecycle_source") if has_lifecycle else ""
        if status not in (None, "", STATUS_ACTIVE):
            counts["already_non_active"] += 1
            continue
        if source:
            counts["already_sourced"] += 1
            continue
        reason = (chosen.get("archive_reason") or "").strip()
        if not reason:
            counts["legacy_without_reason"] += 1
            reason = LEGACY_REASON
        if _as_int(chosen.get("order_count")) or 0:
            counts["with_orders"] += 1
        plan.append(
            {
                "customer_id": customer_id,
                "registration_id": chosen["registration_id"],
                "lifecycle_status": STATUS_DELETED,
                "lifecycle_changed_at": chosen.get("archived_at") or "",
                "lifecycle_reason": reason,
                "lifecycle_changed_by_user_id": _as_int(chosen.get("archived_by_user_id")),
                "lifecycle_source": SOURCE_PREFIX + str(chosen["registration_id"]),
                "order_count": _as_int(chosen.get("order_count")) or 0,
                "customer_name": chosen.get("customer_name"),
            }
        )
    counts["recoverable"] = len(plan)
    return plan, counts, notes


def write_backup(path, payload):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    return path


def apply_plan(db, plan, backup_dir):
    """Conditional, idempotent writes. Returns the per-row outcomes."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = os.path.join(backup_dir, "ABI-341953272-lifecycle-recovery-%s.json" % stamp)
    before_rows = []
    for row in plan:
        before = db.query("SELECT * FROM customers WHERE id = ?", (row["customer_id"],))
        from app.services.reports import row_dict
        before_rows.extend(row_dict(saved) for saved in before)
    write_backup(
        backup_path,
        {
            "ticket": "ABI-341953272",
            "created_at": stamp,
            "target": db.describe,
            "planned_rows": plan,
            "before_customers": before_rows,
        },
    )
    outcomes = []
    for row in plan:
        cur = db.exec(
            "UPDATE customers SET lifecycle_status = ?, lifecycle_changed_at = ?, "
            "lifecycle_reason = ?, lifecycle_changed_by_user_id = ?, lifecycle_source = ? "
            "WHERE id = ? AND COALESCE(lifecycle_status, 'active') = 'active' "
            "AND COALESCE(lifecycle_source, '') = ''",
            (
                row["lifecycle_status"],
                row["lifecycle_changed_at"],
                row["lifecycle_reason"],
                row["lifecycle_changed_by_user_id"],
                row["lifecycle_source"],
                row["customer_id"],
            ),
        )
        changed = cursor_rowcount(cur)
        outcomes.append({"customer_id": row["customer_id"], "changed": changed})
        if changed and PAUSE_SECONDS:
            time.sleep(PAUSE_SECONDS)
    return outcomes, backup_path


def cursor_rowcount(cursor):
    try:
        return int(cursor.rowcount or 0)
    except (AttributeError, TypeError, ValueError):
        return 0


def verify_plan(db, plan):
    """Read the target columns back: the plan is only 'done' when the rows say so."""
    if not plan:
        return [], 0
    ids = [row["customer_id"] for row in plan]
    placeholders = ", ".join("?" for _ in ids)
    rows = db.query(
        "SELECT id, lifecycle_status, lifecycle_source FROM customers WHERE id IN (%s)" % placeholders,
        tuple(ids),
    )
    actual = {}
    for row in rows:
        actual[_value(row, "id")] = (_value(row, "lifecycle_status"), _value(row, "lifecycle_source"))
    mismatches = []
    for row in plan:
        want = (row["lifecycle_status"], row["lifecycle_source"])
        if actual.get(row["customer_id"]) != want:
            mismatches.append({"customer_id": row["customer_id"], "want": want, "got": actual.get(row["customer_id"])})
    return mismatches, len(actual)


def main(argv=None):
    args = parse_args(argv)
    if args.live and args.database:
        raise SystemExit("choose either --live or --database, not both")
    db = connect(database=args.database, live=args.live)
    print("Target: %s" % db.describe)

    existing = columns(db, "customers")
    if not existing:
        raise SystemExit("customers table not found on this database")
    missing = [name for name in LIFECYCLE_COLUMNS if name not in existing]
    if "portal_registrations" not in {row["name"] for row in db.query(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}:
        raise SystemExit("portal_registrations table not found on this database")
    has_lifecycle = not missing

    evidence = evidence_rows(db, has_lifecycle)
    plan, counts, notes = build_plan(evidence, has_lifecycle=has_lifecycle)

    print("portal tombstones read          : %d" % counts["tombstones"])
    print("customers recoverable now       : %d" % counts["recoverable"])
    print("already non-active (skipped)    : %d" % counts["already_non_active"])
    print("already sourced (skipped)       : %d" % counts["already_sourced"])
    print("tombstone without live customer : %d" % counts["missing_customer"])
    print("multiple tombstones (conflict)  : %d" % counts["conflicting_evidence"])
    print("legacy tombstone, no reason     : %d" % counts["legacy_without_reason"])
    print("recovered rows with orders      : %d" % counts["with_orders"])
    if notes:
        print("\nEvidence notes:")
        for note in notes:
            print("  - %s" % note)
    for row in plan[:5]:
        sample = "#%s registration #%s -> %s (%s)" % (
            row["customer_id"],
            row["registration_id"],
            row["lifecycle_status"],
            row["lifecycle_source"],
        )
        if args.show_names and row.get("customer_name"):
            sample = "%s %r" % (sample, row["customer_name"])
        print("  sample: %s" % sample)

    if args.plan_out:
        write_backup(
            args.plan_out,
            {"ticket": "ABI-341953272", "target": db.describe, "counts": counts, "rows": plan},
        )
        print("plan written to %s" % args.plan_out)

    if missing:
        print("\ncustomers is missing lifecycle columns: %s" % ", ".join(missing))
        print("Dry run only. Add the columns (app migration) before --apply.")
        db.close()
        return 0

    if not args.apply:
        print("\nDRY RUN - nothing written. Re-run with --apply to recover (owner approval first).")
        db.close()
        return 0

    if not plan:
        print("\nNothing to recover - already idempotent.")
        db.close()
        return 0

    outcomes, backup_path = apply_plan(db, plan, args.backup_dir)
    changed = sum(1 for outcome in outcomes if outcome["changed"])
    mismatches, checked = verify_plan(db, plan)
    print("\nBackup: %s" % backup_path)
    print("Rows updated: %d / %d" % (changed, len(plan)))
    print("Verified rows: %d, mismatches: %d" % (checked, len(mismatches)))
    for mismatch in mismatches:
        print("  MISMATCH customer #%s want=%r got=%r"
              % (mismatch["customer_id"], mismatch["want"], mismatch["got"]))
    db.close()
    return 0 if changed == len(plan) and not mismatches else 1


if __name__ == "__main__":
    sys.exit(main())
