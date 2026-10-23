"""Regression tests for the shop catalogue performance work.

The performance change makes four promises that are all observable from
outside the module, and each one is pinned here:

* the public catalogue is served in bounded pages whose totals agree with the
  rows walked across every page (search, category and model-group totals
  included), and the ``format=fragment`` "load more" payload carries the next
  page URL;
* the editor's listings screen pages at 20 within the session's depot scope,
  with SQL state filters whose totals match the chip counts;
* a listing/metadata read never transfers image bytes — only the media route
  does, in one round trip per object, with a conditional (ETag/304) response
  and a short public cache, while hidden media still needs staff in the owning
  branch;
* uploads are bounded by bytes *and* pixels (before decoding), stored photos
  and card thumbnails are both regenerated when a photo is replaced, and the
  Render start command runs the checked-in four-thread gunicorn config.

Helpers are reused from ``test_shop_catalogue`` rather than re-created, so the
fixtures stay one definition.
"""

import binascii
import importlib.util
import io
import re
import struct
import sys
from pathlib import Path

import pytest
from PIL import Image

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import test_shop_catalogue as shop_tests  # noqa: E402  (path setup above)

from app.db import get_db  # noqa: E402
from app.services import shop_catalogue  # noqa: E402
from app.services.shop_catalogue import (  # noqa: E402
    ADMIN_PAGE_SIZE,
    MAX_IMAGE_EDGE,
    MAX_IMAGE_PIXELS,
    MAX_SOURCE_EDGE,
    MAX_UPLOAD_BYTES,
    PUBLIC_PAGE_SIZE,
    THUMB_EDGE,
    add_image,
    editable_listing_counts,
    home_image_url,
    list_public_products,
    page_editable_products,
    page_public_products,
    product_images,
    read_upload,
    replace_image,
)
from app.services.shop_catalogue import ShopImageError  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures and small helpers (test_shop_catalogue owns the seed helpers)
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(tmp_path):
    return shop_tests.make_app(tmp_path / "shop_perf.sqlite")


def add_catalogue(count, prefix="Perf sale trailer", product_type="sale", **kwargs):
    ids = []
    for index in range(count):
        ids.append(
            shop_tests.add_product(
                name=f"{prefix} {index + 1:02d}", product_type=product_type, price=1000.0 + index, **kwargs
            )
        )
    return ids


def set_staff_session(branch_ids, user_id=None):
    """Prime a request-context session as ``sign_in`` does for a test client."""
    from flask import session

    session.update(
        user_id=user_id or 1,
        user_role="staff",
        user_name="Perf Staff",
        staff_modules=["online_store"],
        can_view_all_branches=False,
        branch_ids=list(branch_ids),
    )


def traced(app, action):
    """Run ``action`` with every SQL statement captured from the request DB."""
    statements = []
    with app.app_context():
        db = get_db()
        db.set_trace_callback(statements.append)
        try:
            action()
        finally:
            db.set_trace_callback(None)
    return statements


def png_header(width, height):
    """A structurally valid PNG header declaring an arbitrary pixel size.

    The size is refused (or accepted) before Pillow decodes anything, so the
    test never has to materialise a bomb-sized bitmap.
    """

    def chunk(kind, payload):
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def shop_cards(html):
    return html.count('class="shop-product"')


def attach_photo(product_id, width=900, height=600):
    media_id = add_image(
        product_id, shop_tests._Upload("perf.png", shop_tests.png_bytes(width, height))
    )
    return media_id


# ---------------------------------------------------------------------------
# public pagination: every item, exactly once, with honest totals
# ---------------------------------------------------------------------------


def test_public_catalogue_paginates_every_item_exactly_once(app):
    with app.app_context():
        shop_tests.add_group("Trailers for sale")
        names = [f"Perf sale trailer {i:02d}" for i in range(1, 31)]
        add_catalogue(30, prefix="Perf sale trailer")

    client = app.test_client()
    seen = []
    bodies = {}
    for page in (1, 2, 3):
        response = client.get(f"/store/shop?page={page}")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        bodies[page] = body
        on_page = [name for name in names if name in body]
        assert len(on_page) == shop_cards(body)
        seen.extend(on_page)
        if page == 1:
            assert "30 items" in body
            assert "page 1 of 3" in body

    # 30 sale products at the documented public page size: 12 + 12 + 6.
    assert PUBLIC_PAGE_SIZE == 12
    assert shop_cards(bodies[1]) == 12
    assert shop_cards(bodies[2]) == 12
    assert shop_cards(bodies[3]) == 6
    assert sorted(seen) == sorted(names), "a page dropped or duplicated an item"

    # The last page offers no "next" link, and the walk terminated.
    assert "Next page" not in bodies[3]
    assert "page 3 of 3" in bodies[3]


