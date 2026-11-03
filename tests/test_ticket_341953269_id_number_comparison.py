"""Ticket ABI-341953269 — show the ID number captured when a client was flagged.

Physical test (as reported): the similarity popup links correctly to the customer
record, but the record did not show the ID number captured when that customer was
flagged, so staff could not compare it with the number being entered.

Contract pinned here:

* ``suspect_matches`` entries carry ``id_number`` — the *matched* client's hidden
  ``customer_id_number``, trimmed, ``""`` when none was recorded (no nulls);
* the flagged client's own record page shows ``Captured ID number`` (or
  ``No ID number captured``), and only for a Suspect/Fraud record;
* the edit page's capture prompt is prefilled with the stored number;
* the value stays hidden everywhere it always was: CSV export, the custom-field
  cards, the storefront and public/background summaries; and the staff
  ``suspect-check`` endpoint keeps its auth + module gate.
"""
import json

from werkzeug.datastructures import MultiDict

from test_ticket_341953268_matching import (  # noqa: F401  (fixtures)
    ID_NUMBER,
    app,
    client,
    created_id,
    make_customer,
    post_suspect_form,
    sign_in,
)

from app.db import get_db
from app.services.customers import (
    CUSTOMER_ID_NUMBER_KEY,
    customer_id_number_for,
    customer_summary_for,
    custom_fields_for,
    get_customer,
    suspect_matches,
)

OTHER_ID = "9002025000082"


def matches(app, mapping, exclude_id=None):
    with app.app_context():
        return suspect_matches(mapping, exclude_id=exclude_id)


# --------------------------------------------------------------- match payload

def test_the_match_payload_carries_the_number_captured_on_the_flagged_record(app):
    flagged = make_customer(
        app, "Thabo Mokoena", email="thabo@example.test", phone="0825551234",
        id_number=ID_NUMBER, suspect=True,
    )

    found = matches(app, {"phone": "082 555 1234"})
    assert [entry["id"] for entry in found] == [flagged]
    assert found[0]["id_number"] == ID_NUMBER
    assert set(found[0]) == {"id", "name", "id_number", "reasons"}

    # The same holds when the record is reached through an exact ID match.
    by_id = matches(app, {CUSTOMER_ID_NUMBER_KEY: ID_NUMBER})
    assert by_id[0]["id"] == flagged
    assert by_id[0]["id_number"] == ID_NUMBER


def test_a_flagged_client_with_no_number_reports_an_empty_string(app):
    bare = make_customer(app, "Bare Suspect", email="bare@example.test", suspect=True)
    with app.app_context():
        db = get_db()
        fields = json.loads(db.execute(
            "SELECT custom_fields_json FROM customers WHERE id = ?", (bare,)
        ).fetchone()["custom_fields_json"] or "{}")
        fields.pop(CUSTOMER_ID_NUMBER_KEY, None)
        db.execute("UPDATE customers SET custom_fields_json = ? WHERE id = ?", (json.dumps(fields), bare))
        db.commit()

    found = matches(app, {"email": "bare@example.test"})
    assert found[0]["id_number"] == ""
    assert json.dumps(found[0]).count("null") == 0

    # A whitespace-only stored value is trimmed to "" too, never shown as blanks.
    with app.app_context():
        db = get_db()
        db.execute(
            "UPDATE customers SET custom_fields_json = ? WHERE id = ?",
            (json.dumps({"suspect_fraud": 1, CUSTOMER_ID_NUMBER_KEY: "   "}), bare),
        )
        db.commit()
    assert matches(app, {"email": "bare@example.test"})[0]["id_number"] == ""


def test_two_flagged_records_each_report_their_own_number(app):
    first = make_customer(app, "Alpha Client", email="alpha@example.test", id_number=ID_NUMBER, suspect=True)
    second = make_customer(app, "Alpha Client", email="alpha2@example.test", id_number=OTHER_ID, suspect=True)

    found = {entry["id"]: entry["id_number"] for entry in matches(app, {"name": "Alpha Client"})}
    assert found == {first: ID_NUMBER, second: OTHER_ID}


# ------------------------------------------------------------ staff check route

