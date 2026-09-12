#!/usr/bin/env python3
"""One-off: import the Booqable product/inventory master list into ABI Rental.

Defaults to a READ-ONLY dry run. Nothing is written unless you pass --commit.

Usage
-----
Local rehearsal:
    .venv/bin/python scripts/import_booqable_products.py \
        --csv ~/abi-backups/booqable-import/products-export-2026-09-12.csv \
        --database /tmp/abi_import.db

Dry run against LIVE Turso (read-only):
    .venv/bin/python scripts/import_booqable_products.py \
        --csv ~/abi-backups/booqable-import/products-export-2026-09-12.csv --live

Real import:
    .venv/bin/python scripts/import_booqable_products.py \
        --csv ~/abi-backups/booqable-import/products-export-2026-09-12.csv --live --commit

Notes
-----
* Rentals tracked individually in Booqable become ONE product row per physical
  trailer, SKU = registration plate (the convention the client already uses).
* Branch is deliberately left NULL (owner decision 2026-09-12) - unassigned
  products are bookable from every branch and can be set later.
* Re-runnable: keyed on (source_system, source_id) with a partial unique index.
"""
import argparse
import collections
import csv
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from booqable_db import connect, ensure_source_columns, finish, ROOT  # noqa: E402

sys.path.insert(0, str(ROOT))
from app.services.booqable_import import (  # noqa: E402
    SOURCE_SYSTEM, map_product_row, parse_stock_identifier, read_export, validate_product,
)

INSERT_COLUMNS = [
    "name", "product_type", "tracking_method", "description", "sku", "active",
    "public_visible", "price_amount", "price_unit", "security_deposit",
    "hourly_extra_rate", "tax_profile_id", "product_group_id", "quantity",
    "branch_id", "created_at", "source_system", "source_id",
]
CHUNK = 40


def default_tax_profile_id(db):
    try:
        row = db.query("SELECT id FROM tax_profiles WHERE is_default = 1 ORDER BY id LIMIT 1")
        if row:
            return row[0]["id"]
        row = db.query("SELECT id FROM tax_profiles ORDER BY id LIMIT 1")
        return row[0]["id"] if row else None
    except Exception:
        return None


