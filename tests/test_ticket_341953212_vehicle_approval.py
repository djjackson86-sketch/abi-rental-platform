"""ABI-341953212: the scan screen's own vehicle approval and its explicit "Approve client?" step.

The owner's change moves the client approval onto the scan screen and gives the *vehicle* an
approval state of its own:

* the review form's two saving actions — **Approve & Save Vehicle** (``action=approve``, the
  default) and **Disapprove Vehicle** (``action=disapprove``) — record the vehicle's **own**
  ``vehicles.status`` (``approved`` / ``unapproved``). The vehicle always stays on the client:
  its state is deliberately separate from the client's verification (``customers.client_verified``),
  their rental block (``is_blocked``) and their portal approval, so one can never drift into another;
* an unapproved vehicle reads as a red **Unapproved** state on the client's file and on the review
  panel when the same plate is scanned again — and "Approve & Save Vehicle" puts it back;
* the client approval is now an explicit Yes/No ("``approve_client=1``") asked only when the client
  on the form is a **saved, ID-verified, still-pending portal registration**; "No" — or no answer —
  saves the vehicle and leaves the client exactly as they were;
* approving the client and recording their main vehicle are one atomic step: the approval's own
  checks run **before** any row is written, and a still-refusing approval rolls the vehicle write
  back (including a transfer), so a failed approval can never leave a half-saved vehicle behind;
* the pending-registration panel no longer carries its own "Approve client" button (or its inline
  form) — it points at the scan screen instead;
* the scanner is reached two ways and both behave the same for the same client: from the
  verification checklist (``customer_id`` + ``return_to_verification=1``, which returns staff to
  ``customers.edit#pending-registration``) and from the ordinary scanner (no return intent);
* ``GET /scan-vehicle/client-approval-state`` is the small JSON the picker asks so the "Approve
  client?" dialog is armed for the client actually **picked on the page** — it answers
  ``{customer_id, ask_approval}`` only for a persisted, ID-verified, still-pending client and needs
  the ``scan_vehicle`` module like every other scanner route.

Fixtures are synthetic (no real plate/VIN/ID data) and notifications are off.
"""

import json
from pathlib import Path

import pytest

from app import create_app
from app.db import get_db
from app.services import branches as branches_service
from app.services import portal_registrations, vehicles
from app.services.customers import create_customer, list_customers

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "disc"


def disc_text(name):
    return (FIXTURE_DIR / f"{name}.txt").read_text(encoding="utf-8")


# ── fixtures & helpers ──────────────────────────────────────────────────────


@pytest.fixture()
def app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "vehicle_approval.sqlite"),
            "SECRET_KEY": "test",
            "ADMIN_EMAIL": "admin@abi.local",
            "ADMIN_PASSWORD": "admin123",
            "TELEGRAM_NOTIFICATIONS_ENABLED": "",
        }
    )


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def branches(app):
    with app.app_context():
        return {
            "midrand": branches_service.create_branch({"name": "Midrand Depot", "active": 1}),
            "roodepoort": branches_service.create_branch({"name": "Roodepoort Depot", "active": 1}),
        }


def owner_id(app):
    with app.app_context():
        return get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()["id"]


def sign_in(client, app, *, role="owner", branch_id=None, branch_ids=(), modules=(), can_view_all=True):
    """The pending-registration fixtures' sign-in: a real session, so branch scope and modules bite."""
    with client.session_transaction() as session:
        session["user_id"] = owner_id(app)
        session["user_role"] = role
        session["user_name"] = "Synthetic Tester"
        session["initials"] = "ST"
        session["branch_id"] = branch_id
        session["can_view_all_branches"] = can_view_all
        session["branch_ids"] = list(branch_ids)
        session["staff_modules"] = list(modules)
        session["_csrf_tokens"] = {"portal_approval": "synthetic-token"}


def make_customer(app, name="Charmaine Mokoena", phone="0821234567"):
    with app.app_context():
        return create_customer({"name": name, "customer_type": "individual", "phone": phone})


