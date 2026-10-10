"""Main client vehicle — the client's invoiced vehicle mirrored into their Custom details.

Covers the 2026-10-10 request (after the parent's correction trimming the mirror to the seven
scan-manual fields):

* the seven canonical ``vehicle_*`` keys map to the client's main vehicle and nothing else;
* a scan that links a vehicle can make it the client's main vehicle (make_main=yes), and only then
  — "no" leaves the current main alone, a first vehicle defaults to main for older callers;
* a *different* vehicle may not silently replace the main one: with a main on file and no choice
  posted, the save is refused with the choice highlighted and nothing is written;
* re-scanning a plate the client already holds updates that record (never duplicates it) and the
  disk expiry follows;
* the main-vehicle mirror is cleared when the vehicle is deleted or transferred away, and replacing
  the main clears the stale fields — all while every other custom field survives;
* a customer-form save is authoritative over the linked vehicle row (a plate another client owns is
  refused), while a partial post (portal/order card) can neither blank the mirror nor push a stale
  snapshot into the row;
* the internal ``main_vehicle_id`` never renders, and a legacy ``vehicle_color`` stays stored.

Every identifier is synthetic.
"""

import os
import tempfile

import pytest

from app import create_app
from app.db import get_db
from app.services import customers as customers_service
from app.services import vehicles
from app.services.customers import (
    CUSTOM_FIELD_FORM_KEYS,
    HIDDEN_CUSTOM_FIELD_KEYS,
    VISIBLE_CUSTOM_FIELD_ORDER,
    create_customer,
    custom_fields_for,
    get_customer,
    raw_custom_fields_for,
    update_customer,
)

SEVEN_KEYS = (
    "vehicle_reg_no",
    "vehicle_licence_number",
    "vehicle_registration_number",
    "vehicle_vin",
    "vehicle_engine_number",
    "vehicle_make",
    "vehicle_licence_disk_expiry",
)


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
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def customer_id(app):
    with app.app_context():
        return create_customer({"name": "Charmaine Mokoena", "customer_type": "individual", "phone": "0821234567"})


@pytest.fixture()
def other_customer_id(app):
    with app.app_context():
        return create_customer({"name": "Pieter van Wyk", "customer_type": "individual", "phone": "0837654321"})


def login_owner(client):
    with client.application.app_context():
        row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
    return client.post("/login", data={"user_id": str(row["id"]), "password": "admin123"}, follow_redirects=True)


def vehicle_form(registration="ABC123GP", **overrides):
    """A scan-screen vehicle payload — the seven fields the app records plus the disk extras."""
    data = {
        "registration": registration,
        "registration_number": "ZZ1234Z",
        "licence_number": "T9876543210X",
        "make": "TOYOTA",
        "model": "HILUX 2.4 GD-6",
        "colour": "WHITE",
        "vehicle_type": "BAKKIE",
        "vin": "AHTFR22G10L123456",
        "engine_number": "2GD1234567",
        "registering_authority": "JOHANNESBURG",
        "control_number": "CN0001",
        "licence_disk_expiry": "2027-03-31",
        "source": "scan",
    }
    data.update(overrides)
    return data


def save_scan(client, customer_id, registration="ABC123GP", **overrides):
    data = vehicle_form(registration, **overrides)
    data["customer_id"] = str(customer_id) if customer_id else ""
    return client.post("/scan-vehicle/save", data=data, follow_redirects=True)


def customer_form(name="Charmaine Mokoena", **overrides):
    """A full customer-form payload — it carries all seven mirrored vehicle fields."""
    data = {
        "customer_type": "individual",
        "name": name,
        "email": "",
        "phone": "0821234567",
        "marketing_opt_in": "",
        "standard_discount_percent": "0",
        "client_verified": "",
        "vehicle_reg_no": "",
        "vehicle_licence_number": "",
        "vehicle_registration_number": "",
        "vehicle_vin": "",
        "vehicle_engine_number": "",
        "vehicle_make": "",
        "vehicle_licence_disk_expiry": "",
        "alternative_contact_name": "",
        "alternative_contact_number": "",
        "alternative_contact_relationship": "",
        "vat_number": "",
        "company_reg_no": "",
    }
    data.update(overrides)
    return data


