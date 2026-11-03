"""Ticket ABI-341953268 — suspected-fraud classification and identity matching.

The service is the security boundary for this feature: the parent ticket wires the
UI later, so these tests drive the service directly (through the existing customer
form routes where that is the real path) and pin the contract:

* a suspect classification is written as a normal block (``is_blocked=1``) in the
  SAME INSERT as the customer, so a suspect is never briefly orderable;
* the classification and the optional supplementary ID number live in the already
  hidden ``custom_fields_json`` blob — no customer-table migration;
* the flags are read only from the customer form's own blocking panel when it was
  submitted by its ``save_suspect`` action from a signed-in staff session, so a
  public or inline forged post can neither set nor clear them;
* neither value ever reaches ``custom_fields_for``, form summaries, page HTML or
  the CSV export, and ``suspect_matches`` never returns an ID value;
* matching is conservative: exact email (case-insensitive), SA-normalised phone
  (+27 / 0 / 0027 agree), exact supplementary ID, and full-name fuzzy matching
  that refuses blanks and lone first names.
"""
import json
import os
import tempfile
from contextlib import contextmanager

import pytest
from flask import session
from werkzeug.datastructures import MultiDict

from app import create_app
from app.db import get_db
from app.services.customers import (
    CUSTOMER_ID_NUMBER_KEY,
    SUSPECT_DEFAULT_BLOCK_REASON,
    SUSPECT_FLAG_KEY,
    can_set_customer_credit,  # noqa: F401  (import proves the module surface stays intact)
    create_customer,
    customer_fields_changed,
    customer_form_values_for,
    customer_id_number_for,
    customer_is_suspect,
    customer_summary_for,
    custom_fields_for,
    get_customer,
    suspect_fields_submitted,
    suspect_matches,
    suspect_panel_present,
    update_customer,
)

ID_NUMBER = "8001015000081"


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


def owner_id(app):
    with app.app_context():
        return get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()["id"]


def sign_in(client):
    with client.application.app_context():
        row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
    response = client.post("/login", data={"user_id": str(row["id"]), "password": "admin123"}, follow_redirects=True)
    assert response.status_code == 200
    return row["id"]


@contextmanager
def staff_request(app, user_id):
    """A signed-in staff request context, for direct service calls."""
    with app.test_request_context("/customers/new", method="POST"):
        session["user_id"] = user_id
        session["user_role"] = "owner"
        yield


@contextmanager
def public_request(app):
    """A request with no session — the storefront/portal/background boundary."""
    with app.test_request_context("/store", method="POST"):
        yield


def customer_row(app, customer_id):
    with app.app_context():
        return get_db().execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()


def raw_fields(app, customer_id):
    return json.loads(customer_row(app, customer_id)["custom_fields_json"] or "{}")


def make_customer(app, name, email="", phone="", id_number=None, suspect=False):
    """Create a plain (unblocked, unflagged) customer directly through the service."""
    with app.app_context():
        customer_id = create_customer({"name": name, "email": email, "phone": phone})
    if id_number is not None or suspect:
        with app.app_context():
            fields = json.loads(
                get_db().execute("SELECT custom_fields_json FROM customers WHERE id = ?", (customer_id,)).fetchone()[
                    "custom_fields_json"
                ] or "{}"
            )
            fields[CUSTOMER_ID_NUMBER_KEY] = id_number
            if suspect:
                fields[SUSPECT_FLAG_KEY] = 1
            db = get_db()
            db.execute(
                "UPDATE customers SET custom_fields_json = ?, is_blocked = ? WHERE id = ?",
                (json.dumps(fields), int(suspect), customer_id),
            )
            db.commit()
    return customer_id


def matches(app, mapping, exclude_id=None):
    """Call the DB-backed matcher inside an application context."""
    with app.app_context():
        return suspect_matches(mapping, exclude_id=exclude_id)


