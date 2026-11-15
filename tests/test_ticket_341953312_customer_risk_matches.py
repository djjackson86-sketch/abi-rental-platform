"""Ticket ABI-341953312 — broaden the customer risk warning's identity sources.

The warning is advisory only: a match never blocks, merges or edits the client.
This ticket widens *which stored records can be a source* and *what signals count*:

* a source is a client who is blocked **OR** classified Suspect/Fraud — before
  this, only records that were both were scanned, so a flagged client whose block
  was lifted, and a client blocked for another reason, were invisible;
* the original four signals (email, phone, exact ID, conservative full name) keep
  their behaviour and their payload shape;
* ID/passport numbers also match stored equivalents (``id_or_license``,
  ``passport_number``, …), not just the canonical ``customer_id_number``;
* the exact number plate and VIN of the client or of any vehicle recorded against
  them (the ``vehicles`` table, i.e. secondary vehicles, as well as the main
  vehicle mirror in ``custom_fields_json``) are signals;
* a complete address matches only when the street and the unit/suburb/city/
  postal components agree — the same street with a different unit does not;
* phone normalisation compares a complete number, never the last nine digits, so
  a foreign number cannot collide with another country's number;
* the payload stays exactly ``{"id", "name", "id_number", "reasons"}`` for staff
  callers, never carries the private Suspect/Fraud reason, and never appears on a
  public/background summary;
* the blocked-OR-suspect scan is cached for one request and invalidated by the
  existing customer writes.
"""
import json
import os
import tempfile
from contextlib import contextmanager

import pytest
from flask import session

from app import create_app
from app.db import get_db, now
from app.services.customers import (
    CUSTOMER_ID_NUMBER_KEY,
    SUSPECT_REASON_KEY,
    create_customer,
    customer_is_suspect,
    customer_summary_for,
    get_customer,
    suspect_matches,
)

ID_NUMBER = "8001015000081"
PASSPORT_NUMBER = "A12345678"
FOREIGN_PHONE = "+44 20 7946 0958"


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


def owner_id(app):
    with app.app_context():
        return get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()["id"]


@contextmanager
def staff_request(app, user_id):
    with app.test_request_context("/customers/new", method="POST"):
        session["user_id"] = user_id
        session["user_role"] = "owner"
        yield


@contextmanager
def public_request(app):
    with app.test_request_context("/store", method="POST"):
        yield


def make_customer(app, name, *, email="", phone="", address=None, custom=None,
                  blocked=None, suspect=False, id_number=None):
    """A client created through the service, then given its stored metadata.

    ``blocked=None`` means "blocked exactly when flagged" (the feature's own
    invariant); pass ``blocked=False`` to model a flagged client whose block was
    later lifted, or ``blocked=True`` for a client blocked for another reason.
    """
    with app.app_context():
        customer_id = create_customer({
            "name": name,
            "email": email,
            "phone": phone,
            **(address or {}),
        }, notify=False)
        db = get_db()
        stored = db.execute(
            "SELECT custom_fields_json FROM customers WHERE id = ?", (customer_id,)
        ).fetchone()
        fields = json.loads(stored["custom_fields_json"] or "{}")
        if id_number is not None:
            fields[CUSTOMER_ID_NUMBER_KEY] = id_number
        if custom:
            fields.update(custom)
        if suspect:
            fields["suspect_fraud"] = 1
        is_blocked = int(suspect if blocked is None else blocked)
        db.execute(
            "UPDATE customers SET custom_fields_json = ?, is_blocked = ? WHERE id = ?",
            (json.dumps(fields), is_blocked, customer_id),
        )
        db.commit()
    return customer_id


def add_vehicle(app, customer_id, *, registration="", vin=""):
    with app.app_context():
        db = get_db()
        stamp = now()
        db.execute(
            "INSERT INTO vehicles (customer_id, registration, vin, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (customer_id, registration, vin, stamp, stamp),
        )
        db.commit()


def db_row(app, customer_id):
    with app.app_context():
        return get_db().execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()


def matches(app, mapping, exclude_id=None):
    with app.app_context():
        return suspect_matches(mapping, exclude_id=exclude_id)


def reasons_for(app, mapping, customer_id):
    found = [entry for entry in matches(app, mapping) if entry["id"] == customer_id]
    assert found, f"expected a match for {mapping!r}"
    return found[0]["reasons"]


# ------------------------------------------------------------- source status

def test_a_blocked_client_is_a_source_even_when_never_classified_suspect(app):
    blocked_id = make_customer(
        app, "Blocked Client", email="blocked@example.test", phone="082 555 1000", blocked=True
    )
    assert customer_is_suspect(db_row(app, blocked_id)) is False

    found = matches(app, {"email": "BLOCKED@example.test"})
    assert [entry["id"] for entry in found] == [blocked_id]
    assert found[0]["reasons"] == ["email"]
    # The payload shape existing staff surfaces depend on is unchanged.
    assert set(found[0]) == {"id", "name", "id_number", "reasons"}


