"""One-time seed: restore the original Sano sale-trailer catalogue into ABI.

The source is the client's own pre-ABI static store (``E:/Hermes/sano-store``):
``assets/js/data.js`` transcribes the 2026 sales price list, and the sixteen
``assets/img/sale-*.jpg`` files are the cropped photos made from the client's
spec-sheet images (photo band only, no stale price table). This module imports
them through ``data/sale-trailers/manifest.json`` — nothing is retyped here.

Import rules, in order of importance
------------------------------------
1. **Create once.** A product this importer created is bound to its manifest
   ``source_key`` in ``shop_listing_seeds``. A rerun sees the seed row and
   touches nothing: staff edits to photos, copy, price and publish state are
   theirs, and the original source is a seed, not a runtime catalogue.
2. **One catalogue.** Rows are ordinary ``products`` with ``product_type='sale'``
   in ordinary, store-visible categories. The Online store editor stays the
   authority for their photos, descriptions and selling price; the public card,
   detail page and purchase enquiry all read those saved DB fields.
3. **No invented stock.** These trailers are built to order (7–10 working days).
   They are created with ``tracking_method='none'``, ``quantity=0`` and no branch
   assignment — no on-hand units are claimed for a branch that does not hold
   them, and a purchase enquiry is accepted from any active branch.
4. **Never overwrite rentals.** If a manifest SKU already belongs to a rental or
   service product, that item is refused and reported; nothing about the existing
   row is written. Sale rows with the same code but a different model (the source
   price list genuinely reuses ``ST3.0-900UBF``) are reported and allowed, since
   the product name carries the model and size.
5. **Nothing else moves.** No customer, order, reservation, invoice, payment,
   stock count or notification is created or changed.

Usage (dry run is the default — it writes nothing but prints the plan)::

    py -3 scripts/import_sale_trailers.py --db instance/abi_rental.db
    py -3 scripts/import_sale_trailers.py --db instance/abi_rental.db --apply
    py -3 scripts/import_sale_trailers.py --remote --apply   # Turso env creds

``--remote`` uses ``TURSO_DATABASE_URL`` / ``TURSO_AUTH_TOKEN`` from the
environment and refuses to run unless both are set AND the target is printed.
Booting the app applies its normal additive schema, exactly as a deploy does.
"""

import argparse
import hashlib
import io
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DEFAULT_MANIFEST = os.path.join(REPO_ROOT, "data", "sale-trailers", "manifest.json")
DEFAULT_IMAGES = os.path.join(REPO_ROOT, "static", "img", "sale-trailers")
DEFAULT_DB = os.path.join(REPO_ROOT, "instance", "abi_rental.db")

#: The catalogue is made to order; there is no on-hand count to claim.
MADE_TO_ORDER_QUANTITY = 0
MADE_TO_ORDER_TRACKING = "none"
SALE_PRICE_UNIT = "fixed"


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


