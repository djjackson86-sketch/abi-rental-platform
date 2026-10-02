import os
import tempfile

import pytest

from app import create_app
from app.db import get_db


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app = create_app({
        "TESTING": True,
        "DATABASE": path,
        "SECRET_KEY": "test",
        "ADMIN_EMAIL": "admin@abi.local",
        "ADMIN_PASSWORD": "admin123",
    })
    yield app
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client):
    with client.application.app_context():
        row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
        assert row is not None
    return client.post("/login", data={"user_id": str(row["id"]), "password": "admin123"}, follow_redirects=True)


def seed_customer_and_product(client):
    client.post("/customers/new", data={
        "customer_type": "individual",
        "name": "Rate Override Customer",
        "email": "rate-override@example.com",
        "phone": "+27000000000",
    }, follow_redirects=True)
    client.post("/inventory/new", data={
        "name": "Rate Override Trailer",
        "sku": "RATE-TRL",
        "quantity": "4",
        "description": "Ticket 341953084 trailer.",
        "product_type": "rental",
        "price_amount": "200",
        "price_unit": "day",
        "security_deposit": "750",
        "tax_profile_id": "1",
        "active": "1",
        "public_visible": "1",
    }, follow_redirects=True)


def create_draft_order(client):
    res = client.post("/orders/new", data={
        "customer_id": "1",
        "product_id": "1",
        "custom_unit_price": "200",
        "quantity": "1",
        "deposit_option": "security_deposit",
        "start_date": "2026-07-01",
        "start_time": "09:00",
        "end_date": "2026-07-02",
        "end_time": "09:00",
    }, follow_redirects=False)
    assert res.status_code == 302
    return int(res.headers["Location"].rstrip("/").split("/")[-1])


def test_edit_order_accepts_catalog_rate_override_deposit_override_and_decimal_quantity(client, app):
    login(client)
    seed_customer_and_product(client)
    order_id = create_draft_order(client)

    edit_page = client.get(f"/orders/{order_id}/edit")
    assert edit_page.status_code == 200
    assert b'Unit defaults from inventory' in edit_page.data
    assert b'name="security_deposit_amount"' in edit_page.data
    assert b'name="quantity" step="0.01" min="0.01"' in edit_page.data

    res = client.post(f"/orders/{order_id}/edit", data={
        "customer_id": "1",
        "product_id": "1",
        "custom_unit_price": "175.00",
        "quantity": "1.5",
        "deposit_option": "security_deposit",
        "security_deposit_amount": "333.33",
        "start_date": "2026-07-01",
        "start_time": "09:00",
        "end_date": "2026-07-02",
        "end_time": "09:00",
        "notes": "Edited rate/deposit/quantity",
    }, follow_redirects=True)
    assert res.status_code == 200
    assert b"Order saved" in res.data

    with app.app_context():
        db = get_db()
        item = db.execute("SELECT quantity, unit_price, line_subtotal, line_tax, line_total FROM order_items WHERE order_id = ?", (order_id,)).fetchone()
        order = db.execute("SELECT subtotal, tax_total, deposit_total, total, due_total FROM orders WHERE id = ?", (order_id,)).fetchone()
    assert item is not None
    assert order is not None

    assert item["quantity"] == pytest.approx(1.5)
    assert item["unit_price"] == pytest.approx(175.0)
    assert item["line_subtotal"] == pytest.approx(262.5)
    assert item["line_tax"] == pytest.approx(0.0)
    assert item["line_total"] == pytest.approx(262.5)
    assert order["subtotal"] == pytest.approx(262.5)
    assert order["tax_total"] == pytest.approx(0.0)
    assert order["deposit_total"] == pytest.approx(333.33)
    assert order["total"] == pytest.approx(595.83)
    assert order["due_total"] == pytest.approx(595.83)

    edited_again = client.get(f"/orders/{order_id}/edit")
    assert edited_again.status_code == 200
    assert b'value="175.0"' in edited_again.data or b'value="175"' in edited_again.data
    assert b'value="1.5"' in edited_again.data
    assert b'value="333.33"' in edited_again.data


def test_blank_security_deposit_override_keeps_inventory_deposit(client, app):
    login(client)
    seed_customer_and_product(client)
    order_id = create_draft_order(client)

    res = client.post(f"/orders/{order_id}/edit", data={
        "customer_id": "1",
        "product_id": "1",
        "custom_unit_price": "200",
        "quantity": "1.25",
        "deposit_option": "security_deposit",
        "security_deposit_amount": "",
        "start_date": "2026-07-01",
        "start_time": "09:00",
        "end_date": "2026-07-02",
        "end_time": "09:00",
    }, follow_redirects=True)
    assert res.status_code == 200

    with app.app_context():
        order = get_db().execute("SELECT subtotal, deposit_total, total FROM orders WHERE id = ?", (order_id,)).fetchone()
    assert order is not None
    assert order["subtotal"] == pytest.approx(250.0)
    assert order["deposit_total"] == pytest.approx(937.5)
    assert order["total"] == pytest.approx(1187.5)
