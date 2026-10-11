"""Client portal changes — self-registered customers awaiting verification (backend).

The owner's approved changes, pinned end to end on the backend:

* the public form's **required** fields (name, email, phone, address line 1, suburb, city,
  province, postal code and the three alternative-contact fields) are enforced server-side,
  not just by the red asterisk; **country** and the **vehicle** fields are gone from the
  public surface (country defaults to South Africa internally, a crafted value is ignored,
  and vehicle make/colour/reg are never stored from a public post);
* ``website`` is a **company-only** detail — accepted for a company, dropped for an
  individual;
* a submission that creates a **new** client records a **pending registration** (branch =
  the portal link's branch, so Midrand stays Midrand in the customer record and the Telegram
  message). The client does **not** behave as an approved client — it is hidden from the
  default customer list, the order/allocation picker and the counts — until staff approve it;
* a submission that **links** to an existing client records nothing pending, so an
  already-verified client is never downgraded;
* approval is **explicit** and only when the client is ID-verified (``client_verified = 1``)
  **and** has a recorded **main** vehicle — in either order — via a scoped-CSRF,
  branch-checked ``customers.approve_portal_registration`` endpoint. A submission or a scan
  never approves anyone on its own;
* no ID image, ID number or OCR data is stored anywhere.

Fixtures are synthetic. Notifications are disabled in these tests (no message is ever sent);
the one test that inspects the Telegram payload captures the built text, it does not send it.
"""

import json

import pytest

from app import create_app
from app.db import get_db
from app.services import branches as branches_service
from app.services import portal_intake, portal_registrations
from app.services.customers import create_customer, find_customer_id_by_text, get_customer, list_customers, search_customers


def build_app(database_path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": database_path,
            "SECRET_KEY": "test",
            "ADMIN_EMAIL": "admin@abi.local",
            "ADMIN_PASSWORD": "admin123",
            "TELEGRAM_NOTIFICATIONS_ENABLED": "",
        }
    )


@pytest.fixture()
def app(tmp_path):
    return build_app(str(tmp_path / "pending.sqlite"))


@pytest.fixture()
def client(app, monkeypatch):
    monkeypatch.setattr(portal_intake, "registration_is_open", lambda: True)
    return app.test_client()


@pytest.fixture()
def branches(app):
    with app.app_context():
        return {
            "midrand": branches_service.create_branch({"name": "Midrand Depot", "active": 1}),
            "roodepoort": branches_service.create_branch({"name": "Roodepoort Depot", "active": 1}),
        }


def submission(**overrides):
    """A complete public submission — every required field present."""
    data = {
        "name": "Pieter van Wyk",
        "email": "Pieter@Example.CO.ZA",
        "phone": "083 555 1234",
        "address_line1": "12 Kerk Street",
        "suburb": "Randburg",
        "city": "Johannesburg",
        "province": "Gauteng",
        "postal_code": "2194",
        "alternative_contact_name": "Jane Smith",
        "alternative_contact_number": "082 111 2222",
        "alternative_contact_relationship": "Sister",
        "popia_consent": "1",
    }
    data.update(overrides)
    return data


def slug_of(app, branch_id):
    with app.app_context():
        return get_db().execute("SELECT public_slug FROM branches WHERE id = ?", (branch_id,)).fetchone()["public_slug"]


def register(client, app, branch_id, **overrides):
    return client.post(f"/portal/{slug_of(app, branch_id)}/register", data=submission(**overrides))


def rows(app, sql, params=()):
    with app.app_context():
        return [dict(row) for row in get_db().execute(sql, params).fetchall()]


def customer_by_email(app, email):
    found = rows(app, "SELECT * FROM customers WHERE email = ?", (email,))
    return found[0] if found else None


def registration_for(app, customer_id):
    found = rows(app, "SELECT * FROM portal_registrations WHERE customer_id = ?", (customer_id,))
    return found[0] if found else None


def owner_id(app):
    with app.app_context():
        return get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()["id"]


def sign_in(client, app, *, role="owner", branch_id=None, branch_ids=(), modules=(), can_view_all=True):
    with client.session_transaction() as session:
        session["user_id"] = owner_id(app)
        session["user_role"] = role
        session["user_name"] = "Synthetic Tester"
        session["initials"] = "ST"
        session["branch_id"] = branch_id
        session["can_view_all_branches"] = can_view_all
        session["branch_ids"] = list(branch_ids)
        session["staff_modules"] = list(modules)


