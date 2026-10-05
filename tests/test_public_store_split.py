"""The public storefront splits into trailer rentals and shop stock.

Someone hiring a trailer and someone buying a jockey wheel are doing different jobs, so the
store page must lead with that split: rentals stay in their category sections, sale and
service items live under "Parts, accessories & services", and neither half leaks into the
other. A rental with no store-visible category still has to be reachable.
"""
import os
import tempfile

import pytest

from app import create_app
from app.db import get_db


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


def make_group(app, name, sort_order=1, becomes_store_visible=1):
    with app.app_context():
        db = get_db()
        cur = db.execute(
            "INSERT INTO product_groups (name, description, active, sort_order, becomes_store_visible, created_at, updated_at) "
            "VALUES (?, ?, 1, ?, ?, datetime('now'), datetime('now'))",
            (name, f"{name} blurb", sort_order, becomes_store_visible),
        )
        db.commit()
        return cur.lastrowid


def make_product(app, name, product_type="rental", group_id=None, price=250.0, unit="day", quantity=2):
    with app.app_context():
        db = get_db()
        cur = db.execute(
            "INSERT INTO products (name, product_type, description, sku, active, public_visible, "
            "price_amount, price_unit, security_deposit, tax_profile_id, product_group_id, quantity, "
            "tracking_method, under_maintenance, created_at) "
            "VALUES (?, ?, '', '', 1, 1, ?, ?, 0, 1, ?, ?, ?, 0, datetime('now'))",
            (name, product_type, price, unit, group_id, quantity,
             "none" if product_type == "service" else "bulk"),
        )
        db.commit()
        return cur.lastrowid


def areas(body):
    """(rentals HTML, shop HTML) — split on the area anchors so a name in one half cannot
    accidentally satisfy an assertion about the other."""
    shop_at = body.index('id="shop"')
    rentals_at = body.index('id="rentals"')
    return body[rentals_at:shop_at], body[shop_at:]


def test_store_splits_rentals_from_shop_stock(app, client):
    group = make_group(app, "Single Axle Trailers")
    make_product(app, "750kg Utility Trailer", group_id=group, price=250.0)
    make_product(app, "LED Trailer Light Kit", product_type="sale", group_id=group, price=349.0, unit="each")
    make_product(app, "Trailer Safety Inspection", product_type="service", group_id=group, price=450.0, unit="service")

    body = client.get("/store").get_data(as_text=True)
    rentals, shop = areas(body)

    assert "750kg Utility Trailer" in rentals
    assert "750kg Utility Trailer" not in shop, "a rental must not appear under the shop half"
    assert "LED Trailer Light Kit" in shop
    assert "LED Trailer Light Kit" not in rentals, "shop stock must not appear under trailer rentals"
    assert "Trailer Safety Inspection" in shop
    assert "Trailer Safety Inspection" not in rentals

    # Each half is labelled for what it is, and the shop side tags what kind of item it is.
    assert "Trailer rentals" in body
    assert "Parts &amp; services" in body
    assert "For sale" in shop
    assert "Service" in shop
    # Shop stock is further split, so a customer buying a part is not scrolling past repairs.
    assert "Parts &amp; accessories" in shop
    assert "Services" in shop
    assert shop.index("Parts &amp; accessories") < shop.index("LED Trailer Light Kit")
    assert shop.index("Trailer Safety Inspection") > shop.index("For sale")

    # Prices pull through on both sides, in shop-friendly form (no cents when whole).
    assert "R250" in rentals
    assert "R349" in shop
    assert "R450" in shop

    # The section header on the rental side shows the cheapest day rate for the category.
    assert "from" in rentals and "R250" in rentals


def test_store_counts_both_halves(app, client):
    group = make_group(app, "Double Axle Trailers")
    make_product(app, "Double Axle Trailer", group_id=group)
    make_product(app, "Jockey Wheel", product_type="sale", group_id=group, price=189.0, unit="each")

    body = client.get("/store").get_data(as_text=True)
    # One rental, one shop item — the hero and the split tabs both advertise the counts.
    assert '<span class="store-split-count">1</span>' in body
    assert body.count('<span class="store-split-count">1</span>') == 2


def test_store_rental_without_a_category_stays_reachable(app, client):
    make_product(app, "Loose Tipping Trailer", group_id=None)

    body = client.get("/store").get_data(as_text=True)
    rentals, shop = areas(body)
    assert "Loose Tipping Trailer" in rentals
    assert "Loose Tipping Trailer" not in shop
    assert "More trailers from our fleet" in rentals


def test_store_hides_only_shop_when_there_are_no_rentals(app, client):
    make_product(app, "LED Trailer Light Kit", product_type="sale", price=349.0, unit="each")

    body = client.get("/store").get_data(as_text=True)
    rentals, shop = areas(body)
    assert "LED Trailer Light Kit" in shop
    assert "No trailers are listed online right now" in rentals
    assert "No products" not in body, "the shop half still has stock, so the store is not empty"
