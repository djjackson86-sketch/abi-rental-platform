"""One-time restoration of the original Sano sale-trailer catalogue.

These tests exercise the real importer (``scripts/import_sale_trailers.py``),
the real SQLite database created by ``create_app`` and the real HTTP routes for
the Online store editor and the public purchase enquiry. Nothing is mocked and
nothing is retyped: every product's name, SKU, price and photo comes from
``data/sale-trailers/manifest.json``, and every write is read back.

The contract under test:

* a dry run writes nothing; the first apply creates 19 model groups / 52 sale
  variants / 16 committed photos as ordinary editable inventory rows;
* a rerun sees its seed rows and skips every item, preserving product ids and
  anything staff changed (shop copy, gallery, selling price, publish state);
* a rental that already owns a manifest SKU is refused and left byte-identical;
* no customer, order, payment, document or stock count moves;
* a made-to-order item (``tracking_method='none'``, ``quantity=0``, no branch)
  still accepts a public purchase enquiry, and the enquiry writes only itself;
* staff see the list labelled "Online enquiries", not "Store enquiries".
"""

import collections
import io
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "scripts")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import import_sale_trailers as importer  # noqa: E402

from app import create_app  # noqa: E402
from app.db import get_db, now  # noqa: E402
from app.routes.shop_admin import CSRF_SCOPE as SHOP_CSRF_SCOPE  # noqa: E402
from app.services import branches as branches_service  # noqa: E402
from app.services import products as products_service  # noqa: E402
from app.services import shop_catalogue  # noqa: E402
from app.services.reports import row_dict  # noqa: E402

PHOTO_DIR = ROOT / "static" / "img" / "sale-trailers"
FAMILIES = {
    "Utility trailers",
    "Livestock trailers",
    "Flatbed trailers",
    "Special purpose trailers",
    "Car trailers",
}
UNTOUCHED_TABLES = (
    "customers",
    "orders",
    "order_items",
    "payments",
    "documents",
    "product_branch_stock",
    "shop_enquiries",
    "shop_enquiry_items",
    "customer_credits",
    "cash_ups",
)


# ---------------------------------------------------------------------------
# app / fixtures
# ---------------------------------------------------------------------------


def build_app(database_path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": str(database_path),
            "SECRET_KEY": "test",
            "ADMIN_EMAIL": "admin@abi.local",
            "ADMIN_PASSWORD": "admin123",
            "TURSO_DATABASE_URL": "",
            "TURSO_AUTH_TOKEN": "",
            "TELEGRAM_NOTIFICATIONS_ENABLED": "",
            "VAPID_PUBLIC_KEY": "",
            "VAPID_PRIVATE_KEY": "",
            "PUBLIC_BASE_URL": "https://abi.example.test",
        }
    )


@pytest.fixture()
def app(tmp_path):
    application = build_app(tmp_path / "sale-trailers.sqlite")
    context = application.app_context()
    context.push()
    yield application
    context.pop()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture(scope="session")
def manifest():
    return importer.load_manifest()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def rows(sql, params=()):
    return [row_dict(row) or {} for row in get_db().execute(sql, params).fetchall()]


def scalar(sql, params=()):
    row = get_db().execute(sql, params).fetchone()
    return None if row is None else row[0]


