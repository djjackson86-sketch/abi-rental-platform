"""Ticket ABI-341953333 - internal notes live on the ORDER page, not Edit order.

The client asked for the order's internal notes to be visible the moment an order
is opened and editable there, instead of only inside the full "Edit order" form.

What must hold after the move:

* the order page shows the stored note (or says there is none) and can save a new
  one or clear it;
* the narrow notes endpoint touches ``orders.notes`` ONLY - never totals,
  payments or status;
* an ordinary Edit-save (which no longer submits a notes field at all) carries
  the stored note forward instead of blanking it - the data-loss risk of this
  ticket;
* the Edit page no longer renders the notes box, while the NEW order form still
  accepts a note at creation;
* staff-only: the note is escaped on the page, never printed on a customer
  document, and a branch outsider / signed-out caller gets nothing.
"""
import json

import pytest

from app.db import get_db
from test_ticket_341953282_dashboard import TODAY, add_depot_user, app, client, login


def seed_order(app, notes="", status="reserved", branch_id=1, number="ORD-NOTES-1"):
    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO customers (name, branch_id, created_at) VALUES ('Notes Customer', ?, ?)",
            (branch_id, TODAY),
        )
        customer_id = db.execute(
            "SELECT id FROM customers WHERE name = 'Notes Customer'"
        ).fetchone()["id"]
        db.execute(
            """INSERT INTO orders (order_number, customer_id, booking_type, collect_branch_id,
               return_branch_id, status, payment_status, start_at, end_at, subtotal, tax_total,
               deposit_total, total, due_total, notes, created_at)
               VALUES (?, ?, 'return', ?, ?, ?, 'payment_due', ?, ?, 100, 0, 0, 100, 100, ?, ?)""",
            (number, customer_id, branch_id, branch_id, status, TODAY, TODAY, notes, TODAY),
        )
        order_id = db.execute(
            "SELECT id FROM orders WHERE order_number = ?", (number,)
        ).fetchone()["id"]
        db.commit()
    return {"order_id": order_id, "customer_id": customer_id}


def order_row(app, order_id):
    with app.app_context():
        return get_db().execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def order_payload(customer_id):
    """A complete edit-order form - deliberately WITHOUT any "notes" field."""
    from werkzeug.datastructures import MultiDict

    return MultiDict([
        ("customer_id", str(customer_id)),
        ("booking_type", "return"),
        ("collect_branch_id", "1"),
        ("return_branch_id", "1"),
        ("deposit_option", "security_deposit"),
        ("start_date", "2026-07-01"),
        ("start_time", "09:00"),
        ("end_date", "2026-07-03"),
        ("end_time", "15:00"),
        ("product_id", ""),
        ("custom_name", "Single axle trailer hire"),
        ("custom_unit_price", "100"),
        ("custom_billing_mode", "fixed"),
        ("quantity", "1"),
    ])


# ------------------------------------------------------------- visible on the page

def test_the_order_page_shows_the_stored_note(client, app):
    ids = seed_order(app, notes="Trailer needs a new coupler before pickup.")
    login(client)
    page = client.get(f"/orders/{ids['order_id']}").get_data(as_text=True)

    assert "Internal notes" in page
    assert "Trailer needs a new coupler before pickup." in page
    assert f'action="/orders/{ids["order_id"]}/notes"' in page
    assert '<textarea name="notes"' in page


def test_an_order_without_a_note_says_so(client, app):
    ids = seed_order(app)
    login(client)
    page = client.get(f"/orders/{ids['order_id']}").get_data(as_text=True)

    assert "No internal notes yet." in page


def test_a_note_is_escaped_on_the_order_page(client, app):
    ids = seed_order(app, notes="<script>alert('x')</script> call the depot")
    login(client)
    page = client.get(f"/orders/{ids['order_id']}").get_data(as_text=True)

    assert "<script>alert('x')</script>" not in page
    assert "&lt;script&gt;" in page


# --------------------------------------------------------------- editing the note

def test_saving_notes_from_the_order_page_updates_and_redisplays(client, app):
    ids = seed_order(app)
    order_id = ids["order_id"]
    login(client)

    response = client.post(
        f"/orders/{order_id}/notes",
        data={"notes": "  Customer pays the balance on collection.  "},
        follow_redirects=True,
    )
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "Internal notes saved" in page
    assert "Customer pays the balance on collection." in page

    # Stored trimmed, exactly as the edit form always stored it.
    row = order_row(app, order_id)
    assert row["notes"] == "Customer pays the balance on collection."

    # A notes-only save must not move money, status or payments.
    assert row["status"] == "reserved"
    assert row["total"] == 100
    assert row["due_total"] == 100
    with app.app_context():
        payments = get_db().execute(
            "SELECT COUNT(*) AS c FROM payments WHERE order_id = ?", (order_id,)
        ).fetchone()["c"]
    assert payments == 0