# ── the mapping contract ─────────────────────────────────────────────────────


def test_the_seven_mirror_keys_are_the_canonical_mapping():
    assert tuple(vehicles.MAIN_VEHICLE_CUSTOM_KEYS) == SEVEN_KEYS
    assert set(vehicles.MAIN_VEHICLE_FIELD_MAP.values()) == set(SEVEN_KEYS)
    # the customers metadata agrees, and the seven lead the visible order (alternatives after)
    assert tuple(customers_service.VEHICLE_CUSTOM_FIELD_KEYS) == SEVEN_KEYS
    for key in SEVEN_KEYS:
        assert key in VISIBLE_CUSTOM_FIELD_ORDER
        assert key in CUSTOM_FIELD_FORM_KEYS
    assert VISIBLE_CUSTOM_FIELD_ORDER.index("vehicle_licence_disk_expiry") < VISIBLE_CUSTOM_FIELD_ORDER.index(
        "alternative_contact_name"
    )


def test_the_mapping_pairs_every_key_with_a_vehicle_column():
    assert vehicles.MAIN_VEHICLE_FIELD_MAP["registration"] == "vehicle_reg_no"
    assert vehicles.MAIN_VEHICLE_FIELD_MAP["licence_number"] == "vehicle_licence_number"
    assert vehicles.MAIN_VEHICLE_FIELD_MAP["registration_number"] == "vehicle_registration_number"
    assert vehicles.MAIN_VEHICLE_FIELD_MAP["vin"] == "vehicle_vin"
    assert vehicles.MAIN_VEHICLE_FIELD_MAP["engine_number"] == "vehicle_engine_number"
    assert vehicles.MAIN_VEHICLE_FIELD_MAP["make"] == "vehicle_make"
    assert vehicles.MAIN_VEHICLE_FIELD_MAP["licence_disk_expiry"] == "vehicle_licence_disk_expiry"


def test_the_internal_pointer_is_hidden_from_rendered_custom_fields(app, customer_id):
    assert "main_vehicle_id" in HIDDEN_CUSTOM_FIELD_KEYS
    with app.app_context():
        vehicle_id = vehicles.create_vehicle(vehicle_form(), customer_id=customer_id)
        vehicles.promote_main_vehicle(customer_id, vehicle_id)
        raw = raw_custom_fields_for(get_customer(customer_id))
        visible = custom_fields_for(get_customer(customer_id))
    assert raw["main_vehicle_id"] == vehicle_id
    assert "main_vehicle_id" not in visible


# ── promote / clear / sync ───────────────────────────────────────────────────


def test_promote_mirrors_the_seven_fields_and_clears_stale_on_replace(app, customer_id):
    with app.app_context():
        first = vehicles.create_vehicle(
            vehicle_form("AAA111GP", make="TOYOTA", model="HILUX", colour="WHITE"), customer_id=customer_id
        )
        vehicles.promote_main_vehicle(customer_id, first)
        fields = raw_custom_fields_for(get_customer(customer_id))
        assert fields["main_vehicle_id"] == first
        assert fields["vehicle_reg_no"] == "AAA111GP"
        assert fields["vehicle_vin"] == "AHTFR22G10L123456"
        assert fields["vehicle_licence_disk_expiry"] == "2027-03-31"
        # a decoded extra (model) is NOT mirrored — only the seven are
        assert "vehicle_model" not in fields

        # replace the main with a vehicle that carries far fewer fields
        second = vehicles.create_vehicle(
            {"registration": "BBB222GP", "make": "FORD", "customer_id": customer_id, "source": "manual"},
            customer_id=customer_id,
        )
        vehicles.promote_main_vehicle(customer_id, second)
        replaced = raw_custom_fields_for(get_customer(customer_id))
    assert replaced["main_vehicle_id"] == second
    assert replaced["vehicle_reg_no"] == "BBB222GP"
    assert replaced["vehicle_make"] == "FORD"
    # stale fields from the first vehicle are gone, not left behind
    assert replaced.get("vehicle_vin", "") == ""
    assert replaced.get("vehicle_licence_disk_expiry", "") == ""
    assert replaced.get("vehicle_licence_number", "") == ""