def make_pending(app, name="Pieter van Wyk", phone="0835551234", *, verified=True, branch_id=None, reference="PEND-1"):
    """A self-registered client: the customer row plus their pending portal registration."""
    with app.app_context():
        customer_id = create_customer({"name": name, "customer_type": "individual", "phone": phone})
        registration_id = portal_registrations.record_pending_registration(
            customer_id, branch_id, reference, name=name, phone=phone
        )
        if verified:
            db = get_db()
            db.execute("UPDATE customers SET client_verified = 1 WHERE id = ?", (customer_id,))
            db.commit()
    return customer_id, registration_id


def registration_status(app, registration_id):
    with app.app_context():
        row = portal_registrations.registration_by_id(registration_id)
    return row["status"] if row else None


def vehicle_rows(app, registration):
    """Every vehicles row carrying this plate, whatever client it is on."""
    key = registration.replace(" ", "").upper()
    with app.app_context():
        return [
            dict(row)
            for row in get_db()
            .execute("SELECT * FROM vehicles WHERE REPLACE(UPPER(registration), ' ', '') = ?", (key,))
            .fetchall()
        ]


def vehicles_for(app, customer_id):
    with app.app_context():
        return [dict(row) for row in vehicles.list_vehicles(customer_id)]


def visible_customer_ids(app):
    with app.app_context():
        return [row["id"] for row in list_customers()]


def customer_snapshot(app, customer_id):
    with app.app_context():
        row = get_db().execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        return dict(row) if row else None


def save_scan(client, customer_id, registration, *, action="approve", approve_client=None, follow=True, **overrides):
    """Post the review form the way the template renders it (both saving actions are named buttons)."""
    data = {
        "customer_id": str(customer_id) if customer_id else "",
        "customer_pick": "",
        "csrf_token": "synthetic-token",
        "registration": registration,
        "make": "TOYOTA",
        "model": "HILUX 2.4 GD-6",
        "vin": "AHTFR22G10L123456",
        "engine_number": "2GD1234567",
        "colour": "WHITE",
        "licence_number": "T9876543210X",
        "registration_number": "ZZ1234Z",
        "licence_disk_expiry": "2027-03-31",
        "source": "scan",
        "make_main": "yes",
        "tare_kg": "",
        "gvm_kg": "",
        "action": action,
    }
    if approve_client is not None:
        data["approve_client"] = approve_client
    data.update(overrides)
    return client.post("/scan-vehicle/save", data=data, follow_redirects=follow)


def manual_scan(client, customer_id=None, *, return_to_verification=False, follow=True):
    data = {"action": "manual"}
    if customer_id:
        data["customer_id"] = str(customer_id)
    if return_to_verification:
        data["return_to_verification"] = "1"
    return client.post("/scan-vehicle", data=data, follow_redirects=follow)


def approval_state(client, *, customer_id=None, customer_pick=None):
    query = {}
    if customer_id is not None:
        query["customer_id"] = str(customer_id)
    if customer_pick is not None:
        query["customer_pick"] = customer_pick
    return client.get("/scan-vehicle/client-approval-state", query_string=query)


def body_of(response):
    return response.get_data(as_text=True)


# ── the two scanner entry contexts ──────────────────────────────────────────


def test_the_manual_entry_context_echoes_the_return_intent_only_when_asked(app, client, branches):
    """Both doors into the scanner render the same review form; only the checklist's door carries
    the return intent, and the same client arms the same question either way."""
    customer_id, _ = make_pending(app, branch_id=branches["midrand"])
    sign_in(client, app)

    with_intent = body_of(manual_scan(client, customer_id, return_to_verification=True))
    assert 'name="return_to_verification" value="1"' in with_intent
    assert 'name="action" value="manual"' in with_intent

    plain = body_of(manual_scan(client, customer_id))
    assert 'name="return_to_verification"' not in plain

    for rendered in (with_intent, plain):
        assert 'name="registration"' in rendered                       # the fields are offered
        assert 'name="customer_pick"' in rendered                      # and the client is on the form
        assert 'value="Pieter van Wyk' in rendered                     # the saved client, pre-selected
        assert 'id="approve-client-dialog"' not in rendered  # ABI-341953220: no redundant question
        assert 'id="scan-approve-save"' in rendered                    # Approve & Save Vehicle
        assert 'id="scan-disapprove-vehicle"' in rendered              # Disapprove Vehicle
        assert 'name="approve_client"' in rendered                     # the hidden Yes/No field