def test_a_flagged_client_stays_a_source_when_the_block_was_lifted(app):
    flagged_id = make_customer(
        app, "Lifted Client", email="lifted@example.test", phone="082 555 1001",
        suspect=True, blocked=False,
    )
    row = db_row(app, flagged_id)
    assert row["is_blocked"] == 0 and customer_is_suspect(row) is True

    assert [entry["id"] for entry in matches(app, {"phone": "082 555 1001"})] == [flagged_id]


def test_a_client_that_is_both_is_still_one_source_with_one_entry(app):
    both_id = make_customer(
        app, "Both Client", email="both@example.test", phone="082 555 1002", blocked=True, suspect=True
    )
    found = matches(app, {"email": "both@example.test", "phone": "0825551002"})
    assert [entry["id"] for entry in found] == [both_id]
    assert found[0]["reasons"] == ["email", "phone"]


def test_the_private_reason_never_enters_the_match_payload(app):
    make_customer(
        app, "Reason Client", email="reason@example.test", blocked=True,
        custom={SUSPECT_REASON_KEY: "Private staff note: do not disclose", "suspect_fraud": 1},
    )

    found = matches(app, {"email": "reason@example.test"})
    assert found and "Private staff note" not in json.dumps(found)
    assert set(found[0]) == {"id", "name", "id_number", "reasons"}


# ----------------------------------------------------------- ID / passport

def test_passport_and_legacy_id_equivalents_match_exactly(app):
    passport_id = make_customer(
        app, "Passport Client", blocked=True, custom={"passport_number": PASSPORT_NUMBER}
    )
    legacy_id = make_customer(
        app, "Legacy ID Client", blocked=True, custom={"id_or_license": "ID 8001015000081"}
    )

    assert reasons_for(app, {CUSTOMER_ID_NUMBER_KEY: "a 1234 5678"}, passport_id) == ["id_number"]
    assert reasons_for(app, {CUSTOMER_ID_NUMBER_KEY: "800101 5000 081"}, legacy_id) == ["id_number"]

    # A partial body is not the same number: exact comparison only.
    assert matches(app, {CUSTOMER_ID_NUMBER_KEY: "12345678"}) == []


def test_the_canonical_id_number_still_matches_as_before(app):
    canonical = make_customer(app, "Canonical Client", blocked=True, id_number=ID_NUMBER)

    assert reasons_for(app, {CUSTOMER_ID_NUMBER_KEY: ID_NUMBER}, canonical) == ["id_number"]
    # Too short to be evidence, even when it is stored verbatim.
    junk = make_customer(app, "Junk ID Client", blocked=True, id_number="1")
    assert matches(app, {CUSTOMER_ID_NUMBER_KEY: "1"}) == []
    assert junk  # created, just never matched


# ------------------------------------------------------------------ phones

def test_sa_prefixes_still_agree(app):
    sa_id = make_customer(app, "SA Client", phone="+27 82 555 1234", blocked=True)

    for typed in ("082 555 1234", "0027825551234", "27 (0)82 555 1234", "+27825551234"):
        assert reasons_for(app, {"phone": typed}, sa_id) == ["phone"], typed


def test_foreign_numbers_never_collide_on_their_last_nine_digits(app):
    # 442079460958 and 331079460958 share the suffix 079460958.
    make_customer(app, "UK Client", phone=FOREIGN_PHONE, blocked=True)

    assert matches(app, {"phone": "+33 1 07 94 60 958"}) == []
    assert matches(app, {"phone": "+442079460958"})[0]["reasons"] == ["phone"]


# ---------------------------------------------------------------- vehicles

def test_a_secondary_vehicle_registration_and_vin_match(app):
    vehicle_owner = make_customer(app, "Vehicle Client", blocked=True)
    add_vehicle(app, vehicle_owner, registration="NB 72 XMGP", vin="1hgcm82633a004352")

    assert reasons_for(app, {"vehicle_reg_no": "nb72xmgp"}, vehicle_owner) == ["vehicle_registration"]
    assert reasons_for(app, {"vehicle_vin": "1HGCM 8263 3A004352"}, vehicle_owner) == ["vin"]
    # A different plate/VIN from the same client's other vehicle is not a signal.
    assert matches(app, {"vehicle_reg_no": "NB72XMGO"}) == []


def test_the_main_vehicle_mirror_matches_without_a_vehicle_row(app):
    mirrored = make_customer(
        app, "Mirror Client", blocked=True,
        custom={"vehicle_reg_no": "CA 123-456", "vehicle_vin": "WVWZZZ1JZXW000001"},
    )

    assert reasons_for(app, {"vehicle_reg_no": "CA123456"}, mirrored) == ["vehicle_registration"]
    assert reasons_for(app, {"vehicle_vin": "wvwzzz1jzxw000001"}, mirrored) == ["vin"]


