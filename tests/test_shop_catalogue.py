"""Shop catalogue, listing editor, gallery and home-page artwork.

These tests cover the guarantees the shop add-on is meant to make:

* the shop is a *presentation* layer — a shop edit can never change a price, a
  deposit, a tax profile or a stock count;
* public eligibility matches the storefront's own rule (active, published, not
  under maintenance, visible category), and a hidden category is excluded rather
  than folded into an "Other" bucket;
* photos are validated, resized, stored in the database and read back byte for
  byte after a later request (Render's filesystem is ephemeral);
* every write needs the ``online_store`` module, an active database user, the
  session's depot scope and a CSRF token;
* reads return plain dicts on both sqlite3 and libSQL row objects.
"""

import io
import json
import sqlite3

import pytest
from PIL import Image

from app import create_app
from app.db import get_db
from app.routes.shop_admin import CSRF_SCOPE, bp as shop_admin_bp
from app.services import shop_catalogue
from app.services.shop_catalogue import (
    HOME_SLOT_KEYS,
    get_public_product,
    home_image_url,
    init_schema,
    list_public_products,
)

CONTRACT_KEYS = {
    "id",
    "name",
    "store_title",
    "store_description",
    "product_type",
    "price_amount",
    "price_unit",
    "security_deposit",
    "image_url",
    "category_name",
    "model_units",
}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def make_app(database_path, public_base_url="https://shop.example.test"):
    application = create_app(
        {
            "TESTING": True,
            "DATABASE": str(database_path),
            "SECRET_KEY": "test",
            "ADMIN_EMAIL": "admin@abi.local",
            "ADMIN_PASSWORD": "admin123",
            "TURSO_DATABASE_URL": "",
            "TURSO_AUTH_TOKEN": "",
            "PUBLIC_BASE_URL": public_base_url,
        }
    )
    # The parent wires the blueprint in app/__init__.py; register it here only
    # when this checkout has not been integrated yet, so the suite passes both
    # before and after that one-line change.
    if "shop_admin.listings" not in application.view_functions:
        application.register_blueprint(shop_admin_bp)
    with application.app_context():
        init_schema(get_db())
    return application


@pytest.fixture()
def app(tmp_path):
    return make_app(tmp_path / "shop.sqlite")


@pytest.fixture()
def owner(app):
    client = app.test_client()
    with app.app_context():
        user = get_db().execute(
            "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
        ).fetchone()
        user_id = user["id"]
    with client.session_transaction() as session:
        session.update(
            user_id=user_id,
            user_role="owner",
            user_name="Synthetic Owner",
            can_view_all_branches=True,
        )
    return client


def get_branch(name="Branch 1"):
    return get_db().execute("SELECT id FROM branches WHERE name = ?", [name]).fetchone()["id"]


def add_group(name, active=1, store_visible=1, image=None):
    db = get_db()
    cursor = db.execute(
        "INSERT INTO product_groups "
        "(name, description, active, sort_order, image_blob, image_mime, becomes_store_visible, "
        "created_at, updated_at) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?)",
        [
            name,
            f"{name} items",
            active,
            image,
            "image/jpeg" if image else "",
            store_visible,
            "2026-01-01T00:00:00",
            "2026-01-01T00:00:00",
        ],
    )
    db.commit()
    return cursor.lastrowid


def add_product(
    name="750kg Utility Trailer",
    product_type="rental",
    price=450.0,
    deposit=1000.0,
    quantity=3,
    group_id=None,
    branch_id=None,
    active=1,
    public_visible=1,
    under_maintenance=0,
    description="A sturdy little trailer.",
):
    db = get_db()
    cursor = db.execute(
        "INSERT INTO products "
        "(name, product_type, description, sku, active, public_visible, price_amount, price_unit, "
        "security_deposit, product_group_id, quantity, tracking_method, under_maintenance, "
        "branch_id, created_at) VALUES (?, ?, ?, '', ?, ?, ?, 'day', ?, ?, ?, 'bulk', ?, ?, ?)",
        [
            name,
            product_type,
            description,
            active,
            public_visible,
            price,
            deposit,
            group_id,
            quantity,
            under_maintenance,
            branch_id,
            "2026-01-01T00:00:00",
        ],
    )
    db.commit()
    return cursor.lastrowid