def test_the_approval_dialog_is_never_armed_without_a_pending_verified_client(app, client, branches):
    ordinary = make_customer(app, name="Charmaine Mokoena", phone="0821234567")
    unverified, _ = make_pending(
        app, name="Unverified Una", phone="0830000001", verified=False, branch_id=branches["midrand"]
    )
    sign_in(client, app)
    for customer_id in (ordinary, unverified, None):
        page = body_of(manual_scan(client, customer_id))
        assert '<dialog id="approve-client-dialog"' not in page
        assert client.get("/scan-vehicle/client-approval-state", query_string={"customer_id": customer_id or ""}).json["ask_approval"] is False


def test_the_save_returns_to_the_verification_checklist_only_when_asked(app, client):
    customer_id = make_customer(app)
    sign_in(client, app)

    asked = save_scan(client, customer_id, "AAA111GP", return_to_verification="1", follow=False)
    assert asked.status_code == 302
    assert asked.headers["Location"].endswith("#pending-registration")

    ordinary = save_scan(client, customer_id, "BBB222GP", follow=False)
    assert ordinary.status_code == 302
    assert ordinary.headers["Location"] == f"/customers/{customer_id}/edit"


# ── "Approve client?" — Yes / No / unverified ───────────────────────────────


def test_approve_yes_saves_the_vehicle_and_approves_the_pending_client(app, client, branches):
    customer_id, registration_id = make_pending(app, branch_id=branches["midrand"])
    sign_in(client, app)

    body = body_of(save_scan(client, customer_id, "NB72XMGP", approve_client="1"))
    assert "Client approved" in body
    assert "allocated to Pieter van Wyk" in body
    assert registration_status(app, registration_id) == "approved"
    assert visible_customer_ids(app) == [customer_id], "an approved client is a normal customer"

    rows = vehicles_for(app, customer_id)
    assert len(rows) == 1
    assert rows[0]["status"] == vehicles.STATUS_APPROVED


@pytest.mark.parametrize("answer", ["0", "", None])
def test_final_vehicle_check_approves_without_an_answer(app, client, branches, answer):
    customer_id, registration_id = make_pending(app, branch_id=branches["midrand"])
    sign_in(client, app)

    body = body_of(save_scan(client, customer_id, "NB72XMGP", approve_client=answer))
    assert "allocated to Pieter van Wyk" in body, "the vehicle still saves"
    assert "Client approved" not in body
    assert registration_status(app, registration_id) == "approved"
    assert visible_customer_ids(app) == [customer_id], "both saved checks complete approval"
    assert len(vehicles_for(app, customer_id)) == 1


def test_approve_yes_is_refused_for_an_unverified_client_before_anything_is_written(app, client, branches):
    customer_id, registration_id = make_pending(app, verified=False, branch_id=branches["midrand"])
    sign_in(client, app)

    body = body_of(save_scan(client, customer_id, "NB72XMGP", approve_client="1"))
    assert "ID verification (Client verified = Yes)" in body
    assert vehicles_for(app, customer_id) == []
    assert registration_status(app, registration_id) == "pending"


def test_approve_yes_is_refused_for_a_client_with_no_pending_registration(app, client):
    customer_id = make_customer(app)
    sign_in(client, app)

    body = body_of(save_scan(client, customer_id, "NB72XMGP", approve_client="1"))
    assert "no pending portal registration to approve" in body
    assert vehicles_for(app, customer_id) == []


