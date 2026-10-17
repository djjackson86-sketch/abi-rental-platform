"""ABI-341953236 — "Allocate from list" for an existing customer, with a main-vehicle choice.

The owner asked for the "Vehicles awaiting allocation" scan to be allocatable to an **existing**
client from the Customers edit page, and for the user to choose whether the scan

* is added beside the main vehicle already on file (``mode=additional`` — the main vehicle and the
  mirrored invoice details stay exactly as they were), or
* replaces the main vehicle (``mode=replace_main`` — the scan becomes the main/invoiced vehicle and
  the previous main row is kept as an additional vehicle, because vehicles are never deleted).

What must not change: a self-registered client's pending-approval allocation (no ``mode`` posted)
still becomes the main vehicle and still auto-approves once both checks pass; a client with no main
vehicle gets it under either choice; branch scope, plate uniqueness and the atomic claim still
apply. Every identifier here is synthetic.
"""

import pytest

from app.db import get_db
from app.services import vehicles
from app.services.customers import create_customer, raw_custom_fields_for, get_customer
from test_ticket_341953212_vehicle_approval import app, branches, client, sign_in  # noqa: F401

CSRF = "synthetic-token"


def scan_form(registration="NB72XMGP", **overrides):
    data = {
        "registration": registration,
        "make": "MITSUBISHI",
        "vin": "JHTFR22G10L654321",
        "engine_number": "K9K7654321",
        "colour": "WHITE",
        "licence_number": "5120367QP4HD",
        "registration_number": "QWR419V",
        "licence_disk_expiry": "31-03-2027",
        "source": "scan",
    }
    data.update(overrides)
    return data


def owner_id(app):
    with app.app_context():
        return int(
            get_db()
            .execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1")
            .fetchone()["id"]
        )


def queue_scan(app, registration="NB 72 XMGP", branch_id=None, **overrides):
    """Queue a scan exactly as the scan screen does, stamped with the scanning user's branch."""
    with app.test_request_context("/scan-vehicle"):
        from flask import session

        session["user_id"] = owner_id(app)
        session["user_role"] = "staff" if branch_id else "owner"
        session["branch_id"] = branch_id
        session["can_view_all_branches"] = branch_id is None
        session["branch_ids"] = [branch_id] if branch_id else []
        return vehicles.save_waiting_vehicle(scan_form(registration, **overrides))


def allocate(app, waiting_id, customer_id, mode=None):
    """Allocate as an unrestricted (owner) session, so only the ticket's rules are under test."""
    with app.test_request_context("/customers"):
        from flask import session

        session["user_id"] = owner_id(app)
        session["user_role"] = "owner"
        session["can_view_all_branches"] = True
        session["branch_ids"] = []
        return vehicles.allocate_waiting_vehicle(waiting_id, customer_id, mode)


def make_customer(app, branch_id=None, name="Thabo Mokoena", **custom):
    with app.app_context():
        return create_customer(
            {"name": name, "customer_type": "individual", "phone": "0821234567", **custom},
            branch_id=branch_id,
        )


def mirror(app, customer_id):
    with app.app_context():
        return raw_custom_fields_for(get_customer(customer_id))


def vehicles_for(app, customer_id):
    with app.app_context():
        return [dict(row) for row in vehicles.list_vehicles(customer_id)]


def queued(app):
    with app.app_context():
        return [dict(row) for row in vehicles.list_waiting_vehicles()]


def with_main(app, customer_id, registration, **overrides):
    """An existing customer with a promoted main vehicle, exactly as the scan screen records it."""
    with app.app_context():
        vehicle_id = vehicles.create_vehicle(
            scan_form(registration, **overrides), customer_id=customer_id
        )
        vehicles.promote_main_vehicle(customer_id, vehicle_id)
        return int(vehicle_id)


# ── additional ──────────────────────────────────────────────────────────────


def test_additional_keeps_the_main_vehicle_and_the_invoice_mirror(app, branches):
    customer_id = make_customer(app, branches["midrand"])
    main_id = with_main(app, customer_id, "OLD 111", make="TOYOTA", colour="BLUE")
    before = mirror(app, customer_id)
    waiting_id = queue_scan(app, "NEW 222", branches["midrand"])

    result = allocate(app, waiting_id, customer_id, "additional")

    assert int(result) != main_id
    assert result.mode_applied == "additional"
    assert result.previous_main_registration == "OLD 111"
    rows = vehicles_for(app, customer_id)
    assert len(rows) == 2                                   # +1, nothing deleted
    with app.app_context():
        assert vehicles.get_main_vehicle_id(customer_id) == main_id
        assert vehicles.get_main_vehicle(customer_id)["registration"] == "OLD 111"
    # The invoiced mirror is untouched — same keys, same values, not one plate swapped.
    assert mirror(app, customer_id) == before
    assert mirror(app, customer_id)["vehicle_reg_no"] == "OLD 111"
    assert mirror(app, customer_id)["vehicle_make"] == "TOYOTA"
    assert queued(app) == []