def test_promote_preserves_every_non_vehicle_custom_field(app, customer_id):
    with app.app_context():
        update_customer(
            customer_id,
            customer_form(**{"vat_number": "4123456789", "alternative_contact_name": "Jane Backup"}),
        )
        vehicle_id = vehicles.create_vehicle(vehicle_form(), customer_id=customer_id)
        vehicles.promote_main_vehicle(customer_id, vehicle_id)
        fields = raw_custom_fields_for(get_customer(customer_id))
    assert fields["vat_number"] == "4123456789"
    assert fields["alternative_contact_name"] == "Jane Backup"
    assert fields["vehicle_reg_no"] == "ABC123GP"


def test_promote_refuses_a_vehicle_that_is_not_the_clients(app, customer_id, other_customer_id):
    with app.app_context():
        theirs = vehicles.create_vehicle(vehicle_form("THEIR1GP"), customer_id=other_customer_id)
        with pytest.raises(ValueError):
            vehicles.promote_main_vehicle(customer_id, theirs)


def test_deleting_the_main_vehicle_clears_the_mirror_but_keeps_other_fields(app, customer_id):
    with app.app_context():
        update_customer(customer_id, customer_form(**{"vat_number": "4999999999"}))
        vehicle_id = vehicles.create_vehicle(vehicle_form(), customer_id=customer_id)
        vehicles.promote_main_vehicle(customer_id, vehicle_id)
        assert raw_custom_fields_for(get_customer(customer_id))["vehicle_reg_no"] == "ABC123GP"

        vehicles.delete_vehicle(vehicle_id)
        fields = raw_custom_fields_for(get_customer(customer_id))
    assert "main_vehicle_id" not in fields
    assert fields.get("vehicle_reg_no", "") == ""
    assert fields.get("vehicle_vin", "") == ""
    assert fields["vat_number"] == "4999999999"  # the non-vehicle field survives


def test_a_stale_pointer_to_a_missing_vehicle_is_dropped(app, customer_id):
    with app.app_context():
        vehicle_id = vehicles.create_vehicle(vehicle_form(), customer_id=customer_id)
        vehicles.promote_main_vehicle(customer_id, vehicle_id)
        get_db().execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))
        get_db().commit()
        assert vehicles.get_main_vehicle_id(customer_id) is None
        assert raw_custom_fields_for(get_customer(customer_id)).get("vehicle_reg_no", "") == ""


def test_transferring_the_main_vehicle_clears_the_former_owners_mirror(app, customer_id, other_customer_id):
    with app.app_context():
        vehicle_id = vehicles.create_vehicle(vehicle_form(), customer_id=customer_id)
        vehicles.promote_main_vehicle(customer_id, vehicle_id)
        vehicles.transfer_vehicle(vehicle_id, other_customer_id)
        former = raw_custom_fields_for(get_customer(customer_id))
    assert "main_vehicle_id" not in former
    assert former.get("vehicle_reg_no", "") == ""


def test_editing_the_main_vehicle_updates_the_mirror(app, customer_id):
    with app.app_context():
        vehicle_id = vehicles.create_vehicle(vehicle_form(), customer_id=customer_id)
        vehicles.promote_main_vehicle(customer_id, vehicle_id)
        vehicles.update_vehicle(vehicle_id, {"licence_disk_expiry": "2028-01-31", "registration": "ABC123GP"})
        fields = raw_custom_fields_for(get_customer(customer_id))
    assert fields["vehicle_licence_disk_expiry"] == "2028-01-31"


# ── the scan save: make_main choice ─────────────────────────────────────────


def test_a_first_scan_defaults_to_main_for_an_older_caller(app, client, customer_id):
    login_owner(client)
    response = save_scan(client, customer_id, "ABC123GP")  # no make_main posted
    assert response.status_code == 200
    with app.app_context():
        assert vehicles.get_main_vehicle_id(customer_id) is not None
        assert raw_custom_fields_for(get_customer(customer_id))["vehicle_reg_no"] == "ABC123GP"


