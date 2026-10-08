"""Ticket ABI-341953151 - a per-item Description on order lines.

Requested: a third description box on each order line (below the
"Or custom product/service" box), pre-filled from the inventory product's
Description but editable, stored as a snapshot on the order line, and printed
beneath the main product in normal weight on the order and its documents
(invoice/quote/contract), including the PDFs.

These tests pin:
  * the form carries the new box and the product picker exposes the inventory
    description,
  * a catalogue line snapshots the inventory Description (or the typed value),
  * a custom line keeps its typed description,
  * later inventory edits never mutate the saved snapshot or a re-downloaded PDF,
  * an edit round-trip preserves the description, and
  * the description is drawn on the invoice, quote and contract PDFs.
"""

import os
import re
import tempfile

import pytest
from werkzeug.datastructures import MultiDict

from app import create_app
from app.db import get_db


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    application = create_app({
        "TESTING": True,
        "DATABASE": path,
        "SECRET_KEY": "test",
        "ADMIN_EMAIL": "admin@abi.local",
        "ADMIN_PASSWORD": "admin123",
    })
    yield application
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client):
    with client.application.app_context():
        row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
        assert row is not None
    return client.post("/login", data={"user_id": str(row["id"]), "password": "admin123"}, follow_redirects=True)


def seed_customer(client):
    client.post("/customers/new", data={
        "customer_type": "individual",
        "name": "Description Customer",
        "email": "desc@example.com",
        "phone": "+27000000001",
    }, follow_redirects=True)
    return 1


def seed_product(client, name="Described Trailer", sku="DESC-TRL", description="Inventory description text.", price="100"):
    client.post("/inventory/new", data={
        "name": name,
        "sku": sku,
        "quantity": "3",
        "description": description,
        "product_type": "rental",
        "price_amount": price,
        "price_unit": "day",
        "security_deposit": "500",
        "tax_profile_id": "1",
        "active": "1",
        "public_visible": "1",
    }, follow_redirects=True)
    with client.application.app_context():
        row = get_db().execute("SELECT id FROM products ORDER BY id DESC LIMIT 1").fetchone()
    return row["id"]


def create_order(client, lines, customer_id="1"):
    """POST /orders/new with parallel per-line lists (mirrors the browser form)."""
    data = [
        ("customer_id", customer_id),
        ("deposit_option", "security_deposit"),
        ("start_date", "2026-07-01"), ("start_time", "09:00"),
        ("end_date", "2026-07-02"), ("end_time", "09:00"),
    ]
    for line in lines:
        data.append(("product_id", str(line.get("product_id", ""))))
        data.append(("custom_name", line.get("custom_name", "")))
        data.append(("item_description", line.get("description", "")))
        data.append(("custom_unit_price", str(line.get("price", ""))))
        data.append(("custom_billing_mode", line.get("billing_mode", "fixed")))
        data.append(("quantity", str(line.get("quantity", 1))))
    res = client.post("/orders/new", data=MultiDict(data), follow_redirects=False)
    assert res.status_code == 302, res.status_code
    return int(res.headers["Location"].rstrip("/").split("/")[-1])