def csrf_token(client, scope="portal_approval"):
    with client.session_transaction() as session:
        return (session.get("_csrf_tokens") or {}).get(scope)


# ---------------------------------------------------------------- required fields (server-side)

REQUIRED = (
    "name", "email", "phone", "address_line1", "suburb", "city", "province", "postal_code",
    "alternative_contact_name", "alternative_contact_number", "alternative_contact_relationship",
)


@pytest.mark.parametrize("field", REQUIRED)
def test_every_required_field_is_enforced_server_side(client, app, branches, field):
    response = register(client, app, branches["midrand"], **{field: "   "})
    assert response.status_code == 400, field
    assert rows(app, "SELECT * FROM customers") == []
    assert rows(app, "SELECT * FROM portal_registrations") == []


def test_a_complete_submission_is_accepted(client, app, branches):
    response = register(client, app, branches["midrand"])
    assert response.status_code == 200
    stored = customer_by_email(app, "pieter@example.co.za")
    assert stored is not None
    assert stored["suburb"] == "Randburg"
    custom = json.loads(stored["custom_fields_json"] or "{}")
    assert custom["alternative_contact_name"] == "Jane Smith"
    assert custom["alternative_contact_number"] == "082 111 2222"
    assert custom["alternative_contact_relationship"] == "Sister"


# ---------------------------------------------------------------- removed fields are ignored

def test_a_crafted_country_is_ignored_and_the_record_defaults_to_south_africa(client, app, branches):
    register(client, app, branches["midrand"], country="Zimbabwe")
    stored = customer_by_email(app, "pieter@example.co.za")
    assert stored["country"] == "South Africa", "a posted country must not reach the record"


def test_crafted_vehicle_fields_are_ignored(client, app, branches):
    register(
        client, app, branches["midrand"],
        vehicle_make="Toyota Hilux", vehicle_color="White", vehicle_reg_no="CA 123-456",
    )
    stored = customer_by_email(app, "pieter@example.co.za")
    custom = json.loads(stored["custom_fields_json"] or "{}")
    for key in ("vehicle_make", "vehicle_color", "vehicle_reg_no"):
        assert key not in custom, f"a public post must not store {key}"


def test_website_is_company_only(client, app, branches):
    register(client, app, branches["midrand"], website="http://individual.example")
    individual = customer_by_email(app, "pieter@example.co.za")
    assert "website" not in json.loads(individual["custom_fields_json"] or "{}")

    register(
        client, app, branches["midrand"], customer_type="company",
        name="Sano Holdings", email="info@sanoholdings.example", phone="083 555 9876",
        website="http://sanoholdings.example",
    )
    company = customer_by_email(app, "info@sanoholdings.example")
    assert json.loads(company["custom_fields_json"] or "{}")["website"] == "http://sanoholdings.example"


# ---------------------------------------------------------------- pending on create, never on link

def test_a_new_submission_records_a_pending_registration_on_the_branch(client, app, branches):
    register(client, app, branches["midrand"])
    stored = customer_by_email(app, "pieter@example.co.za")
    registration = registration_for(app, stored["id"])
    assert registration is not None
    assert registration["status"] == "pending"
    assert registration["branch_id"] == branches["midrand"]
    assert registration["reference"]
    assert stored["branch_id"] == branches["midrand"], "the client keeps the portal branch (Midrand)"


def test_linking_to_an_existing_verified_client_records_nothing_pending(client, app, branches):
    with app.app_context():
        existing = create_customer({"name": "Charmaine Mokoena", "phone": "0821234567", "email": "charmaine@example.co.za"})
        db = get_db()
        db.execute("UPDATE customers SET client_verified = 1 WHERE id = ?", (existing,))
        db.commit()
    register(
        client, app, branches["midrand"], name="Charmaine Mokoena", phone="082 123 4567",
        decision=f"link:{existing}",
    )
    assert registration_for(app, existing) is None, "a link must not make an existing client pending"
    with app.app_context():
        assert get_customer(existing)["client_verified"] == 1, "an already-verified client is never downgraded"
        assert [c["id"] for c in list_customers()] == [existing], "and stays visible in the default list"


def test_existing_clients_with_no_registration_are_never_pending(app):
    with app.app_context():
        created = create_customer({"name": "Legacy Walk-in", "phone": "0830000000"})
        assert [c["id"] for c in list_customers()] == [created]
        assert portal_registrations.pending_registration_for_customer(created) is None


# ---------------------------------------------------------------- hidden until approved