def test_make_main_yes_promotes_and_no_leaves_the_current_main(app, client, customer_id):
    login_owner(client)
    save_scan(client, customer_id, "AAA111GP", make_main="yes")
    with app.app_context():
        first_id = vehicles.get_main_vehicle_id(customer_id)

    # a different vehicle, explicitly NOT main
    save_scan(client, customer_id, "BBB222GP", make_main="no")
    with app.app_context():
        assert vehicles.get_main_vehicle_id(customer_id) == first_id
        assert raw_custom_fields_for(get_customer(customer_id))["vehicle_reg_no"] == "AAA111GP"
        assert len(vehicles.list_vehicles(customer_id)) == 2

    # a third vehicle, explicitly main
    save_scan(client, customer_id, "CCC333GP", make_main="yes")
    with app.app_context():
        assert raw_custom_fields_for(get_customer(customer_id))["vehicle_reg_no"] == "CCC333GP"


def test_a_different_vehicle_with_no_choice_is_refused_before_writing(app, client, customer_id):
    login_owner(client)
    save_scan(client, customer_id, "AAA111GP", make_main="yes")
    with app.app_context():
        before = len(vehicles.list_vehicles(customer_id))
    response = save_scan(client, customer_id, "BBB222GP")  # no make_main
    body = response.data.decode()
    assert "already has a main vehicle on file" in body
    assert 'name="make_main"' in body  # the choice is offered
    with app.app_context():
        assert len(vehicles.list_vehicles(customer_id)) == before  # nothing was written
        assert raw_custom_fields_for(get_customer(customer_id))["vehicle_reg_no"] == "AAA111GP"


def test_rescanning_the_same_plate_updates_it_and_syncs_the_expiry(app, client, customer_id):
    login_owner(client)
    save_scan(client, customer_id, "ABC123GP", make_main="yes", licence_disk_expiry="2027-03-31")
    response = save_scan(client, customer_id, "ABC123GP", make_main="no", licence_disk_expiry="2028-05-31")
    assert b"already recorded for Charmaine Mokoena" in response.data
    with app.app_context():
        rows = vehicles.list_vehicles(customer_id)
        assert len(rows) == 1  # updated, never duplicated
        assert rows[0]["licence_disk_expiry"] == "2028-05-31"
        # it is the main vehicle, so the client's mirrored expiry followed it
        assert raw_custom_fields_for(get_customer(customer_id))["vehicle_licence_disk_expiry"] == "2028-05-31"


def test_the_scan_save_lands_on_the_customer_edit_screen(app, client, customer_id):
    login_owner(client)
    response = save_scan(client, customer_id, "ABC123GP", make_main="yes")
    assert response.request.path == f"/customers/{customer_id}/edit"


def test_edit_vehicle_can_promote_with_make_main_yes(app, client, customer_id):
    login_owner(client)
    save_scan(client, customer_id, "AAA111GP", make_main="yes")
    save_scan(client, customer_id, "BBB222GP", make_main="no")
    with app.app_context():
        other_id = vehicles.get_customer_vehicle_by_registration(customer_id, "BBB222GP")["id"]
    login_owner(client)
    client.post(
        f"/vehicles/{other_id}/edit",
        data={"registration": "BBB222GP", "make": "FORD", "make_main": "yes"},
        follow_redirects=True,
    )
    with app.app_context():
        assert vehicles.get_main_vehicle_id(customer_id) == other_id
        assert raw_custom_fields_for(get_customer(customer_id))["vehicle_reg_no"] == "BBB222GP"


# ── the customer form is authoritative; partial posts are not ───────────────


def test_customer_form_edit_syncs_the_linked_main_vehicle(app, client, customer_id):
    login_owner(client)
    save_scan(client, customer_id, "ABC123GP", make_main="yes")
    response = client.post(
        f"/customers/{customer_id}/edit",
        data=customer_form(
            vehicle_reg_no="ABC 123 GP",
            vehicle_make="TOYOTA HILUX",
            vehicle_vin="NEWVIN0000000001",
            vehicle_engine_number="ENG9999",
            vehicle_registration_number="ZZ1234Z",
            vehicle_licence_number="T9876543210X",
            vehicle_licence_disk_expiry="2029-02-28",
        ),
        follow_redirects=True,
    )
    assert response.status_code == 200
    with app.app_context():
        main_id = vehicles.get_main_vehicle_id(customer_id)
        row = vehicles.get_vehicle(main_id)
        assert row["registration"] == "ABC 123 GP"
        assert row["make"] == "TOYOTA HILUX"
        assert row["vin"] == "NEWVIN0000000001"
        assert row["engine_number"] == "ENG9999"
        assert row["licence_disk_expiry"] == "2029-02-28"
        assert raw_custom_fields_for(get_customer(customer_id))["vehicle_make"] == "TOYOTA HILUX"