def test_additional_vehicle_is_approved_and_never_becomes_main(app, branches):
    customer_id = make_customer(app, branches["midrand"])
    main_id = with_main(app, customer_id, "OLD 111")
    waiting_id = queue_scan(app, "NEW 222", branches["midrand"])

    new_id = allocate(app, waiting_id, customer_id, "additional")
    with app.app_context():
        row = vehicles.get_vehicle(new_id)
        assert vehicles.vehicle_status(row) == vehicles.STATUS_APPROVED
        assert int(row["customer_id"]) == customer_id
        assert vehicles.get_main_vehicle_id(customer_id) == main_id
        # Editing the additional vehicle must not rewrite the main mirror either.
        vehicles.update_vehicle(new_id, {"make": "ISUZU"})
        assert vehicles.get_main_vehicle_id(customer_id) == main_id
    assert mirror(app, customer_id)["vehicle_reg_no"] == "OLD 111"


# ── replace ─────────────────────────────────────────────────────────────────


def test_replace_rewrites_the_invoice_mirror_and_keeps_the_old_row(app, branches):
    customer_id = make_customer(app, branches["midrand"])
    old_id = with_main(app, customer_id, "OLD 111", make="TOYOTA", colour="BLUE")
    waiting_id = queue_scan(app, "NEW 222", branches["midrand"], make="NISSAN", colour="RED")

    result = allocate(app, waiting_id, customer_id, "replace_main")

    assert result.mode_applied == "replace_main"
    assert result.previous_main_registration == "OLD 111"
    with app.app_context():
        main = vehicles.get_main_vehicle(customer_id)
        assert int(main["id"]) == int(result)
        assert main["registration"] == "NEW 222"
        assert vehicles.get_main_vehicle_id(customer_id) != old_id
        # The previous main vehicle is still on the client — demoted, never deleted.
        old = vehicles.get_vehicle(old_id)
        assert old is not None and int(old["customer_id"]) == customer_id
    rows = {row["registration"]: row for row in vehicles_for(app, customer_id)}
    assert set(rows) == {"OLD 111", "NEW 222"}
    # The invoice-facing mirror follows the new main vehicle, including the make.
    fields = mirror(app, customer_id)
    assert fields["vehicle_reg_no"] == "NEW 222"
    assert fields["vehicle_make"] == "NISSAN"
    assert queued(app) == []


def test_replace_clears_details_the_new_vehicle_does_not_carry(app, branches):
    customer_id = make_customer(app, branches["midrand"])
    with_main(app, customer_id, "OLD 111", vin="OLDVIN000000000001")
    assert mirror(app, customer_id)["vehicle_vin"] == "OLDVIN000000000001"
    waiting_id = queue_scan(app, "NEW 222", branches["midrand"])

    allocate(app, waiting_id, customer_id, "replace_main")

    fields = mirror(app, customer_id)
    assert fields["vehicle_reg_no"] == "NEW 222"
    assert fields.get("vehicle_vin") != "OLDVIN000000000001"


# ── no main vehicle yet ─────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["additional", "replace_main", "main", None])
def test_a_client_with_no_main_vehicle_gets_it_as_main_either_way(app, branches, mode):
    customer_id = make_customer(app, branches["midrand"])
    waiting_id = queue_scan(app, "NEW 222", branches["midrand"])

    result = allocate(app, waiting_id, customer_id, mode)

    assert result.mode_applied == "main"
    assert result.previous_main_registration == ""
    with app.app_context():
        assert vehicles.get_main_vehicle_id(customer_id) == int(result)
        assert vehicles.get_main_vehicle(customer_id)["registration"] == "NEW 222"
    assert mirror(app, customer_id)["vehicle_reg_no"] == "NEW 222"


def test_a_client_with_hand_typed_vehicle_fields_is_treated_as_having_a_main(app, branches):
    customer_id = make_customer(app, branches["midrand"])
    with app.app_context():
        get_db().execute(
            "UPDATE customers SET custom_fields_json = ? WHERE id = ?",
            ('{"vehicle_reg_no": "LEG 123", "vehicle_make": "FORD"}', customer_id),
        )
        get_db().commit()
    waiting_id = queue_scan(app, "NEW 222", branches["midrand"])

    # "additional" leaves the typed main details alone …
    result = allocate(app, waiting_id, customer_id, "additional")
    assert result.mode_applied == "additional"
    assert result.previous_main_registration == "LEG 123"
    assert mirror(app, customer_id)["vehicle_reg_no"] == "LEG 123"