def test_a_pending_client_is_hidden_from_lists_and_selection_until_approved(client, app, branches):
    register(client, app, branches["midrand"])
    stored = customer_by_email(app, "pieter@example.co.za")
    with app.app_context():
        assert list_customers() == [], "a pending sign-up must not show as an approved client"
        assert search_customers("Pieter") == []
        assert find_customer_id_by_text("Pieter van Wyk") is None
        from app.services.customers import customer_counts
        assert customer_counts()["total"] == 0
        # ...but the staff review path still reaches the record directly.
        assert get_customer(stored["id"])["name"] == "Pieter van Wyk"


def test_the_pending_queue_exposes_the_contract_shape(client, app, branches):
    register(client, app, branches["midrand"])
    stored = customer_by_email(app, "pieter@example.co.za")
    with app.app_context():
        queue = portal_registrations.list_pending_registrations()
    assert len(queue) == 1
    item = queue[0]
    for key in ("id", "reference", "customer_id", "name", "email", "phone", "branch_name", "created_at"):
        assert key in item
    assert item["customer_id"] == stored["id"]
    assert item["name"] == "Pieter van Wyk"
    assert item["email"] == "pieter@example.co.za"
    assert item["phone"] == "083 555 1234"
    assert item["branch_name"] == "Midrand Depot"


def test_the_pending_queue_is_branch_scoped(client, app, branches):
    register(client, app, branches["midrand"])
    register(
        client, app, branches["roodepoort"], name="Ann Other", email="ann@example.co.za", phone="084 111 2222",
    )

    def queue_for(role, branch_id, modules, can_view_all):
        from flask import session as flask_session
        with app.test_request_context("/customers"):
            flask_session["user_id"] = owner_id(app)
            flask_session["user_role"] = role
            flask_session["branch_id"] = branch_id
            flask_session["branch_ids"] = []
            flask_session["can_view_all_branches"] = can_view_all
            flask_session["staff_modules"] = list(modules)
            return portal_registrations.list_pending_registrations()

    # Owner (all branches) sees both.
    assert len(queue_for("owner", None, [], True)) == 2
    # A staff account scoped to Midrand sees only Midrand's.
    scoped = queue_for("staff", branches["midrand"], ["customers"], False)
    assert [item["branch_name"] for item in scoped] == ["Midrand Depot"]
    # ...and a staff account scoped to Roodepoort sees only Roodepoort's.
    other = queue_for("staff", branches["roodepoort"], ["customers"], False)
    assert [item["branch_name"] for item in other] == ["Roodepoort Depot"]


# ---------------------------------------------------------------- approval: explicit, both checks

def _set_id_verified(client, app, customer_id, email="pieter@example.co.za", name="Pieter van Wyk"):
    return client.post(
        f"/customers/{customer_id}/edit",
        data={
            "name": name, "email": email, "phone": "083 555 1234",
            "address_line1": "12 Kerk Street", "suburb": "Randburg", "city": "Johannesburg",
            "province": "Gauteng", "postal_code": "2194", "client_verified": "1",
        },
    )


def _capture_main_vehicle(client, customer_id, registration="NB72XMGP"):
    return client.post(
        "/scan-vehicle/save",
        data={"customer_id": str(customer_id), "registration": registration, "make": "Toyota", "make_main": "yes"},
    )


def test_a_submission_or_scan_never_approves_on_its_own(client, app, branches):
    register(client, app, branches["midrand"])
    stored = customer_by_email(app, "pieter@example.co.za")
    registration = registration_for(app, stored["id"])
    assert registration["status"] == "pending"
    with app.app_context():
        assert list_customers() == []
        with pytest.raises(ValueError):
            portal_registrations.approve_registration(registration["id"])
        assert list_customers() == [], "a refused approval changes nothing"


@pytest.mark.parametrize("order", ["id_first", "vehicle_first"])
def test_approval_requires_id_and_main_vehicle_in_either_order(client, app, branches, order):
    register(client, app, branches["midrand"])
    stored = customer_by_email(app, "pieter@example.co.za")
    registration = registration_for(app, stored["id"])
    sign_in(client, app, role="owner", modules=[])

    with app.app_context():
        with pytest.raises(ValueError):
            portal_registrations.approve_registration(registration["id"])

    if order == "id_first":
        assert _set_id_verified(client, app, stored["id"]).status_code in (302, 200)
        with app.app_context():
            with pytest.raises(ValueError):
                portal_registrations.approve_registration(registration["id"])
        _capture_main_vehicle(client, stored["id"])
    else:
        _capture_main_vehicle(client, stored["id"])
        with app.app_context():
            with pytest.raises(ValueError):
                portal_registrations.approve_registration(registration["id"])
        _set_id_verified(client, app, stored["id"])

    # Both checks met → the client becomes an approved client.
    with app.app_context():
        portal_registrations.approve_registration(registration["id"])
        assert list_customers() and list_customers()[0]["id"] == stored["id"]
        assert portal_registrations.pending_registration_for_customer(stored["id"]) is None
        assert portal_registrations.registration_by_id(registration["id"])["status"] == "approved"