def load_manifest(path=DEFAULT_MANIFEST):
    """Read and validate the committed seed manifest."""
    with open(path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    if manifest.get("schema") != 1:
        raise ValueError("Unsupported manifest schema: %r" % manifest.get("schema"))
    items = manifest.get("items") or []
    if not items:
        raise ValueError("Manifest has no items")
    keys, names = set(), set()
    for item in items:
        for field in ("source_key", "family", "model", "code", "size", "price", "name", "image"):
            if item.get(field) in (None, ""):
                raise ValueError("Manifest item missing %s: %r" % (field, item))
        if item["source_key"] in keys:
            raise ValueError("Duplicate source_key: %s" % item["source_key"])
        if item["name"] in names:
            raise ValueError("Duplicate product name: %s" % item["name"])
        keys.add(item["source_key"])
        names.add(item["name"])
        if float(item["price"]) <= 0:
            raise ValueError("Refusing a non-positive selling price: %s" % item["source_key"])
    return manifest


def image_bytes(images_dir, filename):
    path = os.path.join(images_dir, filename)
    with open(path, "rb") as fh:
        return fh.read()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# lookups
# ---------------------------------------------------------------------------


def _rows(db, sql, params=()):
    from app.services.shop_catalogue import row_dict

    return [row_dict(row) or {} for row in db.execute(sql, params).fetchall()]


def _category_name(family):
    return "%s for sale" % family


def find_category(db, family):
    rows = _rows(
        db,
        "SELECT id, name, active, becomes_store_visible FROM product_groups "
        "WHERE LOWER(name) = LOWER(?)",
        (_category_name(family),),
    )
    return rows[0] if rows else None


def create_category(db, family, sort_order):
    from app.db import now

    stamp = now()
    cur = db.execute(
        "INSERT INTO product_groups (name, description, active, sort_order, "
        "becomes_store_visible, created_at, updated_at) VALUES (?, '', 1, ?, 1, ?, ?)",
        (_category_name(family), int(sort_order), stamp, stamp),
    )
    db.commit()
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# one item
# ---------------------------------------------------------------------------


def _classify(db, item, seed_module):
    """Return ``(status, product_id, reason)`` for one manifest item.

    Statuses: ``existing`` (already seeded — never touched), ``adopted`` (an
    identical sale row exists without a seed; bind it and write nothing else),
    ``conflict`` (a rental/service or a different sale row owns the SKU/name),
    ``create`` (nothing exists yet).
    """
    seeded = seed_module.seed_product_id(item["source_key"])
    if seeded is not None:
        return "existing", seeded, ""

    same_name = _rows(
        db,
        "SELECT id, product_type, sku, public_visible FROM products WHERE name = ?",
        (item["name"],),
    )
    same_sku = _rows(
        db,
        "SELECT id, product_type, name, sku FROM products WHERE sku = ?",
        (item["code"],),
    )

    for row in same_sku:
        if (row.get("product_type") or "") != "sale":
            return (
                "conflict",
                int(row["id"]),
                "SKU %s already belongs to a %s product (%s)"
                % (item["code"], row.get("product_type"), row.get("name")),
            )

    for row in same_name:
        if (row.get("product_type") or "") != "sale":
            return (
                "conflict",
                int(row["id"]),
                "Name already belongs to a %s product" % row.get("product_type"),
            )
        if (row.get("sku") or "") != item["code"]:
            return (
                "conflict",
                int(row["id"]),
                "A sale product with this name uses SKU %r" % row.get("sku"),
            )
        if seed_module.seed_source_key(int(row["id"])):
            return (
                "conflict",
                int(row["id"]),
                "Name is already bound to another seed key",
            )
        return "adopted", int(row["id"]), ""

    return "create", None, ""


def create_product_row(db, item, category_id):
    """Insert one sale product. Only the columns this catalogue needs are set."""
    from app.db import now
    from app.services.settings import global_tax_profile_id

    cur = db.execute(
        "INSERT INTO products (name, product_type, tracking_method, wheel_size, "
        "under_maintenance, registration, licence_number, registration_number, "
        "description, sku, active, public_visible, price_amount, price_unit, "
        "security_deposit, hourly_extra_rate, tax_profile_id, product_group_id, "
        "quantity, branch_id, created_at) "
        "VALUES (?, 'sale', ?, '', 0, '', '', '', ?, ?, 1, 1, ?, ?, 0, 0, ?, ?, ?, NULL, ?)",
        (
            item["name"],
            MADE_TO_ORDER_TRACKING,
            item["description"],
            item["code"],
            float(item["price"]),
            SALE_PRICE_UNIT,
            global_tax_profile_id(),
            category_id,
            MADE_TO_ORDER_QUANTITY,
            now(),
        ),
    )
    db.commit()
    return int(cur.lastrowid)


def apply_listing(product_id, item, images_dir=DEFAULT_IMAGES):
    """Shop copy, gallery and publish switch — through the existing services."""
    from werkzeug.datastructures import FileStorage

    from app.services import shop_catalogue

    shop_catalogue.save_listing(
        product_id,
        {
            "store_title": item["name"],
            "store_description": item["description"],
            "model_key": "",
            "store_sort_order": str(item.get("sort") or 0),
            "public_visible": "1",
        },
    )
    existing = shop_catalogue.product_images(product_id)
    if not existing:
        data = image_bytes(images_dir, item["image"])
        shop_catalogue.add_image(
            product_id,
            FileStorage(
                stream=io.BytesIO(data),
                filename=item["image"],
                content_type="image/jpeg",
            ),
            alt_text=item["name"],
        )
    return len(shop_catalogue.product_images(product_id))


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


def run_import(app, manifest, images_dir=DEFAULT_IMAGES, apply=False):
    """Plan (or apply) the seed. Returns a report dict; ``apply=False`` writes nothing."""
    from app.db import get_db
    from app.services import shop_catalogue

    report = {
        "applied": bool(apply),
        "items": [],
        "counts": {},
        "categories": {},
        "warnings": [],
        "errors": [],
    }
    db = get_db()

    if apply:
        shop_catalogue.init_schema(db)
    elif not _table_exists(db, "shop_listing_seeds"):
        report["warnings"].append(
            "shop_listing_seeds is missing — boot the app (or apply) before trusting this dry run."
        )

    category_ids = {}
    families = []
    for item in manifest["items"]:
        if item["family"] not in families:
            families.append(item["family"])
    for index, family in enumerate(families):
        row = find_category(db, family)
        if row is None:
            if apply:
                category_ids[family] = create_category(db, family, 100 + index)
                report["categories"][family] = {"status": "created", "id": category_ids[family]}
            else:
                category_ids[family] = None
                report["categories"][family] = {"status": "would-create"}
            continue
        category_ids[family] = int(row["id"])
        report["categories"][family] = {"status": "existing", "id": int(row["id"])}
        if not int(row.get("active") or 0) or not int(row.get("becomes_store_visible") or 0):
            report["warnings"].append(
                "Category %r exists but is hidden or switched off — its listings stay off the shop."
                % _category_name(family)
            )

    for item in manifest["items"]:
        status, product_id, reason = _classify(db, item, shop_catalogue)
        entry = {
            "source_key": item["source_key"],
            "code": item["code"],
            "name": item["name"],
            "price": float(item["price"]),
            "status": status,
            "product_id": product_id,
        }
        if reason:
            entry["reason"] = reason
            report["warnings" if status == "conflict" else "errors"].append(
                "%s: %s" % (item["source_key"], reason)
            )
        if status in ("create", "adopted") and apply:
            if status == "adopted":
                shop_catalogue.record_seed(
                    product_id, item["source_key"], source_ref=_source_ref(manifest)
                )
            else:
                category_id = category_ids.get(item["family"])
                if category_id is None:
                    entry["status"] = "conflict"
                    entry["reason"] = "Category could not be resolved"
                    report["errors"].append("%s: category missing" % item["source_key"])
                    report["items"].append(entry)
                    continue
                # An interruption must not leave a product without its photo
                # and provenance: reruns must never adopt a half-seeded item.
                from app.db import write_transaction
                with write_transaction():
                    product_id = create_product_row(get_db(), item, category_id)
                    entry["product_id"] = product_id
                    apply_listing(product_id, item, images_dir=images_dir)
                    shop_catalogue.record_seed(
                        product_id, item["source_key"], source_ref=_source_ref(manifest)
                    )
            entry["status"] = "created" if status == "create" else "adopted"
        report["items"].append(entry)
        report["counts"][entry["status"]] = report["counts"].get(entry["status"], 0) + 1

    return report


def _source_ref(manifest):
    source = manifest.get("source") or {}
    return "sano-store/assets/js/data.js@%s" % (source.get("data_sha256") or "")[:12]


def _table_exists(db, name):
    try:
        row = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (name,)
        ).fetchone()
    except Exception:
        return True  # libSQL: assume the boot migrations created it
    return row is not None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_app(args):
    from app import create_app

    config = {
        "SECRET_KEY": "sale-trailer-import",
        "ADMIN_EMAIL": "import@abi.local",
        "ADMIN_PASSWORD": "import-only-not-a-login",
        "TELEGRAM_NOTIFICATIONS_ENABLED": "",
        "VAPID_PUBLIC_KEY": "",
        "VAPID_PRIVATE_KEY": "",
        "PUBLIC_BASE_URL": "",
        "ENV": "development",
    }
    if args.remote:
        url = os.environ.get("TURSO_DATABASE_URL", "").strip()
        token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
        if not url or not token:
            raise SystemExit("--remote needs TURSO_DATABASE_URL and TURSO_AUTH_TOKEN in the environment.")
        config["TURSO_DATABASE_URL"] = url
        config["TURSO_AUTH_TOKEN"] = token
        print("Target: Turso %s" % url.split("//")[-1].split(".")[0], file=sys.stderr)
    else:
        config["DATABASE"] = os.path.abspath(args.db)
        config["TURSO_DATABASE_URL"] = ""
        config["TURSO_AUTH_TOKEN"] = ""
        print("Target: SQLite %s" % config["DATABASE"], file=sys.stderr)
    return create_app(config)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Seed the original Sano sale-trailer catalogue.")
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--images", default=DEFAULT_IMAGES)
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path (ignored with --remote)")
    parser.add_argument("--remote", action="store_true", help="Use TURSO_DATABASE_URL/TOKEN from the environment")
    parser.add_argument("--apply", action="store_true", help="Write. Without it the run is a dry run.")
    parser.add_argument("--report", default="", help="Optional path to write the JSON report")
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    for filename in manifest.get("images", {}):
        path = os.path.join(args.images, filename)
        if not os.path.isfile(path):
            raise SystemExit("Missing committed photo: %s" % path)

    app = build_app(args)
    with app.app_context():
        if not args.apply:
            print("DRY RUN — no product, listing, photo or price will be written.", file=sys.stderr)
        report = run_import(app, manifest, images_dir=args.images, apply=args.apply)

    report["manifest"] = {
        "path": args.manifest,
        "items": len(manifest["items"]),
        "images": len(manifest.get("images") or {}),
        "source_sha256": (manifest.get("source") or {}).get("data_sha256", ""),
    }
    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if args.report:
        with open(args.report, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
    if report["errors"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