def test_customer_form_edit_refuses_a_plate_another_client_owns(app, client, customer_id, other_customer_id):
    login_owner(client)
    save_scan(client, customer_id, "AAA111GP", make_main="yes")
    save_scan(client, other_customer_id, "BBB222GP", make_main="yes")
    response = client.post(
        f"/customers/{customer_id}/edit",
        data=customer_form(vehicle_reg_no="BBB222GP", vehicle_make="TOYOTA"),
        follow_redirects=True,
    )
    body = response.data.decode()
    assert "already recorded for Pieter van Wyk" in body
    with app.app_context():
        # the plate was not swapped in, and the original vehicle is intact
        assert vehicles.get_vehicle(vehicles.get_main_vehicle_id(customer_id))["registration"] == "AAA111GP"
        assert raw_custom_fields_for(get_customer(customer_id))["vehicle_reg_no"] == "AAA111GP"


def test_a_partial_post_cannot_wipe_the_mirror_or_the_vehicle(app, customer_id):
    with app.app_context():
        vehicle_id = vehicles.create_vehicle(vehicle_form(), customer_id=customer_id)
        vehicles.promote_main_vehicle(customer_id, vehicle_id)

        # an order-card / portal style partial post: only the legacy few vehicle fields, and stale
        partial = {"name": "Charmaine Mokoena", "customer_type": "individual", "vehicle_reg_no": "STALE1GP"}
        update_customer(customer_id, partial)

        fields = raw_custom_fields_for(get_customer(customer_id))
        row = vehicles.get_vehicle(vehicle_id)
    # the stale partial value was NOT pushed onto the main row ...
    assert row["registration"] == "ABC123GP"
    # ... and the mirror still reflects the row, not the stale snapshot
    assert fields["vehicle_reg_no"] == "ABC123GP"
    # the fields the partial post did not carry survived
    assert fields["vehicle_vin"] == "AHTFR22G10L123456"
    assert fields["vehicle_licence_disk_expiry"] == "2027-03-31"


def test_legacy_vehicle_colour_is_preserved_and_not_exposed_as_a_new_field(app, customer_id):
    with app.app_context():
        # a client with a legacy colour already stored
        update_customer(customer_id, customer_form())
        get_db().execute(
            "UPDATE customers SET custom_fields_json = ? WHERE id = ?",
            ('{"vehicle_color": "WHITE", "vehicle_make": "TOYOTA"}', customer_id),
        )
        get_db().commit()
        update_customer(customer_id, customer_form(vehicle_make="TOYOTA"))
        raw = raw_custom_fields_for(get_customer(customer_id))
        # colour is not one of the seven offered fields ...
        assert "vehicle_color" not in customers_service.VEHICLE_CUSTOM_FIELD_KEYS
        # ... but the stored value is untouched and still decoded for documents/invoices
        assert raw["vehicle_color"] == "WHITE"
        visible = custom_fields_for(get_customer(customer_id))
    assert visible.get("vehicle_color") == "WHITE"


# ── expiry warning helper ────────────────────────────────────────────────────


def test_the_expiry_state_flags_an_expired_disk_and_guesses_nothing(app):
    with app.app_context():
        expired = vehicles.licence_disk_expiry_state("2020-01-01", today="2026-10-10")
        valid = vehicles.licence_disk_expiry_state("2027-03-31", today="2026-10-10")
        blank = vehicles.licence_disk_expiry_state("", today="2026-10-10")
    assert expired["expired"] is True and "expired on 2020-01-01" in expired["warning"]
    assert valid["expired"] is False and valid["days_left"] > 0
    assert blank["expired"] is None and blank["warning"] is None