def test_vehicle_signal_combines_with_the_other_reasons_in_payload_order(app):
    owner_id = make_customer(app, "Combined Client", email="combined@example.test", blocked=True)
    add_vehicle(app, owner_id, registration="JHB 123 GP")

    found = matches(app, {"email": "combined@example.test", "vehicle_reg_no": "JHB123GP"})
    assert found[0]["reasons"] == ["email", "vehicle_registration"]


# ----------------------------------------------------------------- address

ADDRESS = {
    "address_line1": "12 Kerk Street",
    "address_line2": "Unit 4",
    "suburb": "Randburg",
    "city": "Johannesburg",
    "province": "Gauteng",
    "postal_code": "2194",
}


def test_a_complete_address_including_the_unit_matches(app):
    address_id = make_customer(app, "Address Client", blocked=True, address=ADDRESS)

    typed = {
        "address_line1": "12 KERK STREET",
        "address_line2": "unit 4",
        "suburb": "randburg",
        "city": "JOHANNESBURG",
        "province": "Gauteng",
        "postal_code": "2194",
    }
    assert reasons_for(app, typed, address_id) == ["address"]


def test_the_same_building_with_a_different_unit_is_not_the_same_address(app):
    make_customer(app, "Unit Four", blocked=True, address=ADDRESS)

    different_unit = dict(ADDRESS, address_line2="Unit 5")
    assert matches(app, different_unit) == []

    # A common street name on its own is not evidence either.
    assert matches(app, {"address_line1": "12 Kerk Street"}) == []
    # Nor is the same street in another city.
    assert matches(app, dict(ADDRESS, city="Pretoria")) == []


# -------------------------------------------------- ordinary clients / safety

def test_ordinary_unblocked_unclassified_clients_are_never_sources(app):
    make_customer(
        app, "Ordinary Client", email="ordinary@example.test", phone="084 555 1234",
        address=ADDRESS, id_number=ID_NUMBER, custom={"vehicle_reg_no": "XYZ 999 GP"},
    )

    assert matches(app, {
        "email": "ordinary@example.test",
        "phone": "084 555 1234",
        CUSTOMER_ID_NUMBER_KEY: ID_NUMBER,
        "vehicle_reg_no": "XYZ999GP",
        **ADDRESS,
    }) == []


def test_a_match_is_a_warning_and_never_auto_blocks_the_new_client(app):
    source_id = make_customer(
        app, "Known Client", email="known@example.test", phone="085 555 1234", blocked=True
    )

    with app.app_context():
        new_id = create_customer({
            "name": "New Client",
            "email": "known@example.test",
            "phone": "085 555 1234",
        }, notify=False)
        row = get_db().execute(
            "SELECT is_blocked, custom_fields_json FROM customers WHERE id = ?", (new_id,)
        ).fetchone()
    assert row["is_blocked"] == 0
    assert "suspect_fraud" not in json.loads(row["custom_fields_json"] or "{}")
    # The source is still discoverable for staff, it just never acts by itself.
    assert [entry["id"] for entry in matches(app, {"email": "known@example.test"})] == [source_id]


# ------------------------------------------------------------ cache / scope

def test_the_scan_is_cached_for_one_request_and_invalidated_by_a_write(app):
    user_id = owner_id(app)

    with staff_request(app, user_id):
        assert suspect_matches({"email": "late@example.test"}) == []

        # A raw write inside the same request is not seen: the scan is cached.
        db = get_db()
        cur = db.execute(
            "INSERT INTO customers (name, email, phone, is_blocked, created_at) VALUES (?, ?, ?, 1, ?)",
            ("Late Blocked", "late@example.test", "", now()),
        )
        late_id = cur.lastrowid
        db.commit()
        assert suspect_matches({"email": "late@example.test"}) == []

        # A real service write clears the cache, so the next check sees both rows.
        create_customer({"name": "Fresh Client"}, notify=False)
        assert [entry["id"] for entry in suspect_matches({"email": "late@example.test"})] == [late_id]


def test_staff_summary_carries_the_matches_and_public_summaries_do_not(app):
    user_id = owner_id(app)
    source_id = make_customer(
        app, "Summary Source", email="summary@example.test", phone="086 555 1234",
        blocked=True, custom={SUSPECT_REASON_KEY: "Private summary reason"},
    )
    duplicate_id = make_customer(app, "Summary Source", email="summary@example.test")

    with staff_request(app, user_id):
        summary = customer_summary_for(get_customer(duplicate_id))
    assert [entry["id"] for entry in summary["suspect_matches"]] == [source_id]
    assert summary["suspect_matches"][0]["reasons"] == ["email", "name"]
    assert set(summary["suspect_matches"][0]) == {"id", "name", "id_number", "reasons"}
    assert "Private summary reason" not in json.dumps(summary)

    with public_request(app):
        public_summary = customer_summary_for(get_customer(duplicate_id))
    assert "suspect_matches" not in public_summary
    assert "is_suspect" not in public_summary
    assert "Private summary reason" not in json.dumps(public_summary)