def test_saving_an_empty_box_clears_the_note(client, app):
    ids = seed_order(app, notes="Old internal note")
    order_id = ids["order_id"]
    login(client)

    response = client.post(
        f"/orders/{order_id}/notes", data={"notes": ""}, follow_redirects=True
    )
    assert "Internal notes saved" in response.get_data(as_text=True)
    assert order_row(app, order_id)["notes"] == ""
    assert "No internal notes yet." in response.get_data(as_text=True)


def test_notes_cannot_be_written_without_signing_in(client, app):
    ids = seed_order(app, notes="Guarded note")
    response = client.post(f"/orders/{ids['order_id']}/notes", data={"notes": "anon"})
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    assert order_row(app, ids["order_id"])["notes"] == "Guarded note"


# --------------------------------------------------- the data-loss guard (critical)

def test_edit_save_without_a_notes_field_keeps_the_stored_note(client, app):
    ids = seed_order(app, notes="Keep me through an edit-save")
    order_id = ids["order_id"]
    login(client)

    response = client.post(
        f"/orders/{order_id}/edit", data=order_payload(ids["customer_id"])
    )
    assert response.status_code == 302, response.status_code

    # The edit really ran (items rebuilt), and the absent notes field did not wipe.
    row = order_row(app, order_id)
    assert row["notes"] == "Keep me through an edit-save"
    assert row["total"] == 115  # R100 hire + 15% VAT: the payload really rebuilt
    with app.app_context():
        items = get_db().execute(
            "SELECT custom_name FROM order_items WHERE order_id = ?", (order_id,)
        ).fetchall()
    assert [item["custom_name"] for item in items] == ["Single axle trailer hire"]

    assert "Keep me through an edit-save" in client.get(f"/orders/{order_id}").get_data(as_text=True)


def test_an_explicitly_cleared_notes_field_still_clears(client, app):
    """The guard must not protect a CLEARED note submitted by a full form."""
    ids = seed_order(app, notes="Remove me if a form says so")
    order_id = ids["order_id"]
    login(client)

    form = order_payload(ids["customer_id"])
    form["notes"] = ""
    client.post(f"/orders/{order_id}/edit", data=form)

    assert order_row(app, order_id)["notes"] == ""


# ------------------------------------------------------------ the edit-page surface

def test_the_edit_page_no_longer_carries_the_notes_box(client, app):
    ids = seed_order(app, notes="Only on the order page now")
    login(client)

    edit_page = client.get(f"/orders/{ids['order_id']}/edit").get_data(as_text=True)
    assert '<textarea name="notes"' not in edit_page
    assert "Only on the order page now" not in edit_page

    # A note can still be seeded when the order is first raised.
    new_page = client.get("/orders/new").get_data(as_text=True)
    assert '<textarea name="notes"' in new_page


# ------------------------------------------------------------------- staff-only

def test_a_branch_outsider_cannot_read_or_write_notes(client, app):
    ids = seed_order(app, notes="Branch one only")
    add_depot_user(app, "Notes Depot Two", 2)
    with app.app_context():
        db = get_db()
        db.execute(
            "UPDATE users SET modules_json = ? WHERE name = 'Notes Depot Two'",
            (json.dumps(["dashboard", "orders"]),),
        )
        db.commit()

    login(client, "Notes Depot Two", password="staff123")
    assert client.get(f"/orders/{ids['order_id']}").status_code == 404
    assert client.post(
        f"/orders/{ids['order_id']}/notes", data={"notes": "hijacked"}
    ).status_code == 404
    assert order_row(app, ids["order_id"])["notes"] == "Branch one only"


def test_an_internal_note_never_prints_on_a_customer_document(app):
    import pymupdf

    from app.services.documents import create_document
    from app.services.pdf_documents import document_pdf_bytes

    ids = seed_order(app, notes="INTERNAL-ONLY-MARKER do not print")
    with app.app_context():
        document_id = create_document(ids["order_id"], "quote")
        data = document_pdf_bytes(document_id)
        text = "".join(
            page.get_text() for page in pymupdf.open(stream=data, filetype="pdf")
        )
    assert "INTERNAL-ONLY-MARKER" not in text
