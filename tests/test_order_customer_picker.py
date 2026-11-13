"""Bounded order-form customer picker (new/edit never embed the client book).

The page used to render every client with a full summary in one HTML document
(5 034 clients / ~9.3 MB on the live book), which OOM-killed a 512 MiB Render
worker. These tests pin the replacement: bounded server-side search, one summary
after an explicit pick, the same guard clauses as before, and a working no-JS
path. They run against an isolated SQLite database created by ``create_app`` —
never the shared Turso/live database.
"""

import json
import os
import tempfile

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from app.db import get_db

DAY = "2026-10-04"
BODY_BUDGET = 200 * 1024


@pytest.fixture(autouse=True)
def isolated_database_env(monkeypatch):
    """Never let an inherited Turso configuration reach these tests."""
    for name in (
        "TURSO_DATABASE_URL",
        "TURSO_AUTH_TOKEN",
        "LIBSQL_URL",
        "LIBSQL_AUTH_TOKEN",
        "DATABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE", ":memory:")


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    application = create_app(
        {
            "TESTING": True,
            "DATABASE": path,
            "SECRET_KEY": "test",
            "ADMIN_EMAIL": "admin@abi.local",
            "ADMIN_PASSWORD": "admin123",
        }
    )
    yield application
    application.config["DATABASE"] = path
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def db(app):
    with client.application.app_context():
        yield get_db()


def login(client):
    with client.application.app_context():
        row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
    response = client.post(
        "/login", data={"user_id": str(row["id"]), "password": "admin123"}, follow_redirects=False
    )
    assert response.status_code in (302, 303)
    return response


def login_staff(client, name, modules):
    with client.application.app_context():
        db = get_db()
        branch = db.execute("SELECT id FROM branches ORDER BY id LIMIT 1").fetchone()
        db.execute(
            """INSERT INTO users (name, email, password_hash, initials, role, branch_id,
                can_view_all_branches, active, modules_json, created_at)
               VALUES (?, ?, ?, 'ST', 'staff', ?, 1, 1, ?, ?)""",
            (name, f"{name.lower().replace(' ', '.')}@example.test",
             generate_password_hash("staff123"), branch["id"] if branch else None,
             json.dumps(modules), f"{DAY}T08:00:00"),
        )
        db.commit()
        user_id = db.execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()["id"]
    response = client.post("/login", data={"user_id": str(user_id), "password": "staff123"},
                           follow_redirects=False)
    assert response.status_code in (302, 303)


def add_customer(db, name, *, email="", phone="", lifecycle="active"):
    db.execute(
        """INSERT INTO customers (customer_type, name, email, phone, marketing_opt_in,
            address_line1, address_line2, suburb, city, province, postal_code, country,
            custom_fields_json, standard_discount_percent, client_verified, is_blocked,
            blocked_reason, created_by_user_id, created_at, lifecycle_status)
           VALUES ('individual', ?, ?, ?, 0, '', '', '', '', '', '', 'South Africa',
                   '{}', 0, 1, 0, '', 1, ?, ?)""",
        (name, email, phone, f"{DAY}T08:00:00", lifecycle),
    )
    return db.execute("SELECT id FROM customers WHERE name = ?", (name,)).fetchone()["id"]


def add_registration(db, customer_id, status):
    db.execute(
        """INSERT INTO portal_registrations (reference, customer_id, name, email, phone,
            status, created_at) VALUES (?, ?, '', '', '', ?, ?)""",
        (f"ref-{customer_id}", customer_id, status, f"{DAY}T08:00:00"),
    )


def add_order(db, customer_id, number="ORD-PICKER-1", *, status="draft"):
    branch = db.execute("SELECT id FROM branches ORDER BY id LIMIT 1").fetchone()
    branch_id = branch["id"] if branch else None
    db.execute(
        """INSERT INTO orders (order_number, customer_id, booking_type, collect_branch_id,
            return_branch_id, status, payment_status, start_at, end_at, subtotal, tax_total,
            deposit_total, total, due_total, notes, created_at)
           VALUES (?, ?, 'return', ?, ?, ?, 'payment_due', ?, ?, 400, 0, 0, 400, 400, '', ?)""",
        (number, customer_id, branch_id, branch_id, status,
         f"{DAY}T09:00:00", f"{DAY}T17:00:00", f"{DAY}T08:00:00"),
    )
    return db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()["id"]


