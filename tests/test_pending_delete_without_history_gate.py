"""Pending-portal delete: no financial/order-history eligibility gate, tombstone not erase.

The owner's corrected verification-phase rule, pinned end to end:

* a pending self-registration is removed from the queue **always** — a stored order/invoice/
  payment/credit on the client (the pending-link feature can leave a pending registration on
  an order that already exists) is **never** an eligibility gate;
* the removal is a **tombstone** (`portal_registrations.status = 'deleted'`), not an erase:
  the client row and every row that references it (orders, order_items, payments, credits,
  legacy balances, vehicles, consents) stay behind, so no ledger link is broken or orphaned
  and nothing financial is ever wiped;
* the tombstoned client leaves the pending queue, the default customer list, the search
  picker and the order/allocation picker, and is still refused by `is_pending`, so a removed
  sign-up can never be booked;
* an **approved** client is still refused by this action — the ordinary `delete_customer`
  history guard is untouched and is not used as an eligibility gate here.

Fixtures are synthetic; notifications are disabled.
"""

import pytest

from app import create_app
from app.db import get_db
from app.services import branches as branches_service
from app.services import portal_intake, portal_registrations
from app.services.customers import get_customer, list_customers, search_customers
from app.services.portal_registrations import pending_exclusion_clause


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
    return build_app(str(tmp_path / "pending_gate.sqlite"))


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


def _submission(**overrides):
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


def _register(client, app, branch_id):
    with app.app_context():
        slug = get_db().execute("SELECT public_slug FROM branches WHERE id = ?", (branch_id,)).fetchone()["public_slug"]
    return client.post(f"/portal/{slug}/register", data=_submission())


def _rows(app, sql, params=()):
    with app.app_context():
        return [dict(row) for row in get_db().execute(sql, params).fetchall()]


def _one(app, sql, params=()):
    found = _rows(app, sql, params)
    return found[0] if found else None


def sign_in(client, app, *, role="owner", branch_id=None, modules=(), can_view_all=True):
    with app.app_context():
        uid = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()["id"]
    with client.session_transaction() as session:
        session["user_id"] = uid
        session["user_role"] = role
        session["user_name"] = "Synthetic Tester"
        session["initials"] = "ST"
        session["branch_id"] = branch_id
        session["can_view_all_branches"] = can_view_all
        session["branch_ids"] = []
        session["staff_modules"] = list(modules)


def _delete_token(client, customer_id):
    client.get(f"/customers/{customer_id}/edit")
    with client.session_transaction() as session:
        return (session.get("_csrf_tokens") or {}).get("portal_pending_delete")


def _pending_with_history(client, app, branches):
    """A pending portal client that already carries a full order + ledger trail."""
    _register(client, app, branches["midrand"])
    stored = _one(app, "SELECT * FROM customers WHERE email = 'pieter@example.co.za'")
    registration = _one(app, "SELECT * FROM portal_registrations WHERE customer_id = ?", (stored["id"],))
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO orders (order_number, customer_id, created_at) VALUES ('GATE-ORD-1', ?, '')", (stored["id"],))
        order_id = db.execute("SELECT id FROM orders WHERE order_number = 'GATE-ORD-1'").fetchone()["id"]
        db.execute("INSERT INTO order_items (order_id, custom_name, quantity, unit_price) VALUES (?, 'Trailer hire', 2, 250.0)", (order_id,))
        db.execute("INSERT INTO payments (order_id, amount, method, created_at) VALUES (?, 500.0, 'cash', '')", (order_id,))
        db.execute(
            "INSERT INTO customer_credits (customer_id, amount, source_order_id, created_at) VALUES (?, 75.0, ?, '')",
            (stored["id"], order_id),
        )
        db.execute(
            "INSERT INTO legacy_customer_balances (customer_id, source_order_number, amount_due, created_at, updated_at) "
            "VALUES (?, 'LEG-9', 120.0, '', '')",
            (stored["id"],),
        )
        db.commit()
    return stored, registration, order_id


def test_a_pending_client_with_history_is_removed_not_gated_and_nothing_financial_is_erased(client, app, branches):
    stored, registration, order_id = _pending_with_history(client, app, branches)
    customer_id = stored["id"]

    # Service level: no ValueError ("cannot be deleted"), returns True, tombstone written.
    with app.app_context():
        assert portal_registrations.delete_pending_registration(registration["id"]) is True
        assert portal_registrations.registration_by_id(registration["id"])["status"] == "deleted"

    # Nothing financial was erased, and no reference dangles.
    order = _one(app, "SELECT * FROM orders WHERE id = ?", (order_id,))
    assert order is not None and order["customer_id"] == customer_id, "the order and its link are kept"
    assert _one(app, "SELECT * FROM order_items WHERE order_id = ?", (order_id,))["quantity"] == 2
    assert _one(app, "SELECT * FROM payments WHERE order_id = ?", (order_id,))["amount"] == 500.0
    assert _one(app, "SELECT * FROM customer_credits WHERE customer_id = ?", (customer_id,))["amount"] == 75.0
    assert _one(app, "SELECT * FROM legacy_customer_balances WHERE customer_id = ?", (customer_id,))["amount_due"] == 120.0
    with app.app_context():
        assert get_customer(customer_id) is not None, "the client row is preserved for the ledger"

    # The tombstoned client is refused by the order gate — is_pending treats 'deleted' as hidden.
    with app.app_context():
        assert portal_registrations.is_pending(customer_id) is True