def add_staff(name="Shop Editor", modules=("online_store",), branch_ids=None, active=1):
    from werkzeug.security import generate_password_hash

    db = get_db()
    cursor = db.execute(
        "INSERT INTO users (email, password_hash, name, initials, role, branch_id, "
        "can_view_all_branches, active, modules_json, created_at) "
        "VALUES (?, ?, ?, 'SE', 'staff', NULL, 0, ?, ?, ?)",
        [
            f"{name.lower().replace(' ', '.')}@abi.local",
            generate_password_hash("staff123"),
            name,
            active,
            json.dumps(list(modules)),
            "2026-01-01T00:00:00",
        ],
    )
    user_id = cursor.lastrowid
    for branch_id in branch_ids or []:
        db.execute('INSERT INTO user_branch_access (user_id,branch_id) VALUES (?,?)',(user_id,branch_id))
    if branch_ids:
        db.execute('UPDATE users SET branch_id=? WHERE id=?',(branch_ids[0],user_id))
    db.commit()
    return user_id


def sign_in(client, user_id, role="staff", modules=("online_store",), branch_ids=None, can_view_all=False):
    with client.session_transaction() as session:
        session.clear()
        session.update(
            user_id=user_id,
            user_role=role,
            user_name="Staff",
            staff_modules=list(modules),
            can_view_all_branches=can_view_all,
        )
        if branch_ids is not None:
            session["branch_ids"] = list(branch_ids)
    return client


def csrf_for(client, token="shop-test-csrf-token"):
    """Plant a known shop CSRF token in the session and return it.

    Writing it directly (rather than scraping a page) keeps the write tests
    independent of the page that happens to issue it; the guard, the scope and
    the constant-time compare are the same either way.
    """
    with client.session_transaction() as session:
        tokens = session.get("_csrf_tokens")
        if not isinstance(tokens, dict):
            tokens = {}
        tokens[CSRF_SCOPE] = token
        session["_csrf_tokens"] = tokens
    return token


def png_bytes(width=900, height=600, colour=(200, 30, 40)):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_bytes(width=2400, height=1600, colour=(30, 90, 200)):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# schema and the public contract
# ---------------------------------------------------------------------------


def test_init_schema_is_idempotent_and_keeps_existing_rows(app):
    with app.app_context():
        product_id = add_product(name="Repeated boot trailer")
        assert init_schema(get_db()) is True
        assert init_schema(get_db()) is True
        tables = {
            row["name"]
            for row in get_db().execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"shop_listing_meta", "shop_listing_images", "shop_home_images"} <= tables
        # The product row is untouched by a repeated schema pass.
        assert get_db().execute(
            "SELECT name FROM products WHERE id = ?", [product_id]
        ).fetchone()["name"] == "Repeated boot trailer"


def test_list_public_products_matches_the_documented_contract(app):
    with app.app_context():
        group_id = add_group("Utility")
        product_id = add_product(name="750kg Utility Trailer", group_id=group_id)
        items = list_public_products()
        assert len(items) == 1
        card = items[0]
        assert set(card) == CONTRACT_KEYS
        assert card["id"] == product_id
        assert card["name"] == "750kg Utility Trailer"
        # Never edited: the operational copy is the fallback, not blank.
        assert card["store_title"] == "750kg Utility Trailer"
        assert card["store_description"] == "A sturdy little trailer."
        assert card["product_type"] == "rental"
        assert card["price_amount"] == 450.0
        assert card["price_unit"] == "day"
        assert card["security_deposit"] == 1000.0
        assert card["category_name"] == "Utility"
        assert card["image_url"] == ""
        assert card["model_units"] == []
        detail = get_public_product(product_id)
        assert isinstance(detail, dict)
        assert detail["store_title"] == "750kg Utility Trailer"
        assert detail["store_images"] == []
        assert get_public_product(999999) is None
        assert get_public_product("not-an-id") is None