def test_public_page_numbers_are_clamped_not_trusted(app):
    with app.app_context():
        ids = add_catalogue(5, prefix="Clamp trailer")

        first = page_public_products(product_type="sale", page=0)
        assert first["page"] == 1 and first["per_page"] == PUBLIC_PAGE_SIZE
        assert first["total"] == len(ids) and first["pages"] == 1

        negative = page_public_products(product_type="sale", page=-4)
        assert negative["page"] == 1 and len(negative["items"]) == len(ids)

        garbage = page_public_products(product_type="sale", page="not-a-page", per_page="nope")
        assert garbage["page"] == 1
        assert garbage["per_page"] == PUBLIC_PAGE_SIZE

        huge = page_public_products(product_type="sale", per_page=5000)
        assert huge["per_page"] == 100, "per_page must be capped, not honoured"

        beyond = page_public_products(product_type="sale", page=9999)
        assert beyond["items"] == []
        assert beyond["total"] == len(ids)
        assert beyond["has_next"] is False and beyond["has_prev"] is True


def test_public_search_and_category_totals_agree_with_every_page(app):
    with app.app_context():
        sale_group = shop_tests.add_group("Trailers for sale")
        spare_group = shop_tests.add_group("Spares")
        brake_ids = [
            shop_tests.add_product(name=f"Brake kit {i}", product_type="sale", group_id=sale_group)
            for i in range(7)
        ]
        for i in range(13):
            shop_tests.add_product(
                name=f"Tipping trailer {i}", product_type="sale", group_id=sale_group
            )
        for i in range(4):
            shop_tests.add_product(
                name=f"Brake shoe spare {i}", product_type="sale", group_id=spare_group
            )

        expected = {item["id"] for item in list_public_products(product_type="sale", query="brake")}
        # "brake" matches both the seven kits and the four spare parts.
        assert len(expected) == 11
        expected_pages = (len(expected) + 2) // 3

        walked = []
        page = 1
        while True:
            result = page_public_products(product_type="sale", query="brake", page=page, per_page=3)
            assert result["total"] == len(expected), "total drifted between pages"
            assert result["pages"] == expected_pages
            walked.extend(item["id"] for item in result["items"])
            if not result["has_next"]:
                break
            page += 1
        assert len(walked) == len(set(walked)) == len(expected)
        assert set(walked) == expected

        # A category filter counts only that category's rows.
        category = page_public_products(product_type="sale", category="Spares", page=1)
        assert category["total"] == 4

        # A search with no hit is an empty, well-formed page.
        empty = page_public_products(product_type="sale", query="no-such-product")
        assert empty["total"] == 0 and empty["items"] == [] and empty["pages"] == 0
        assert empty["has_next"] is False


def test_public_totals_count_model_groups_once_with_complete_units(app):
    with app.app_context():
        group_id = shop_tests.add_group("Utility")
        units = []
        for model in range(5):
            pair = []
            for slot in range(2):
                product_id = shop_tests.add_product(
                    name=f"Model {model} unit {slot}",
                    product_type="sale",
                    group_id=group_id,
                    price=450.0,
                    quantity=1 + slot,
                )
                shop_catalogue.save_listing(
                    product_id,
                    {"store_title": f"Model {model} unit {slot}", "model_key": f"perf-model-{model}"},
                )
                pair.append(product_id)
            units.append(pair)

        walked = []
        page = 1
        while True:
            result = page_public_products(product_type="sale", page=page, per_page=2)
            assert result["total"] == 5, "a model group must count as one card"
            assert result["pages"] == 3
            walked.extend(result["items"])
            if not result["has_next"]:
                break
            page += 1

        assert len(walked) == 5
        representative_ids = [card["id"] for card in walked]
        assert len(set(representative_ids)) == 5, "a model group appeared on two pages"

        # Every card carries both physical units, each with its own quantity, and
        # no unit is invented or repeated.
        seen_unit_ids = []
        for card in walked:
            unit_ids = [unit["id"] for unit in card["model_units"]]
            assert len(unit_ids) == len(set(unit_ids)) == 2
            seen_unit_ids.extend(unit_ids)
            assert all(isinstance(unit["quantity"], int) for unit in card["model_units"])
        expected_units = {product_id for pair in units for product_id in pair}
        assert set(seen_unit_ids) == expected_units
        assert len(seen_unit_ids) == len(expected_units), "a unit was listed twice"