def post_suspect_form(client, **overrides):
    data = {
        "customer_type": "individual",
        "name": "Suspect Client",
        "email": "suspect@example.test",
        "phone": "+27 82 555 1234",
        "blocking_panel": "1",
        "action": "save_suspect",
        SUSPECT_FLAG_KEY: "1",
        CUSTOMER_ID_NUMBER_KEY: ID_NUMBER,
    }
    data.update(overrides)
    return client.post("/customers/new", data=data, follow_redirects=False)


def created_id(response):
    assert response.status_code in (302, 303), response.status_code
    return int(response.headers["Location"].rstrip("/").split("/")[-1])


# ------------------------------------------------------------------ schema

def test_customer_schema_needs_no_migration_and_keeps_the_hidden_json_blob(app):
    """The ticket stores its metadata in the existing hidden JSON blob, not a column."""
    with app.app_context():
        columns = {row["name"] for row in get_db().execute("PRAGMA table_info(customers)").fetchall()}
    assert "custom_fields_json" in columns
    assert "suspect_fraud" not in columns
    assert "customer_id_number" not in columns


# ------------------------------------------------------- staff classification

def test_staff_suspect_create_is_blocked_in_the_same_insert_and_id_is_hidden(client, app):
    sign_in(client)
    customer_id = created_id(post_suspect_form(client))

    row = customer_row(app, customer_id)
    assert row["is_blocked"] == 1
    # A suspect classification IS a block, even when the form posted no reason.
    assert row["blocked_reason"] == SUSPECT_DEFAULT_BLOCK_REASON
    assert customer_is_suspect(row) is True
    assert customer_id_number_for(row) == ID_NUMBER
    assert raw_fields(app, customer_id)[SUSPECT_FLAG_KEY] == 1

    # Hidden metadata never enters the custom-field card or the editable form values.
    assert SUSPECT_FLAG_KEY not in custom_fields_for(row)
    assert CUSTOMER_ID_NUMBER_KEY not in custom_fields_for(row)
    form_values = customer_form_values_for(row)
    assert SUSPECT_FLAG_KEY not in form_values
    assert CUSTOMER_ID_NUMBER_KEY not in form_values
    # Ticket ABI-341953269: the flagged record page and the edit-page prompt now DO show
    # the captured ID (staff comparison), but the CSV export and the custom-field cards
    # still must not carry it.
    assert f"Captured ID number" in client.get(f"/customers/{customer_id}").get_data(as_text=True)
    edit_page = client.get(f"/customers/{customer_id}/edit").get_data(as_text=True)
    assert f'"{ID_NUMBER}"' in edit_page
    export = client.get("/customers/export.csv")
    assert export.status_code == 200
    assert ID_NUMBER not in export.get_data(as_text=True)
    assert CUSTOMER_ID_NUMBER_KEY not in export.get_data(as_text=True)


def test_the_supplementary_id_number_is_optional(client, app):
    sign_in(client)
    customer_id = created_id(post_suspect_form(client, **{CUSTOMER_ID_NUMBER_KEY: ""}))

    row = customer_row(app, customer_id)
    assert row["is_blocked"] == 1
    assert customer_is_suspect(row) is True
    assert customer_id_number_for(row) == ""
    assert CUSTOMER_ID_NUMBER_KEY not in raw_fields(app, customer_id)


def test_a_staff_suspect_panel_save_blocks_an_existing_customer(client, app):
    sign_in(client)
    customer_id = make_customer(app, "Existing Client", email="existing@example.test", phone="082 111 2222")
    assert customer_row(app, customer_id)["is_blocked"] == 0

    response = client.post(f"/customers/{customer_id}/edit", data={
        "customer_type": "individual",
        "name": "Existing Client",
        "email": "existing@example.test",
        "phone": "082 111 2222",
        "blocking_panel": "1",
        "action": "save_suspect",
        SUSPECT_FLAG_KEY: "1",
        CUSTOMER_ID_NUMBER_KEY: ID_NUMBER,
        "blocked_reason": "Duplicate identity",
    }, follow_redirects=True)
    assert b"Customer saved" in response.data

    row = customer_row(app, customer_id)
    assert row["is_blocked"] == 1
    assert row["blocked_reason"] == "Duplicate identity"
    assert customer_is_suspect(row) is True
    assert customer_id_number_for(row) == ID_NUMBER