# ── unchanged behaviour ─────────────────────────────────────────────────────


def test_no_mode_with_an_existing_main_is_refused_not_overwritten(app, branches):
    """The pending panel and older callers post no mode: never silently replace the main."""
    customer_id = make_customer(app, branches["midrand"])
    main_id = with_main(app, customer_id, "OLD 111")
    waiting_id = queue_scan(app, "NEW 222", branches["midrand"])

    with pytest.raises(ValueError) as refused:
        allocate(app, waiting_id, customer_id, None)

    assert "main vehicle" in str(refused.value)
    assert "choose" in str(refused.value).lower()
    with app.app_context():
        assert vehicles.get_main_vehicle_id(customer_id) == main_id
    assert len(vehicles_for(app, customer_id)) == 1
    assert len(queued(app)) == 1                       # nothing claimed, nothing written


def test_unknown_mode_is_refused_before_anything_is_claimed(app, branches):
    customer_id = make_customer(app, branches["midrand"])
    waiting_id = queue_scan(app, "NEW 222", branches["midrand"])

    with pytest.raises(ValueError) as refused:
        allocate(app, waiting_id, customer_id, "obliterate")

    assert "choose" in str(refused.value).lower()
    assert len(queued(app)) == 1
    assert vehicles_for(app, customer_id) == []


@pytest.mark.parametrize("alias,applied", [("add", "additional"), ("keep_main", "additional"),
                                           ("replace", "replace_main")])
def test_friendly_mode_aliases(app, branches, alias, applied):
    customer_id = make_customer(app, branches["midrand"])
    with_main(app, customer_id, "OLD 111")
    waiting_id = queue_scan(app, "NEW 222", branches["midrand"])

    assert allocate(app, waiting_id, customer_id, alias).mode_applied == applied


def test_a_stale_waiting_id_is_refused(app, branches):
    customer_id = make_customer(app, branches["midrand"])
    with pytest.raises(ValueError) as refused:
        allocate(app, 9999, customer_id, "additional")
    assert "no longer waiting" in str(refused.value)


def test_the_same_waiting_scan_cannot_be_allocated_twice(app, branches):
    customer_id = make_customer(app, branches["midrand"])
    waiting_id = queue_scan(app, "NEW 222", branches["midrand"])
    allocate(app, waiting_id, customer_id, "additional")
    with pytest.raises(ValueError) as refused:
        allocate(app, waiting_id, customer_id, "additional")
    assert "no longer waiting" in str(refused.value)
    assert len(vehicles_for(app, customer_id)) == 1


def test_a_duplicate_plate_leaves_the_scan_waiting(app, branches):
    """Somebody records the plate between scanning and allocation: refused, scan stays waiting."""
    customer_id = make_customer(app, branches["midrand"])
    other_id = make_customer(app, branches["midrand"], name="Someone Else")
    waiting_id = queue_scan(app, "TAKEN 1", branches["midrand"])
    with_main(app, other_id, "TAKEN 1")

    with pytest.raises(ValueError):
        allocate(app, waiting_id, customer_id, "additional")

    assert len(queued(app)) == 1
    assert vehicles_for(app, customer_id) == []


def test_a_scan_waiting_at_another_branch_is_refused(app, branches):
    customer_id = make_customer(app, branches["roodepoort"])
    waiting_id = queue_scan(app, "CROSS 111", branches["midrand"])

    with pytest.raises(ValueError) as refused:
        allocate(app, waiting_id, customer_id, "additional")

    assert "waiting at" in str(refused.value)
    assert len(queued(app)) == 1
    assert vehicles_for(app, customer_id) == []


def test_a_client_outside_the_session_branch_scope_is_refused(app, branches):
    customer_id = make_customer(app, branches["midrand"])
    waiting_id = queue_scan(app, "SCOPE 111", branches["midrand"])

    with app.test_request_context("/customers"):
        from flask import session

        session["user_id"] = owner_id(app)
        session["user_role"] = "staff"
        session["can_view_all_branches"] = False
        session["branch_ids"] = [branches["roodepoort"]]
        with pytest.raises(ValueError) as refused:
            vehicles.allocate_waiting_vehicle(waiting_id, customer_id, "additional")

    assert "branches you can access" in str(refused.value)
    assert len(queued(app)) == 1


