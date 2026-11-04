"""Ticket ABI-341953272 — a retired client cannot take a new booking.

``customers.lifecycle_status`` (added by the sibling change with
``TEXT NOT NULL DEFAULT 'active'``) marks a client record as 'archived'
(withdrawn) or 'deleted' (removed sign-up). Retiring a client must change
nothing about the record itself or its history; it only stops the record being
a candidate for NEW business:

* the New order / Edit order customer picker no longer offers the client;
* the server-side order validation refuses a new booking for the client, even
  when the browser is crafted — while an order that already had the client
  attached (saved before the retirement) still saves and still shows them;
* the public match paths (portal "is this you", duplicate refusal, enquiry
  conversion) never treat a retired row as an existing client, so a fresh
  submission creates a fresh client instead of reviving the retired one.
"""

import os
import tempfile

import pytest

from app import create_app
from app.db import get_db
from app.services import branches as branches_service
from app.services import portal_intake
from app.services.shop_enquiries import resolve_customer


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setattr(portal_intake, "registration_is_open", lambda: True)
    monkeypatch.setattr(portal_intake, "allow_lookup", lambda *_: True)
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    application = create_app({
        "TESTING": True,
        "DATABASE": path,
        "SECRET_KEY": "test",
        "ADMIN_EMAIL": "admin@abi.local",
        "ADMIN_PASSWORD": "admin123",
        "TELEGRAM_NOTIFICATIONS_ENABLED": "",
    })
    yield application
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client):
    with client.application.app_context():
        row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
    return client.post("/login", data={"user_id": str(row["id"]), "password": "admin123"}, follow_redirects=True)


def create_customer(client, name="Lifecycle Test Client", email="lifecycle@example.test", phone="0835550177"):
    response = client.post("/customers/new", data={
        "customer_type": "individual",
        "name": name,
        "email": email,
        "phone": phone,
    }, follow_redirects=True)
    assert b"Customer created" in response.data
    return int(response.request.path.rstrip("/").split("/")[-1])


def set_lifecycle(app, customer_id, status):
    with app.app_context():
        db = get_db()
        db.execute("UPDATE customers SET lifecycle_status = ? WHERE id = ?", (status, customer_id))
        db.commit()


def customer_row(app, customer_id):
    with app.app_context():
        return get_db().execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()


def order_row(app, order_id):
    with app.app_context():
        return get_db().execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def order_count(app):
    with app.app_context():
        return get_db().execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]


def order_payload(customer_id, quantity="1"):
    from werkzeug.datastructures import MultiDict

    return MultiDict([
        ("customer_id", str(customer_id) if customer_id else ""),
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
        ("quantity", quantity),
    ])


def create_order(client, customer_id, quantity="1"):
    response = client.post("/orders/new", data=order_payload(customer_id, quantity), follow_redirects=False)
    assert response.status_code == 302, response.status_code
    return int(response.headers["Location"].rstrip("/").split("/")[-1])


# --------------------------------------------------------------------------- DB

def test_the_lifecycle_column_defaults_to_active(client, app):
    with app.app_context():
        columns = {row["name"]: row for row in get_db().execute("PRAGMA table_info(customers)").fetchall()}
    assert "lifecycle_status" in columns
    assert columns["lifecycle_status"]["notnull"] == 1
    assert str(columns["lifecycle_status"]["dflt_value"]).strip() in ("'active'", '"active"')

    login(client)
    customer_id = create_customer(client)
    # Every existing/new client is untouched by the rule until a record is retired.
    assert customer_row(app, customer_id)["lifecycle_status"] == "active"


# ------------------------------------------------------------------ the picker

def test_new_order_picker_offers_only_active_clients(client, app):
    login(client)
    active_id = create_customer(client, name="Active Picker Client", email="active.picker@example.test", phone="0835550101")
    archived_id = create_customer(client, name="Archived Picker Client", email="archived.picker@example.test", phone="0835550102")
    deleted_id = create_customer(client, name="Deleted Picker Client", email="deleted.picker@example.test", phone="0835550103")
    set_lifecycle(app, archived_id, "archived")
    set_lifecycle(app, deleted_id, "deleted")

    page = client.get("/orders/new").get_data(as_text=True)

    assert "Active Picker Client" in page
    assert "Archived Picker Client" not in page
    assert "Deleted Picker Client" not in page
    # The retired rows are untouched, not deleted: the picker filter is the only change.
    assert customer_row(app, archived_id)["lifecycle_status"] == "archived"
    assert customer_row(app, deleted_id)["lifecycle_status"] == "deleted"
    assert customer_row(app, active_id)["lifecycle_status"] == "active"


# ------------------------------------------------------ server-side validation

@pytest.mark.parametrize("status,message", [
    ("archived", "This client is archived and cannot take a new booking."),
    ("deleted", "This client is deleted and cannot take a new booking."),
])
def test_a_new_order_for_a_retired_client_is_refused(client, app, status, message):
    login(client)
    customer_id = create_customer(client)
    set_lifecycle(app, customer_id, status)

    response = client.post("/orders/new", data=order_payload(customer_id), follow_redirects=True)

    assert response.status_code == 200
    assert message in response.get_data(as_text=True)
    assert order_count(app) == 0


