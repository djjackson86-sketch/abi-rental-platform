"""Sale model cards: 19 restored model lines over 52 saved trailer sizes.

The source price list sold one trailer model in several sizes. The storefront
shows one card per model — the pre-merge behaviour — and the card opens a real
product whose detail page lists its sibling sizes. Nothing is merged in the
database: all 52 sale rows, their prices, photos and copy stay separate and
editable, and the editor still lists every one of them.

The contract pinned here:

* 19 model cards cover the 52 trailer variants exactly once; the 33 spares stay
  33 separate cards, and the public total is 19 + 33;
* sizes are grouped only by the durable ``sale/<model>/<code>`` import seed —
  never by name, never by price, and never across a rental;
* a model whose sizes are priced differently shows the lowest saved price as
  "From" and still opens one real product (the cheapest size);
* filtering and paging count model cards, not rows, with exact totals, and the
  detail page selector lists each sibling's saved code, dimension and DB price;
* choosing a size opens that size's own product and the cart stores that actual
  product id; a hidden size is absent from the selector and 404s on its own URL;
* staff price/title edits show up without a reseed, and the editor keeps all 52
  variant products (nothing deleted or merged).
"""

import json
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import test_shop_catalogue as shop_tests  # noqa: E402  (path setup above)

from app.db import get_db  # noqa: E402
from app.services import shop_catalogue  # noqa: E402
from app.services.shop_catalogue import (  # noqa: E402
    PUBLIC_PAGE_SIZE,
    compatible_public_unit_ids,
    get_public_product,
    page_editable_products,
    page_public_products,
    set_public,
)

MANIFEST_PATH = ROOT / "data" / "sale-trailers" / "manifest.json"
PARTS_COUNT = 33


def manifest_items():
    with open(MANIFEST_PATH, encoding="utf-8") as handle:
        return json.load(handle)["items"]


def manifest_models():
    """``{model group: [items, ...]}`` from the committed seed manifest."""
    models = {}
    for item in manifest_items():
        models.setdefault(item["source_key"].split("/")[1], []).append(item)
    return models


@pytest.fixture()
def app(tmp_path):
    return shop_tests.make_app(tmp_path / "sale-model-selection.sqlite")


def seed_trailers():
    """Insert the 52 trailer variants as the importer does, photos omitted.

    Rows, shop copy, publish state and the durable seed key are the same shape
    the real importer writes; a test that needs a photo attaches one explicitly.
    """
    ids = {}
    categories = {}
    db = get_db()
    for item in manifest_items():
        category = f"{item['family']} for sale"
        if category not in categories:
            categories[category] = shop_tests.add_group(category)
        cursor = db.execute(
            "INSERT INTO products (name, product_type, description, sku, active, "
            "public_visible, price_amount, price_unit, security_deposit, "
            "product_group_id, quantity, tracking_method, under_maintenance, "
            "branch_id, created_at) VALUES (?, 'sale', ?, ?, 1, 1, ?, 'fixed', 0, "
            "?, 0, 'none', 0, NULL, '2026-01-01T00:00:00')",
            [
                item["name"],
                item["description"],
                item["code"],
                float(item["price"]),
                categories[category],
            ],
        )
        db.commit()
        product_id = cursor.lastrowid
        shop_catalogue.save_listing(
            product_id,
            {
                "store_title": item["name"],
                "store_description": item["description"],
                "model_key": "",
                "store_sort_order": str(item["sort"]),
                "public_visible": "1",
            },
        )
        shop_catalogue.record_seed(product_id, item["source_key"])
        ids[item["source_key"]] = product_id
    return ids, categories


def seed_parts():
    group_id = shop_tests.add_group("Spares for sale")
    return [
        shop_tests.add_product(
            name=f"Spare part {index + 1:02d}",
            product_type="sale",
            price=100.0 + index,
            group_id=group_id,
        )
        for index in range(PARTS_COUNT)
    ]


def family_cards(cards):
    """Model cards: the 19 families, including the four sold in a single size."""
    return [card for card in cards if card["model_family"]]


def walk(page_size, **filters):
    """Every card across every page, asserting the totals agree with the walk."""
    walked, page = [], 1
    while True:
        result = page_public_products(per_page=page_size, page=page, **filters)
        assert result["total"] >= len(walked) + len(result["items"])
        walked.extend(result["items"])
        if not result["has_next"]:
            break
        page += 1
    assert len(walked) == result["total"]
    assert len({card["id"] for card in walked}) == len(walked), "a card repeated"
    return walked


# ---------------------------------------------------------------------------
# 19 model cards over 52 variants, 33 parts untouched
# ---------------------------------------------------------------------------