def test_public_fragment_returns_cards_and_the_next_page_url(app):
    with app.app_context():
        add_catalogue(14, prefix="Fragment trailer")
        priced = shop_tests.add_product(name="Fragment priced trailer", product_type="sale", price=1500.0)

    client = app.test_client()
    first = client.get("/store/shop?page=1&format=fragment")
    assert first.status_code == 200
    assert first.is_json
    payload = first.get_json()
    assert set(payload) >= {"html", "next_url", "prev_url", "total", "page", "pages"}
    assert payload["total"] >= 15
    assert payload["page"] == 1
    assert payload["pages"] == 2
    assert payload["prev_url"] == ""
    assert "page=2" in payload["next_url"]
    assert shop_cards(payload["html"]) == PUBLIC_PAGE_SIZE
    assert "/store/" in payload["html"]

    last = client.get("/store/shop?page=2&format=fragment").get_json()
    assert last["next_url"] == ""
    assert last["pages"] == 2
    assert last["prev_url"] != ""
    assert shop_cards(last["html"]) == payload["total"] - PUBLIC_PAGE_SIZE
    # The priced product is still reachable across the walk, with its price.
    combined = payload["html"] + last["html"]
    assert "Fragment priced trailer" in combined
    assert "R1,500.00" in combined
    assert priced

    # The next-page URL keeps the search filter, so "load more" cannot widen it.
    searched = client.get("/store/shop?q=fragment&format=fragment").get_json()
    assert searched["total"] >= 14
    assert "q=fragment" in (searched["next_url"] or "q=fragment")


def test_public_card_html_uses_the_thumbnail_route(app):
    with app.app_context():
        product_id = shop_tests.add_product(name="Thumb card trailer", product_type="sale")
        media_id = attach_photo(product_id)
        assert product_images(product_id)[0]["thumb_url"].startswith(
            f"/store/media/{media_id}/thumb"
        )

    body = app.test_client().get("/store/shop").get_data(as_text=True)
    assert "Thumb card trailer" in body
    # Cards should request the ~480px thumbnail, not the full photo, or the
    # storefront gains nothing from generating one.
    assert f"/store/media/{media_id}/thumb" in body
    assert f'src="/store/media/{media_id}?v=' not in body


# ---------------------------------------------------------------------------
# editor listings: 20 per page inside the depot scope, SQL state filters
# ---------------------------------------------------------------------------


def test_admin_listings_page_is_twenty_and_branch_filtered(app):
    with app.app_context():
        branch_one = shop_tests.get_branch("Branch 1")
        branch_two = shop_tests.get_branch("Branch 2")
        own = [
            shop_tests.add_product(name=f"Depot one trailer {i:02d}", product_type="sale", branch_id=branch_one)
            for i in range(25)
        ]
        for i in range(3):
            shop_tests.add_product(name=f"Depot two trailer {i}", product_type="sale", branch_id=branch_two)
        shop_tests.add_product(name="Unassigned depot trailer 1", product_type="sale")
        shop_tests.add_product(name="Unassigned depot trailer 2", product_type="sale")
        staff = shop_tests.add_staff("Perf Depot One", branch_ids=[branch_one])

    client = shop_tests.sign_in(app.test_client(), staff, branch_ids=[branch_one])
    first = client.get("/online-store/listings")
    assert first.status_code == 200
    body = first.get_data(as_text=True)
    assert body.count('class="shop-row-title"') == ADMIN_PAGE_SIZE == 20
    assert "27 listings" in body
    assert "page 1 of 2" in body
    assert "Depot two trailer" not in body

    second = client.get("/online-store/listings?page=2").get_data(as_text=True)
    assert second.count('class="shop-row-title"') == 7
    assert "page 2 of 2" in second
    assert "Depot two trailer" not in second
    assert "Unassigned depot trailer" in second