def test_a_blocked_suspect_cannot_have_a_new_order(client, app):
    sign_in(client)
    customer_id = created_id(post_suspect_form(client))
    response = client.post("/orders/new", data=MultiDict([
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
    ]), follow_redirects=True)
    assert f"Customer blocked — {SUSPECT_DEFAULT_BLOCK_REASON}" in response.get_data(as_text=True)
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"] == 0


# --------------------------------------------------- forged / public writes

def test_a_public_request_cannot_forge_the_suspect_fields(app):
    # The blocking panel and suspect action are forged, but no staff session exists.
    # If the suspect flag were honoured it would force is_blocked=1; it must not.
    with public_request(app):
        customer_id = create_customer({
            "customer_type": "individual",
            "name": "Forged Public Client",
            "email": "forged@example.test",
            "phone": "082 999 0000",
            "blocking_panel": "1",
            "action": "save_suspect",
            SUSPECT_FLAG_KEY: "1",
            CUSTOMER_ID_NUMBER_KEY: ID_NUMBER,
        })

    row = customer_row(app, customer_id)
    assert row["is_blocked"] == 0
    assert customer_is_suspect(row) is False
    assert customer_id_number_for(row) == ""
    assert SUSPECT_FLAG_KEY not in raw_fields(app, customer_id)


def test_an_inline_order_card_cannot_forge_or_clear_the_suspect_fields(client, app):
    user_id = sign_in(client)
    customer_id = make_customer(app, "Inline Client", email="inline@example.test", phone="083 444 5555")

    with staff_request(app, user_id):
        update_customer(customer_id, MultiDict([
            ("customer_type", "individual"),
            ("name", "Inline Client"),
            ("email", "inline@example.test"),
            ("phone", "083 444 5555"),
            (SUSPECT_FLAG_KEY, "1"),
            (CUSTOMER_ID_NUMBER_KEY, ID_NUMBER),
            # no blocking panel marker, no save_suspect action
        ]))

    row = customer_row(app, customer_id)
    assert row["is_blocked"] == 0
    assert customer_is_suspect(row) is False
    assert customer_id_number_for(row) == ""


def test_the_panel_marker_alone_is_not_enough_without_the_suspect_action(client, app):
    user_id = sign_in(client)
    customer_id = make_customer(app, "Action Client", email="action@example.test", phone="084 123 4567")

    with staff_request(app, user_id):
        update_customer(customer_id, MultiDict([
            ("customer_type", "individual"),
            ("name", "Action Client"),
            ("email", "action@example.test"),
            ("phone", "084 123 4567"),
            ("blocking_panel", "1"),
            ("action", "save"),
            (SUSPECT_FLAG_KEY, "1"),
            (CUSTOMER_ID_NUMBER_KEY, ID_NUMBER),
        ]))

    row = customer_row(app, customer_id)
    assert customer_is_suspect(row) is False
    assert customer_id_number_for(row) == ""
    assert row["is_blocked"] == 0


def test_suspect_panel_detection_requires_a_staff_request(app):
    form = MultiDict([("blocking_panel", "1"), ("action", "save_suspect"), (SUSPECT_FLAG_KEY, "1")])
    with public_request(app):
        assert suspect_panel_present(form) is False
        assert suspect_fields_submitted(form) is True
    with staff_request(app, owner_id(app)):
        assert suspect_panel_present(form) is True
    assert suspect_fields_submitted(MultiDict([("name", "Nobody")])) is False


# --------------------------------------------------- edit blocking safety

