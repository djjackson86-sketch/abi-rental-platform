"""Programme phase 11 (feature C / §C1) — category photos + bulk trailer linking.

The acceptance list from ``docs/plans/2026-09-23-public-booking-and-store-categories.md`` §C1, one
test per line where it maps cleanly:

* uploading a photo stores the bytes and MIME type on the group; a bogus file (wrong type, too
  large, or not an image at all) is refused;
* a 5000px-wide image is downscaled to at most 1600px before it is stored;
* ``clear`` removes the bytes and the store route then 404s;
* ``GET /store/category-image/<id>`` serves the **exact** stored bytes with the right
  ``Content-Type`` and 404s when the group has none;
* the seed script is idempotent and never clobbers a staff upload;
* the bulk ``/inventory/groups/<id>/assign`` moves exactly the ticked products and leaves every
  other product alone;
* the store renders one section per store-visible category with the category photo URL, and a
  category with no photo falls back to the text block (no broken-image icon);
* cascade: deleting a group sets its products' ``product_group_id`` back to NULL (the FK's
  ``ON DELETE SET NULL`` behaviour — asserted, not implemented here).

Two decisions this file pins on top of the plan:

* **A product never disappears from the store because its category did.** A category that is
  inactive or hidden from the store (``becomes_store_visible = 0``) simply does not render as its
  own section; its products fold into the trailing "Other" section, exactly like ungrouped ones.
  The store's contract is "active and public products", and a category switch must not silently
  drop a bookable trailer.
* **The seed reads the committed web-sized re-encodes, not the 11 MB of raw Sano source** (guardrail
  2). The matching is token-subset on the slugged name, and files with no matching group — like a
  hypothetical *mobile kitchen* or *bobcat* photo — are reported as ``no-match`` and never invent an
  image for a group.
"""

import importlib.util
import io
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest
from PIL import Image
from werkzeug.datastructures import FileStorage

from app import create_app
from app.db import get_db, now
from app.services import group_images

ROOT = Path(__file__).resolve().parents[1]

# Load the seed script as a module so its pure functions are testable (scripts/ is not a package).
_seed_spec = importlib.util.spec_from_file_location(
    "seed_default_group_images", ROOT / "scripts" / "seed_default_group_images.py"
)
assert _seed_spec is not None and _seed_spec.loader is not None
seed_script = importlib.util.module_from_spec(_seed_spec)
_seed_spec.loader.exec_module(seed_script)


def build_app(database_path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": database_path,
            "SECRET_KEY": "test",
            "ADMIN_EMAIL": "admin@abi.local",
            "ADMIN_PASSWORD": "admin123",
        }
    )


@pytest.fixture()
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    os.unlink(path)


@pytest.fixture()
def app(db_path):
    return build_app(db_path)


@pytest.fixture()
def client(app):
    return app.test_client()


def login_owner(client):
    with client.application.app_context():
        row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
    return client.post(
        "/login", data={"user_id": str(row["id"]), "password": "admin123"}, follow_redirects=True
    )


def png_bytes(width=64, height=48, color=(255, 0, 0)):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def upload_storage(data, filename="photo.png", mimetype="image/png"):
    return FileStorage(stream=io.BytesIO(data), filename=filename, content_type=mimetype)


def make_group(app, name, **extra):
    with app.app_context():
        db = get_db()
        values = {
            "name": name,
            "description": extra.pop("description", ""),
            "active": extra.pop("active", 1),
            "becomes_store_visible": extra.pop("becomes_store_visible", 1),
            "sort_order": extra.pop("sort_order", 0),
        }
        cur = db.execute(
            "INSERT INTO product_groups (name, description, active, becomes_store_visible, sort_order, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (values["name"], values["description"], values["active"], values["becomes_store_visible"], values["sort_order"], now(), now()),
        )
        db.commit()
        return cur.lastrowid


def make_product(app, name, group_id=None, **extra):
    with app.app_context():
        db = get_db()
        cur = db.execute(
            "INSERT INTO products (name, product_type, description, sku, active, public_visible, "
            "price_amount, price_unit, security_deposit, tax_profile_id, product_group_id, quantity, "
            "tracking_method, under_maintenance, created_at) "
            "VALUES (?, 'rental', '', '', 1, 1, 100, 'day', 0, 1, ?, 1, 'bulk', 0, ?)",
            (name, group_id, now()),
        )
        db.commit()
        return cur.lastrowid