def test_fifty_two_trailer_variants_present_as_nineteen_model_cards(app):
    with app.app_context():
        ids, _categories = seed_trailers()
        seed_parts()
        assert len(ids) == 52
        assert len(manifest_models()) == 19

        result = page_public_products(product_type="sale", per_page=100)
        assert result["total"] == 19 + PARTS_COUNT
        models = family_cards(result["items"])
        assert len(models) == 19, "each restored model line is one card"
        assert sum(card["variant_count"] for card in models) == 52
        # Every card name is a saved product (the cheapest size of its family).
        for card in models:
            detail = get_public_product(card["id"])
            assert detail is not None and detail["price_amount"] == card["price_amount"]
            assert card["price_amount"] == card["price_from"], "card opens its cheapest size"
        # The spares stay individual cards.
        assert len(result["items"]) - len(models) == PARTS_COUNT
        assert all(card["variant_count"] == 1 for card in result["items"] if card not in models)


def test_paging_walks_nineteen_cards_and_counts_each_variant_once(app):
    with app.app_context():
        seed_trailers()
        seed_parts()

        walked = walk(4, product_type="sale")
        assert len(walked) == 19 + PARTS_COUNT
        models = family_cards(walked)
        assert len(models) == 19
        assert sum(card["variant_count"] for card in models) == 52

        # Fourteen pages of four would be 56 slots for 52 cards: the last page
        # carries exactly the remainder, not a padded or duplicated set.
        last = page_public_products(product_type="sale", per_page=4, page=14)
        assert len(last["items"]) == (19 + PARTS_COUNT) - 13 * 4
        assert last["has_next"] is False


def test_category_filter_and_page_totals_count_model_cards(app):
    with app.app_context():
        _ids, categories = seed_trailers()
        seed_parts()
        models = manifest_models()
        expected_by_category = {}
        for item in manifest_items():
            key = categories[f"{item['family']} for sale"]
            expected_by_category.setdefault(key, set()).add(item["source_key"].split("/")[1])

        for category_id, expected_models in expected_by_category.items():
            result = page_public_products(
                product_type="sale", category=str(category_id), per_page=100
            )
            assert result["total"] == len(expected_models)
            walked = walk(2, product_type="sale", category=str(category_id))
            assert len(walked) == len(expected_models)
            assert {card["id"] for card in walked} == {card["id"] for card in result["items"]}

        total_models = sum(
            len(page_public_products(product_type="sale", category=str(key), per_page=100)["items"])
            for key in expected_by_category
        )
        assert total_models == 19

        parts = page_public_products(product_type="sale", category="Spares for sale", per_page=100)
        assert parts["total"] == PARTS_COUNT
        assert all(card["variant_count"] == 1 for card in parts["items"])

        assert page_public_products(product_type="sale", query="no-such-trailer")["total"] == 0


def test_differently_priced_sizes_share_one_card_at_the_lowest_price(app):
    with app.app_context():
        ids, _categories = seed_trailers()
        group = manifest_models()["utility-900"]
        prices = sorted(float(item["price"]) for item in group)
        assert len(set(prices)) > 1, "the fixture must contain differently priced sizes"

        result = page_public_products(product_type="sale", per_page=100)
        cards = [card for card in result["items"] if card["id"] == ids[group[0]["source_key"]]]
        assert len(cards) == 1
        card = cards[0]
        assert card["variant_count"] == len(group)
        assert card["price_varies"] is True
        assert card["price_from"] == pytest.approx(prices[0])
        assert card["price_amount"] == pytest.approx(prices[0])
        assert card["model_family"] == group[0]["model"]
        # No stock is claimed: made-to-order rows carry no on-hand count.
        assert "quantity" not in card and "in_stock" not in card


def test_a_same_name_product_without_a_seed_is_never_folded_in(app):
    with app.app_context():
        ids, _categories = seed_trailers()
        group = manifest_models()["utility-900"]
        decoy = shop_tests.add_product(
            name=group[0]["name"], product_type="sale", price=12345.0
        )
        assert decoy not in set(ids.values())

        result = page_public_products(product_type="sale", per_page=100)
        assert result["total"] == 19 + 1, "the decoy is its own card"
        card = next(card for card in result["items"] if card["id"] == decoy)
        assert card["variant_count"] == 1 and card["model_family"] == ""
        family = next(
            card for card in result["items"] if card["id"] == ids[group[0]["source_key"]]
        )
        assert family["variant_count"] == len(group)
        detail = get_public_product(decoy)
        assert [variant["id"] for variant in detail["model_variants"]] == []