# ── the pending path is unchanged ───────────────────────────────────────────


def test_a_pending_client_still_gets_the_scan_as_its_main_vehicle(app, branches):
    from app.services import portal_registrations

    with app.app_context():
        customer_id = create_customer(
            {"name": "Charmaine Naidoo", "customer_type": "individual", "phone": "0821234567"},
            branch_id=branches["midrand"],
        )
        portal_registrations.record_pending_registration(
            customer_id, branches["midrand"], f"REG-{customer_id}", name="Charmaine Naidoo",
            phone="0821234567",
        )
    waiting_id = queue_scan(app, "PEND 111", branches["midrand"])

    result = allocate(app, waiting_id, customer_id)     # no mode, exactly as the panel posts

    assert result.mode_applied == "main"
    with app.app_context():
        assert vehicles.get_main_vehicle_id(customer_id) == int(result)
        assert vehicles.get_main_vehicle(customer_id)["registration"] == "PEND 111"
        assert portal_registrations.pending_registration_for_customer(customer_id)["status"] == "pending"


# ── route ───────────────────────────────────────────────────────────────────


def post(client, customer_id, waiting_id, mode=None):
    data = {"waiting_id": waiting_id, "csrf_token": CSRF}
    if mode is not None:
        data["mode"] = mode
    return client.post(f"/customers/{customer_id}/allocate-waiting-vehicle", data=data)


def test_route_additional_flashes_and_redirects_to_the_main_vehicle(app, client, branches):
    customer_id = make_customer(app, branches["midrand"])
    with_main(app, customer_id, "OLD 111")
    waiting_id = queue_scan(app, "NEW 222", branches["midrand"])
    sign_in(client, app)

    response = post(client, customer_id, waiting_id, "additional")

    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/customers/{customer_id}/edit#main-vehicle")
    html = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "allocated as an additional vehicle" in html
    assert "OLD 111 is unchanged" in html
    assert len(vehicles_for(app, customer_id)) == 2
    assert mirror(app, customer_id)["vehicle_reg_no"] == "OLD 111"


def test_route_replace_flashes_the_replaced_plate(app, client, branches):
    customer_id = make_customer(app, branches["midrand"])
    with_main(app, customer_id, "OLD 111")
    waiting_id = queue_scan(app, "NEW 222", branches["midrand"])
    sign_in(client, app)

    response = post(client, customer_id, waiting_id, "replace_main")

    assert response.status_code == 302
    html = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "replacing OLD 111" in html
    assert mirror(app, customer_id)["vehicle_reg_no"] == "NEW 222"


def test_route_no_main_still_redirects_to_the_main_vehicle_and_flashes(app, client, branches):
    customer_id = make_customer(app, branches["midrand"])
    waiting_id = queue_scan(app, "NEW 222", branches["midrand"])
    sign_in(client, app)

    response = post(client, customer_id, waiting_id, "additional")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("#main-vehicle")
    html = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "allocated as the customer&#39;s main vehicle" in html or \
           "allocated as the customer's main vehicle" in html


def test_route_names_the_choice_in_the_error_when_no_mode_is_posted(app, client, branches):
    customer_id = make_customer(app, branches["midrand"])
    with_main(app, customer_id, "OLD 111")
    waiting_id = queue_scan(app, "NEW 222", branches["midrand"])
    sign_in(client, app)

    response = post(client, customer_id, waiting_id)

    html = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "choose whether to replace it" in html
    assert len(vehicles_for(app, customer_id)) == 1
    assert len(queued(app)) == 1


def test_route_refuses_an_unknown_mode(app, client, branches):
    customer_id = make_customer(app, branches["midrand"])
    waiting_id = queue_scan(app, "NEW 222", branches["midrand"])
    sign_in(client, app)

    response = post(client, customer_id, waiting_id, "obliterate")

    assert response.status_code == 302
    html = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Choose whether to keep the main vehicle" in html
    assert len(queued(app)) == 1


def test_route_requires_the_scan_vehicle_module(app, client, branches):
    customer_id = make_customer(app, branches["midrand"])
    waiting_id = queue_scan(app, "NEW 222", branches["midrand"])
    sign_in(client, app, role="staff", modules=["customers"])

    assert post(client, customer_id, waiting_id, "additional").status_code == 403
    assert len(queued(app)) == 1