# ---------------------------------------------------------------- the approve endpoint

def _pending(client, app, branches, **overrides):
    register(client, app, branches["midrand"], **overrides)
    stored = customer_by_email(app, overrides.get("email", "pieter@example.co.za"))
    return stored, registration_for(app, stored["id"])


def test_approve_endpoint_refuses_without_the_scoped_csrf_token(client, app, branches):
    stored, registration = _pending(client, app, branches)
    sign_in(client, app, role="owner")
    assert client.post(f"/customers/portal/{registration['id']}/approve").status_code == 400
    assert registration_for(app, stored["id"])["status"] == "pending"
    # A wrong token is refused too.
    assert client.post(f"/customers/portal/{registration['id']}/approve", data={"csrf_token": "nope"}).status_code == 400


def test_approve_endpoint_rejects_unmet_checks_and_leaves_it_pending(client, app, branches):
    stored, registration = _pending(client, app, branches)
    sign_in(client, app, role="owner")
    client.get(f"/customers/{stored['id']}/edit")  # issues the scoped token into the session
    token = csrf_token(client)
    assert token
    response = client.post(f"/customers/portal/{registration['id']}/approve", data={"csrf_token": token})
    assert response.status_code == 302
    assert registration_for(app, stored["id"])["status"] == "pending"
    with app.app_context():
        assert list_customers() == []


def test_approve_endpoint_approves_when_both_checks_are_met(client, app, branches):
    stored, registration = _pending(client, app, branches)
    sign_in(client, app, role="owner")
    _set_id_verified(client, app, stored["id"])
    _capture_main_vehicle(client, stored["id"])
    client.get(f"/customers/{stored['id']}/edit")
    token = csrf_token(client)
    response = client.post(f"/customers/portal/{registration['id']}/approve", data={"csrf_token": token})
    assert response.status_code == 302
    with app.app_context():
        assert portal_registrations.registration_by_id(registration["id"])["status"] == "approved"
        assert [c["id"] for c in list_customers()] == [stored["id"]], "the client is now a normal customer"


def test_approve_endpoint_is_refused_for_another_branch(client, app, branches):
    stored, registration = _pending(client, app, branches)
    # A staff account that may only see Roodepoort cannot approve a Midrand registration.
    sign_in(client, app, role="staff", branch_id=branches["roodepoort"], modules=["customers"], can_view_all=False)
    client.get(f"/customers/{stored['id']}/edit")
    token = csrf_token(client)
    assert token
    assert client.post(f"/customers/portal/{registration['id']}/approve", data={"csrf_token": token}).status_code == 403
    assert registration_for(app, stored["id"])["status"] == "pending"


def test_approve_endpoint_404s_for_an_unknown_registration(client, app, branches):
    sign_in(client, app, role="owner")
    with client.session_transaction() as session:
        session["_csrf_tokens"] = {"portal_approval": "tok"}
    assert client.post("/customers/portal/9999/approve", data={"csrf_token": "tok"}).status_code == 404


def test_approve_endpoint_needs_the_customers_module(client, app, branches):
    stored, registration = _pending(client, app, branches)
    sign_in(client, app, role="staff", branch_id=branches["midrand"], modules=["orders"], can_view_all=False)
    assert client.post(f"/customers/portal/{registration['id']}/approve", data={"csrf_token": "x"}).status_code == 403


# ---------------------------------------------------------------- the verification panel context

def test_the_edit_page_exposes_the_pending_panel_contract(client, app, branches):
    stored, registration = _pending(client, app, branches)
    sign_in(client, app, role="owner")
    response = client.get(f"/customers/{stored['id']}/edit")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert str(registration["id"]) in body or "csrf_token" in body
    # The scoped token is issued for the approve form and nothing is verified/captured yet.
    assert csrf_token(client)
    with app.app_context():
        customer = get_customer(stored["id"])
        assert portal_registrations.id_verified(customer) is False
        assert portal_registrations.main_vehicle_recorded(stored["id"]) is False