@pytest.fixture()
def book(app):
    """5 000 ordinary clients plus the guard cases the picker must respect."""
    with app.app_context():
        db = get_db()
        db.executemany(
            """INSERT INTO customers (customer_type, name, email, phone, created_at)
               VALUES ('individual', ?, ?, ?, ?)""",
            [
                (f"Client {index:04d}", f"client{index:04d}@example.test",
                 f"071{index:07d}", f"{DAY}T08:00:00")
                for index in range(5000)
            ],
        )
        ids = {
            "zulu": add_customer(db, "Zulu Trailer Hire", email="zulu@example.test", phone="0821112222"),
            "other": add_customer(db, "Alpha Logistics", email="alpha@example.test"),
            "archived": add_customer(db, "Archived Client", lifecycle="archived"),
            "deleted": add_customer(db, "Deleted Lifecycle Client", lifecycle="deleted"),
            "pending": add_customer(db, "Pending Registration Client"),
            "tombstoned": add_customer(db, "Removed Signup Client"),
        }
        add_registration(db, ids["pending"], "pending")
        add_registration(db, ids["tombstoned"], "deleted")
        db.commit()
        return ids


# --- search endpoint ---------------------------------------------------------


def test_search_requires_login_and_a_module(client, book):
    response = client.get("/orders/customer-search?q=zulu")
    assert response.status_code == 302 and "/login" in response.headers["Location"]


def test_search_is_case_insensitive_and_requires_two_characters(client, book):
    login(client)
    assert client.get("/orders/customer-search?q=z").get_json()["customers"] == []
    assert client.get("/orders/customer-search?q= ").get_json()["customers"] == []
    found = client.get("/orders/customer-search?q=zUlU").get_json()["customers"]
    assert [row["name"] for row in found] == ["Zulu Trailer Hire"]
    assert found[0]["id"] == book["zulu"]
    assert found[0]["display"] == "Zulu Trailer Hire — zulu@example.test"


def test_search_is_bounded_to_twenty_rows(client, book):
    login(client)
    payload = client.get("/orders/customer-search?q=client").get_json()
    assert len(payload["customers"]) == 20
    # Identity columns only — no balances/history in a search result.
    assert set(payload["customers"][0]) == {"id", "name", "email", "display"}


@pytest.mark.parametrize("query", ["%", "_", "%%%", "\\", "A" * 500, "cl%ient"])
def test_search_treats_wildcards_and_overlong_queries_as_text(client, book, query):
    login(client)
    response = client.get("/orders/customer-search", query_string={"q": query})
    assert response.status_code == 200
    assert len(response.get_json()["customers"]) <= 20


def test_search_hides_retired_and_tombstoned_clients_but_keeps_pending(client, book):
    login(client)
    for name in ("Archived Client", "Deleted Lifecycle Client", "Removed Signup Client"):
        found = client.get("/orders/customer-search", query_string={"q": name}).get_json()["customers"]
        assert found == [], name
    pending = client.get("/orders/customer-search", query_string={"q": "Pending Registration"}).get_json()["customers"]
    assert [row["id"] for row in pending] == [book["pending"]]


def test_search_endpoint_gated_by_order_form_modules(client, book):
    login_staff(client, "Picker New Order", ["new_order"])
    assert client.get("/orders/customer-search?q=zulu").status_code == 200

    other = client.application.test_client()
    login_staff(other, "Picker Customers Only", ["customers"])
    assert other.get("/orders/customer-search?q=zulu").status_code == 403


# --- summary endpoint --------------------------------------------------------


def test_summary_refuses_retired_client_without_its_order(client, book):
    login(client)
    assert client.get(f"/orders/customer-summary/{book['archived']}").status_code == 404
    assert client.get(f"/orders/customer-summary/{book['tombstoned']}").status_code == 404


def test_summary_allows_retained_client_only_for_its_own_order(client, book):
    with client.application.app_context():
        db = get_db()
        order_id = add_order(db, book["archived"], "ORD-RETAINED")
        other_order_id = add_order(db, book["other"], "ORD-OTHER")
        db.commit()
    login(client)
    retained = client.get(
        f"/orders/customer-summary/{book['archived']}", query_string={"order_id": order_id}
    )
    assert retained.status_code == 200
    assert retained.get_json()["customer"]["id"] == book["archived"]
    # Claiming another order's id does not borrow that allowance.
    assert client.get(
        f"/orders/customer-summary/{book['archived']}", query_string={"order_id": other_order_id}
    ).status_code == 404
    assert client.get(
        f"/orders/customer-summary/{book['archived']}", query_string={"order_id": "999999"}
    ).status_code == 404


def test_summary_returns_pending_client_as_before(client, book):
    login(client)
    response = client.get(f"/orders/customer-summary/{book['pending']}")
    assert response.status_code == 200
    assert response.get_json()["customer"]["id"] == book["pending"]


# --- new order page ----------------------------------------------------------


def test_new_order_page_never_embeds_the_client_book(client, book):
    login(client)
    response = client.get("/orders/new")
    body = response.get_data()
    assert response.status_code == 200
    assert len(body) < BODY_BUDGET, len(body)
    assert b"Client 4999" not in body
    assert body.count(b"data-summary=") == 0