def test_non_panel_edits_preserve_both_the_suspect_flag_and_the_block(client, app):
    sign_in(client)
    customer_id = created_id(post_suspect_form(client))

    with app.app_context():
        update_customer(customer_id, MultiDict([
            ("customer_type", "individual"),
            ("name", "Suspect Client"),
            ("email", "suspect@example.test"),
            ("phone", "082 555 9999"),  # a real field change still saves
            ("standard_discount_percent", "0"),
        ]))

    row = customer_row(app, customer_id)
    assert row["phone"] == "082 555 9999"
    assert row["is_blocked"] == 1
    assert customer_is_suspect(row) is True
    assert customer_id_number_for(row) == ID_NUMBER


def test_unticking_the_suspect_box_clears_the_classification_but_not_the_block(client, app):
    sign_in(client)
    customer_id = created_id(post_suspect_form(client))

    response = client.post(f"/customers/{customer_id}/edit", data={
        "customer_type": "individual",
        "name": "Suspect Client",
        "email": "suspect@example.test",
        "phone": "+27 82 555 1234",
        "blocking_panel": "1",
        "action": "save_suspect",
        # suspect_fraud deliberately absent: staff un-classified the client
        "is_blocked": "1",
        "blocked_reason": "Still under review",
    }, follow_redirects=True)
    assert b"Customer saved" in response.data

    row = customer_row(app, customer_id)
    assert customer_is_suspect(row) is False
    assert row["is_blocked"] == 1
    # The supplementary ID is retained as evidence, still hidden.
    assert customer_id_number_for(row) == ID_NUMBER
    assert CUSTOMER_ID_NUMBER_KEY not in custom_fields_for(row)


# ------------------------------------------------------------- matching

def test_matching_uses_normalised_email_phone_and_the_exact_id(app):
    customer_id = make_customer(
        app, "Thabo Mokoena", email="Thabo@Example.Test", phone="+27 82 555 1234", id_number=ID_NUMBER, suspect=True
    )

    found = matches(app, {
        "name": "Thabo Mokoena",
        "email": "thabo@example.test",
        "phone": "082 555 1234",
        CUSTOMER_ID_NUMBER_KEY: ID_NUMBER,
    })
    assert [entry["id"] for entry in found] == [customer_id]
    assert found[0]["reasons"] == ["email", "phone", "id_number", "name"]
    assert set(found[0]) == {"id", "name", "id_number", "reasons"}
    # Ticket ABI-341953269: the popup shows the number captured on the flagged record.
    assert found[0]["id_number"] == ID_NUMBER

    # A flagged client with no number recorded reports an empty string, never None.
    bare_id = make_customer(app, "Bare Match", email="bare@example.test", suspect=True)
    bare = [entry for entry in matches(app, {"email": "bare@example.test"}) if entry["id"] == bare_id]
    assert bare and bare[0]["id_number"] == ""

    # Each signal matches on its own, and the alternate SA prefixes agree too.
    assert matches(app, {"email": "THABO@example.TEST"})[0]["reasons"] == ["email"]
    assert matches(app, {"phone": "0027 82 555 1234"})[0]["reasons"] == ["phone"]
    assert matches(app, {CUSTOMER_ID_NUMBER_KEY: ID_NUMBER})[0]["reasons"] == ["id_number"]


def test_blank_identity_values_never_match(app):
    make_customer(app, "Blank Fields", email="", phone="")

    assert matches(app, {"name": "", "email": "", "phone": ""}) == []
    assert matches(app, {"email": "@", "phone": "123", CUSTOMER_ID_NUMBER_KEY: ""}) == []
    # A form that only carries junk contact values never gains email/phone reasons.
    assert matches(app, {"name": "Someone Else", "email": " ", "phone": "---"}) == []