def test_route_pending_client_keeps_the_pending_redirect(app, client, branches):
    from app.services import portal_registrations

    with app.app_context():
        customer_id = create_customer(
            {"name": "Pieter van Wyk", "customer_type": "individual", "phone": "0821234567"},
            branch_id=branches["midrand"],
        )
        portal_registrations.record_pending_registration(
            customer_id, branches["midrand"], f"REG-{customer_id}", name="Pieter van Wyk",
            phone="0821234567",
        )
    waiting_id = queue_scan(app, "PEND 222", branches["midrand"])
    sign_in(client, app)

    response = post(client, customer_id, waiting_id)

    assert response.headers["Location"].endswith("#pending-registration")


# ── the page really offers the choice ───────────────────────────────────────


def test_edit_page_offers_the_choice_for_an_existing_customer(app, client, branches):
    customer_id = make_customer(app, branches["midrand"])
    with_main(app, customer_id, "OLD 111")
    queue_scan(app, "NEW 222", branches["midrand"])
    sign_in(client, app)

    html = client.get(f"/customers/{customer_id}/edit").get_data(as_text=True)

    assert "Allocate from list" in html
    assert 'id="allocate-waiting-dialog-main"' in html
    assert 'value="additional"' in html and 'value="replace_main"' in html
    assert "NEW 222" in html
    assert "Main client vehicle is <b>OLD 111</b>" in html
    # One dialog only, and it is not nested inside the customer form.
    assert html.count('id="allocate-waiting-dialog-main"') == 1
    assert 'id="allocate-waiting-dialog"' not in html


def test_edit_page_asks_no_choice_when_there_is_no_main_vehicle(app, client, branches):
    customer_id = make_customer(app, branches["midrand"])
    queue_scan(app, "NEW 222", branches["midrand"])
    sign_in(client, app)

    html = client.get(f"/customers/{customer_id}/edit").get_data(as_text=True)

    assert "Allocate from list" in html
    assert 'name="mode"' not in html
    assert "This becomes the client's main vehicle" in html


def test_edit_page_hides_the_button_when_the_queue_is_empty(app, client, branches):
    customer_id = make_customer(app, branches["midrand"])
    with_main(app, customer_id, "OLD 111")
    sign_in(client, app)

    html = client.get(f"/customers/{customer_id}/edit").get_data(as_text=True)

    assert "Allocate from list" not in html


def test_edit_page_offers_only_the_clients_own_branch(app, client, branches):
    customer_id = make_customer(app, branches["midrand"])
    with_main(app, customer_id, "OLD 111")
    queue_scan(app, "MINE 111", branches["midrand"])
    queue_scan(app, "THEIRS 111", branches["roodepoort"])
    sign_in(client, app)

    html = client.get(f"/customers/{customer_id}/edit").get_data(as_text=True)

    assert "MINE 111" in html
    assert "THEIRS 111" not in html


def test_detail_page_renders_one_allocate_dialog_for_a_pending_client(app, client, branches):
    """The panel is the only allocate UI on the detail page — one dialog, never two."""
    from app.services import portal_registrations

    with app.app_context():
        customer_id = create_customer(
            {"name": "Nomsa Dlamini", "customer_type": "individual", "phone": "0821234567"},
            branch_id=branches["midrand"],
        )
        portal_registrations.record_pending_registration(
            customer_id, branches["midrand"], f"REG-{customer_id}", name="Nomsa Dlamini",
            phone="0821234567",
        )
    queue_scan(app, "PEND 333", branches["midrand"])
    sign_in(client, app)

    html = client.get(f"/customers/{customer_id}").get_data(as_text=True)

    assert "Allocate from list" in html
    assert html.count('id="allocate-waiting-dialog-main"') == 1
    assert 'id="allocate-waiting-dialog"' not in html


def test_a_pending_client_with_a_main_vehicle_gets_the_choice_too(app, client, branches):
    """The panel used to refuse this outright; now it offers keep-or-replace like the edit page."""
    from app.services import portal_registrations

    with app.app_context():
        customer_id = create_customer(
            {"name": "Johan Botha", "customer_type": "individual", "phone": "0821234567"},
            branch_id=branches["midrand"],
        )
        portal_registrations.record_pending_registration(
            customer_id, branches["midrand"], f"REG-{customer_id}", name="Johan Botha",
            phone="0821234567",
        )
    with_main(app, customer_id, "KEEP 999")
    queue_scan(app, "PEND 444", branches["midrand"])
    sign_in(client, app)

    html = client.get(f"/customers/{customer_id}/edit").get_data(as_text=True)

    assert 'value="additional"' in html and 'value="replace_main"' in html
    assert "Main client vehicle is <b>KEEP 999</b>" in html