def test_nojs_search_on_new_order_returns_bounded_server_rendered_links(client, book):
    login(client)
    response = client.get("/orders/new", query_string={"q": "client"})
    body = response.get_data()
    assert response.status_code == 200
    assert len(body) < BODY_BUDGET
    assert body.count(b'customer_id=') == 20
    assert b"maximum 20" in body

    found = client.get("/orders/new", query_string={"q": "zulu"}).get_data()
    assert f'?customer_id={book["zulu"]}'.encode() in found


def test_new_order_accepts_a_nojs_selection_by_query_id(client, book):
    login(client)
    body = client.get("/orders/new", query_string={"customer_id": book["zulu"]}).get_data()
    assert f'id="customer-id" value="{book["zulu"]}"'.encode() in body
    assert b"Zulu Trailer Hire" in body
    assert body.count(b"data-summary=") == 1


def test_new_order_drops_a_retired_client_id_from_the_query(client, book):
    login(client)
    body = client.get("/orders/new", query_string={"customer_id": book["archived"]}).get_data()
    assert b'id="customer-id" value=""' in body
    assert b"Archived Client" not in body


# --- edit order page ---------------------------------------------------------


def test_edit_order_page_keeps_its_client_without_the_book(client, book):
    with client.application.app_context():
        db = get_db()
        order_id = add_order(db, book["zulu"], "ORD-EDIT")
        db.commit()
    login(client)
    response = client.get(f"/orders/{order_id}/edit")
    body = response.get_data()
    assert response.status_code == 200
    assert len(body) < BODY_BUDGET
    assert b"Client 4999" not in body
    assert body.count(b"data-summary=") == 1
    assert f'id="customer-id" value="{book["zulu"]}"'.encode() in body


def test_edit_order_nojs_selection_preserves_edit_context(client, book):
    with client.application.app_context():
        db = get_db()
        order_id = add_order(db, book["zulu"], "ORD-EDIT-SWAP")
        db.commit()
    login(client)
    body = client.get(
        f"/orders/{order_id}/edit", query_string={"q": "client", "customer_id": book["other"]}
    ).get_data()
    assert f'id="customer-id" value="{book["other"]}"'.encode() in body
    assert b"Alpha Logistics" in body
    # The pick links still point back at this order, not at /orders/new.
    assert f'/orders/{order_id}/edit?customer_id='.encode() in body


def test_edit_order_shows_a_retained_client_only_on_its_own_order(client, book):
    with client.application.app_context():
        db = get_db()
        retained_order = add_order(db, book["archived"], "ORD-RETAINED-EDIT")
        other_order = add_order(db, book["other"], "ORD-OTHER-EDIT")
        db.commit()
    login(client)
    body = client.get(f"/orders/{retained_order}/edit").get_data()
    assert f'id="customer-id" value="{book["archived"]}"'.encode() in body
    assert b"Archived Client" in body
    # A different order cannot select the retired client, and an invalid pick
    # falls back to that order's own client rather than blanking the form.
    body = client.get(f"/orders/{other_order}/edit", query_string={"customer_id": book["archived"]}).get_data()
    assert f'id="customer-id" value="{book["other"]}"'.encode() in body
    assert b"Archived Client" not in body


# --- existing flows ----------------------------------------------------------


def test_inline_create_and_continue_still_answers_json(client, book):
    login(client)
    response = client.post(
        "/orders/new",
        data={
            "order_action": "create_customer_continue",
            "customer_type": "individual",
            "name": "Inline Picker Client",
            "email": "inline@example.test",
            "phone": "0729998888",
        },
        headers={"X-Requested-With": "fetch", "Accept": "application/json"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["customer"]["name"] == "Inline Picker Client"
    found = client.get("/orders/customer-search", query_string={"q": "Inline Picker"}).get_json()["customers"]
    assert [row["id"] for row in found] == [payload["customer"]["id"]]


def test_nojs_search_and_pick_keep_shop_enquiry_context(client, app, book):
    from test_shop_enquiries import add_branch, add_product, publish_notice, post_enquiry, rental_form
    branch = add_branch(app, "Picker Enquiry")
    product = add_product(app, "Picker Enquiry Trailer", branch_id=branch)
    publish_notice(app)
    response = post_enquiry(client, rental_form(branch, product, request_key="picker-enquiry"))
    with app.app_context():
        enquiry_id = get_db().execute("SELECT id FROM shop_enquiries ORDER BY id DESC LIMIT 1").fetchone()["id"]
    login(client)
    response = client.get("/orders/new", query_string={"enquiry_id": enquiry_id, "q": "Zulu"})
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert f'name="enquiry_id" value="{enquiry_id}"' in body
    assert f'enquiry_id={enquiry_id}&amp;customer_id={book["zulu"]}' in body