def test_admin_state_filters_match_their_chip_counts(app):
    with app.app_context():
        branch_one = shop_tests.get_branch("Branch 1")
        visible_group = shop_tests.add_group("Visible")
        live_with_image = shop_tests.add_product(
            name="Live with photo", product_type="sale", group_id=visible_group, branch_id=branch_one
        )
        shop_tests.add_product(
            name="Live without photo", product_type="sale", group_id=visible_group, branch_id=branch_one
        )
        unpublished = shop_tests.add_product(
            name="Unpublished trailer",
            product_type="sale",
            group_id=visible_group,
            branch_id=branch_one,
            public_visible=0,
        )
        under_maintenance = shop_tests.add_product(
            name="Workshop trailer",
            product_type="sale",
            group_id=visible_group,
            branch_id=branch_one,
            under_maintenance=1,
        )
        featured = shop_tests.add_product(
            name="Featured trailer", product_type="sale", group_id=visible_group, branch_id=branch_one
        )
        no_description = shop_tests.add_product(
            name="Blank copy trailer",
            product_type="sale",
            group_id=visible_group,
            branch_id=branch_one,
            description="",
        )
        shop_tests.add_product(
            name="Rental sibling", product_type="rental", group_id=visible_group, branch_id=branch_one
        )
        shop_catalogue.save_listing(featured, {"store_title": "Featured trailer", "featured": "1"})
        attach_photo(live_with_image)
        attach_photo(featured)
        staff = shop_tests.add_staff("Perf State Editor", branch_ids=[branch_one])

        with app.test_request_context("/online-store/listings"):
            set_staff_session([branch_one], user_id=staff)
            counts, type_counts = editable_listing_counts(branch_ids=[branch_one])
            total = page_editable_products(branch_ids=[branch_one])["total"]
            assert total == 7 == counts["all"]
            assert type_counts == {"sale": 6, "rental": 1}
            assert counts["live"] + counts["hidden"] == counts["all"]

            live = page_editable_products(state="live", branch_ids=[branch_one])
            hidden = page_editable_products(state="hidden", branch_ids=[branch_one])
            assert live["total"] == counts["live"]
            assert hidden["total"] == counts["hidden"]
            live_ids = {item["id"] for item in list_public_products()}
            assert {card["id"] for card in live["items"]} == live_ids
            assert {card["id"] for card in hidden["items"]} == {unpublished, under_maintenance}

            missing_image = page_editable_products(state="missing_image", branch_ids=[branch_one])
            assert missing_image["total"] == counts["missing_image"] == 5
            assert {card["id"] for card in missing_image["items"]}.isdisjoint(
                {live_with_image, featured}
            )

            missing_description = page_editable_products(
                state="missing_description", branch_ids=[branch_one]
            )
            assert [card["id"] for card in missing_description["items"]] == [no_description]

            only_featured = page_editable_products(state="featured", branch_ids=[branch_one])
            assert [card["id"] for card in only_featured["items"]] == [featured]

            typed = page_editable_products(
                product_type="rental", state="live", branch_ids=[branch_one]
            )
            assert typed["total"] == 1


# ---------------------------------------------------------------------------
# metadata reads never carry image bytes
# ---------------------------------------------------------------------------


def test_listing_and_metadata_reads_never_select_image_bytes(app):
    with app.app_context():
        product_id = shop_tests.add_product(name="Traced trailer", product_type="sale")
        media_id = attach_photo(product_id)
        shop_catalogue.set_home_image(
            "hire", shop_tests._Upload("home.png", shop_tests.png_bytes(300, 200))
        )

    public_statements = traced(app, lambda: page_public_products(product_type="sale"))
    admin_statements = []
    with app.test_request_context("/online-store/listings"):
        set_staff_session([1])
        admin_statements = []
        db = get_db()
        db.set_trace_callback(admin_statements.append)
        try:
            page_editable_products()
            product_images(product_id)
            home_image_url("hire")
        finally:
            db.set_trace_callback(None)

    # A ``column IS NULL`` presence probe is not a byte transfer; anything else
    # naming a BLOB column would be.
    presence_check = re.compile(r"[\w.]+\.image_blob\s+IS\s+(NOT\s+)?NULL", re.IGNORECASE)

    for label, statements in (("public", public_statements), ("admin", admin_statements)):
        assert any("shop_listing_images" in sql for sql in statements), (
            f"{label} read did not touch the gallery table; the test proved nothing"
        )
        blobs = [
            sql
            for sql in statements
            if "image_blob" in presence_check.sub("", sql).lower()
        ]
        assert blobs == [], f"{label} listing/metadata path selected image bytes: {blobs}"

    assert public_statements and "shop_listing_thumbnails" in " ".join(public_statements)
    assert media_id  # the photo exists; the trap would have caught a BLOB read