def test_the_service_refuses_a_crafted_new_order_too(client, app):
    """The browser is only the visible half; the service refuses on its own."""
    login(client)
    customer_id = create_customer(client)
    set_lifecycle(app, customer_id, "archived")

    with app.app_context():
        from app.services.orders import create_order as create_order_service

        with pytest.raises(ValueError) as refused:
            create_order_service(order_payload(customer_id))
        assert "archived and cannot take a new booking" in str(refused.value)
        assert get_db().execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"] == 0


# ------------------------------------------- existing orders keep working

def test_an_order_already_raised_survives_the_client_being_archived(client, app):
    login(client)
    customer_id = create_customer(client, name="Retired After Order", email="retired.order@example.test")
    order_id = create_order(client, customer_id)
    set_lifecycle(app, customer_id, "archived")

    detail = client.get(f"/orders/{order_id}")
    assert detail.status_code == 200
    assert "Retired After Order" in detail.get_data(as_text=True)

    edit_page = client.get(f"/orders/{order_id}/edit")
    assert edit_page.status_code == 200
    # The picker no longer lists the client, but the order's own attached client is
    # still named on the form so the order stays editable.
    assert "Retired After Order" in edit_page.get_data(as_text=True)

    saved = client.post(
        f"/orders/{order_id}/edit", data=order_payload(customer_id, quantity="2"), follow_redirects=True
    )
    assert "Order saved" in saved.get_data(as_text=True)
    assert order_row(app, order_id)["customer_id"] == customer_id
    with app.app_context():
        item = get_db().execute("SELECT quantity FROM order_items WHERE order_id = ?", (order_id,)).fetchone()
    assert item["quantity"] == 2


def test_pointing_an_existing_order_at_a_different_retired_client_is_refused(client, app):
    login(client)
    attached_id = create_customer(client, name="Attached Client", email="attached@example.test", phone="0835550111")
    retired_id = create_customer(client, name="Another Retired Client", email="another.retired@example.test", phone="0835550112")
    order_id = create_order(client, attached_id)
    set_lifecycle(app, retired_id, "archived")

    response = client.post(
        f"/orders/{order_id}/edit", data=order_payload(retired_id), follow_redirects=True
    )

    assert "This client is archived and cannot take a new booking." in response.get_data(as_text=True)
    assert order_row(app, order_id)["customer_id"] == attached_id


# ------------------------------------------------------- public match paths

def test_public_match_paths_ignore_a_retired_client(client, app):
    login(client)
    customer_id = create_customer(
        client, name="Public Match Client", email="public.match@example.test", phone="0835550121"
    )
    set_lifecycle(app, customer_id, "archived")

    enquiry = {"email": "public.match@example.test", "phone": "0835550121"}

    with app.app_context():
        assert portal_intake.find_possible_matches("Public Match Client", "0835550121", "public.match@example.test") == []
        assert portal_intake.lookup_public("Public Match Client", "0835550121")["found"] is False
        assert resolve_customer(enquiry) == {"customer_id": None, "ambiguous": False}

        # The same data, reactivated, is matched again — so the assertions above are
        # about the retirement, not about the fixture failing to match at all.
        db = get_db()
        db.execute("UPDATE customers SET lifecycle_status = 'active' WHERE id = ?", (customer_id,))
        db.commit()
        assert [match["customer_id"] for match in
                portal_intake.find_possible_matches("Public Match Client", "0835550121", "public.match@example.test")] == [customer_id]
        assert portal_intake.lookup_public("Public Match Client", "0835550121")["found"] is True
        assert resolve_customer(enquiry)["customer_id"] == customer_id


def test_a_public_portal_submission_creates_a_fresh_client_not_a_revival(client, app):
    login(client)
    with app.app_context():
        branch_id = branches_service.create_branch({"name": "Lifecycle Portal Branch", "active": 1})
        slug = get_db().execute("SELECT public_slug FROM branches WHERE id = ?", (branch_id,)).fetchone()["public_slug"]

    retired_id = create_customer(
        client, name="Retired Portal Client", email="retired.portal@example.test", phone="0835550131"
    )
    set_lifecycle(app, retired_id, "archived")

    submission = {
        "customer_type": "individual",
        "first_names": "New",
        "surname": "Person",
        "name": "New Person",
        "email": "retired.portal@example.test",
        "phone": "0835550131",
        "address_line1": "1 New Road",
        "suburb": "Randburg",
        "city": "Johannesburg",
        "province": "Gauteng",
        "postal_code": "2194",
        "alternative_contact_name": "Contact Person",
        "alternative_contact_number": "0835550132",
        "alternative_contact_relationship": "Friend",
        "popia_consent": "1",
        "own_use_declaration": "individual",
    }
    response = client.post(f"/portal/{slug}/register", data=submission)
    assert response.status_code == 200

    with app.app_context():
        rows = [dict(row) for row in get_db().execute(
            "SELECT id, name, lifecycle_status FROM customers WHERE LOWER(email) = ? ORDER BY id",
            ("retired.portal@example.test",),
        ).fetchall()]
    assert len(rows) == 2
    # The retired record is exactly as it was; the new submission got its own row.
    assert rows[0]["id"] == retired_id and rows[0]["lifecycle_status"] == "archived"
    assert rows[0]["name"] == "Retired Portal Client"
    assert rows[1]["id"] != retired_id and rows[1]["lifecycle_status"] == "active"
    assert rows[1]["name"] == "New Person"