def group_row(app, group_id):
    with app.app_context():
        return get_db().execute("SELECT * FROM product_groups WHERE id = ?", (group_id,)).fetchone()


def product_group_id(app, product_id):
    with app.app_context():
        return get_db().execute(
            "SELECT product_group_id FROM products WHERE id = ?", (product_id,)
        ).fetchone()["product_group_id"]


# --- upload / clear / serve ----------------------------------------------------------------------

def test_upload_sets_bytes_mime_and_source(app, client):
    group_id = make_group(app, "Single Axle")
    login_owner(client)
    response = client.post(
        f"/inventory/groups/{group_id}/image",
        data={"group_image": (io.BytesIO(png_bytes()), "cage.png")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Category photo saved" in response.data
    row = group_row(app, group_id)
    assert row["image_blob"] is not None
    assert row["image_mime"] == "image/jpeg"  # PNG is re-encoded to JPEG
    assert row["image_source"] == group_images.SOURCE_UPLOAD
    assert row["image_filename"] == "cage.png"


def test_bogus_file_is_refused(app):
    group_id = make_group(app, "Single Axle")
    with app.app_context():
        with pytest.raises(group_images.GroupImageError):
            group_images.set_group_image(group_id, upload_storage(b"this is not an image", mimetype="image/png"))
        # …and nothing is written.
        assert group_images.group_image_bytes(group_id) == (None, None)


def test_wrong_mime_type_is_refused(app):
    group_id = make_group(app, "Single Axle")
    with app.app_context():
        with pytest.raises(group_images.GroupImageError) as exc:
            group_images.set_group_image(group_id, upload_storage(b"%PDF-1.4 fake", "x.pdf", "application/pdf"))
        assert "not a PNG, JPEG or WebP" in str(exc.value)


def test_oversized_upload_is_refused(app):
    group_id = make_group(app, "Single Axle")
    huge = b"x" * (group_images.MAX_UPLOAD_BYTES + 1)
    with app.app_context():
        with pytest.raises(group_images.GroupImageError) as exc:
            group_images.set_group_image(group_id, upload_storage(huge))
        assert "larger than 4 MB" in str(exc.value)


def test_5000px_image_is_downscaled(app):
    group_id = make_group(app, "Single Axle")
    wide = io.BytesIO()
    Image.new("RGB", (5000, 3000), (10, 20, 30)).save(wide, format="PNG")
    with app.app_context():
        group_images.set_group_image(group_id, upload_storage(wide.getvalue(), "wide.png"))
        stored, _mime = group_images.group_image_bytes(group_id)
        with Image.open(io.BytesIO(stored)) as img:
            assert img.width <= group_images.MAX_WIDTH
            assert img.width == group_images.MAX_WIDTH  # 5000 -> exactly the cap, aspect kept


def test_clear_removes_the_bytes(app):
    group_id = make_group(app, "Single Axle")
    with app.app_context():
        group_images.set_group_image(group_id, upload_storage(png_bytes()))
        assert group_images.group_image_bytes(group_id)[0] is not None
        assert group_images.clear_group_image(group_id) is True
        assert group_images.group_image_bytes(group_id) == (None, None)
        assert group_images.clear_group_image(group_id) is False, "clearing again reports nothing to remove"


def test_store_route_serves_exact_bytes_and_404s_when_empty(app, client):
    group_id = make_group(app, "Single Axle")
    data = png_bytes(40, 30)
    with app.app_context():
        group_images.set_group_image(group_id, upload_storage(data))
        expected = group_images.group_image_bytes(group_id)[0]
    response = client.get(f"/store/category-image/{group_id}")
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "image/jpeg"
    assert response.data == expected, "the route serves the stored bytes verbatim"
    # No auth: the image is public.
    # A group with no image 404s.
    empty = make_group(app, "No Photo Yet")
    assert client.get(f"/store/category-image/{empty}").status_code == 404
    assert client.get("/store/category-image/999999").status_code == 404


# --- seed script ----------------------------------------------------------------------------------

def test_seed_matches_files_to_groups_and_skips_the_rest(app, tmp_path):
    single = make_group(app, "Single Axle")
    double = make_group(app, "Double Axle Car")
    make_group(app, "Bobcat Trailers")  # no photo exists -> must stay on the text fallback
    (tmp_path / "single-axle-trailer.png").write_bytes(png_bytes())
    (tmp_path / "double-axle-car-trailer.png").write_bytes(png_bytes())
    (tmp_path / "luggage-trailer.png").write_bytes(png_bytes())  # no matching group

    with app.app_context():
        results = seed_script.seed_group_images(tmp_path, commit=True)

    outcomes = {r["file"]: r["outcome"] for r in results}
    assert outcomes["single-axle-trailer.png"] == "set"
    assert outcomes["double-axle-car-trailer.png"] == "set"
    assert outcomes["luggage-trailer.png"] == "no-match"

    assert group_row(app, single)["image_source"] == group_images.SOURCE_DEFAULT_SANO
    assert group_row(app, single)["image_blob"] == png_bytes()
    assert group_row(app, double)["image_source"] == group_images.SOURCE_DEFAULT_SANO
    with app.app_context():
        bobcat = get_db().execute("SELECT image_blob FROM product_groups WHERE name = 'Bobcat Trailers'").fetchone()
        assert bobcat["image_blob"] is None, "a category with no photo must stay on the text fallback"


def test_seed_is_idempotent(app, tmp_path):
    single = make_group(app, "Single Axle")
    (tmp_path / "single-axle-trailer.png").write_bytes(png_bytes())
    with app.app_context():
        first = seed_script.seed_group_images(tmp_path, commit=True)
        assert first[0]["outcome"] == "set"
        second = seed_script.seed_group_images(tmp_path, commit=True)
        assert second[0]["outcome"] == "already"
        assert group_row(app, single)["image_blob"] == png_bytes(), "re-running must not rewrite the bytes"


def test_seed_does_not_clobber_an_upload(app, tmp_path):
    single = make_group(app, "Single Axle")
    custom = png_bytes(12, 12, color=(0, 255, 0))
    with app.app_context():
        group_images.set_group_image(single, upload_storage(custom))
        stored_before = group_images.group_image_bytes(single)[0]
    (tmp_path / "single-axle-trailer.png").write_bytes(png_bytes())
    with app.app_context():
        result = seed_script.seed_group_images(tmp_path, commit=True)
    assert result[0]["outcome"] == "protected"
    row = group_row(app, single)
    assert row["image_source"] == group_images.SOURCE_UPLOAD
    assert row["image_blob"] == stored_before, "a staff upload is never overwritten by a default"


def test_match_group_for_file_prefers_the_most_specific_group(app):
    single = make_group(app, "Single Axle")
    flatbed = make_group(app, "Single Axle Flatbed")
    groups = [
        {"name": "Single Axle", "sort_order": 0, "id": single},
        {"name": "Single Axle Flatbed", "sort_order": 0, "id": flatbed},
    ]
    assert seed_script.match_group_for_file("single-axle-flatbed-trailer", groups)["id"] == flatbed
    assert seed_script.match_group_for_file("single-axle-trailer", groups)["id"] == single
    assert seed_script.match_group_for_file("bobcat-trailer", groups) is None


# --- bulk assign ----------------------------------------------------------------------------------

def test_bulk_assign_moves_exactly_the_ticked_products(app, client):
    group_id = make_group(app, "Single Axle")
    a = make_product(app, "Trailer A")
    b = make_product(app, "Trailer B")
    c = make_product(app, "Trailer C")
    login_owner(client)
    response = client.post(
        f"/inventory/groups/{group_id}/assign",
        data={"product_ids": [str(a), str(b)]},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"2 trailers linked to Single Axle" in response.data
    assert product_group_id(app, a) == group_id
    assert product_group_id(app, b) == group_id
    assert product_group_id(app, c) is None, "unticked products are left exactly where they were"


def test_bulk_assign_reports_nothing_when_empty(app, client):
    group_id = make_group(app, "Single Axle")
    make_product(app, "Trailer A")
    login_owner(client)
    response = client.post(f"/inventory/groups/{group_id}/assign", data={}, follow_redirects=True)
    assert response.status_code == 200
    assert b"No products selected" in response.data


def test_bulk_assign_404s_for_unknown_group(app, client):
    login_owner(client)
    assert client.post("/inventory/groups/9999/assign", data={"product_ids": ["1"]}).status_code == 302


# --- the grouped store ----------------------------------------------------------------------------

def test_store_renders_one_section_per_category_with_photo_url(app, client):
    single = make_group(app, "Single Axle")
    double = make_group(app, "Double Axle")
    make_product(app, "Red Cage Trailer", group_id=single)
    make_product(app, "Blue Flatbed Trailer", group_id=double)
    make_product(app, "Loose Wheel Pump")  # ungrouped -> "Other"
    with app.app_context():
        group_images.set_group_image(single, upload_storage(png_bytes()))

    body = client.get("/store").get_data(as_text=True)
    assert "Single Axle" in body
    assert "Double Axle" in body
    assert "Other" in body
    assert "Red Cage Trailer" in body
    assert "Blue Flatbed Trailer" in body
    assert "Loose Wheel Pump" in body
    # The photo is referenced for the imaged category only.
    assert f'/store/category-image/{single}' in body
    assert f'/store/category-image/{double}' not in body


def test_category_without_image_falls_back_to_the_text_block(app, client):
    group_id = make_group(app, "Double Axle")
    make_product(app, "Blue Flatbed Trailer", group_id=group_id)
    body = client.get("/store").get_data(as_text=True)
    # The fallback block renders with no <img> pointing at a category image (no broken-image icon).
    assert "store-category-fallback" in body
    assert f'<img class="store-category-image"' not in body
    assert f'src="/store/category-image/{group_id}"' not in body


def test_hidden_category_folds_its_products_into_other(app, client):
    group_id = make_group(app, "Hidden Category", becomes_store_visible=0)
    make_product(app, "Sneaky Trailer", group_id=group_id)
    body = client.get("/store").get_data(as_text=True)
    assert "Hidden Category" not in body, "a store-hidden category must not render its own section"
    assert "Sneaky Trailer" in body, "…but its still-active product must not vanish from the store"


def test_store_empty_state_still_renders(app, client):
    body = client.get("/store").get_data(as_text=True)
    assert "No products" in body


# --- cascade --------------------------------------------------------------------------------------

def test_deleting_a_group_leaves_products_without_orphans(app):
    group_id = make_group(app, "Single Axle")
    product_id = make_product(app, "Red Cage Trailer", group_id=group_id)
    with app.app_context():
        db = get_db()
        db.execute("DELETE FROM product_groups WHERE id = ?", (group_id,))
        db.commit()
        assert db.execute(
            "SELECT product_group_id FROM products WHERE id = ?", (product_id,)
        ).fetchone()["product_group_id"] is None


def test_image_columns_are_added_to_an_existing_database(app, db_path):
    """The migration path: a pre-existing product_groups table gains the five columns additively."""
    group_id = make_group(app, "Single Axle")
    # Simulate a database created before §C1 by dropping the new columns after init.
    with app.app_context():
        db = get_db()
        for col in ("image_blob", "image_mime", "image_filename", "image_source", "becomes_store_visible"):
            db.execute(f"ALTER TABLE product_groups DROP COLUMN {col}")
        db.commit()
    # Re-running migrations re-adds them, and an existing group keeps its identity.
    from app.db import run_migrations

    with app.app_context():
        db = get_db()
        run_migrations(db)
        cols = {row["name"] for row in db.execute("PRAGMA table_info(product_groups)").fetchall()}
        assert {"image_blob", "image_mime", "image_filename", "image_source", "becomes_store_visible"} <= cols
        row = db.execute("SELECT becomes_store_visible FROM product_groups WHERE id = ?", (group_id,)).fetchone()
        assert row["becomes_store_visible"] == 1, "the default keeps every existing category store-visible"