# ---------------------------------------------------------------------------
# media route: one round trip, correct caching, conditional 304
# ---------------------------------------------------------------------------


def trace_requests(app):
    """Attach a per-request SQL recorder; returns the captured statement list."""
    captured = []

    @app.before_request
    def _capture_sql():
        captured.clear()
        get_db().set_trace_callback(captured.append)

    return captured


def image_queries(captured):
    return [
        sql
        for sql in captured
        if "shop_listing_images" in sql or "shop_listing_thumbnails" in sql
    ]


def test_public_media_is_one_query_with_a_short_public_cache(app):
    with app.app_context():
        product_id = shop_tests.add_product(name="Cached trailer", product_type="sale")
        media_id = attach_photo(product_id)
        stored = bytes(
            get_db()
            .execute("SELECT image_blob FROM shop_listing_images WHERE id = ?", [media_id])
            .fetchone()["image_blob"]
        )

    captured = trace_requests(app)
    response = app.test_client().get(f"/store/media/{media_id}")
    assert response.status_code == 200
    assert response.data == stored
    assert response.headers["Content-Type"] == "image/jpeg"
    assert response.headers["X-Content-Type-Options"] == "nosniff"

    queries = image_queries(captured)
    assert len(queries) == 1, f"public media took {len(queries)} image queries: {queries}"

    # Public bytes may be reused for a short window, but a product unpublished
    # midway must not stay in a browser cache for a year.
    cache = response.headers["Cache-Control"]
    assert "public" in cache
    assert "must-revalidate" in cache
    assert "immutable" not in cache
    assert "max-age=300" in cache
    assert "no-store" not in cache
    assert response.headers.get("ETag")


def test_thumbnail_route_serves_the_small_copy_in_one_query(app):
    with app.app_context():
        product_id = shop_tests.add_product(name="Thumb media trailer", product_type="sale")
        media_id = attach_photo(product_id, 1600, 900)
        full = bytes(
            get_db()
            .execute("SELECT image_blob FROM shop_listing_images WHERE id = ?", [media_id])
            .fetchone()["image_blob"]
        )

    captured = trace_requests(app)
    client = app.test_client()
    response = client.get(f"/store/media/{media_id}/thumb")
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "image/jpeg"
    assert response.data != full
    assert len(response.data) < len(full)
    assert len(image_queries(captured)) == 1, image_queries(captured)

    thumb = Image.open(io.BytesIO(response.data))
    assert max(thumb.size) <= THUMB_EDGE
    assert thumb.size == (480, 270)

    cache = response.headers["Cache-Control"]
    assert "max-age=300" in cache and "must-revalidate" in cache

    full_response = client.get(f"/store/media/{media_id}")
    # A thumbnail and its photo must not share a validator.
    assert response.headers["ETag"] != full_response.headers["ETag"]
    assert "-t" in response.headers["ETag"]
    assert "-f" in full_response.headers["ETag"]


def test_media_answers_a_matching_if_none_match_with_304(app):
    with app.app_context():
        product_id = shop_tests.add_product(name="Revalidated trailer", product_type="sale")
        media_id = attach_photo(product_id)

    client = app.test_client()
    full = client.get(f"/store/media/{media_id}")
    assert full.status_code == 200
    etag = full.headers["ETag"]

    revalidated = client.get(f"/store/media/{media_id}", headers={"If-None-Match": etag})
    assert revalidated.status_code == 304
    assert revalidated.data == b""
    assert revalidated.headers["ETag"] == etag

    thumb = client.get(f"/store/media/{media_id}/thumb")
    assert thumb.status_code == 200
    thumb_304 = client.get(
        f"/store/media/{media_id}/thumb", headers={"If-None-Match": thumb.headers["ETag"]}
    )
    assert thumb_304.status_code == 304

    with app.app_context():
        replace_image(
            media_id,
            product_id,
            shop_tests._Upload("new.jpg", shop_tests.jpeg_bytes(800, 500), "image/jpeg"),
        )
    replaced = client.get(f"/store/media/{media_id}")
    assert replaced.status_code == 200
    assert replaced.headers["ETag"] != etag, "a replaced photo must not reuse the old validator"
    stale = client.get(f"/store/media/{media_id}", headers={"If-None-Match": etag})
    assert stale.status_code == 200


