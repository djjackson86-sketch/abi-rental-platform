"""Mark historical finalized invoices as email-sent without sending mail.

Ticket ABI-341953082: ABI asked for all finalized invoices created on or before
2026-09-27 to show as Sent instead of Unsent. This maintenance script only
updates invoice documents that are still explicitly unsent:

    document_type = 'invoice'
    status = 'finalized'
    date(created_at) <= cutoff date
    email_status is '' or 'not_sent'
    sent_at is ''

Dry-run is the default and prints the affected invoice IDs/numbers/order numbers
without customer details. ``--commit`` writes a JSON backup to ``~/abi-backups``
first because live Turso statements autocommit.

Usage:
    .venv/bin/python scripts/mark_finalized_invoices_sent.py --live
    .venv/bin/python scripts/mark_finalized_invoices_sent.py --live --commit
    .venv/bin/python scripts/mark_finalized_invoices_sent.py --database /tmp/test.db
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from booqable_db import connect, finish  # noqa: E402

BACKUP_DIR = os.path.join(os.path.expanduser("~"), "abi-backups")
DEFAULT_CUTOFF = "2026-09-27"
TICKET_ID = "ABI-341953082"

AFFECTED_SQL = """
SELECT
    d.id,
    d.number,
    d.order_id,
    d.created_at,
    COALESCE(d.email_status, '') AS email_status,
    COALESCE(d.sent_at, '') AS sent_at,
    o.order_number
FROM documents d
JOIN orders o ON o.id = d.order_id
LEFT JOIN customers c ON c.id = o.customer_id
WHERE d.document_type = 'invoice'
  AND d.status = 'finalized'
  AND substr(d.created_at, 1, 10) <= ?
  AND COALESCE(d.email_status, '') IN ('', 'not_sent')
  AND COALESCE(d.sent_at, '') = ''
ORDER BY date(d.created_at), d.id
"""

VERIFY_REMAINING_SQL = """
SELECT COUNT(*) AS remaining
FROM documents d
JOIN orders o ON o.id = d.order_id
WHERE d.document_type = 'invoice'
  AND d.status = 'finalized'
  AND substr(d.created_at, 1, 10) <= ?
  AND COALESCE(d.email_status, '') IN ('', 'not_sent')
  AND COALESCE(d.sent_at, '') = ''
"""

UPDATE_SQL = """
UPDATE documents
SET email_status = 'sent',
    sent_at = ?,
    email_error = ''
WHERE document_type = 'invoice'
  AND status = 'finalized'
  AND substr(created_at, 1, 10) <= ?
  AND COALESCE(email_status, '') IN ('', 'not_sent')
  AND COALESCE(sent_at, '') = ''
"""


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Mark historical finalized invoice email markers as sent.")
    parser.add_argument("--database", help="local sqlite database to work on")
    parser.add_argument("--live", action="store_true", help="target the live Turso database")
    parser.add_argument("--commit", action="store_true", help="write the changes (default is dry run)")
    parser.add_argument("--cutoff-date", default=DEFAULT_CUTOFF, help="inclusive YYYY-MM-DD document created_at cutoff")
    parser.add_argument("--sent-at", default="", help="explicit ISO timestamp to write; defaults to current UTC time")
    return parser.parse_args(argv)


def _value(row, key):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key)


def _row_dict(row):
    keys = ("id", "number", "order_id", "created_at", "email_status", "sent_at", "order_number")
    return {key: _value(row, key) for key in keys}


def affected_invoices(db, cutoff_date):
    return [_row_dict(row) for row in db.query(AFFECTED_SQL, (cutoff_date,))]


def remaining_count(db, cutoff_date):
    row = db.query(VERIFY_REMAINING_SQL, (cutoff_date,))[0]
    return int(_value(row, "remaining") or 0)


def mark_invoices_sent(db, cutoff_date, sent_at):
    cursor = db.exec(UPDATE_SQL, (sent_at, cutoff_date))
    return getattr(cursor, "rowcount", None)


def _print_audit(rows):
    if not rows:
        print("affected invoices: 0")
        return
    print(f"affected invoices: {len(rows)}")
    print("id | invoice | order | created_at | email_status | sent_at")
    for row in rows:
        print(
            f"{row['id']} | {row['number'] or '(blank)'} | {row['order_number'] or row['order_id']} | "
            f"{row['created_at']} | {row['email_status'] or '(blank)'} | {row['sent_at'] or '(blank)'}"
        )


def main(argv=None):
    args = parse_args(argv)
    if args.live and args.database:
        raise SystemExit("choose either --live or --database, not both")
    if not args.cutoff_date or len(args.cutoff_date) != 10:
        raise SystemExit("--cutoff-date must be YYYY-MM-DD")

    db = connect(database=args.database, live=args.live)
    print(f"Target: {db.describe}")
    print(f"Ticket: {TICKET_ID}")
    print(f"Cutoff date: {args.cutoff_date} (inclusive document created_at date)")

    columns = db.table_columns("documents")
    required = {"id", "order_id", "document_type", "status", "number", "created_at", "email_status", "sent_at", "email_error"}
    missing = sorted(required - columns)
    if missing:
        raise SystemExit(f"documents table is missing required columns: {', '.join(missing)}")

    rows = affected_invoices(db, args.cutoff_date)
    _print_audit(rows)
    before_remaining = remaining_count(db, args.cutoff_date)
    print(f"matching unsent count before: {before_remaining}")

    if not args.commit:
        print("\nDRY RUN - nothing written. Re-run with --commit only after the affected list is approved/expected.")
        db.close()
        return 0

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = os.path.join(BACKUP_DIR, f"{TICKET_ID.lower()}-mark-invoices-sent-{stamp}.json")
    sent_at = args.sent_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with open(backup_path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "ticket": TICKET_ID,
                "created_at": stamp,
                "target": db.describe,
                "cutoff_date": args.cutoff_date,
                "sent_at_to_write": sent_at,
                "rows": rows,
            },
            handle,
            ensure_ascii=False,
            indent=1,
        )
    print(f"\nBackup written: {backup_path}")

    rowcount = mark_invoices_sent(db, args.cutoff_date, sent_at)
    after_rows = affected_invoices(db, args.cutoff_date)
    after_remaining = remaining_count(db, args.cutoff_date)
    print(f"rows updated: {rowcount if rowcount is not None else len(rows)}")
    print(f"matching unsent count after: {after_remaining}")
    if after_rows:
        print("WARNING: rows still match the unsent predicate after update:")
        _print_audit(after_rows)
        db.close()
        return 2
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