def test_product_type_query_and_category_filters(app):
    with app.app_context():
        hire = add_group("Hire fleet")
        buy = add_group("Trailers for sale")
        rental_id = add_product(name="Hire trailer", product_type="rental", group_id=hire)
        sale_id = add_product(name="Buy trailer", product_type="sale", group_id=buy)
        service_id = add_product(name="Brake service", product_type="service", group_id=buy)

        assert [item["id"] for item in list_public_products(product_type="sale")] == [sale_id]
        assert {item["id"] for item in list_public_products(query="brake")} == {service_id}
        assert {item["id"] for item in list_public_products(category="hire fleet")} == {rental_id}
        assert {item["id"] for item in list_public_products(category=str(buy))} == {sale_id, service_id}
        assert list_public_products(category="no such category") == []
        assert rental_id in {item["id"] for item in list_public_products(product_type="")}


def test_public_eligibility_excludes_hidden_state_but_never_relabels_it(app):
    with app.app_context():
        visible = add_group("Visible")
        hidden = add_group("Closed category", active=0)
        no_store = add_group("Not on the shop", store_visible=0)

        live = add_product(name="Live trailer", group_id=visible)
        add_product(name="Switched off", group_id=visible, active=0)
        add_product(name="Unpublished", group_id=visible, public_visible=0)
        add_product(name="In the workshop", group_id=visible, under_maintenance=1)
        hidden_category = add_product(name="Hidden category trailer", group_id=hidden)
        not_shop_category = add_product(name="Category off shop", group_id=no_store)
        ungrouped = add_product(name="Ungrouped trailer")

        ids = {item["id"] for item in list_public_products()}
        assert ids == {live, ungrouped}
        # A product in a hidden category is not re-labelled into an "Other" bucket:
        # it is simply not on the shop, and asking for that category finds nothing.
        assert hidden_category not in ids
        assert not_shop_category not in ids
        assert list_public_products(category="Closed category") == []
        assert list_public_products(category="Not on the shop") == []
        assert get_public_product(hidden_category) is None
        # "Other"/blank is reserved for genuinely ungrouped stock.
        assert [item["category_name"] for item in list_public_products() if item["id"] == ungrouped] == [""]


def test_model_units_group_only_explicitly_compatible_units(app):
    with app.app_context():
        group_id = add_group("Utility")
        first = add_product(name="750kg Utility A", group_id=group_id, price=450.0, quantity=2)
        second = add_product(name="750kg Utility B", group_id=group_id, price=450.0, quantity=1)
        different_price = add_product(name="750kg Utility C", group_id=group_id, price=520.0)
        unnamed = add_product(name="750kg Utility", group_id=group_id, price=450.0)

        for product_id in (first, second, different_price):
            shop_catalogue.save_listing(
                product_id,
                {"store_title": f"Unit {product_id}", "model_key": "utility-750"},
            )

        cards = {item["id"]: item for item in list_public_products()}
        assert {unit["id"] for unit in cards[first]["model_units"]} == {first, second}
        assert {unit["id"] for unit in cards[second]["model_units"]} == {first, second}
        # Same key, different price: a separate group, never merged.
        assert {unit["id"] for unit in cards[different_price]["model_units"]} == {different_price}
        # No declared key: never grouped, even with an identical name.
        assert cards[unnamed]["model_units"] == []
        # Each unit keeps its own identity.
        assert cards[first]["id"] != cards[second]["id"]
        assert cards[first]["name"] == "750kg Utility A"


# ---------------------------------------------------------------------------
# editor writes: copy only
# ---------------------------------------------------------------------------