def test_hidden_media_needs_staff_in_the_owning_branch(app):
    with app.app_context():
        branch_one = shop_tests.get_branch("Branch 1")
        branch_two = shop_tests.get_branch("Branch 2")
        product_id = shop_tests.add_product(
            name="Hidden depot trailer", product_type="sale", branch_id=branch_one, public_visible=0
        )
        media_id = attach_photo(product_id)
        own_staff = shop_tests.add_staff("Perf Own Branch", branch_ids=[branch_one])
        other_staff = shop_tests.add_staff("Perf Other Branch", branch_ids=[branch_two])

    assert app.test_client().get(f"/store/media/{media_id}").status_code == 404

    own = shop_tests.sign_in(app.test_client(), own_staff, branch_ids=[branch_one])
    preview = own.get(f"/store/media/{media_id}")
    assert preview.status_code == 200
    assert "private" in preview.headers["Cache-Control"]
    assert "no-store" in preview.headers["Cache-Control"]
    assert "public" not in preview.headers["Cache-Control"]
    assert own.get(f"/store/media/{media_id}/thumb").status_code == 200

    other = shop_tests.sign_in(app.test_client(), other_staff, branch_ids=[branch_two])
    assert other.get(f"/store/media/{media_id}").status_code == 404
    assert other.get(f"/store/media/{media_id}/thumb").status_code == 404


def test_store_switch_off_hides_public_media_but_keeps_staff_preview(app):
    with app.app_context():
        branch_one = shop_tests.get_branch("Branch 1")
        product_id = shop_tests.add_product(
            name="Store off trailer", product_type="sale", branch_id=branch_one
        )
        media_id = attach_photo(product_id)
        db = get_db()
        db.execute("UPDATE company_settings SET store_enabled = 0 WHERE id = 1")
        db.commit()
        staff = shop_tests.add_staff("Perf Store Off", branch_ids=[branch_one])

    assert app.test_client().get(f"/store/media/{media_id}").status_code == 404

    signed_in = shop_tests.sign_in(app.test_client(), staff, branch_ids=[branch_one])
    response = signed_in.get(f"/store/media/{media_id}")
    assert response.status_code == 200
    assert "no-store" in response.headers["Cache-Control"]


# ---------------------------------------------------------------------------
# upload limits: bytes, pixels, and a stored thumbnail
# ---------------------------------------------------------------------------


def test_upload_refuses_files_over_the_byte_cap_before_touching_the_gallery(app):
    with app.app_context():
        product_id = shop_tests.add_product(name="Too big trailer")
        oversized = b"\x89PNG\r\n\x1a\n" + b"\0" * (MAX_UPLOAD_BYTES + 1)
        with pytest.raises(ShopImageError) as refused:
            add_image(product_id, shop_tests._Upload("huge.png", oversized))
        assert "5 MB" in str(refused.value)
        assert MAX_UPLOAD_BYTES == 5 * 1024 * 1024
        assert product_images(product_id) == []
        assert read_upload(shop_tests._Upload("ok.png", shop_tests.png_bytes(10, 10)))


def test_upload_refuses_oversized_pixels_from_the_header_before_decoding(app):
    with app.app_context():
        product_id = shop_tests.add_product(name="Guarded trailer")

        # Longest edge over the source cap.
        with pytest.raises(ShopImageError) as edge:
            add_image(
                product_id,
                shop_tests._Upload("wide.png", png_header(MAX_SOURCE_EDGE + 1, 40)),
            )
        assert "longest side must be" in str(edge.value)
        assert str(MAX_SOURCE_EDGE) in str(edge.value)

        # Inside the edge cap but over the pixel cap (decompression-bomb guard).
        with pytest.raises(ShopImageError) as pixels:
            add_image(product_id, shop_tests._Upload("bomb.png", png_header(6000, 5000)))
        assert "too large to process" in str(pixels.value)
        assert 6000 * 5000 > MAX_IMAGE_PIXELS

        # Nothing was stored and no thumbnail row was orphaned.
        assert product_images(product_id) == []
        assert (
            get_db()
            .execute("SELECT COUNT(*) AS c FROM shop_listing_thumbnails")
            .fetchone()["c"]
            == 0
        )


