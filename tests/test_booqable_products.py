"""Tests for the Booqable product (inventory) mapping - 2026-09-12."""
from app.services.booqable_import import (
    map_product_row, parse_stock_identifier, validate_product,
)

TRAILER = {
    "id": "63c5d7c0-d017-47f2-9f0d-cfc103daf9d0",
    "name": "1.8m Luggage Trailer ",
    "product_type": "rental",
    "price_type": "structure",
    "sku": "1_8M_LUGGAGE_TRAILER",
    "quantity": "1",
    "bulk": "false",
    "stock_identifier": "054 - MS 26 CS GP",
    "status": "in_stock",
    "base_price_as_decimal": "245.0",
    "deposit_as_decimal": "200.0",
    "description": "",
}

SALES = {
    "id": "65b3d15b-ab29-4351-96e4-18846f531dea",
    "name": "13 to 7 Pin Adapter",
    "product_type": "sales_item",
    "sku": "13_TO_7_PIN_ADAPTER",
    "quantity": "9",
    "bulk": "true",
    "stock_identifier": "",
    "status": "",
    "base_price_as_decimal": "150.0",
    "deposit_as_decimal": "0.0",
    "description": "",
}

SERVICE = {
    "id": "40850cb1-eb1a-4f3c-b051-d9f207fded3c",
    "name": "Installation of 7 Pin Sockets",
    "product_type": "service",
    "sku": "INSTALLATION_OF_7_PIN_SOCKETS",
    "quantity": "0",
    "bulk": "true",
    "stock_identifier": "",
    "status": "",
    "base_price_as_decimal": "85.0",
    "deposit_as_decimal": "0.0",
    "description": "",
}


# --------------------------------------------------------------------------- #
# stock identifier parsing
# --------------------------------------------------------------------------- #

def test_stock_identifier_splits_into_fleet_number_and_plate():
    assert parse_stock_identifier("054 - MS 26 CS GP") == ("054", "MS 26 CS", "GP")
    assert parse_stock_identifier(" 042 - MP 55 DY GP") == ("042", "MP 55 DY", "GP")


def test_stock_identifier_tolerates_the_variants_in_the_real_file():
    # missing space, trailing suffix, no dash, double space
    assert parse_stock_identifier("061 -MY 80 GR GP-1") == ("061", "MY 80 GR", "GP")
    assert parse_stock_identifier("058 - KF  39 PR GP") == ("058", "KF 39 PR", "GP")
    assert parse_stock_identifier("063- MZ 43 PY GP") == ("063", "MZ 43 PY", "GP")
    assert parse_stock_identifier("037 - KR 90 ZG GP-1") == ("037", "KR 90 ZG", "GP")


def test_blank_stock_identifier_is_none():
    assert parse_stock_identifier("") is None
    assert parse_stock_identifier(None) is None


# --------------------------------------------------------------------------- #
# product mapping
# --------------------------------------------------------------------------- #

def test_trailer_becomes_one_individual_product_with_the_plate_as_sku():
    mapped = map_product_row(TRAILER, tax_profile_id=4)
    assert mapped["product_type"] == "rental"
    assert mapped["tracking_method"] == "individual"
    assert mapped["sku"] == "054 - MS 26 CS GP"
    assert mapped["quantity"] == 1
    assert mapped["price_amount"] == 245.0
    assert mapped["price_unit"] == "day"
    assert mapped["security_deposit"] == 200.0
    assert mapped["name"] == "1.8m Luggage Trailer"       # trailing space trimmed
    assert mapped["_fleet_no"] == "054"
    assert mapped["_plate"] == "MS 26 CS"


def test_branch_is_left_blank_by_owner_decision():
    for row in (TRAILER, SALES, SERVICE):
        assert map_product_row(row, tax_profile_id=4)["branch_id"] is None


def test_sales_item_keeps_its_own_sku_and_stock_quantity():
    mapped = map_product_row(SALES, tax_profile_id=4)
    assert mapped["product_type"] == "sale"
    assert mapped["tracking_method"] == "bulk"
    assert mapped["sku"] == "13_TO_7_PIN_ADAPTER"
    assert mapped["quantity"] == 9
    assert mapped["price_unit"] == "fixed"


def test_service_never_carries_stock():
    mapped = map_product_row(SERVICE, tax_profile_id=4)
    assert mapped["product_type"] == "service"
    assert mapped["quantity"] == 0


def test_negative_quantity_is_clamped_to_zero():
    mapped = map_product_row(dict(SALES, name="Thirst Mineral Water - Still ", quantity="-1"),
                             tax_profile_id=4)
    assert mapped["quantity"] == 0
    assert mapped["_raw_qty"] == -1          # raw value kept for the report


def test_price_unit_is_day_for_rentals_and_fixed_otherwise():
    assert map_product_row(TRAILER, tax_profile_id=1)["price_unit"] == "day"
    assert map_product_row(SALES, tax_profile_id=1)["price_unit"] == "fixed"
    assert map_product_row(SERVICE, tax_profile_id=1)["price_unit"] == "fixed"


def test_source_keys_are_recorded_for_idempotent_reruns():
    mapped = map_product_row(SALES, tax_profile_id=4)
    assert mapped["source_system"] == "booqable"
    assert mapped["source_id"] == SALES["id"]


def test_each_trailer_gets_a_unique_row_key():
    """Booqable gives every unit of a product the SAME product id, so the row key
    must include the unit - otherwise a unique index silently drops 63 of 87 rows."""
    a = map_product_row(dict(TRAILER, stock_identifier="054 - MS 26 CS GP"), tax_profile_id=4)
    b = map_product_row(dict(TRAILER, stock_identifier="036 - KR 90 ZB GP"), tax_profile_id=4)
    assert a["source_id"] != b["source_id"]
    assert a["source_id"] == f'{TRAILER["id"]}:054 - MS 26 CS GP'
    assert a["_parent_source_id"] == TRAILER["id"] == b["_parent_source_id"]


def test_bulk_rows_keep_the_plain_product_id():
    assert map_product_row(SALES, tax_profile_id=4)["source_id"] == SALES["id"]


def test_products_are_active_and_public_by_default():
    mapped = map_product_row(TRAILER, tax_profile_id=4)
    assert mapped["active"] == 1
    assert mapped["public_visible"] == 1


def test_hourly_extra_rate_is_not_invented():
    assert map_product_row(TRAILER, tax_profile_id=4)["hourly_extra_rate"] == 0.0


def test_validation_flags_a_missing_name():
    assert "missing name" in validate_product(map_product_row(dict(TRAILER, name=" "), 4))


def test_validation_passes_a_good_row():
    assert validate_product(map_product_row(TRAILER, tax_profile_id=4)) == []