def table_names():
    return {
        row["name"]
        for row in get_db().execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def snapshot(tables):
    existing = table_names()
    data = {}
    for table in tables:
        if table in existing:
            data[table] = rows("SELECT * FROM %s ORDER BY id" % table)
    return data


def add_branch(name="Unrelated Depot"):
    return branches_service.create_branch({"name": name, "active": 1})


def add_product(
    name,
    product_type="rental",
    sku="",
    branch_id=None,
    price=450.0,
    quantity=3,
    group_id=None,
    active=1,
    public_visible=1,
):
    cursor = get_db().execute(
        "INSERT INTO products (name, product_type, description, sku, active, "
        "public_visible, price_amount, price_unit, security_deposit, quantity, "
        "tracking_method, under_maintenance, branch_id, product_group_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'bulk', 0, ?, ?, ?)",
        [
            name,
            product_type,
            "Synthetic row for the import safety tests.",
            sku,
            active,
            public_visible,
            price,
            "day" if product_type == "rental" else "fixed",
            quantity,
            branch_id,
            group_id,
            now(),
        ],
    )
    get_db().commit()
    return int(cursor.lastrowid)


def add_group(name, active=1, store_visible=1):
    cursor = get_db().execute(
        "INSERT INTO product_groups (name, description, active, sort_order, "
        "becomes_store_visible, created_at, updated_at) VALUES (?, '', ?, 0, ?, ?, ?)",
        [name, active, store_visible, now(), now()],
    )
    get_db().commit()
    return int(cursor.lastrowid)


def sign_in_owner(client):
    owner = get_db().execute(
        "SELECT id, name FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
    ).fetchone()
    assert owner is not None, "create_app did not seed an owner account"
    with client.session_transaction() as session:
        session.clear()
        session.update(
            user_id=owner["id"],
            user_role="owner",
            user_name=owner["name"] or "Owner",
            can_view_all_branches=True,
        )
    return client


def plant_csrf(client, scope):
    token = "test-" + scope
    with client.session_transaction() as session:
        tokens = session.get("_csrf_tokens")
        if not isinstance(tokens, dict):
            tokens = {}
        tokens[scope] = token
        session["_csrf_tokens"] = tokens
    return token


def publish_privacy_notice(version="1.1"):
    get_db().execute(
        "INSERT INTO popia_notice_versions "
        "(notice_version, notice_content_hash, notice_text, published_at) "
        "VALUES (?, ?, ?, ?)",
        [version, "hash-" + version, "Privacy notice " + version, now()],
    )
    get_db().commit()
    return version


def jpeg_bytes(width=640, height=480, colour=(120, 30, 40)):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def imported_product_id(source_key):
    product_id = shop_catalogue.seed_product_id(source_key)
    assert product_id is not None, "missing seed for %s" % source_key
    return product_id


def submit_purchase_enquiry(client, branch_id, product_id, quantity=1):
    token = plant_csrf(client, "shop_public")
    form = {
        "csrf_token": token,
        "enquiry_type": "purchase",
        "branch_id": str(branch_id),
        "customer_name": "Lerato Mahlangu",
        "customer_email": "lerato@example.com",
        "customer_phone": "082 555 0177",
        "notes": "Please confirm the build lead time.",
        "privacy_accepted": "1",
        "notice_version": "1.1",
        "items_json": json.dumps([{"product_id": product_id, "quantity": quantity}]),
    }
    return client.post("/store/enquiries/request", data=form)


# ---------------------------------------------------------------------------
# the committed source
# ---------------------------------------------------------------------------


def test_manifest_matches_the_original_price_list(manifest):
    items = manifest["items"]
    source = manifest["source"]

    assert manifest["schema"] == 1
    assert len(items) == 52
    assert len({item["source_key"] for item in items}) == 52
    assert len({item["name"] for item in items}) == 52
    assert len({item["group"] for item in items}) == 19
    assert {item["family"] for item in items} == FAMILIES
    assert source["groups"] == 19
    assert source["variants"] == 52
    assert source["data_sha256"]

    # The source price list genuinely reuses one SKU for two models. The
    # importer must keep both as separate sale rows rather than silently
    # dropping one.
    duplicate_codes = {
        code for code, count in collections.Counter(i["code"] for i in items).items() if count > 1
    }
    assert duplicate_codes == {"ST3.0-900UBF"}
    assert len({i["name"] for i in items if i["code"] == "ST3.0-900UBF"}) == 2

    for item in items:
        assert item["price"] > 0
        assert item["description"].strip()
        assert item["image"] in manifest["images"]

    # All sixteen committed photos exist and are byte-identical to the hashes
    # recorded when they were cropped from the client's spec sheets.
    assert len(manifest["images"]) == 16
    assert {item["image"] for item in items} == set(manifest["images"])
    for filename, meta in manifest["images"].items():
        path = PHOTO_DIR / filename
        assert path.is_file(), "missing committed photo %s" % path
        assert importer.sha256_file(str(path)) == meta["sha256"], filename


# ---------------------------------------------------------------------------
# dry run, first apply, rerun
# ---------------------------------------------------------------------------


def test_dry_run_reports_the_plan_and_writes_nothing(app, manifest):
    before = {
        "products": scalar("SELECT COUNT(*) FROM products"),
        "groups": scalar("SELECT COUNT(*) FROM product_groups"),
        "seeds": scalar("SELECT COUNT(*) FROM shop_listing_seeds"),
        "images": scalar("SELECT COUNT(*) FROM shop_listing_images"),
        "meta": scalar("SELECT COUNT(*) FROM shop_listing_meta"),
    }

    report = importer.run_import(app, manifest, apply=False)

    assert report["applied"] is False
    assert report["errors"] == []
    assert report["counts"] == {"create": 52}
    assert all(entry["status"] == "create" for entry in report["items"])
    assert all(entry["product_id"] is None for entry in report["items"])
    assert set(report["categories"]) == FAMILIES
    assert all(
        entry["status"] == "would-create" for entry in report["categories"].values()
    )

    after = {
        "products": scalar("SELECT COUNT(*) FROM products"),
        "groups": scalar("SELECT COUNT(*) FROM product_groups"),
        "seeds": scalar("SELECT COUNT(*) FROM shop_listing_seeds"),
        "images": scalar("SELECT COUNT(*) FROM shop_listing_images"),
        "meta": scalar("SELECT COUNT(*) FROM shop_listing_meta"),
    }
    assert after == before


def test_first_import_creates_the_catalogue_and_rerun_preserves_ids(app, client, manifest):
    report = importer.run_import(app, manifest, apply=True)

    assert report["applied"] is True
    assert report["errors"] == []
    assert report["counts"] == {"created": 52}
    assert set(report["categories"]) == FAMILIES
    assert all(entry["status"] == "created" for entry in report["categories"].values())
    assert all(entry["status"] == "created" for entry in report["items"])
    assert all(entry["product_id"] for entry in report["items"])

    # --- categories -------------------------------------------------------
    groups = rows("SELECT * FROM product_groups ORDER BY id")
    sale_groups = {g["name"]: g for g in groups if g["name"].endswith(" for sale")}
    assert set(sale_groups) == {family + " for sale" for family in FAMILIES}
    assert all(int(g["active"]) == 1 for g in sale_groups.values())
    assert all(int(g["becomes_store_visible"]) == 1 for g in sale_groups.values())

    # --- products ---------------------------------------------------------
    products = rows("SELECT * FROM products WHERE product_type = 'sale' ORDER BY id")
    assert len(products) == 52
    by_name = {p["name"]: p for p in products}
    assert set(by_name) == {item["name"] for item in manifest["items"]}
    for item in manifest["items"]:
        product = by_name[item["name"]]
        assert product["sku"] == item["code"]
        assert abs(float(product["price_amount"]) - float(item["price"])) < 0.005
        assert product["price_unit"] == "fixed"
        assert product["tracking_method"] == "none"
        assert int(product["quantity"]) == 0
        assert product["branch_id"] is None
        assert int(product["active"]) == 1
        assert int(product["public_visible"]) == 1
        assert int(product["under_maintenance"]) == 0
        assert int(product["security_deposit"]) == 0
        assert product["product_group_id"] == sale_groups[item["family"] + " for sale"]["id"]

    # Two sale variants share ST3.0-900UBF in the source; both must exist.
    dual = [p for p in products if p["sku"] == "ST3.0-900UBF"]
    assert len(dual) == 2
    assert len({p["name"] for p in dual}) == 2

    # --- shop copy, gallery and seed provenance ---------------------------
    meta = {row["product_id"]: row for row in rows("SELECT * FROM shop_listing_meta")}
    assert len(meta) == 52
    images = rows(
        "SELECT id, product_id, image_mime, is_cover, version, "
        "LENGTH(image_blob) AS size, image_filename FROM shop_listing_images ORDER BY id"
    )
    assert len(images) == 52
    per_product = collections.Counter(row["product_id"] for row in images)
    for item in manifest["items"]:
        product = by_name[item["name"]]
        assert meta[product["id"]]["store_title"] == item["name"]
        assert meta[product["id"]]["store_description"] == item["description"]
        assert int(meta[product["id"]]["store_sort_order"]) == int(item.get("sort") or 0)
        assert per_product[product["id"]] == 1
    assert all(row["image_mime"] == "image/jpeg" for row in images)
    assert all(int(row["is_cover"]) == 1 for row in images)
    assert all(int(row["size"]) > 1000 for row in images)
    assert {row["image_filename"] for row in images} == set(manifest["images"])

    seeds = rows("SELECT * FROM shop_listing_seeds ORDER BY product_id")
    assert len(seeds) == 52
    assert {seed["source_key"] for seed in seeds} == {
        item["source_key"] for item in manifest["items"]
    }
    assert {seed["product_id"] for seed in seeds} == {p["id"] for p in products}
    assert all(
        seed["source_ref"].startswith("sano-store/assets/js/data.js@")
        for seed in seeds
    )

    # The storefront really serves them: 52 separate public cards, with the
    # imported copy and price, and the photo bytes readable over HTTP.
    cards = shop_catalogue.list_public_products(product_type="sale")
    assert len(cards) == 52
    assert {card["id"] for card in cards} == {p["id"] for p in products}
    card_by_name = {card["name"]: card for card in cards}
    for item in manifest["items"]:
        card = card_by_name[item["name"]]
        assert card["store_title"] == item["name"]
        assert abs(float(card["price_amount"]) - float(item["price"])) < 0.005
        assert card["image_url"]

    first_image = images[0]
    media = client.get("/store/media/%d" % first_image["id"])
    assert media.status_code == 200
    assert media.data[:3] == b"\xff\xd8\xff"

    # --- rerun: every item is already seeded and must be skipped ----------
    before_products = {p["id"]: dict(p) for p in products}
    before_meta = {row["product_id"]: dict(row) for row in meta.values()}
    before_images = [
        {key: row[key] for key in ("id", "product_id", "image_mime", "version", "is_cover")}
        for row in images
    ]
    before_seeds = {seed["source_key"]: seed["product_id"] for seed in seeds}

    second = importer.run_import(app, manifest, apply=True)

    assert second["applied"] is True
    assert second["errors"] == []
    assert second["counts"] == {"existing": 52}
    assert all(entry["status"] == "existing" for entry in second["items"])

    after_products = {
        row["id"]: row for row in rows("SELECT * FROM products WHERE product_type = 'sale'")
    }
    assert set(after_products) == set(before_products)
    for product_id, before_row in before_products.items():
        assert after_products[product_id] == before_row
    assert {entry["product_id"] for entry in second["items"]} == set(before_products)
    assert scalar("SELECT COUNT(*) FROM shop_listing_seeds") == 52
    assert scalar("SELECT COUNT(*) FROM shop_listing_images") == 52
    assert {seed["source_key"]: seed["product_id"] for seed in rows(
        "SELECT * FROM shop_listing_seeds"
    )} == before_seeds
    assert {
        row["product_id"]: {key: row[key] for key in before_meta[row["product_id"]]}
        for row in rows("SELECT * FROM shop_listing_meta")
    } == before_meta
    assert [
        {key: row[key] for key in ("id", "product_id", "image_mime", "version", "is_cover")}
        for row in rows("SELECT * FROM shop_listing_images ORDER BY id")
    ] == before_images

    # A dry run after the import reports the same "nothing to do" answer.
    third = importer.run_import(app, manifest, apply=False)
    assert third["counts"] == {"existing": 52}
    assert third["errors"] == []


# ---------------------------------------------------------------------------
# nothing else moves; rental SKUs stay rentals
# ---------------------------------------------------------------------------


def test_import_writes_no_customer_order_payment_or_stock_rows(app, manifest):
    branch = add_branch()
    rental = add_product(
        "Rental 750 kg utility trailer",
        sku="RENT-750-UNRELATED",
        branch_id=branch,
        price=480.0,
        quantity=4,
    )
    get_db().execute(
        "INSERT INTO product_branch_stock (product_id, branch_id, quantity, updated_at) "
        "VALUES (?, ?, 4, ?)",
        [rental, branch, now()],
    )
    customer = get_db().execute(
        "INSERT INTO customers (name, email, phone, created_at) VALUES (?, ?, ?, ?)",
        ["Existing Client", "client@example.com", "082 555 0100", now()],
    ).lastrowid
    order = get_db().execute(
        "INSERT INTO orders (order_number, customer_id, booking_type, collect_branch_id, "
        "return_branch_id, status, start_at, end_at, total, due_total, created_at) "
        "VALUES ('ORD-UNRELATED-1', ?, 'return', ?, ?, 'draft', ?, ?, 480, 480, ?)",
        [
            customer,
            branch,
            branch,
            "2026-11-02T09:00:00",
            "2026-11-04T15:00:00",
            now(),
        ],
    ).lastrowid
    get_db().execute(
        "INSERT INTO order_items (order_id, product_id, quantity, unit_price, "
        "line_subtotal, line_total, description) VALUES (?, ?, 1, 480, 480, 480, 'Hire')",
        [order, rental],
    )
    get_db().execute(
        "INSERT INTO payments (order_id, amount, method, reference, payment_date, created_at) "
        "VALUES (?, 480, 'cash', 'UNRELATED', '2026-11-02', ?)",
        [order, now()],
    )
    get_db().execute(
        "INSERT INTO documents (order_id, document_type, number, status, created_at) "
        "VALUES (?, 'invoice', 'INV-UNRELATED-1', 'draft', ?)",
        [order, now()],
    )
    get_db().commit()

    rental_before = row_dict(
        get_db().execute("SELECT * FROM products WHERE id = ?", [rental]).fetchone()
    )
    before = snapshot(UNTOUCHED_TABLES)

    report = importer.run_import(app, manifest, apply=True)

    assert report["errors"] == []
    assert report["counts"] == {"created": 52}

    rental_after = row_dict(
        get_db().execute("SELECT * FROM products WHERE id = ?", [rental]).fetchone()
    )
    assert rental_after == rental_before
    assert scalar("SELECT COUNT(*) FROM products WHERE sku = 'RENT-750-UNRELATED'") == 1
    assert scalar("SELECT COUNT(*) FROM products WHERE product_type = 'sale'") == 52

    created_ids = {entry["product_id"] for entry in report["items"]}
    assert rental not in created_ids
    for table, before_rows in before.items():
        after_rows = rows("SELECT * FROM %s ORDER BY id" % table)
        assert after_rows == before_rows, "%s must not change during the import" % table


def test_rental_sku_conflict_is_refused_and_the_rental_is_untouched(app, manifest):
    item = manifest["items"][0]
    rental = add_product(
        "Rental unit that already owns the SKU",
        sku=item["code"],
        price=900.0,
        quantity=7,
    )
    rental_before = row_dict(
        get_db().execute("SELECT * FROM products WHERE id = ?", [rental]).fetchone()
    )

    report = importer.run_import(app, manifest, apply=True)

    assert report["counts"] == {"conflict": 1, "created": 51}
    assert report["errors"] == []
    assert len(report["warnings"]) == 1
    assert "SKU %s already belongs to a rental" % item["code"] in report["warnings"][0]

    conflicted = next(
        entry for entry in report["items"] if entry["source_key"] == item["source_key"]
    )
    assert conflicted["status"] == "conflict"
    assert conflicted["product_id"] == rental
    assert item["code"] in conflicted["reason"]

    rental_after = row_dict(
        get_db().execute("SELECT * FROM products WHERE id = ?", [rental]).fetchone()
    )
    assert rental_after == rental_before
    assert scalar("SELECT COUNT(*) FROM products WHERE product_type = 'sale'") == 51
    assert scalar(
        "SELECT COUNT(*) FROM products WHERE product_type = 'sale' AND sku = ?",
        [item["code"]],
    ) == 0
    assert scalar("SELECT COUNT(*) FROM shop_listing_seeds") == 51
    assert shop_catalogue.seed_source_key(rental) == ""
    assert shop_catalogue.seed_product_id(item["source_key"]) is None

    # A rerun still refuses it; it never "adopts" the rental on a second pass.
    rerun = importer.run_import(app, manifest, apply=True)
    assert rerun["counts"] == {"conflict": 1, "existing": 51}
    assert scalar("SELECT COUNT(*) FROM products WHERE product_type = 'sale'") == 51
    assert row_dict(
        get_db().execute("SELECT * FROM products WHERE id = ?", [rental]).fetchone()
    ) == rental_before


def test_interrupted_photo_fails_atomically_and_a_rerun_completes(app, tmp_path, manifest):
    """A missing photo must not leave a product without its listing/seed."""
    first = manifest["items"][0]  # sale-01 photo
    car = next(item for item in manifest["items"] if item["image"].startswith("sale-16-"))
    mini = {
        "schema": 1,
        "source": manifest["source"],
        "images": {
            first["image"]: manifest["images"][first["image"]],
            car["image"]: manifest["images"][car["image"]],
        },
        "items": [first, car],
    }
    partial_images = tmp_path / "partial-images"
    partial_images.mkdir()
    shutil.copy(PHOTO_DIR / first["image"], partial_images / first["image"])

    with pytest.raises(FileNotFoundError):
        importer.run_import(app, mini, images_dir=str(partial_images), apply=True)

    # The first item was committed whole; the failing item left no product,
    # no shop copy, no photo and no seed behind.
    products = rows("SELECT * FROM products WHERE product_type = 'sale'")
    assert [p["name"] for p in products] == [first["name"]]
    product_id = products[0]["id"]
    assert shop_catalogue.seed_product_id(first["source_key"]) == product_id
    assert shop_catalogue.seed_product_id(car["source_key"]) is None
    assert scalar("SELECT COUNT(*) FROM shop_listing_images WHERE product_id = ?", [product_id]) == 1
    assert scalar("SELECT COUNT(*) FROM shop_listing_meta WHERE product_id = ?", [product_id]) == 1
    assert scalar("SELECT COUNT(*) FROM products WHERE name = ?", [car["name"]]) == 0
    assert scalar("SELECT COUNT(*) FROM shop_listing_seeds") == 1

    # With the committed photos in place the rerun skips the finished item and
    # creates only the one that failed.
    report = importer.run_import(app, mini, apply=True)
    assert report["counts"] == {"existing": 1, "created": 1}
    assert report["errors"] == []
    assert scalar("SELECT COUNT(*) FROM products WHERE product_type = 'sale'") == 2
    assert scalar("SELECT COUNT(*) FROM shop_listing_seeds") == 2
    assert scalar("SELECT COUNT(*) FROM shop_listing_images") == 2
    car_id = imported_product_id(car["source_key"])
    assert shop_catalogue.product_images(car_id)


# ---------------------------------------------------------------------------
# staff edits in the Online store survive a rerun
# ---------------------------------------------------------------------------


def test_online_store_copy_photo_and_price_edits_survive_a_reseed(app, client, manifest):
    importer.run_import(app, manifest, apply=True)
    item = manifest["items"][0]
    product_id = imported_product_id(item["source_key"])
    product_before = row_dict(
        get_db().execute("SELECT * FROM products WHERE id = ?", [product_id]).fetchone()
    )

    sign_in_owner(client)
    token = plant_csrf(client, SHOP_CSRF_SCOPE)

    page = client.get("/online-store/listings/%d" % product_id)
    assert page.status_code == 200

    saved = client.post(
        "/online-store/listings/%d" % product_id,
        data={
            "csrf_token": token,
            "store_title": "Staff title for the sale trailer",
            "store_description": "Staff description written in the Online store editor.",
            "model_key": "",
            "store_sort_order": "5",
            "public_visible": "1",
        },
        follow_redirects=True,
    )
    assert saved.status_code == 200
    assert b"Shop listing saved." in saved.data

    uploaded = client.post(
        "/online-store/listings/%d/images" % product_id,
        data={
            "csrf_token": token,
            "alt_text": "Staff photo",
            "image": (io.BytesIO(jpeg_bytes()), "staff-photo.jpg"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert uploaded.status_code == 200
    assert b"Photo added to the listing." in uploaded.data

    repriced = client.post(
        "/online-store/listings/%d/price" % product_id,
        data={"csrf_token": token, "price_amount": "R 21 500,50"},
        follow_redirects=True,
    )
    assert repriced.status_code == 200
    assert b"21500.50" in repriced.data

    product_after_edits = row_dict(
        get_db().execute("SELECT * FROM products WHERE id = ?", [product_id]).fetchone()
    )
    assert abs(float(product_after_edits["price_amount"]) - 21500.50) < 0.005
    for column in (
        "name",
        "product_type",
        "sku",
        "quantity",
        "tracking_method",
        "security_deposit",
        "tax_profile_id",
        "product_group_id",
        "price_unit",
        "branch_id",
        "active",
        "public_visible",
    ):
        assert product_after_edits[column] == product_before[column], column

    meta_before = row_dict(
        get_db().execute(
            "SELECT * FROM shop_listing_meta WHERE product_id = ?", [product_id]
        ).fetchone()
    )
    assert meta_before["store_title"] == "Staff title for the sale trailer"
    assert meta_before["store_description"] == (
        "Staff description written in the Online store editor."
    )
    assert int(meta_before["store_sort_order"]) == 5

    images_before = shop_catalogue.product_images(product_id)
    assert len(images_before) == 2
    bytes_before = [client.get(image["url"]).data for image in images_before]
    assert all(data for data in bytes_before)
    assert bytes_before[0] != bytes_before[1]

    report = importer.run_import(app, manifest, apply=True)
    assert report["counts"] == {"existing": 52}
    assert report["errors"] == []
    assert imported_product_id(item["source_key"]) == product_id
    assert scalar("SELECT COUNT(*) FROM shop_listing_seeds") == 52
    assert scalar("SELECT COUNT(*) FROM shop_listing_images") == 52 + 1

    product_after_reseed = row_dict(
        get_db().execute("SELECT * FROM products WHERE id = ?", [product_id]).fetchone()
    )
    assert product_after_reseed == product_after_edits
    assert scalar("SELECT COUNT(*) FROM products WHERE name = ?", [item["name"]]) == 1

    meta_after = row_dict(
        get_db().execute(
            "SELECT * FROM shop_listing_meta WHERE product_id = ?", [product_id]
        ).fetchone()
    )
    assert meta_after == meta_before

    images_after = shop_catalogue.product_images(product_id)
    assert [image["id"] for image in images_after] == [image["id"] for image in images_before]
    assert [client.get(image["url"]).data for image in images_after] == bytes_before

    card = next(
        row
        for row in shop_catalogue.list_public_products(product_type="sale")
        if row["id"] == product_id
    )
    assert card["store_title"] == "Staff title for the sale trailer"
    assert card["store_description"] == (
        "Staff description written in the Online store editor."
    )
    assert abs(float(card["price_amount"]) - 21500.50) < 0.005


def test_selling_price_edit_rejects_non_numeric_and_non_finite_values(app, client):
    sale_id = add_product(
        "Synthetic sale trailer", product_type="sale", price=19600.0, quantity=0
    )
    rental_id = add_product("Synthetic rental trailer", price=450.0)
    sign_in_owner(client)
    token = plant_csrf(client, SHOP_CSRF_SCOPE)

    bad_values = ("", " ", "abc", "nan", "NaN", "inf", "-inf", "1e309", "-5")
    for value in bad_values:
        response = client.post(
            "/online-store/listings/%d/price" % sale_id,
            data={"csrf_token": token, "price_amount": value},
        )
        assert response.status_code == 302, value
        assert scalar("SELECT price_amount FROM products WHERE id = ?", [sale_id]) == 19600.0

    for value in ("nan", "inf", "-inf", "1e309", "abc", ""):
        with pytest.raises(ValueError):
            products_service.update_sale_price(sale_id, value)
    assert scalar("SELECT price_amount FROM products WHERE id = ?", [sale_id]) == 19600.0

    # Non-finite input is refused with a message, not a 500.
    flash_page = client.post(
        "/online-store/listings/%d/price" % sale_id,
        data={"csrf_token": token, "price_amount": "nan"},
        follow_redirects=True,
    )
    assert flash_page.status_code == 200
    assert b"Enter a selling price between" in flash_page.data

    # The shop route and the service both refuse a rental's daily rate.
    refused = client.post(
        "/online-store/listings/%d/price" % rental_id,
        data={"csrf_token": token, "price_amount": "999"},
    )
    assert refused.status_code == 404
    with pytest.raises(ValueError):
        products_service.update_sale_price(rental_id, "999")
    assert scalar("SELECT price_amount FROM products WHERE id = ?", [rental_id]) == 450.0

    # A valid rand amount still saves (comma decimal, currency prefix, spaces).
    saved = client.post(
        "/online-store/listings/%d/price" % sale_id,
        data={"csrf_token": token, "price_amount": " R 24 900,75 "},
        follow_redirects=True,
    )
    assert saved.status_code == 200
    assert scalar("SELECT price_amount FROM products WHERE id = ?", [sale_id]) == 24900.75


# ---------------------------------------------------------------------------
# purchase enquiry for a made-to-order item
# ---------------------------------------------------------------------------


def test_purchase_enquiry_is_accepted_for_made_to_order_stock(app, client, manifest):
    importer.run_import(app, manifest, apply=True)
    branch = add_branch("Enquiry Depot")
    publish_privacy_notice("1.1")
    item = manifest["items"][0]
    product_id = imported_product_id(item["source_key"])

    before_sale = row_dict(
        get_db().execute("SELECT * FROM products WHERE id = ?", [product_id]).fetchone()
    )
    assert int(before_sale["quantity"]) == 0
    assert before_sale["tracking_method"] == "none"
    assert before_sale["branch_id"] is None
    before = snapshot(UNTOUCHED_TABLES)

    response = submit_purchase_enquiry(client, branch, product_id, quantity=2)

    assert response.status_code == 303
    assert "/store/request/" in response.headers["Location"]

    enquiries = rows("SELECT * FROM shop_enquiries")
    assert len(enquiries) == 1
    enquiry = enquiries[0]
    assert enquiry["enquiry_type"] == "purchase"
    assert enquiry["branch_id"] == branch
    assert enquiry["status"] == "new"
    assert enquiry["start_at"] is None and enquiry["end_at"] is None

    lines = rows("SELECT * FROM shop_enquiry_items")
    assert len(lines) == 1
    assert lines[0]["product_id"] == product_id
    assert lines[0]["product_name"] == item["name"]
    assert lines[0]["sku"] == item["code"]
    assert int(lines[0]["quantity"]) == 2
    assert abs(float(lines[0]["unit_price"]) - float(item["price"])) < 0.005
    assert abs(float(lines[0]["line_total"]) - float(item["price"]) * 2) < 0.005

    # The item is still made to order: no stock, no branch, no reservation.
    product_after = row_dict(
        get_db().execute("SELECT * FROM products WHERE id = ?", [product_id]).fetchone()
    )
    assert product_after == before_sale
    assert scalar("SELECT COUNT(*) FROM product_branch_stock WHERE product_id = ?", [product_id]) == 0
    assert scalar("SELECT COUNT(*) FROM shop_listing_seeds") == 52

    after = snapshot(UNTOUCHED_TABLES)
    for table, before_rows in before.items():
        if table.startswith("shop_enquir"):
            continue
        assert after[table] == before_rows, "%s must not change on a purchase enquiry" % table

    receipt = client.get(response.headers["Location"])
    assert receipt.status_code == 200
    assert item["name"] not in receipt.get_data(as_text=True)


# ---------------------------------------------------------------------------
# staff-facing label
# ---------------------------------------------------------------------------


def test_admin_navigation_and_enquiry_pages_say_online_enquiries(app, client):
    group = add_group("Utility trailers for sale")
    product_id = add_product(
        "Label check sale trailer",
        product_type="sale",
        sku="LABEL-SALE-1",
        price=15000.0,
        quantity=0,
        group_id=group,
    )
    branch = add_branch("Label Depot")
    publish_privacy_notice("1.1")

    response = submit_purchase_enquiry(client, branch, product_id)
    assert response.status_code == 303
    enquiry_id = scalar("SELECT id FROM shop_enquiries ORDER BY id DESC LIMIT 1")

    sign_in_owner(client)

    index_page = client.get("/online-store/enquiries")
    assert index_page.status_code == 200
    index_html = index_page.get_data(as_text=True)
    assert "Online enquiries" in index_html
    assert "Store enquiries" not in index_html

    detail_page = client.get("/online-store/enquiries/%d" % enquiry_id)
    assert detail_page.status_code == 200
    detail_html = detail_page.get_data(as_text=True)
    assert "Online enquiry" in detail_html
    assert "All online enquiries" in detail_html
    assert "Store enquiry" not in detail_html

    store_page = client.get("/online-store")
    assert store_page.status_code == 200
    assert "Online enquiries" in store_page.get_data(as_text=True)

    listings_page = client.get("/online-store/listings")
    assert listings_page.status_code == 200
    assert "Online enquiries" in listings_page.get_data(as_text=True)