def test_approve_yes_is_refused_for_a_blocked_client(app, client, branches):
    customer_id, registration_id = make_pending(app, branch_id=branches["midrand"])
    with app.app_context():
        db = get_db()
        db.execute("UPDATE customers SET is_blocked = 1, blocked_reason = 'Chargeback' WHERE id = ?", (customer_id,))
        db.commit()
    sign_in(client, app)

    body = body_of(save_scan(client, customer_id, "NB72XMGP", approve_client="1"))
    assert "This client is blocked" in body
    assert vehicles_for(app, customer_id) == []
    assert registration_status(app, registration_id) == "pending"


def test_approving_the_client_and_disapproving_the_vehicle_in_one_post_is_refused(app, client, branches):
    customer_id, registration_id = make_pending(app, branch_id=branches["midrand"])
    sign_in(client, app)

    body = body_of(save_scan(client, customer_id, "NB72XMGP", action="disapprove", approve_client="1"))
    assert "save them one at a time" in body
    assert vehicles_for(app, customer_id) == []
    assert registration_status(app, registration_id) == "pending"


# ── disapprove: the vehicle's own red state persists ────────────────────────


def test_disapprove_records_the_vehicle_unapproved_and_the_red_state_persists(app, client):
    customer_id = make_customer(app)
    sign_in(client, app)

    body = body_of(save_scan(client, customer_id, "ABC123GP", action="disapprove", make_main="yes"))
    assert "allocated to Charmaine Mokoena" in body
    rows = vehicles_for(app, customer_id)
    assert len(rows) == 1
    assert rows[0]["status"] == vehicles.STATUS_UNAPPROVED
    # The vehicle's state never moves into the client's own verification.
    assert not customer_snapshot(app, customer_id)["client_verified"]
    assert "Client approved" not in body

    # ... it reads as the red state on the client's file ...
    page = client.get(f"/customers/{customer_id}/edit").get_data(as_text=True)
    assert "vehicle-unapproved-badge" in page and "Unapproved" in page

    # ... and the review panel says so when the same plate is scanned again.
    review = body_of(
        client.post(
            "/scan-vehicle",
            data={"disc_text": disc_text("natis_positional"), "customer_id": str(customer_id)},
            follow_redirects=True,
        )
    )
    assert "recorded on the client's file as" in review
    assert "vehicle-unapproved-badge" in review

    # "Approve & Save Vehicle" puts the very same vehicle back, with no second row.
    save_scan(client, customer_id, "ABC123GP", action="approve")
    rows = vehicles_for(app, customer_id)
    assert len(rows) == 1
    assert rows[0]["status"] == vehicles.STATUS_APPROVED
    assert "vehicle-unapproved-badge" not in client.get(f"/customers/{customer_id}/edit").get_data(as_text=True)


def test_disapproving_a_vehicle_never_changes_the_client_own_approval(app, client, branches):
    customer_id, registration_id = make_pending(app, branch_id=branches["midrand"])
    before = customer_snapshot(app, customer_id)
    sign_in(client, app)

    save_scan(client, customer_id, "NB72XMGP", action="disapprove")
    after = customer_snapshot(app, customer_id)
    assert after["client_verified"] == before["client_verified"]
    assert after["is_blocked"] == before["is_blocked"]
    assert registration_status(app, registration_id) == "pending"
    assert visible_customer_ids(app) == []


# ── the duplicate plate stays one row ───────────────────────────────────────


def test_the_same_plate_saved_twice_updates_in_place_without_a_second_row(app, client):
    customer_id = make_customer(app)
    sign_in(client, app)

    first = body_of(save_scan(client, customer_id, "ABC123GP", action="disapprove"))
    assert "allocated to Charmaine Mokoena" in first

    second = body_of(save_scan(client, customer_id, "ABC123GP", action="approve", colour="SILVER"))
    assert "already recorded for Charmaine Mokoena" in second
    assert "updated the existing vehicle instead of adding a second one" in second

    rows = vehicles_for(app, customer_id)
    assert len(rows) == 1, "a re-scan is a correction, never a second vehicle"
    assert rows[0]["colour"] == "SILVER"
    assert rows[0]["status"] == vehicles.STATUS_APPROVED, "the re-save's action moved the status in place"