def test_the_removed_signup_leaves_the_queue_list_search_and_order_picker(client, app, branches):
    stored, registration, _ = _pending_with_history(client, app, branches)
    customer_id = stored["id"]

    sign_in(client, app, role="owner")
    token = _delete_token(client, customer_id)
    assert token
    response = client.post(f"/customers/portal/{registration['id']}/delete", data={"csrf_token": token, "reason": "Verification refused"}, follow_redirects=True)
    assert response.status_code == 200
    assert b"Client disapproved" in response.data

    with app.app_context():
        assert portal_registrations.list_pending_registrations() == [], "gone from the verification queue"
        assert portal_registrations.pending_registration_for_customer(customer_id) is not None
        assert list_customers() == [], "a tombstoned sign-up is not a normal customer"
        assert search_customers("Pieter") == [], "and is not offered by the name search"
        # The order / allocation picker uses the same exclusion clause.
        picker = get_db().execute(
            "SELECT id FROM customers WHERE id = ?" + pending_exclusion_clause("customers"), (customer_id,)
        ).fetchall()
        assert picker == [], "and is not offered by the order/allocation picker"


def test_the_tombstone_is_idempotent_and_an_unknown_registration_is_refused(client, app, branches):
    stored, registration, _ = _pending_with_history(client, app, branches)
    with app.app_context():
        assert portal_registrations.delete_pending_registration(registration["id"]) is True
        # A second attempt has nothing left to remove and writes no second tombstone.
        assert portal_registrations.delete_pending_registration(registration["id"]) is False
        assert portal_registrations.registration_by_id(registration["id"])["status"] == "deleted"
    with pytest.raises(ValueError):
        with app.app_context():
            portal_registrations.delete_pending_registration(9999)


def test_an_approved_client_is_never_touched_by_the_pending_action(client, app, branches):
    """The ordinary delete semantics are not broadened: approval takes the client out of reach."""
    _register(client, app, branches["midrand"])
    stored = _one(app, "SELECT * FROM customers WHERE email = 'pieter@example.co.za'")
    registration = _one(app, "SELECT * FROM portal_registrations WHERE customer_id = ?", (stored["id"],))
    with app.app_context():
        from app.services import vehicles

        db = get_db()
        db.execute("UPDATE customers SET client_verified = 1 WHERE id = ?", (stored["id"],))
        db.commit()
        vehicle_id = vehicles.create_vehicle(
            {"registration": "AP001GP", "make": "TEST MAKE", "vin": "AHTFR22G10L999999"}, customer_id=stored["id"]
        )
        vehicles.promote_main_vehicle(stored["id"], vehicle_id)
        portal_registrations.approve_registration(registration["id"])

    sign_in(client, app, role="owner")
    token = _delete_token(client, stored["id"])
    response = client.post(f"/customers/portal/{registration['id']}/delete", data={"csrf_token": token, "reason": "Verification refused"}, follow_redirects=True)
    assert response.status_code == 200
    assert b"Only a pending client can be disapproved" in response.data
    with app.app_context():
        assert get_customer(stored["id"]) is not None
        assert portal_registrations.registration_by_id(registration["id"])["status"] == "approved"


def test_the_removal_still_needs_its_scoped_csrf_token_and_branch(client, app, branches):
    stored, registration, _ = _pending_with_history(client, app, branches)
    # No token / wrong token -> refused, nothing changes.
    sign_in(client, app, role="owner")
    assert client.post(f"/customers/portal/{registration['id']}/delete").status_code == 400
    assert client.post(f"/customers/portal/{registration['id']}/delete", data={"csrf_token": "nope"}).status_code == 400
    with app.app_context():
        assert portal_registrations.registration_by_id(registration["id"])["status"] == "pending"
    # A staff account scoped to the other branch cannot act on a Midrand sign-up.
    sign_in(client, app, role="staff", branch_id=branches["roodepoort"], modules=["customers"], can_view_all=False)
    token = _delete_token(client, stored["id"])
    assert client.post(f"/customers/portal/{registration['id']}/delete", data={"csrf_token": token, "reason": "Verification refused"}).status_code == 403
    with app.app_context():
        assert portal_registrations.registration_by_id(registration["id"])["status"] == "pending"