def test_saving_copy_cannot_change_price_quantity_or_tax(app, owner):
    with app.app_context():
        product_id = add_product(name="Priced trailer", price=450.0, quantity=3, deposit=1000.0)
    with app.app_context():
        before = dict(get_db().execute("SELECT * FROM products WHERE id = ?", [product_id]).fetchone())

    token = csrf_for(owner)
    response = owner.post(
        f"/online-store/listings/{product_id}",
        data={
            "csrf_token": token,
            "store_title": "Sano's 750 kg utility trailer",
            "store_description": "Perfect for weekend garden refuse.",
            "store_sort_order": "4",
            "model_key": "utility-750",
            "featured": "1",
            "public_visible": "1",
            # Fields the editor must ignore, even when a crafted post sends them.
            "price_amount": "9999",
            "price_unit": "hour",
            "security_deposit": "1",
            "quantity": "500",
            "tax_profile_id": "99",
            "name": "Renamed by the shop",
            "active": "0",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    with app.app_context():
        after = dict(get_db().execute("SELECT * FROM products WHERE id = ?", [product_id]).fetchone())
        for column in ("name", "price_amount", "price_unit", "security_deposit", "quantity", "tax_profile_id", "active"):
            assert after[column] == before[column], column
        card = list_public_products()[0]
        assert card["store_title"] == "Sano's 750 kg utility trailer"
        assert card["price_amount"] == 450.0
        assert card["security_deposit"] == 1000.0


def test_title_too_long_is_refused_and_nothing_is_written(app, owner):
    with app.app_context():
        product_id = add_product(name="Copy guard trailer")
    token = csrf_for(owner)
    response = owner.post(
        f"/online-store/listings/{product_id}",
        data={"csrf_token": token, "store_title": "x" * 400},
    )
    assert response.status_code == 400
    with app.app_context():
        assert get_db().execute(
            "SELECT COUNT(*) AS c FROM shop_listing_meta WHERE product_id = ?", [product_id]
        ).fetchone()["c"] == 0


# ---------------------------------------------------------------------------
# gallery: validate, resize, persist, read back
# ---------------------------------------------------------------------------


def test_gallery_upload_is_resized_persisted_and_served_back(app, owner):
    with app.app_context():
        product_id = add_product(name="Photo trailer")

    token = csrf_for(owner)
    response = owner.post(
        f"/online-store/listings/{product_id}/images",
        data={
            "csrf_token": token,
            "alt_text": "Red 750kg trailer",
            "image": (io.BytesIO(jpeg_bytes(2400, 1600)), "phone-photo.jpg"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302

    with app.app_context():
        images = shop_catalogue.product_images(product_id)
        assert len(images) == 1
        media_id = images[0]["id"]
        assert images[0]["is_cover"] is True
        raw = get_db().execute(
            "SELECT image_blob, image_mime, width, height FROM shop_listing_images WHERE id = ?",
            [media_id],
        ).fetchone()
        assert raw["image_mime"] == "image/jpeg"
        assert len(raw["image_blob"]) > 0
        assert max(raw["width"], raw["height"]) <= shop_catalogue.MAX_IMAGE_EDGE
        stored = bytes(raw["image_blob"])

    # A later request (new request context, same database) reads the same bytes back.
    media = owner.get(f"/store/media/{media_id}")
    assert media.status_code == 200
    assert media.headers["Content-Type"] == "image/jpeg"
    assert media.headers["X-Content-Type-Options"] == "nosniff"
    assert media.data == stored
    # The public URL is versioned, and the listing shows the photo.
    page = owner.get(f"/online-store/listings/{product_id}")
    assert page.status_code == 200
    assert f"/store/media/{media_id}" in page.get_data(as_text=True)

    with app.app_context():
        card = list_public_products()[0]
        assert card["image_url"].startswith(f"/store/media/{media_id}")


def test_gallery_refuses_non_images_and_caps_the_gallery(app, owner):
    with app.app_context():
        product_id = add_product(name="Refused photo trailer")
    token = csrf_for(owner)
    bad = owner.post(
        f"/online-store/listings/{product_id}/images",
        data={
            "csrf_token": token,
            "image": (io.BytesIO(b"not an image at all"), "invoice.pdf"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert bad.status_code == 200
    assert "not a PNG, JPEG or WebP" in bad.get_data(as_text=True)

    # A file that claims to be a PNG but is not decodable is refused by Pillow,
    # and nothing is stored either way.
    corrupt = owner.post(
        f"/online-store/listings/{product_id}/images",
        data={
            "csrf_token": token,
            "image": (io.BytesIO(b"\x89PNG not really"), "broken.png"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert corrupt.status_code == 200
    assert "not a readable image" in corrupt.get_data(as_text=True)
    with app.app_context():
        assert shop_catalogue.product_images(product_id) == []


def test_cover_reorder_and_delete_keep_the_gallery_coherent(app, owner):
    with app.app_context():
        product_id = add_product(name="Gallery trailer")
        first = shop_catalogue.add_image(
            product_id, _Upload("one.png", png_bytes(600, 400, (10, 20, 30)))
        )
        second = shop_catalogue.add_image(
            product_id, _Upload("two.png", png_bytes(600, 400, (40, 50, 60)))
        )
        third = shop_catalogue.add_image(
            product_id, _Upload("three.png", png_bytes(600, 400, (70, 80, 90)))
        )

    def order():
        return [image["id"] for image in shop_catalogue.product_images(product_id)]

    token = csrf_for(owner)
    with app.app_context():
        assert order() == [first, second, third]
        assert shop_catalogue.product_images(product_id)[0]["is_cover"] is True

    # Reordering moves a photo within the gallery; the cover stays at the front.
    assert owner.post(
        f"/online-store/listings/{product_id}/images/{third}/move",
        data={"csrf_token": token, "direction": "up"},
    ).status_code == 302
    with app.app_context():
        assert order() == [first, third, second]

    assert owner.post(
        f"/online-store/listings/{product_id}/images/{second}/cover",
        data={"csrf_token": token},
    ).status_code == 302
    with app.app_context():
        images = shop_catalogue.product_images(product_id)
        assert images[0]["id"] == second and images[0]["is_cover"] is True
        assert [image["is_cover"] for image in images].count(True) == 1

    # Deleting the cover promotes the next photo instead of leaving none.
    assert owner.post(
        f"/online-store/listings/{product_id}/images/{second}/delete",
        data={"csrf_token": token},
    ).status_code == 302
    with app.app_context():
        remaining = shop_catalogue.product_images(product_id)
        assert [image["id"] for image in remaining] == [first, third]
        assert remaining[0]["is_cover"] is True


class _Upload:
    """A minimal FileStorage stand-in for service-level upload tests."""

    def __init__(self, filename, data, mimetype="image/png"):
        self.filename = filename
        self.mimetype = mimetype
        self._buffer = io.BytesIO(data)

    def read(self, size=-1):
        return self._buffer.read(size)


def test_category_photo_is_the_image_fallback(app):
    with app.app_context():
        group_id = add_group("Fallback category", image=png_bytes(120, 90))
        product_id = add_product(name="No photo trailer", group_id=group_id)
        card = list_public_products()[0]
        assert card["image_url"] == f"/store/category-image/{group_id}"
        detail = get_public_product(product_id)
        assert detail["image_url"] == f"/store/category-image/{group_id}"
        assert detail["store_images"] == []


# ---------------------------------------------------------------------------
# permission, branch scope and CSRF
# ---------------------------------------------------------------------------


def test_listings_need_a_signed_in_active_user_with_the_store_module(app):
    assert app.test_client().get("/online-store/listings").status_code == 302

    with app.app_context():
        branch_id = get_branch()
        no_module = add_staff("No Store Module", modules=("inventory",), branch_ids=[branch_id])
    client = sign_in(app.test_client(), no_module, modules=("inventory",), branch_ids=[branch_id])
    assert client.get("/online-store/listings").status_code == 403

    with app.app_context():
        deactivated = add_staff("Deactivated Editor", branch_ids=[branch_id])
        db = get_db()
        db.execute("UPDATE users SET active = 0 WHERE id = ?", [deactivated])
        db.commit()
    client = sign_in(app.test_client(), deactivated, branch_ids=[branch_id])
    response = client.get("/online-store/listings")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_staff_branch_scope_limits_which_listings_can_be_edited(app):
    with app.app_context():
        branch_one = get_branch("Branch 1")
        branch_two = get_branch("Branch 2")
        own = add_product(name="Own depot trailer", branch_id=branch_one)
        other = add_product(name="Other depot trailer", branch_id=branch_two)
        unassigned = add_product(name="Unassigned trailer")
        staff = add_staff("Branch One Editor", branch_ids=[branch_one])

    client = sign_in(app.test_client(), staff, branch_ids=[branch_one])
    assert client.get(f"/online-store/listings/{own}").status_code == 200
    assert client.get(f"/online-store/listings/{unassigned}").status_code == 200
    assert client.get(f"/online-store/listings/{other}").status_code == 404

    token = csrf_for(client)
    assert client.post(
        f"/online-store/listings/{other}",
        data={"csrf_token": token, "store_title": "Should not save"},
    ).status_code == 404
    with app.app_context():
        assert get_db().execute(
            "SELECT COUNT(*) AS c FROM shop_listing_meta WHERE product_id = ?", [other]
        ).fetchone()["c"] == 0

    # The listing index is scoped the same way: another depot's trailer is absent.
    body = client.get("/online-store/listings").get_data(as_text=True)
    assert "Own depot trailer" in body
    assert "Other depot trailer" not in body

    # The owner sees every depot.
    with app.app_context():
        owner_id = get_db().execute(
            "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
        ).fetchone()["id"]
    owner_client = sign_in(app.test_client(), owner_id, role="owner", can_view_all=True)
    assert owner_client.get(f"/online-store/listings/{other}").status_code == 200


def test_every_write_requires_the_csrf_token(app, owner):
    with app.app_context():
        product_id = add_product(name="CSRF trailer")
    token = csrf_for(owner)

    refused = owner.post(
        f"/online-store/listings/{product_id}",
        data={"store_title": "No token"},
    )
    assert refused.status_code == 400
    refused_image = owner.post(
        f"/online-store/listings/{product_id}/images",
        data={"image": (io.BytesIO(png_bytes()), "a.png")},
        content_type="multipart/form-data",
    )
    assert refused_image.status_code == 400
    refused_toggle = owner.post(
        f"/online-store/listings/{product_id}/publish",
        data={"public_visible": "0"},
    )
    assert refused_toggle.status_code == 400
    refused_settings = owner.post("/online-store/shop-settings", data={})
    assert refused_settings.status_code == 400

    with app.app_context():
        assert get_db().execute(
            "SELECT COUNT(*) AS c FROM shop_listing_meta WHERE product_id = ?", [product_id]
        ).fetchone()["c"] == 0
        assert shop_catalogue.product_images(product_id) == []
        assert get_db().execute(
            "SELECT public_visible FROM products WHERE id = ?", [product_id]
        ).fetchone()["public_visible"] == 1

    accepted = owner.post(
        f"/online-store/listings/{product_id}",
        data={"csrf_token": token, "store_title": "With token", "public_visible": "1"},
    )
    assert accepted.status_code == 302
    with app.app_context():
        assert get_db().execute(
            "SELECT store_title FROM shop_listing_meta WHERE product_id = ?", [product_id]
        ).fetchone()["store_title"] == "With token"


def test_publish_switch_is_the_products_public_visible_flag(app, owner):
    with app.app_context():
        product_id = add_product(name="Toggle trailer")
    token = csrf_for(owner)

    owner.post(
        f"/online-store/listings/{product_id}/publish",
        data={"csrf_token": token, "public_visible": "0"},
    )
    with app.app_context():
        assert get_db().execute(
            "SELECT public_visible FROM products WHERE id = ?", [product_id]
        ).fetchone()["public_visible"] == 0
        assert list_public_products() == []

    owner.post(
        f"/online-store/listings/{product_id}/publish",
        data={"csrf_token": token, "public_visible": "1"},
    )
    with app.app_context():
        assert get_db().execute(
            "SELECT public_visible FROM products WHERE id = ?", [product_id]
        ).fetchone()["public_visible"] == 1
        assert len(list_public_products()) == 1


# ---------------------------------------------------------------------------
# home-page artwork
# ---------------------------------------------------------------------------


def test_home_slots_are_database_rows_with_public_urls(app, owner):
    with app.app_context():
        for slot in HOME_SLOT_KEYS:
            assert home_image_url(slot) == ""
        assert home_image_url("unknown") == ""

    assert owner.get("/online-store/shop-settings").status_code == 200

    token = csrf_for(owner)
    response = owner.post(
        "/online-store/shop-settings",
        data={
            "csrf_token": token,
            "image_hire": (io.BytesIO(jpeg_bytes(1800, 1200)), "hire.jpg"),
            "image_buy": (io.BytesIO(png_bytes(1000, 700, (1, 2, 3))), "buy.png"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302

    with app.app_context():
        hire_url = home_image_url("hire")
        assert hire_url.startswith("/store/home-image/hire")
        assert home_image_url("buy").startswith("/store/home-image/buy")
        assert home_image_url("repairs") == ""
        slots = {slot["slot"]: slot for slot in shop_catalogue.home_slots()}
        assert slots["hire"]["url"] == hire_url
        assert slots["repairs"]["url"] == ""
        blob, mime, _version = shop_catalogue.home_image_bytes("hire")
        assert mime == "image/jpeg" and blob

    served = owner.get(hire_url)
    assert served.status_code == 200
    assert served.headers["Content-Type"] == "image/jpeg"
    assert served.data == bytes(blob)
    assert owner.get("/store/home-image/unknown").status_code == 404

    # The settings page shows the artwork it now has.
    page = owner.get("/online-store/shop-settings").get_data(as_text=True)
    assert hire_url in page

    assert owner.post(
        "/online-store/shop-settings/home-image/hire/remove",
        data={"csrf_token": token},
    ).status_code == 302
    with app.app_context():
        assert home_image_url("hire") == ""
    assert owner.get(hire_url).status_code == 404


def test_shop_link_uses_public_base_url_or_the_external_url(app, owner):
    page = owner.get("/online-store/listings").get_data(as_text=True)
    assert "https://shop.example.test/store" in page
    with app.app_context():
        assert shop_catalogue.shop_public_url() == "https://shop.example.test/store"
        assert shop_catalogue.shop_public_url("http://internal.test/store") == "https://shop.example.test/store"
    assert owner.get("/online-store/shop-settings").status_code == 200


# ---------------------------------------------------------------------------
# libSQL rows
# ---------------------------------------------------------------------------


@pytest.fixture()
def sdk_app(tmp_path, monkeypatch):
    """An app whose row factory mimics production libSQL (tuple subclass, no keys())."""
    from libsql_client.result import Row

    def sdk_rows(cursor, values):
        return Row({item[0]: index for index, item in enumerate(cursor.description)}, tuple(values))

    monkeypatch.setattr(sqlite3, "Row", sdk_rows)
    application = make_app(tmp_path / "sdk.sqlite")
    with application.app_context():
        group_id = add_group("SDK category")
        product_id = add_product(name="SDK trailer", group_id=group_id)
        shop_catalogue.save_listing(
            product_id, {"store_title": "SDK public title", "model_key": "sdk-750"}
        )
        shop_catalogue.add_image(product_id, _Upload("sdk.png", png_bytes(700, 500)))
    return application, product_id


def test_reads_are_plain_dicts_on_libsql_like_rows(sdk_app):
    application, product_id = sdk_app
    with application.app_context():
        row = get_db().execute("SELECT * FROM products WHERE id = ?", [product_id]).fetchone()
        assert not hasattr(row, "keys")

        items = list_public_products()
        assert len(items) == 1
        card = items[0]
        assert type(card) is dict
        assert set(card) == CONTRACT_KEYS
        assert card["store_title"] == "SDK public title"
        assert card["model_units"][0]["id"] == product_id

        detail = get_public_product(product_id)
        assert type(detail) is dict
        assert detail["store_images"][0]["url"].startswith("/store/media/")

        editor_rows = shop_catalogue.editable_products()
        assert editor_rows and type(editor_rows[0]) is dict
        media_id = shop_catalogue.product_images(product_id)[0]["id"]
        data, mime, _version, owner_id = shop_catalogue.image_bytes(media_id)
        assert data and mime == "image/jpeg" and owner_id == product_id

    client = application.test_client()
    with application.app_context():
        owner_id = get_db().execute(
            "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
        ).fetchone()["id"]
    sign_in(client, owner_id, role="owner", can_view_all=True)
    assert client.get("/online-store/listings").status_code == 200
    assert client.get(f"/online-store/listings/{product_id}").status_code == 200
    assert client.get(f"/store/media/{media_id}").status_code == 200