# ── the pending panel's own approve button is gone ──────────────────────────


def test_the_pending_panel_no_longer_offers_its_own_approve_button(app, client, branches):
    customer_id, _ = make_pending(app, branch_id=branches["midrand"])
    sign_in(client, app)

    page = client.get(f"/customers/{customer_id}/edit").get_data(as_text=True)
    assert "pending-registration-panel" in page
    assert "pending-approve-form" not in page, "the inline approve form was removed"
    assert "data-portal-approval" not in page
    assert "Approve client</button>" not in page
    # ... the approval moved to the scan screen, which the panel points at ...
    assert "becomes Approved automatically" in page
    assert f"/scan-vehicle?customer_id={customer_id}" in page
    # ... while the refusal control stays here.
    assert "Disapprove client" in page


# ── access: module, branch scope, sign-in ───────────────────────────────────


def test_the_scan_save_and_the_approval_state_need_the_scan_module(app, client, branches):
    customer_id, registration_id = make_pending(app, branch_id=branches["midrand"])
    sign_in(
        client,
        app,
        role="staff",
        branch_id=branches["midrand"],
        branch_ids=[branches["midrand"]],
        modules=["dashboard", "customers"],
        can_view_all=False,
    )
    assert client.get("/scan-vehicle").status_code == 403
    assert save_scan(client, customer_id, "NB72XMGP", approve_client="1", follow=False).status_code == 403
    assert approval_state(client, customer_id=customer_id).status_code == 403
    assert registration_status(app, registration_id) == "pending"
    assert vehicles_for(app, customer_id) == []


def test_a_pending_client_outside_the_session_branch_cannot_be_approved_from_the_scan(app, client, branches):
    customer_id, registration_id = make_pending(
        app, branch_id=branches["roodepoort"], reference="ROOD-1"
    )
    sign_in(
        client,
        app,
        role="staff",
        branch_id=branches["midrand"],
        branch_ids=[branches["midrand"]],
        modules=["scan_vehicle", "customers"],
        can_view_all=False,
    )
    body = body_of(save_scan(client, customer_id, "NB72XMGP", approve_client="1"))
    assert "outside the branches you can access" in body
    assert vehicles_for(app, customer_id) == []
    assert registration_status(app, registration_id) == "pending"

    # An all-branch account may, and the vehicle comes with the approval.
    sign_in(client, app)
    assert "Client approved" in body_of(save_scan(client, customer_id, "NB72XMGP", approve_client="1"))
    assert registration_status(app, registration_id) == "approved"


# ── invalid fields: refused before a row exists ─────────────────────────────


@pytest.mark.parametrize(
    "posted, message",
    [
        ({"tare_kg": "very heavy"}, "Tare mass must be a number in kilograms"),
        ({"gvm_kg": "-5"}, "GVM cannot be negative"),
        ({"licence_disk_expiry": "31-31-2027"}, "Licence disk expiry must be a date"),
    ],
)
def test_invalid_fields_are_refused_before_any_row_is_written(app, client, posted, message):
    customer_id = make_customer(app)
    sign_in(client, app)
    body = body_of(save_scan(client, customer_id, "ABC123GP", **posted))
    assert message in body
    assert vehicles_for(app, customer_id) == []


def test_invalid_fields_do_not_approve_the_client_either(app, client, branches):
    customer_id, registration_id = make_pending(app, branch_id=branches["midrand"])
    sign_in(client, app)
    body = body_of(save_scan(client, customer_id, "ABC123GP", approve_client="1", tare_kg="very heavy"))
    assert "Tare mass must be a number in kilograms" in body
    assert registration_status(app, registration_id) == "pending"
    assert vehicles_for(app, customer_id) == []


# ── an approval that still fails rolls the vehicle write back ───────────────