def test_rental_units_are_never_folded_into_a_sale_model_card(app):
    with app.app_context():
        seed_trailers()
        first = shop_tests.add_product(
            name="Hire 900 utility", product_type="rental", price=450.0
        )
        second = shop_tests.add_product(
            name="Hire 900 utility", product_type="rental", price=520.0
        )
        for product_id in (first, second):
            shop_catalogue.save_listing(product_id, {"model_key": "utility-900"})

        rentals = page_public_products(product_type="rental", per_page=100)
        assert rentals["total"] == 2, "a differently priced rental is its own unit"
        # The rental allocation signature is unchanged: different prices never
        # form one compatible hire group.
        assert compatible_public_unit_ids(first) == [first]
        cards = page_public_products(product_type="sale", per_page=100)
        assert all(card["product_type"] == "sale" for card in cards["items"])
        for card in cards["items"]:
            assert card["id"] not in (first, second)


# ---------------------------------------------------------------------------
# detail page: size selector over saved code / dimension / price
# ---------------------------------------------------------------------------


def test_detail_selector_lists_each_sibling_code_size_and_saved_price(app):
    with app.app_context():
        ids, _categories = seed_trailers()
        group = manifest_models()["utility-900"]
        cheapest = min(group, key=lambda item: float(item["price"]))
        product_id = ids[cheapest["source_key"]]

        detail = get_public_product(product_id)
        variants = {variant["id"]: variant for variant in detail["model_variants"]}
        assert set(variants) == {ids[item["source_key"]] for item in group}
        for item in group:
            variant = variants[ids[item["source_key"]]]
            assert variant["code"] == item["code"]
            assert variant["price_amount"] == pytest.approx(float(item["price"]))
            assert variant["label"] == item["size"]
        assert detail["variant_code"] == cheapest["code"]
        assert detail["variant_size"] == cheapest["size"]

    client = app.test_client()
    page = client.get(f"/store/products/{product_id}")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    for item in group:
        assert item["code"] in body, "every sibling code is offered"
    assert f"R{float(cheapest['price']):,.2f}" in body
    for sibling in group:
        if sibling is cheapest:
            continue  # the current size is rendered as the selected option
        assert f"/store/products/{ids[sibling['source_key']]}" in body

    # Choosing another size opens that size's own saved product: its id, price,
    # code and copy — not a relabelled copy of the cheapest one.
    other = max(group, key=lambda item: float(item["price"]))
    other_page = client.get(f"/store/products/{ids[other['source_key']]}")
    other_body = other_page.get_data(as_text=True)
    assert other_page.status_code == 200
    assert other["code"] in other_body
    assert f"R{float(other['price']):,.2f}" in other_body
    assert other["name"] in other_body


def test_selected_size_carries_its_own_photo(app):
    with app.app_context():
        ids, _categories = seed_trailers()
        group = manifest_models()["golf-bike"]
        picked = group[1]
        product_id = ids[picked["source_key"]]
        media_id = shop_catalogue.add_image(
            product_id, shop_tests._Upload("size.png", shop_tests.png_bytes(400, 300))
        )
        assert media_id

    client = app.test_client()
    body = client.get(f"/store/products/{product_id}").get_data(as_text=True)
    assert f"/store/media/{media_id}" in body
    # The sibling shares the family selector but not this photo.
    sibling = ids[group[0]["source_key"]]
    sibling_body = client.get(f"/store/products/{sibling}").get_data(as_text=True)
    assert f"/store/media/{media_id}" not in sibling_body


# ---------------------------------------------------------------------------
# cart uses the selected, actual product
# ---------------------------------------------------------------------------


def public_csrf(client):
    client.get("/store/shop")
    with client.session_transaction() as session:
        return session["_csrf_tokens"]["shop_public"]


def test_cart_stores_the_actual_size_product(app):
    with app.app_context():
        ids, _categories = seed_trailers()
        group = manifest_models()["utility-900"]
        cheapest = min(group, key=lambda item: float(item["price"]))
        chosen = max(group, key=lambda item: float(item["price"]))
        chosen_id = ids[chosen["source_key"]]
        cheapest_id = ids[cheapest["source_key"]]

    client = app.test_client()
    token = public_csrf(client)
    added = client.post(
        f"/store/cart/add/{chosen_id}", data={"quantity": "1", "csrf_token": token}
    )
    assert added.status_code == 302
    with client.session_transaction() as session:
        assert session["shop_cart"] == {str(chosen_id): 1}

    cart = client.get("/store/cart").get_data(as_text=True)
    assert chosen["name"] in cart
    assert cheapest["name"] not in cart
    assert f"R{float(chosen['price']):,.2f}" in cart

    # A second size is a second cart line, because it is a different product.
    client.post(f"/store/cart/add/{cheapest_id}", data={"quantity": "2", "csrf_token": token})
    with client.session_transaction() as session:
        assert session["shop_cart"] == {str(chosen_id): 1, str(cheapest_id): 2}