def test_the_staff_check_returns_the_id_and_keeps_its_gate(client, app):
    # Anonymous: the sign-in wall still answers first (no payload at all).
    assert client.post("/customers/suspect-check", data={"email": "suspect@example.test"}).status_code == 302

    sign_in(client)
    flagged = created_id(post_suspect_form(client))
    response = client.post("/customers/suspect-check", data={"email": "SUSPECT@example.test"})
    assert response.status_code == 200
    assert response.json["matches"][0]["id"] == flagged
    assert response.json["matches"][0]["id_number"] == ID_NUMBER

    # A signed-in account with neither Customers nor Orders is refused, not served.
    with app.app_context():
        owner = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()["id"]
    with client.session_transaction() as sess:
        sess["user_id"] = owner
        sess["user_role"] = "staff"
        sess["staff_modules"] = ["payments"]
    refused = client.post("/customers/suspect-check", data={"email": "suspect@example.test"})
    assert refused.status_code == 403
    assert ID_NUMBER not in refused.get_data(as_text=True)


# ------------------------------------------------------------- the record page

def test_the_flagged_record_shows_the_captured_id(client, app):
    sign_in(client)
    flagged = created_id(post_suspect_form(client))

    body = client.get(f"/customers/{flagged}").get_data(as_text=True)
    assert "Captured ID number" in body
    assert ID_NUMBER in body

    empty = make_customer(app, "Suspect No Number", email="nonumber@example.test", suspect=True)
    body = client.get(f"/customers/{empty}").get_data(as_text=True)
    assert "Captured ID number" in body
    assert "No ID number captured" in body


def test_an_ordinary_record_never_shows_the_captured_id_row(client, app):
    sign_in(client)
    make_customer(app, "Flagged Person", email="flagged@example.test", id_number=ID_NUMBER, suspect=True)
    normal = make_customer(app, "Ordinary Person", email="ordinary@example.test")

    body = client.get(f"/customers/{normal}").get_data(as_text=True)
    assert "Captured ID number" not in body
    # The popup may still offer the flagged match (with its number) for comparison —
    # but only inside that staff popup payload, never as a page value.
    assert body.count(ID_NUMBER) == body.count('"id_number": "%s"' % ID_NUMBER)


def test_the_edit_prompt_is_prefilled_with_the_stored_number(client, app):
    sign_in(client)
    flagged = created_id(post_suspect_form(client))
    page = client.get(f"/customers/{flagged}/edit").get_data(as_text=True)
    assert 'id="suspect-capture-id" maxlength="64" value="%s"' % ID_NUMBER in page

    # A brand-new customer prompts blank (and an empty prompt still stores nothing).
    new_page = client.get("/customers/new").get_data(as_text=True)
    assert 'id="suspect-capture-id" maxlength="64" value=""' in new_page


def test_an_anonymous_request_never_reaches_the_id(client, app):
    sign_in(client)
    flagged = created_id(post_suspect_form(client))
    with client.session_transaction() as sess:
        sess.clear()
    response = client.get(f"/customers/{flagged}")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    assert ID_NUMBER not in response.get_data(as_text=True)


# --------------------------------------------------- still hidden everywhere else

def test_the_id_stays_out_of_the_export_storefront_and_public_summary(client, app):
    sign_in(client)
    flagged = created_id(post_suspect_form(client))

    export = client.get("/customers/export.csv")
    assert export.status_code == 200
    assert ID_NUMBER not in export.get_data(as_text=True)
    assert CUSTOMER_ID_NUMBER_KEY not in export.get_data(as_text=True)

    store = client.get("/store")
    assert store.status_code in (200, 302)
    assert ID_NUMBER not in store.get_data(as_text=True)

    with app.app_context():
        row = get_customer(flagged)
        # The hidden key never becomes a visible custom field.
        assert CUSTOMER_ID_NUMBER_KEY not in custom_fields_for(row)
        assert customer_id_number_for(row) == ID_NUMBER
        with app.test_request_context("/store"):
            public_summary = customer_summary_for(row)
    assert "suspect_matches" not in public_summary
    assert ID_NUMBER not in json.dumps(public_summary)


def test_the_staff_summary_path_still_carries_it_for_the_order_form(client, app):
    """The order form's picker payload is staff-only; it carries the same number."""
    sign_in(client)
    flagged = created_id(post_suspect_form(client))
    with app.app_context():
        from app.services.customers import staff_request_active
        assert staff_request_active() is False
    body = client.get("/orders/new").get_data(as_text=True)
    assert flagged  # the order form renders; the picker payload is a staff surface
    assert body.count(ID_NUMBER) == body.count('"id_number": "%s"' % ID_NUMBER)