def _explode(*_args, **_kwargs):
    raise ValueError("The approval step failed unexpectedly.")


def test_a_failed_client_approval_rolls_the_new_vehicle_back(app, client, branches, monkeypatch):
    customer_id, registration_id = make_pending(app, branch_id=branches["midrand"])
    sign_in(client, app)
    monkeypatch.setattr(portal_registrations, "approve_registration", _explode)

    body = body_of(save_scan(client, customer_id, "NB72XMGP", approve_client="1"))
    assert "The approval step failed unexpectedly." in body
    assert "Client approved" not in body
    assert vehicles_for(app, customer_id) == []
    assert vehicle_rows(app, "NB72XMGP") == []
    assert registration_status(app, registration_id) == "pending"


def test_an_approval_refused_for_a_missing_main_vehicle_rolls_the_vehicle_back(app, client, branches):
    """The main-vehicle check is the one left to the approval itself, so this is the real,
    unpatched failure path: the vehicle must not survive it."""
    customer_id, registration_id = make_pending(app, branch_id=branches["midrand"])
    sign_in(client, app)

    body = body_of(save_scan(client, customer_id, "NB72XMGP", approve_client="1", make_main="no"))
    assert "main vehicle" in body
    assert "Client approved" not in body
    assert vehicles_for(app, customer_id) == []
    assert registration_status(app, registration_id) == "pending"


def test_a_failed_client_approval_rolls_the_transfer_back(app, client, branches, monkeypatch):
    other_owner = make_customer(app, name="Other Owner", phone="0837654321")
    customer_id, registration_id = make_pending(app, branch_id=branches["midrand"])
    sign_in(client, app)
    assert "allocated to Other Owner" in body_of(save_scan(client, other_owner, "ABC123GP"))

    monkeypatch.setattr(portal_registrations, "approve_registration", _explode)
    body = body_of(
        save_scan(client, customer_id, "ABC123GP", transfer="1", approve_client="1", colour="SILVER")
    )
    assert "The approval step failed unexpectedly." in body

    rows = vehicle_rows(app, "ABC123GP")
    assert len(rows) == 1, "the plate is never duplicated by a rolled-back transfer"
    assert rows[0]["customer_id"] == other_owner, "the move is undone, back to the original owner"
    assert [row["registration"] for row in vehicles_for(app, customer_id)] == []
    assert registration_status(app, registration_id) == "pending"
    assert vehicles_for(app, other_owner)[0]["customer_id"] == other_owner


def test_a_failed_approval_leaves_the_transferred_vehicle_snapshot_unchanged(app, client, branches, monkeypatch):
    """The atomic-transfer snapshot: after a refused approval the plate's row, the owner's mirror
    and the client's approval are exactly what they were before the request."""
    other_owner = make_customer(app, name="Other Owner", phone="0837654321")
    customer_id, registration_id = make_pending(app, branch_id=branches["midrand"])
    sign_in(client, app)
    assert "allocated to Other Owner" in body_of(save_scan(client, other_owner, "ABC123GP", colour="WHITE"))

    before_row = vehicle_rows(app, "ABC123GP")[0]
    before_owner = customer_snapshot(app, other_owner)

    monkeypatch.setattr(portal_registrations, "approve_registration", _explode)
    save_scan(client, customer_id, "ABC123GP", transfer="1", approve_client="1", colour="SILVER")

    after_row = vehicle_rows(app, "ABC123GP")[0]
    after_owner = customer_snapshot(app, other_owner)
    drifted = sorted(
        key
        for key in ("customer_id", "registration", "colour", "status", "make")
        if after_row[key] != before_row[key]
    )
    mirror_before = json.loads(before_owner["custom_fields_json"] or "{}")
    mirror_after = json.loads(after_owner["custom_fields_json"] or "{}")
    assert not drifted and mirror_after == mirror_before, (
        "a refused approval must leave the transfer exactly as it was — "
        f"the row drifted on {drifted}, the owner's mirror went {mirror_before} -> {mirror_after}"
    )