def test_id_number_matches_exactly_and_ignores_whitespace(app):
    customer_id = make_customer(app, "ID Client", id_number=ID_NUMBER, suspect=True)

    assert matches(app, {CUSTOMER_ID_NUMBER_KEY: "800101 5000 081"})[0]["id"] == customer_id
    assert matches(app, {CUSTOMER_ID_NUMBER_KEY: "8001015000082"}) == []
    # A number that merely appears in another custom field is not an ID match.
    with app.app_context():
        db = get_db()
        db.execute(
            "UPDATE customers SET custom_fields_json = ? WHERE id = ?",
            (json.dumps({"note": ID_NUMBER}), customer_id),
        )
        db.commit()
    assert matches(app, {CUSTOMER_ID_NUMBER_KEY: ID_NUMBER}) == []


def test_full_name_matching_is_conservative(app):
    john = make_customer(app, "John Smith", suspect=True)
    make_customer(app, "Jane Smith", suspect=True)
    long_name = make_customer(app, "John Peter Smith", suspect=True)

    assert matches(app, {"name": "John"}) == []          # lone first name
    assert matches(app, {"name": ""}) == []              # blank
    assert [entry["id"] for entry in matches(app, {"name": "Smith John"})] == [john]
    assert matches(app, {"name": "John Peter Smith"})[0]["id"] == long_name
    assert matches(app, {"name": "John Peter Smith"})[0]["reasons"] == ["name"]
    assert [entry["id"] for entry in matches(app, {"name": "Jhon Smith"})] == [john]


def test_matches_exclude_the_client_being_edited_and_never_leak_an_id(app):
    customer_id = make_customer(app, "Self Client", email="self@example.test", id_number=ID_NUMBER, suspect=True)
    row = customer_row(app, customer_id)

    assert matches(app, row, exclude_id=customer_id) == []
    found = matches(app, {"email": "self@example.test"})
    assert found[0]["id"] == customer_id
    assert found[0]["id_number"] == ID_NUMBER
    assert all(set(entry) == {"id", "name", "id_number", "reasons"} for entry in found)


# ------------------------------------------------------- summary surface

def test_staff_summary_carries_the_flag_and_matches_without_ids(client, app):
    user_id = sign_in(client)
    suspect_id = created_id(post_suspect_form(client))
    duplicate_id = make_customer(
        app, "Suspect Client", email="suspect@example.test", phone="082 555 1234"
    )

    with staff_request(app, user_id):
        row = get_customer(duplicate_id)
        summary = customer_summary_for(row)

    assert summary["is_suspect"] is False
    assert [entry["id"] for entry in summary["suspect_matches"]] == [suspect_id]
    assert summary["suspect_matches"][0]["reasons"] == ["email", "phone", "name"]
    assert set(summary["suspect_matches"][0]) == {"id", "name", "id_number", "reasons"}
    # Staff summary carries the flagged number for comparison (ticket ABI-341953269).
    assert summary["suspect_matches"][0]["id_number"] == ID_NUMBER


def test_staff_summary_of_an_ordinary_client_has_no_matches(client, app):
    user_id = sign_in(client)
    customer_id = make_customer(app, "Ordinary Client", email="ordinary@example.test")

    with staff_request(app, user_id):
        summary = customer_summary_for(get_customer(customer_id))

    assert summary["is_suspect"] is False
    assert summary["suspect_matches"] == []


def test_public_summaries_never_mention_suspicion(client, app):
    sign_in(client)
    suspect_id = created_id(post_suspect_form(client))

    with public_request(app):
        summary = customer_summary_for(get_customer(suspect_id))

    assert "is_suspect" not in summary
    assert "suspect_matches" not in summary
    assert SUSPECT_FLAG_KEY not in json.dumps(summary)
    assert ID_NUMBER not in json.dumps(summary)


def test_an_ordinary_edit_is_not_treated_as_a_change_by_the_order_form_card(client, app):
    """The hidden metadata must not make an untouched customer look edited."""
    sign_in(client)
    suspect_id = created_id(post_suspect_form(client))

    with staff_request(app, owner_id(app)):
        row = get_customer(suspect_id)
        untouched = customer_form_values_for(row)
        untouched["marketing_opt_in"] = ""
        assert customer_fields_changed(row, MultiDict(untouched)) is False