def stored_items(app, order_id):
    with app.app_context():
        rows = get_db().execute(
            "SELECT product_id, custom_name, description FROM order_items WHERE order_id = ? ORDER BY id",
            (order_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def create_documents(client, app, order_id, document_types=("invoice", "quote", "contract")):
    documents = {}
    for document_type in document_types:
        created = client.post(f"/orders/{order_id}/documents", data={"document_type": document_type}, follow_redirects=True)
        assert created.status_code == 200
        with app.app_context():
            row = get_db().execute(
                "SELECT id FROM documents WHERE order_id = ? AND document_type = ? ORDER BY id DESC LIMIT 1",
                (order_id, document_type),
            ).fetchone()
        documents[document_type] = row["id"]
    return documents


def drawn_pdf_text(pdf_bytes):
    """All drawn text runs of a PDF, unescaped and joined."""
    chunks = []
    for stream in re.findall(r"stream\r?\n(.*?)\r?\nendstream", pdf_bytes.decode("latin-1"), re.S):
        if " Tj" not in stream:
            continue
        for match in re.finditer(r"\((.*?)\) Tj", stream, re.S):
            chunks.append(re.sub(r"\\([()\\])", r"\1", match.group(1)))
    return "\n".join(chunks)


def test_order_form_has_description_box_and_product_description_data(client):
    login(client)
    seed_product(client, description="Rigid coupling trailer, 750kg GVM.")
    page = client.get("/orders/new")
    assert page.status_code == 200
    html = page.data.decode("utf-8")
    # The third box on each line, wired into the server payload name.
    assert 'name="item_description"' in html
    # The inventory description rides along on the product picker so the JS can fill it.
    assert 'data-description="Rigid coupling trailer, 750kg GVM."' in html


def test_catalog_line_snapshots_typed_description(client, app):
    login(client)
    seed_customer(client)
    product_id = seed_product(client, description="Inventory default description.")
    order_id = create_order(client, [{
        "product_id": product_id,
        "description": "Custom typed description for this hire.",
    }])
    items = stored_items(app, order_id)
    assert len(items) == 1
    assert items[0]["description"] == "Custom typed description for this hire."

    # Order screen shows it beneath the product name.
    detail = client.get(f"/orders/{order_id}").data.decode("utf-8")
    assert "Custom typed description for this hire." in detail
    assert 'class="line-description"' in detail


def test_catalog_line_falls_back_to_inventory_description(client, app):
    login(client)
    seed_customer(client)
    product_id = seed_product(client, description="Inventory default description.")
    order_id = create_order(client, [{"product_id": product_id}])  # no description submitted
    items = stored_items(app, order_id)
    assert items[0]["description"] == "Inventory default description."


def test_custom_line_keeps_its_description(client, app):
    login(client)
    seed_customer(client)
    order_id = create_order(client, [{
        "custom_name": "Site delivery fee",
        "description": "Delivery to the Midrand depot.",
        "price": "250",
    }])
    items = stored_items(app, order_id)
    assert len(items) == 1
    assert items[0]["custom_name"] == "Site delivery fee"
    assert items[0]["description"] == "Delivery to the Midrand depot."


def test_inventory_description_edit_does_not_mutate_snapshot(client, app):
    login(client)
    seed_customer(client)
    product_id = seed_product(client, description="Original inventory description.")
    order_id = create_order(client, [{"product_id": product_id}])
    documents = create_documents(client, app, order_id, ("invoice",))
    before_pdf = drawn_pdf_text(client.get(f"/documents/{documents['invoice']}/download.pdf").data)
    assert "Original inventory description." in before_pdf

    # Someone edits the product description in inventory afterwards.
    with app.app_context():
        get_db().execute("UPDATE products SET description = ? WHERE id = ?", ("CHANGED later in inventory", product_id))
        get_db().commit()

    items = stored_items(app, order_id)
    assert items[0]["description"] == "Original inventory description."

    order_page = client.get(f"/orders/{order_id}").data.decode("utf-8")
    assert "Original inventory description." in order_page
    assert "CHANGED later in inventory" not in order_page

    # A freshly re-downloaded PDF still prints the snapshot, not the new product text.
    after_pdf = drawn_pdf_text(client.get(f"/documents/{documents['invoice']}/download.pdf").data)
    assert "Original inventory description." in after_pdf
    assert "CHANGED later in inventory" not in after_pdf


def test_edit_order_preserves_item_description(client, app):
    login(client)
    seed_customer(client)
    product_id = seed_product(client, description="Inventory default description.")
    order_id = create_order(client, [{"product_id": product_id, "description": "Kept through edits."}])

    # The edit form re-renders the stored snapshot in the box.
    edit_page = client.get(f"/orders/{order_id}/edit").data.decode("utf-8")
    assert 'value="Kept through edits."' in edit_page

    # A round-trip save (what the browser posts back) keeps it.
    res = client.post(f"/orders/{order_id}/edit", data=MultiDict([
        ("customer_id", "1"),
        ("product_id", str(product_id)),
        ("custom_name", ""),
        ("item_description", "Kept through edits."),
        ("custom_unit_price", "100"),
        ("custom_billing_mode", "fixed"),
        ("quantity", "1"),
        ("deposit_option", "security_deposit"),
        ("start_date", "2026-07-01"), ("start_time", "09:00"),
        ("end_date", "2026-07-02"), ("end_time", "09:00"),
    ]), follow_redirects=True)
    assert res.status_code == 200
    items = stored_items(app, order_id)
    assert items[0]["description"] == "Kept through edits."


def test_description_prints_on_invoice_quote_and_contract_pdfs(client, app):
    login(client)
    seed_customer(client)
    product_id = seed_product(client, description="Inventory default description.")
    order_id = create_order(client, [{"product_id": product_id, "description": "Ticket151 pdf snapshot"}])
    documents = create_documents(client, app, order_id, ("invoice", "quote", "contract"))

    for document_type in ("invoice", "quote", "contract"):
        pdf = client.get(f"/documents/{documents[document_type]}/download.pdf").data
        assert pdf.startswith(b"%PDF-")
        assert "Ticket151 pdf snapshot" in drawn_pdf_text(pdf), document_type

    # On-screen document page shows it too (parity with the PDF).
    doc_page = client.get(f"/documents/{documents['invoice']}").data.decode("utf-8")
    assert "Ticket151 pdf snapshot" in doc_page
    assert 'class="line-description"' in doc_page