def replace_existing(db, log, backup=True):
    """Remove the pre-import products after protecting order history.

    ORDER MATTERS:
      1. snapshot the affected rows (a rollback is impossible on libsql - it autocommits)
      2. re-point order lines at the matching imported product (by fleet/plate, then SKU)
      3. backfill order_items.custom_name for any line that would be orphaned, because the
         order/document templates render `product_name or custom_name` and catalogue lines
         store an empty custom_name - a bare delete would blank live invoice descriptions
      4. only then delete the old products
    """
    old = db.query("SELECT id, name, sku FROM products WHERE COALESCE(source_system,'') = ''")
    if not old:
        log("  no pre-import products to remove")
        return {"removed": 0, "remapped": 0, "backfilled": 0}

    new_rows = db.query(
        "SELECT id, sku, name FROM products WHERE source_system = ?", (SOURCE_SYSTEM,))
    by_unit, by_sku = {}, {}
    for row in new_rows:
        unit = parse_stock_identifier(row["sku"])
        if unit:
            by_unit[(unit[0], unit[1])] = row["id"]
        if row["sku"]:
            by_sku[row["sku"].strip()] = row["id"]

    if backup:
        stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        path = os.path.expanduser(f"~/abi-backups/products-replace-{stamp}.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # libsql Row objects are not dict()-convertible, so project by column name
        product_cols = db.table_columns("products")
        item_cols = db.table_columns("order_items")
        dump = {
            "taken_at": stamp,
            "products": [{c: row[c] for c in product_cols}
                         for row in db.query("SELECT * FROM products")],
            "order_items": [{c: row[c] for c in item_cols}
                            for row in db.query("SELECT * FROM order_items")],
        }
        with open(path, "w") as handle:
            json.dump(dump, handle, indent=2, default=str)
        log(f"  snapshot written: {path}")

    remapped = backfilled = 0
    for row in old:
        target = None
        unit = parse_stock_identifier(row["sku"])
        if unit:
            target = by_unit.get((unit[0], unit[1]))
        if target is None and (row["sku"] or "").strip():
            target = by_sku.get(row["sku"].strip())
        lines = db.query(
            "SELECT id, custom_name FROM order_items WHERE product_id = ?", (row["id"],))
        for line in lines:
            if target:
                db.exec("UPDATE order_items SET product_id = ? WHERE id = ?", (target, line["id"]))
                remapped += 1
            elif not (line["custom_name"] or "").strip():
                # keep the description on the order/invoice after the product goes away
                db.exec("UPDATE order_items SET custom_name = ? WHERE id = ?", (row["name"], line["id"]))
                backfilled += 1
        db.exec("DELETE FROM products WHERE id = ?", (row["id"],))

    return {"removed": len(old), "remapped": remapped, "backfilled": backfilled}


def main():
    parser = argparse.ArgumentParser(description="Import Booqable products.")
    parser.add_argument("--csv", required=True)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--database")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--map-out")
    parser.add_argument("--replace-old", action="store_true",
                        help="after importing, remove the pre-import products (protects order links)")
    args = parser.parse_args()

    rows = read_export(args.csv)
    if args.limit:
        rows = rows[:args.limit]

    db = connect(args.database, args.live)
    tax_profile_id = default_tax_profile_id(db)
    created_at = datetime.datetime.now().replace(microsecond=0).isoformat()

    mapped, invalid = [], []
    for row in rows:
        payload = map_product_row(row, tax_profile_id=tax_profile_id)
        problems = validate_product(payload)
        if problems:
            invalid.append((payload["name"] or row.get("id"), problems))
        else:
            mapped.append(payload)

    existing = set()
    if "source_id" in db.table_columns("products"):
        for record in db.query(
                "SELECT source_id FROM products WHERE source_system = ?", (SOURCE_SYSTEM,)):
            existing.add(record["source_id"])
        live_rows = db.query("SELECT id, name, sku FROM products WHERE COALESCE(source_id,'') = ''")
    else:
        live_rows = db.query("SELECT id, name, sku FROM products") if db.table_columns("products") else []
    pending = [p for p in mapped if p["source_id"] not in existing]

    kinds = collections.Counter(p["product_type"] for p in mapped)
    tracking = collections.Counter(p["tracking_method"] for p in mapped)
    trailers = [p for p in mapped if p["tracking_method"] == "individual"]
    plates = collections.Counter(p["sku"] for p in trailers)
    dup_plates = {k: v for k, v in plates.items() if v > 1}
    live_skus = {r["sku"].strip() for r in live_rows if (r["sku"] or "").strip()}
    collisions = sorted({p["sku"] for p in mapped if p["sku"] and p["sku"].strip() in live_skus})

    print("=" * 74)
    print("BOOQABLE PRODUCT IMPORT — %s" % ("COMMIT" if args.commit else "DRY RUN (nothing written)"))
    print("=" * 74)
    print(f"database        : {db.describe}")
    print(f"csv rows        : {len(rows)}")
    print(f"valid products  : {len(mapped) - len(invalid)}")
    print(f"invalid         : {len(invalid)}")
    print(f"already present : {len(existing)}")
    print(f"WOULD INSERT    : {len(pending)}")
    print(f"tax profile id  : {tax_profile_id}")
    if invalid:
        for name, problems in invalid[:20]:
            print(f"   invalid {name!r}: {', '.join(problems)}")
    print()
    print("by type     : " + ", ".join(f"{k}={v}" for k, v in kinds.most_common()))
    print("tracking    : " + ", ".join(f"{k}={v}" for k, v in tracking.most_common()))
    print(f"trailers (individual units) : {len(trailers)}")
    print(f"distinct plates             : {len(plates)}")
    print(f"duplicate plates            : {len(dup_plates)} {list(dup_plates)[:5]}")
    print(f"zero-quantity bulk items    : {sum(1 for p in mapped if p['tracking_method']=='bulk' and p['quantity']==0)}")
    print(f"negative quantities clamped : {sum(1 for p in mapped if p['tracking_method']=='bulk' and int(p.get('_raw_qty') or 0) < 0)}")
    print(f"price range                 : R{min(p['price_amount'] for p in mapped):.2f} – R{max(p['price_amount'] for p in mapped):.2f}")
    print(f"with a security deposit     : {sum(1 for p in mapped if p['security_deposit'])}")
    print(f"branch set                  : {sum(1 for p in mapped if p['branch_id'])} (all blank by decision)")
    print()
    print(f"existing live products (no source_id): {len(live_rows)}")
    print(f"SKU collisions with live rows        : {len(collisions)} {collisions[:6]}")
    print()
    print("first 5 mapped rows:")
    for p in mapped[:5]:
        print(f"  {p['name'][:44]:44} {p['product_type']:6} {p['tracking_method']:10} sku={p['sku'][:24]:24} R{p['price_amount']}")

    if not args.commit:
        print("\nDRY RUN — no rows written. Re-run with --commit to import.")
        db.close()
        finish(0)

    print("\nensuring additive source columns …")
    ensure_source_columns(db, print)

    single = "(" + ", ".join("?" for _ in INSERT_COLUMNS) + ")"
    columns = ", ".join(INSERT_COLUMNS)
    inserted = 0
    for start in range(0, len(pending), CHUNK):
        chunk = pending[start:start + CHUNK]
        values, params = [], []
        for payload in chunk:
            payload = dict(payload)
            payload["created_at"] = created_at
            values.append(single)
            params.extend(payload[c] for c in INSERT_COLUMNS)
        db.exec(f"INSERT OR IGNORE INTO products ({columns}) VALUES {', '.join(values)}",
                tuple(params))
        inserted += len(chunk)
        if inserted % 100 == 0 or inserted == len(pending):
            print(f"  inserted {inserted}/{len(pending)} …")
    print(f"  inserted {inserted} row(s)")

    mapping = {}
    for record in db.query(
            "SELECT id, source_id FROM products WHERE source_system = ?", (SOURCE_SYSTEM,)):
        mapping[record["source_id"]] = record["id"]
    map_path = args.map_out or os.path.expanduser(
        "~/abi-backups/booqable-import/booqable-product-map.json")
    os.makedirs(os.path.dirname(map_path), exist_ok=True)
    with open(map_path, "w") as handle:
        json.dump({"taken_at": datetime.datetime.utcnow().isoformat(),
                   "count": len(mapping), "map": mapping}, handle, indent=2)
    print(f"mapping written: {map_path} ({len(mapping)} entries)")

    if args.replace_old:
        print("\nreplacing pre-import inventory …")
        result = replace_existing(db, print)
        print(f"  removed {result['removed']} old product(s); "
              f"re-pointed {result['remapped']} order line(s); "
              f"backfilled {result['backfilled']} name(s)")

    total = db.query("SELECT COUNT(*) AS c FROM products")[0]["c"]
    print(f"\nproducts in database now: {total}")
    db.close()
    finish(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:            # never hang the libsql session on a crash
        import traceback
        traceback.print_exc()
        finish(1)