def test_upload_stores_a_downscaled_photo_and_a_card_thumbnail(app):
    with app.app_context():
        product_id = shop_tests.add_product(name="Resized trailer")
        original = shop_tests.jpeg_bytes(4000, 3000)
        media_id = add_image(product_id, shop_tests._Upload("big.jpg", original, "image/jpeg"))

        photo = get_db().execute(
            "SELECT image_blob, width, height, image_mime FROM shop_listing_images WHERE id = ?",
            [media_id],
        ).fetchone()
        assert (photo["width"], photo["height"]) == (MAX_IMAGE_EDGE, 960)
        assert photo["image_mime"] == "image/jpeg"
        assert len(photo["image_blob"]) < len(original)

        thumb = get_db().execute(
            "SELECT image_blob, width, height, image_mime FROM shop_listing_thumbnails"
            " WHERE image_id = ?",
            [media_id],
        ).fetchone()
        assert thumb is not None, "every stored photo must get a card thumbnail"
        assert (thumb["width"], thumb["height"]) == (THUMB_EDGE, 360)
        assert thumb["image_mime"] == "image/jpeg"
        assert len(thumb["image_blob"]) < len(photo["image_blob"])
        assert Image.open(io.BytesIO(bytes(thumb["image_blob"]))).size == (480, 360)

        card_photo = product_images(product_id)[0]
        assert card_photo["thumb_url"].endswith("?v=1")
        assert card_photo["url"].endswith("?v=1")


def test_replacing_a_photo_replaces_its_thumbnail_and_bumps_the_version(app):
    with app.app_context():
        product_id = shop_tests.add_product(name="Replaced trailer")
        media_id = add_image(
            product_id, shop_tests._Upload("landscape.png", shop_tests.png_bytes(900, 600))
        )
        before = get_db().execute(
            "SELECT image_blob, width, height FROM shop_listing_thumbnails WHERE image_id = ?",
            [media_id],
        ).fetchone()
        assert (before["width"], before["height"]) == (480, 320)

    with app.app_context():
        version = replace_image(
            media_id,
            product_id,
            shop_tests._Upload("portrait.jpg", shop_tests.jpeg_bytes(800, 1600), "image/jpeg"),
        )
        assert version == 2

        after = get_db().execute(
            "SELECT image_blob, width, height FROM shop_listing_thumbnails WHERE image_id = ?",
            [media_id],
        ).fetchone()
        assert after is not None
        assert (after["width"], after["height"]) == (240, 480)
        assert bytes(after["image_blob"]) != bytes(before["image_blob"])
        assert (
            get_db()
            .execute(
                "SELECT COUNT(*) AS c FROM shop_listing_thumbnails WHERE image_id = ?",
                [media_id],
            )
            .fetchone()["c"]
            == 1
        )
        assert product_images(product_id)[0]["thumb_url"].endswith("?v=2")

        # And the route serves the new thumbnail, not the stale one.
        served = app.test_client().get(f"/store/media/{media_id}/thumb")
        assert served.status_code == 200
        assert Image.open(io.BytesIO(served.data)).size == (240, 480)


def test_deleting_a_photo_removes_its_thumbnail_row(app):
    with app.app_context():
        product_id = shop_tests.add_product(name="Deleted trailer")
        media_id = attach_photo(product_id)
        assert (
            get_db()
            .execute("SELECT COUNT(*) AS c FROM shop_listing_thumbnails")
            .fetchone()["c"]
            == 1
        )
        shop_catalogue.delete_image(media_id, product_id)
        assert (
            get_db()
            .execute("SELECT COUNT(*) AS c FROM shop_listing_thumbnails")
            .fetchone()["c"]
            == 0
        )

    assert app.test_client().get(f"/store/media/{media_id}/thumb").status_code == 404


# ---------------------------------------------------------------------------
# deployment: the gunicorn config Render actually runs
# ---------------------------------------------------------------------------


def load_gunicorn_config():
    path = ROOT / "gunicorn.conf.py"
    spec = importlib.util.spec_from_file_location("abi_gunicorn_conf", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gunicorn_config_runs_four_threads_and_render_uses_it():
    config = load_gunicorn_config()
    assert config.threads == 4
    assert config.worker_class == "gthread"
    assert config.workers == 1
    assert config.timeout == 45
    assert config.graceful_timeout == 30
    assert config.keepalive == 5
    assert config.preload_app is False
    assert config.bind.startswith("0.0.0.0:")

    render = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "gunicorn -c gunicorn.conf.py 'app:create_app()'" in render
    assert "healthCheckPath: /health" in render
    # A worker pool is not a substitute for bounding the query sizes above.
    assert config.threads > 1