# ── the picked-client approval-state endpoint the dialog asks ───────────────


def test_the_approval_state_endpoint_answers_for_a_saved_customer_id(app, client, branches):
    customer_id, _ = make_pending(app, branch_id=branches["midrand"])
    sign_in(client, app)

    response = approval_state(client, customer_id=customer_id)
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["customer_id"] == customer_id
    assert payload["ask_approval"] is True


def test_the_approval_state_endpoint_resolves_the_picked_client(app, client, branches):
    customer_id, _ = make_pending(app, branch_id=branches["midrand"])
    sign_in(client, app)

    payload = approval_state(client, customer_pick="Pieter van Wyk").get_json()
    assert payload["customer_id"] == customer_id, "the picked client must resolve to their saved id"
    assert payload["ask_approval"] is True


def test_the_approval_state_endpoint_is_false_for_unverified_and_ordinary_clients(app, client, branches):
    unverified, _ = make_pending(
        app, name="Unverified Una", phone="0830000001", verified=False, branch_id=branches["midrand"]
    )
    ordinary = make_customer(app, name="Charmaine Mokoena", phone="0821234567")
    sign_in(client, app)

    assert approval_state(client, customer_id=unverified).get_json()["ask_approval"] is False
    assert approval_state(client, customer_id=ordinary).get_json()["ask_approval"] is False

    unknown = approval_state(client, customer_id=999999).get_json()
    assert unknown["ask_approval"] is False
    assert not unknown.get("customer_id")

    # ... and it is arithmetic, not a guess: approving the client flips the answer.
    approvable, _ = make_pending(
        app, name="Approvable Anna", phone="0830000002", branch_id=branches["midrand"], reference="PEND-2"
    )
    assert approval_state(client, customer_id=approvable).get_json()["ask_approval"] is True
    assert "Client approved" in body_of(save_scan(client, approvable, "ANN1GP", approve_client="1"))
    assert approval_state(client, customer_id=approvable).get_json()["ask_approval"] is False


def test_the_approval_state_endpoint_needs_a_sign_in(app, client, branches):
    customer_id, _ = make_pending(app, branch_id=branches["midrand"])
    response = approval_state(client, customer_id=customer_id)
    assert response.status_code in (302, 401), "signed-out callers are sent to the sign-in"


def test_missing_csrf_cannot_approve_or_save(app, client):
    cid, rid = make_pending(app)
    sign_in(client, app)
    response = save_scan(client, cid, "CSRF212", approve_client="1", csrf_token="", follow=False)
    assert response.status_code == 400
    assert not vehicles_for(app, cid)
    assert registration_status(app, rid) == "pending"


def test_scan_only_account_cannot_approve_client(app, client):
    cid, rid = make_pending(app)
    sign_in(client, app, role="staff", modules=("scan_vehicle",))
    assert save_scan(client, cid, "MOD212", approve_client="1", follow=False).status_code == 403
    assert not vehicles_for(app, cid)
    assert registration_status(app, rid) == "pending"


def test_unexpected_approval_exception_rolls_back(app, client, monkeypatch):
    cid, rid = make_pending(app)
    sign_in(client, app)
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic unexpected failure")
    monkeypatch.setattr(portal_registrations, "approve_registration", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        save_scan(client, cid, "ERR212", approve_client="1")
    assert not vehicles_for(app, cid)
    assert registration_status(app, rid) == "pending"
    assert "main_vehicle_id" not in json.loads(customer_snapshot(app, cid)["custom_fields_json"])


def test_unapproved_main_vehicle_cannot_approve_client(app, client):
    cid, rid = make_pending(app)
    sign_in(client, app)
    save_scan(client, cid, "BADMAIN212", action="disapprove")
    response = save_scan(client, cid, "ADDITIONAL212", make_main="no", approve_client="1")
    assert registration_status(app, rid) == "pending"
    assert not vehicle_rows(app, "ADDITIONAL212")
    assert vehicle_rows(app, "BADMAIN212")[0]["status"] == "unapproved"