def test_an_ordinary_client_has_no_pending_registration(client, app, branches):
    with app.app_context():
        created = create_customer({"name": "Walk In", "phone": "0831112222"})
    sign_in(client, app, role="owner")
    assert client.get(f"/customers/{created}/edit").status_code == 200
    with app.app_context():
        assert portal_registrations.pending_registration_for_customer(created) is None


# ---------------------------------------------------------------- no ID data is ever stored

def test_no_id_image_or_id_number_is_stored_anywhere(client, app, branches):
    register(client, app, branches["midrand"], id_number="9001015800085")
    with app.app_context():
        columns = {row["name"] for row in get_db().execute("PRAGMA table_info(portal_registrations)").fetchall()}
    assert columns == {
        "id", "reference", "customer_id", "branch_id", "name", "email", "phone",
        "status", "created_at", "approved_at", "approved_by_user_id",
    }
    for banned in ("id_image", "id_number", "id_document", "ocr", "id_scan"):
        assert banned not in columns
    stored = customer_by_email(app, "pieter@example.co.za")
    custom = json.loads(stored["custom_fields_json"] or "{}")
    assert "id_number" not in custom


# ---------------------------------------------------------------- Telegram payload keeps the branch

def test_the_new_customer_message_keeps_the_branch_and_the_reference(client, app, branches, monkeypatch):
    from app.services import telegram

    register(client, app, branches["midrand"])
    stored = customer_by_email(app, "pieter@example.co.za")
    registration = registration_for(app, stored["id"])

    captured = {}

    def fake_send(text):
        captured["text"] = text
        return {"ok": True, "sent": False, "skipped": "test"}

    monkeypatch.setattr(telegram, "_send_message", fake_send)
    with app.app_context():
        telegram.send_new_customer_notification(stored["id"])
    assert "Midrand Depot" in captured["text"], "the message must carry the Midrand branch, not head office"
    assert registration["reference"] in captured["text"]
    assert "awaiting" in captured["text"].lower()


# ---------------------------------------------------------------- real libSQL Row compatibility

def test_pending_queue_and_approval_with_real_libsql_rows(tmp_path, monkeypatch):
    """The portal/pending surface must not depend on sqlite3.Row's ``keys()``."""
    import sqlite3

    from libsql_client.result import Row

    def sdk_rows(cursor, values):
        return Row({item[0]: i for i, item in enumerate(cursor.description)}, tuple(values))

    monkeypatch.setattr(sqlite3, "Row", sdk_rows)
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "sdk.sqlite"),
            "TURSO_DATABASE_URL": "",
            "TURSO_AUTH_TOKEN": "",
            "SECRET_KEY": "test",
            "ADMIN_EMAIL": "admin@abi.local",
            "ADMIN_PASSWORD": "admin123",
            "TELEGRAM_NOTIFICATIONS_ENABLED": "",
        }
    )
    with app.app_context():
        midrand = branches_service.create_branch({"name": "Midrand Depot", "active": 1})
    monkeypatch.setattr(portal_intake, "registration_is_open", lambda: True)
    test_client = app.test_client()
    test_client.post(f"/portal/{slug_of(app, midrand)}/register", data=submission())

    with app.app_context():
        branch = get_db().execute("SELECT * FROM branches WHERE id = ?", (midrand,)).fetchone()
        assert isinstance(branch, Row) and not hasattr(branch, "keys")
        queue = portal_registrations.list_pending_registrations()
        assert len(queue) == 1 and queue[0]["branch_name"] == "Midrand Depot"
        registration = portal_registrations.registration_by_id(queue[0]["id"])
        assert isinstance(registration, dict)
        customer = get_customer(queue[0]["customer_id"])
        assert portal_registrations.id_verified(customer) is False
        # Both verification flags, exercised through the same helpers the route uses.
        from app.services import vehicles
        vehicle_id = vehicles.create_vehicle(
            {"registration": "SDK123GP", "make": "TEST MAKE", "vin": "AHTFR22G10L123456"}, customer_id=queue[0]["customer_id"]
        )
        vehicles.promote_main_vehicle(queue[0]["customer_id"], vehicle_id)
        assert portal_registrations.main_vehicle_recorded(queue[0]["customer_id"]) is True
        get_db().execute("UPDATE customers SET client_verified = 1 WHERE id = ?", (queue[0]["customer_id"],))
        get_db().commit()
        assert portal_registrations.id_verified(get_customer(queue[0]["customer_id"])) is True
        updated = portal_registrations.approve_registration(queue[0]["id"])
        assert updated["status"] == "approved"
