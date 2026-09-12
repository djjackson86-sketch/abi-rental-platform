#!/usr/bin/env python3
"""One-off: import a Booqable customer export into the ABI Rental Platform.

Defaults to a READ-ONLY dry run. Nothing is written unless you pass --commit.

Usage
-----
Local rehearsal against a scratch sqlite file:
    .venv/bin/python scripts/import_booqable_customers.py \
        --csv ~/abi-backups/booqable-import/customers-export-2026-09-12.csv \
        --database /tmp/abi_import.db

Dry run against the LIVE Turso database (reads only, safe):
    .venv/bin/python scripts/import_booqable_customers.py \
        --csv ~/abi-backups/booqable-import/customers-export-2026-09-12.csv --live

The real import:
    .venv/bin/python scripts/import_booqable_customers.py \
        --csv ~/abi-backups/booqable-import/customers-export-2026-09-12.csv --live --commit

Why it is safe to re-run: rows are keyed on (source_system, source_id) with a
partial unique index, so a second run skips everything already imported.
"""
import argparse
import collections
import csv
import datetime
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.booqable_import import (          # noqa: E402
    SOURCE_SYSTEM, map_customer_row, read_export, validate,
)

INSERT_COLUMNS = [
    "customer_type", "name", "email", "phone", "marketing_opt_in",
    "address_line1", "address_line2", "suburb", "city", "province", "postal_code",
    "country", "custom_fields_json", "balance_due", "standard_discount_percent",
    "created_at", "source_system", "source_id",
]
BATCH = 250


# --------------------------------------------------------------------------- #
# connection
# --------------------------------------------------------------------------- #

class _Wrap:
    """Thin shim so sqlite3.Row and the Turso adapter behave the same."""

    def __init__(self, conn, kind, describe):
        self.conn = conn
        self.kind = kind
        self.describe = describe

    def query(self, sql, params=()):
        if self.kind == "turso":
            result = self.conn.execute(sql, params)
            return result.fetchall()
        cur = self.conn.execute(sql, params)
        return cur.fetchall()

    def exec(self, sql, params=()):
        if self.kind == "turso":
            return self.conn.execute(sql, params)
        cur = self.conn.execute(sql, params)
        try:
            self.conn.commit()
        except Exception:
            pass
        return cur

    def table_columns(self, table):
        return {row["name"] for row in self.query(f"PRAGMA table_info({table})")}

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def finish(code=0):
    """Exit without waiting on the libsql client's background session thread."""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(code)


def connect(args):
    if args.database:
        conn = sqlite3.connect(args.database)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return _Wrap(conn, "sqlite", "local sqlite:%s" % args.database)

    url = os.environ.get("TURSO_DATABASE_URL", "")
    token = os.environ.get("TURSO_AUTH_TOKEN", "")
    if not url and args.live:
        cred = (ROOT / "turso-access.txt")
        if not cred.exists():
            sys.exit("turso-access.txt not found and TURSO_DATABASE_URL is unset")
        text = cred.read_text()
        import re
        url_match = re.search(r"(libsql://\S+)", text)
        token_match = re.search(r"(eyJ[\w.\-]+)", text)
        url = url_match.group(1) if url_match else ""
        token = token_match.group(1) if token_match else ""
    if not url:
        default = os.path.join(ROOT, "instance", "abi_rental.db")
        conn = sqlite3.connect(default)
        conn.row_factory = sqlite3.Row
        return _Wrap(conn, "sqlite", "local sqlite:%s" % default)

    from app.turso_db import connect_turso
    label = "LIVE Turso" if args.live else "Turso"
    return _Wrap(connect_turso(url, token), "turso", label)


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #

def ensure_source_columns(db, log):
    """Additive only - mirrors app.db.run_migrations()."""
    for table in ("customers", "orders", "products"):
        existing = db.table_columns(table)
        if not existing:
            continue
        for column in ("source_system", "source_id"):
            if column not in existing:
                db.exec(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
                log(f"  + {table}.{column}")
        db.exec(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_source "
            f"ON {table}(source_system, source_id) WHERE source_id <> ''"
        )


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #

def print_report(rows, mapped, invalid, existing, limit, db_label, commit, report_csv):
    total = len(mapped)
    print("=" * 74)
    print("BOOQABLE CUSTOMER IMPORT — %s" % ("COMMIT" if commit else "DRY RUN (nothing written)"))
    print("=" * 74)
    print(f"database        : {db_label}")
    print(f"csv rows        : {len(rows)}")
    if limit:
        print(f"limit applied   : first {limit} rows")
    print(f"valid rows      : {total - len(invalid)}")
    print(f"invalid rows    : {len(invalid)}")
    print(f"already present : {len(existing)}")
    print(f"WOULD INSERT    : {total - len(invalid) - len(existing)}")
    if invalid:
        print("\ninvalid rows:")
        for name, problems in invalid[:20]:
            print(f"  - {name!r}: {', '.join(problems)}")

    types = collections.Counter(r["customer_type"] for r in mapped)
    conf = collections.Counter(r["_address"]["confidence"] for r in mapped)
    coverage = {k: sum(1 for r in mapped if r[k]) for k in ("city", "suburb", "province", "postal_code")}
    print("\nfield coverage (of %d):" % total)
    for key, count in coverage.items():
        print(f"  {key:<12} {count:>5}  ({count / total * 100:.0f}%)")
    print(f"  address_line1 non-empty: {sum(1 for r in mapped if r['address_line1'])}")
    print("\naddress confidence: " + ", ".join(f"{k}={v}" for k, v in conf.most_common()))
    print("customer_type     : " + ", ".join(f"{k}={v}" for k, v in types.most_common()))
    print(f"non-zero discount : {sum(1 for r in mapped if r['standard_discount_percent'])}")
    print(f"marketing opt-in  : {sum(1 for r in mapped if r['marketing_opt_in'])}")
    print(f"companies (inferred): {types.get('company', 0)}")

    emails = collections.Counter(r["email"].lower() for r in mapped if r["email"])
    phones = collections.Counter(r["phone"] for r in mapped if r["phone"])
    dup_e = [(v, c) for v, c in emails.items() if c > 1]
    dup_p = [(v, c) for v, c in phones.items() if c > 1]
    print(f"\nduplicate email values: {len(dup_e)}   duplicate phone values: {len(dup_p)}")
    junk = [r["name"] for r in mapped if len(r["name"]) <= 2]
    print(f"junk-looking names   : {len(junk)} {junk[:10]}")

    print("\nsample of the first 5 mapped rows:")
    for r in mapped[:5]:
        print(f"  {r['name']!r} [{r['customer_type']}] {r['email']!r}")
        print(f"     L1={r['address_line1']!r} sub={r['suburb']!r} city={r['city']!r} "
              f"prov={r['province']!r} postal={r['postal_code']!r}")

    if report_csv:
        with open(report_csv, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["booqable_number", "name", "customer_type", "address_line1",
                             "suburb", "city", "province", "postal_code", "confidence"])
            for r in mapped:
                writer.writerow([r["_custom"].get("booqable_number", ""), r["name"],
                                 r["customer_type"], r["address_line1"], r["suburb"],
                                 r["city"], r["province"], r["postal_code"],
                                 r["_address"]["confidence"]])
        print(f"\nreview csv written : {report_csv}")


# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description="Import Booqable customers.")
    parser.add_argument("--csv", required=True)
    parser.add_argument("--commit", action="store_true", help="actually write rows")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--database", help="local sqlite file to use")
    parser.add_argument("--live", action="store_true", help="use live Turso credentials")
    parser.add_argument("--report-csv", help="write a per-row review csv")
    parser.add_argument("--map-out", help="write the source_id -> local id map here")
    args = parser.parse_args()

    def log(message):
        print(message, flush=True)

    rows = read_export(args.csv)
    if args.limit:
        rows = rows[:args.limit]

    mapped, invalid = [], []
    for index, row in enumerate(rows, start=1):
        payload = map_customer_row(row)
        problems = validate(payload)
        if problems:
            invalid.append((payload["name"] or row.get("id"), problems))
        else:
            payload["_row_number"] = index
            mapped.append(payload)

    db = connect(args)
    label = db.describe
    existing = set()
    customer_columns = db.table_columns("customers")
    if "source_id" in customer_columns:
        for record in db.query(
                "SELECT source_id FROM customers WHERE source_system = ?", (SOURCE_SYSTEM,)):
            existing.add(record["source_id"])
        source_columns_ready = True
    else:
        source_columns_ready = False
        log("note: source columns not present on this database yet "
            "(they are created automatically during --commit).")
    pending = [r for r in mapped if r["source_id"] not in existing]

    print_report(rows, mapped, invalid, existing, args.limit, label, args.commit,
                 args.report_csv)
    if source_columns_ready:
        already = db.query("SELECT COUNT(*) AS c FROM customers")[0]["c"]
        log(f"rows in customers table now: {already} "
            f"(unrelated rows are never touched)")

    if not args.commit:
        print("\nDRY RUN — no rows written. Re-run with --commit to import.")
        db.close()
        finish(0)

    log("\nensuring additive source columns …")
    ensure_source_columns(db, log)

    inserted = 0
    single = "(" + ", ".join("?" for _ in INSERT_COLUMNS) + ")"
    columns = ", ".join(INSERT_COLUMNS)
    # Multi-row statements keep the number of live HTTP round-trips down
    # (18 columns x 40 rows = 720 bound values, safely under SQLite's limit).
    chunk_size = 40
    for start in range(0, len(pending), chunk_size):
        chunk = pending[start:start + chunk_size]
        values, params = [], []
        for payload in chunk:
            payload = dict(payload)
            payload["balance_due"] = 0      # owner decision D3: no opening balances
            values.append(single)
            params.extend(payload[c] for c in INSERT_COLUMNS)
        db.exec(f"INSERT OR IGNORE INTO customers ({columns}) VALUES {', '.join(values)}",
                tuple(params))
        inserted += len(chunk)
        if inserted % BATCH == 0 or inserted == len(pending):
            log(f"  inserted {inserted}/{len(pending)} …")
    log(f"  inserted {inserted} row(s)")

    mapping = {}
    for record in db.query(
            "SELECT id, source_id FROM customers WHERE source_system = ?", (SOURCE_SYSTEM,)):
        mapping[record["source_id"]] = record["id"]
    map_path = args.map_out or os.path.expanduser(
        "~/abi-backups/booqable-import/booqable-customer-map.json")
    os.makedirs(os.path.dirname(map_path), exist_ok=True)
    with open(map_path, "w") as handle:
        json.dump({"taken_at": datetime.datetime.utcnow().isoformat(),
                   "source_system": SOURCE_SYSTEM,
                   "count": len(mapping),
                   "map": mapping}, handle, indent=2)
    log(f"mapping written: {map_path} ({len(mapping)} entries)")

    total = db.query("SELECT COUNT(*) AS c FROM customers")[0]["c"]
    log(f"\ncustomers in database now: {total}")
    db.close()
    finish(0)


if __name__ == "__main__":
    main()
