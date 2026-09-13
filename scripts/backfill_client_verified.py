"""Backfill customers.client_verified from the Booqable import audit field.

The Booqable customer export carried a ``client_verification`` value which the
import preserved inside ``customers.custom_fields_json`` as
``booqable_client_verification`` (a hidden audit key). This script copies it into
the new first-class ``customers.client_verified`` column so staff see the real
Yes/No instead of "Not set".

Rules
-----
* Only rows whose ``client_verified`` is NULL are touched - an answer a staff
  member entered in the app is never overwritten.
* ``Yes``/``true``/``1`` -> 1, ``No``/``false``/``0`` -> 0, anything else
  (blank, unknown wording) is left NULL and reported as skipped.
* Dry run is the default. ``--commit`` writes, and dumps every row it is about
  to change to ``~/abi-backups/`` first (libsql autocommits - there is no
  rollback inside the database).

Usage
-----
    .venv/bin/python scripts/backfill_client_verified.py                 # dry run, local default DB
    .venv/bin/python scripts/backfill_client_verified.py --database /tmp/x.db
    .venv/bin/python scripts/backfill_client_verified.py --live          # dry run against live Turso
    .venv/bin/python scripts/backfill_client_verified.py --live --commit # live write (owner-approved)
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from booqable_db import connect, finish  # noqa: E402

SOURCE_KEY = "booqable_client_verification"
TRUE_WORDS = {"yes", "true", "1"}
FALSE_WORDS = {"no", "false", "0"}
BACKUP_DIR = os.path.join(os.path.expanduser("~"), "abi-backups")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Backfill customers.client_verified from the Booqable import audit field.")
    parser.add_argument("--database", help="local sqlite database to work on")
    parser.add_argument("--live", action="store_true", help="target the live Turso database")
    parser.add_argument("--commit", action="store_true", help="write the changes (default is a dry run)")
    return parser.parse_args(argv)


def _raw_value(custom_fields_json):
    try:
        data = json.loads(custom_fields_json or "{}")
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data.get(SOURCE_KEY)


def _proposed(value):
    if value is None:
        return None, "no booqable_client_verification value"
    text = str(value).strip().lower()
    if text in TRUE_WORDS:
        return 1, ""
    if text in FALSE_WORDS:
        return 0, ""
    return None, f"unrecognised value {value!r}"


def main(argv=None):
    args = parse_args(argv)
    if args.live and args.database:
        raise SystemExit("choose either --live or --database, not both")
    db = connect(database=args.database, live=args.live)
    print(f"Target: {db.describe}")
    columns = db.table_columns("customers")
    if "client_verified" not in columns:
        raise SystemExit(
            "customers.client_verified is missing on this database - deploy the app "
            "(run_migrations adds it) before backfilling."
        )

    rows = db.query("SELECT id, name, client_verified, custom_fields_json FROM customers ORDER BY id")
    plan = []
    skipped = {}
    counts = {"already_set": 0, "would_set_yes": 0, "would_set_no": 0, "skipped": 0}
    for row in rows:
        if row["client_verified"] is not None:
            counts["already_set"] += 1
            continue
        value, reason = _proposed(_raw_value(row["custom_fields_json"]))
        if value is None:
            counts["skipped"] += 1
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        counts["would_set_yes" if value else "would_set_no"] += 1
        plan.append({"id": row["id"], "name": row["name"], "value": value})

    print(f"customers scanned      : {len(rows)}")
    print(f"already answered       : {counts['already_set']}")
    print(f"would set Client Verified = Yes : {counts['would_set_yes']}")
    print(f"would set Client Verified = No  : {counts['would_set_no']}")
    print(f"left as Not set        : {counts['skipped']}")
    for reason, number in sorted(skipped.items(), key=lambda item: -item[1]):
        print(f"    - {reason}: {number}")
    for row in plan[:5]:
        print(f"    sample: #{row['id']} {row['name']!r} -> {'Yes' if row['value'] else 'No'}")

    if not args.commit:
        print("\nDRY RUN - nothing written. Re-run with --commit to apply (owner approval first).")
        db.close()
        return 0

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = os.path.join(BACKUP_DIR, f"client-verified-backfill-{stamp}.json")
    with open(backup_path, "w", encoding="utf-8") as handle:
        json.dump(
            {"created_at": stamp, "target": db.describe, "source_key": SOURCE_KEY, "rows": plan},
            handle,
            ensure_ascii=False,
            indent=1,
        )
    print(f"\nBackup written: {backup_path}")

    written = 0
    for row in plan:
        db.exec("UPDATE customers SET client_verified = ? WHERE id = ?", (row["value"], row["id"]))
        written += 1
    print(f"rows updated: {written}")

    verified = db.query(
        "SELECT SUM(CASE WHEN client_verified = 1 THEN 1 ELSE 0 END) AS yes,"
        " SUM(CASE WHEN client_verified = 0 THEN 1 ELSE 0 END) AS no,"
        " SUM(CASE WHEN client_verified IS NULL THEN 1 ELSE 0 END) AS unset FROM customers"
    )[0]
    print(f"re-read: Yes {verified['yes']} | No {verified['no']} | Not set {verified['unset']}")
    db.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # never hang the libsql session on an error
        print(f"ERROR: {type(exc).__name__}: {exc}")
        finish(1)
    finally:
        finish(0)