# ---------------------------------------------------------------------------
# hidden sizes: out of the selector and out of reach
# ---------------------------------------------------------------------------


def test_hidden_size_leaves_the_selector_and_its_url(app):
    with app.app_context():
        ids, _categories = seed_trailers()
        group = manifest_models()["utility-900"]
        cheapest = min(group, key=lambda item: float(item["price"]))
        hidden_id = ids[cheapest["source_key"]]
        next_item = sorted(group, key=lambda item: float(item["price"]))[1]
        set_public(hidden_id, False)

        result = page_public_products(product_type="sale", per_page=100)
        card = next(
            card for card in result["items"] if card["id"] == ids[next_item["source_key"]]
        )
        assert card["variant_count"] == len(group) - 1
        assert card["price_from"] == pytest.approx(float(next_item["price"]))
        assert hidden_id not in {variant["id"] for variant in get_public_product(
            ids[next_item["source_key"]]
        )["model_variants"]}
        assert get_public_product(hidden_id) is None

    client = app.test_client()
    assert client.get(f"/store/products/{hidden_id}").status_code == 404
    token = public_csrf(client)
    assert client.post(
        f"/store/cart/add/{hidden_id}", data={"quantity": "1", "csrf_token": token}
    ).status_code == 404
    assert f'/store/products/{hidden_id}"' not in client.get("/store/shop").get_data(as_text=True)


# ---------------------------------------------------------------------------
# editor keeps 52 rows; staff edits show without a reseed
# ---------------------------------------------------------------------------


def test_editor_still_lists_every_variant_product(app):
    with app.app_context():
        ids, _categories = seed_trailers()
        seeded = set(ids.values())

        page = page_editable_products(product_type="sale", per_page=100)
        assert page["total"] == 52
        assert {row["id"] for row in page["items"]} == seeded
        # Grouping is presentation on the shop page only: no editor row carries a
        # merged family, and each row still has its own saved title and price.
        for row in page["items"]:
            assert row["sale_family"], "the seed is still the durable provenance"
            assert row["store_title"]
            assert row["model_variants"] == []
        walked = []
        page_number = 1
        while True:
            chunk = page_editable_products(product_type="sale", per_page=20, page=page_number)
            walked.extend(row["id"] for row in chunk["items"])
            if not chunk["has_next"]:
                break
            page_number += 1
        assert sorted(walked) == sorted(seeded)


def test_staff_edits_show_on_the_card_and_selector_without_a_reseed(app):
    with app.app_context():
        ids, _categories = seed_trailers()
        group = manifest_models()["utility-900"]
        target = group[1]
        product_id = ids[target["source_key"]]
        db = get_db()
        db.execute("UPDATE products SET price_amount = ? WHERE id = ?", [99_999.0, product_id])
        db.commit()
        shop_catalogue.save_listing(
            product_id,
            {"store_title": "Staff renamed 2.0m utility", "model_key": "", "public_visible": "1"},
        )

        detail = get_public_product(product_id)
        assert detail["store_title"] == "Staff renamed 2.0m utility"
        assert detail["price_amount"] == pytest.approx(99_999.0)
        variant = next(
            entry for entry in detail["model_variants"] if entry["id"] == product_id
        )
        assert variant["price_amount"] == pytest.approx(99_999.0)
        assert variant["title"] == "Staff renamed 2.0m utility"

        result = page_public_products(product_type="sale", per_page=100)
        assert result["total"] == 19, "an edit never clones or removes a card"
        assert db.execute(
            "SELECT COUNT(*) AS c FROM products WHERE product_type = 'sale'"
        ).fetchone()["c"] == 52
        assert db.execute(
            "SELECT COUNT(*) AS c FROM shop_listing_seeds"
        ).fetchone()["c"] == 52
        card = next(
            card for card in result["items"] if card["id"] in {ids[i["source_key"]] for i in group}
        )
        # The edited size is now the dearest, so "from" is another saved size.
        assert card["price_from"] == pytest.approx(
            min(float(item["price"]) for item in group if item is not target)
        )


def test_public_page_size_and_trailer_first_order(app):
    with app.app_context():
        seed_trailers()
        seed_parts()

        first = page_public_products(product_type="sale", page=1)
        assert len(first["items"]) == PUBLIC_PAGE_SIZE
        # Trailer categories sort before spares, so the model cards come first.
        assert family_cards(first["items"]) == first["items"][: len(family_cards(first["items"]))]
