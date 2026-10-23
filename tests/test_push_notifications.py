"""Staff Web Push: recipient scoping, endpoint/key validation, CSRF, delivery hygiene, and
the one-shot trigger from the public portal.

Synthetic fixtures only: no real client data, no network. ``pywebpush`` is optional on this
host, so the delivery tests stand in for it (``push_notifications._dispatch``) — that is the
same seam the real library sits behind, so the 404/410, timeout/TTL and count behaviour is
exercised without sending anything.
"""

import base64
import json
import re

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from app.db import get_db, now
from app.services import branches as branches_service
from app.services import customers as customers_service
from app.services import portal_intake
from app.services import push_notifications as push


def build_app(database_path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": database_path,
            "SECRET_KEY": "test",
            "ADMIN_EMAIL": "admin@abi.local",
            "ADMIN_PASSWORD": "admin123",
            "TELEGRAM_NOTIFICATIONS_ENABLED": "",
            "VAPID_PUBLIC_KEY": "BPublicKeyForTestsOnly",
            "VAPID_PRIVATE_KEY": "private-key-for-tests-only",
            "VAPID_SUBJECT": "mailto:test@abi.local",
            "PUSH_TTL_SECONDS": 900,
            "PUSH_TIMEOUT_SECONDS": 5,
        }
    )


@pytest.fixture()
def app(tmp_path):
    return build_app(str(tmp_path / "push.sqlite"))


@pytest.fixture()
def client(app):
    return app.test_client()


def add_branch(app, name):
    with app.app_context():
        return branches_service.create_branch({"name": name, "active": 1})


def add_user(app, name, branch_id=None, role="staff", active=1, extra_branches=()):
    with app.app_context():
        db = get_db()
        cur = db.execute(
            """INSERT INTO users (name, email, password_hash, initials, role, branch_id,
                                  can_view_all_branches, active, modules_json, created_at)
               VALUES (?, ?, ?, 'ST', ?, ?, ?, ?, ?, ?)""",
            (
                name,
                f"{name.lower().replace(' ', '.')}@example.test",
                generate_password_hash("staff123"),
                role,
                branch_id,
                1 if role == "owner" else 0,
                active,
                json.dumps(["dashboard", "customers"]),
                now(),
            ),
        )
        user_id = cur.lastrowid
        for branch in extra_branches:
            db.execute(
                "INSERT INTO user_branch_access (user_id, branch_id) VALUES (?, ?)",
                (user_id, branch),
            )
        db.commit()
        return user_id


def sign_in(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id


def make_customer(app, name, branch_id=None):
    with app.app_context():
        return customers_service.create_customer(
            {"customer_type": "individual", "name": name, "phone": "082 555 0001"},
            branch_id=branch_id,
            notify=False,
        )


def token(client):
    response = client.get("/push/config")
    assert response.status_code == 200
    return response.get_json()["csrf_token"]


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def subscription(endpoint="https://fcm.googleapis.com/fcm/send/abc-123"):
    return {
        "endpoint": endpoint,
        "keys": {"p256dh": b64url(b"\x04" + b"k" * 64), "auth": b64url(b"a" * 16)},
    }


def rows(app):
    with app.app_context():
        return get_db().execute(
            "SELECT user_id, endpoint, failure_count, disabled_at FROM push_subscriptions"
        ).fetchall()


def capture_sends(monkeypatch):
    sent = []
    monkeypatch.setattr(
        push,
        "send_to_users",
        lambda user_ids, payload: sent.append((list(user_ids), dict(payload)))
        or {"recipients": len(user_ids), "sent": len(user_ids)},
    )
    return sent


# ------------------------------------------------------------------ recipient scoping


def test_only_the_customers_branch_staff_are_recipients(app, monkeypatch):
    branch_a = add_branch(app, "Alpha Depot")
    branch_b = add_branch(app, "Beta Depot")
    primary = add_user(app, "Primary A", branch_id=branch_a)
    granted = add_user(app, "Granted A", branch_id=branch_b, extra_branches=(branch_a,))
    add_user(app, "Other branch", branch_id=branch_b)
    add_user(app, "Owner", branch_id=branch_a, role="owner")
    add_user(app, "Inactive A", branch_id=branch_a, active=0)
    add_user(app, "No branch")
    customer_id = make_customer(app, "Scoped Client", branch_id=branch_a)

    with app.app_context():
        recipients, resolved_branch, branch_name = push.portal_recipients(customer_id)

    assert recipients == [primary, granted]
    assert resolved_branch == branch_a
    assert branch_name == "Alpha Depot"


def test_a_customer_without_a_branch_is_never_broadcast(app, monkeypatch):
    add_branch(app, "Alpha Depot")
    add_user(app, "Primary A", branch_id=add_branch(app, "Gamma Depot"))
    customer_id = make_customer(app, "Unassigned Client", branch_id=None)
    sent = capture_sends(monkeypatch)

    with app.app_context():
        result = push.notify_portal_submission(customer_id)

    assert result["recipients"] == 0
    assert sent == [], "an unknown branch must not become an all-branches alert"


def test_the_owner_is_excluded_even_when_they_see_every_branch(app, monkeypatch):
    branch = add_branch(app, "Alpha Depot")
    owner = add_user(app, "Owner", branch_id=branch, role="owner")
    staff = add_user(app, "Counter", branch_id=branch)
    customer_id = make_customer(app, "Owner Scope Client", branch_id=branch)
    sent = capture_sends(monkeypatch)

    with app.app_context():
        push.notify_portal_submission(customer_id)

    assert sent and sent[0][0] == [staff]
    assert owner not in sent[0][0]


def test_payload_is_minimal_and_points_at_the_customer(app, monkeypatch):
    branch = add_branch(app, "Alpha Depot")
    add_user(app, "Counter", branch_id=branch)
    customer_id = make_customer(app, "Private Person", branch_id=branch)
    sent = capture_sends(monkeypatch)

    with app.app_context():
        push.notify_portal_submission(customer_id)

    payload = sent[0][1]
    assert payload["url"] == f"/customers/{customer_id}"
    blob = json.dumps(payload)
    assert "Private Person" not in blob
    assert "082 555 0001" not in blob


# ---------------------------------------------------------- subscribe / auth / CSRF


def test_config_requires_a_signed_in_account(client):
    assert client.get("/push/config").status_code == 401
    assert client.post("/push/subscribe", json=subscription()).status_code == 401


def test_a_deactivated_account_cannot_subscribe_or_configure(app, client):
    branch = add_branch(app, "Alpha Depot")
    user_id = add_user(app, "Gone", branch_id=branch, active=0)
    sign_in(client, user_id)
    assert client.get("/push/config").status_code == 401
    assert client.post("/push/subscribe", json=subscription()).status_code == 401
    assert rows(app) == []


def test_subscribe_requires_the_scoped_csrf_token(app, client):
    branch = add_branch(app, "Alpha Depot")
    sign_in(client, add_user(app, "Counter", branch_id=branch))

    refused = client.post("/push/subscribe", json=subscription())
    assert refused.status_code == 400
    assert "token" in refused.get_json()["error"].lower()
    assert rows(app) == [], "a refused token writes nothing"

    allowed = client.post(
        "/push/subscribe", json=subscription(), headers={"X-CSRF-Token": token(client)}
    )
    assert allowed.status_code == 200
    assert allowed.get_json()["subscription_count"] == 1


def test_config_returns_only_the_public_key(app, client):
    branch = add_branch(app, "Alpha Depot")
    sign_in(client, add_user(app, "Counter", branch_id=branch))
    response = client.get("/push/config")
    body = response.get_json()
    assert body["enabled"] is True
    assert body["vapid_public_key"] == "BPublicKeyForTestsOnly"
    assert body["csrf_token"]
    assert "private-key-for-tests-only" not in response.get_data(as_text=True)


def test_an_endpoint_is_bound_to_one_account(app, client):
    branch = add_branch(app, "Alpha Depot")
    first = add_user(app, "First", branch_id=branch)
    second = add_user(app, "Second", branch_id=branch)

    sign_in(client, first)
    assert client.post(
        "/push/subscribe", json=subscription(), headers={"X-CSRF-Token": token(client)}
    ).status_code == 200

    sign_in(client, second)
    assert client.post(
        "/push/subscribe", json=subscription(), headers={"X-CSRF-Token": token(client)}
    ).status_code == 200

    stored = rows(app)
    assert len(stored) == 1, "one endpoint is one row, never two"
    assert stored[0]["user_id"] == second


def test_unsubscribe_removes_only_the_callers_own_binding(app, client):
    branch = add_branch(app, "Alpha Depot")
    owner_of_device = add_user(app, "Owner Of Device", branch_id=branch)
    other = add_user(app, "Other", branch_id=branch)

    sign_in(client, owner_of_device)
    client.post("/push/subscribe", json=subscription(), headers={"X-CSRF-Token": token(client)})

    sign_in(client, other)
    response = client.post(
        "/push/unsubscribe",
        json={"endpoint": subscription()["endpoint"]},
        headers={"X-CSRF-Token": token(client)},
    )
    assert response.get_json()["removed"] is False
    assert len(rows(app)) == 1, "another account's device is untouched"

    sign_in(client, owner_of_device)
    removed = client.post(
        "/push/unsubscribe",
        json={"endpoint": subscription()["endpoint"]},
        headers={"X-CSRF-Token": token(client)},
    )
    assert removed.get_json()["removed"] is True
    assert rows(app) == []


def test_unsubscribe_requires_csrf(app, client):
    branch = add_branch(app, "Alpha Depot")
    sign_in(client, add_user(app, "Counter", branch_id=branch))
    response = client.post("/push/unsubscribe", json={"endpoint": subscription()["endpoint"]})
    assert response.status_code == 400


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/fcm/send/abc",              # not https
        "https://192.168.1.10/fcm/send/abc",                  # bare IP
        "https://fcm.googleapis.com:8443/fcm/send/abc",       # non-default port
        "https://user:pass@fcm.googleapis.com/fcm/send/abc",  # credentials
        "https://evil.example.test/fcm/send/abc",             # not a push service
        "https://fcm.googleapis.com.evil.test/x",             # look-alike suffix
        "https://push.apple.com/webpush/abc",                 # Apple needs a shard label
        "https://fcm.googleapis.com/fcm/send/abc#frag",       # fragment
    ],
)
def test_bad_endpoints_are_refused(app, client, endpoint):
    branch = add_branch(app, "Alpha Depot")
    sign_in(client, add_user(app, "Counter", branch_id=branch))
    response = client.post(
        "/push/subscribe",
        json=subscription(endpoint),
        headers={"X-CSRF-Token": token(client)},
    )
    assert response.status_code == 400
    assert rows(app) == []


@pytest.mark.parametrize(
    "keys",
    [
        {"p256dh": b64url(b"k" * 65), "auth": b64url(b"a" * 16)},   # not a 0x04 point
        {"p256dh": b64url(b"\x04" + b"k" * 40), "auth": b64url(b"a" * 16)},  # short point
        {"p256dh": b64url(b"\x04" + b"k" * 64), "auth": b64url(b"a" * 8)},   # short auth
        {"p256dh": "", "auth": b64url(b"a" * 16)},
        {"p256dh": "%%%not-base64%%%", "auth": b64url(b"a" * 16)},
    ],
)
def test_bad_keys_are_refused(app, client, keys):
    branch = add_branch(app, "Alpha Depot")
    sign_in(client, add_user(app, "Counter", branch_id=branch))
    response = client.post(
        "/push/subscribe",
        json={"endpoint": subscription()["endpoint"], "keys": keys},
        headers={"X-CSRF-Token": token(client)},
    )
    assert response.status_code == 400
    assert rows(app) == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://web.push.apple.com/QG9vZC1zaGFyZA",
        "https://updates.push.services.mozilla.com/wpush/v2/abc",
        "https://fcm.googleapis.com/fcm/send/abc",
    ],
)
def test_the_three_real_push_services_are_accepted(app, client, endpoint):
    branch = add_branch(app, "Alpha Depot")
    sign_in(client, add_user(app, "Counter", branch_id=branch))
    response = client.post(
        "/push/subscribe",
        json=subscription(endpoint),
        headers={"X-CSRF-Token": token(client)},
    )
    assert response.status_code == 200


# ---------------------------------------------------------------- delivery hygiene


def subscribe_directly(app, user_id, endpoint="https://fcm.googleapis.com/fcm/send/direct"):
    with app.app_context():
        return push.subscribe(user_id, subscription(endpoint))


def test_a_gone_subscription_is_deleted_and_counted(app, monkeypatch):
    branch = add_branch(app, "Alpha Depot")
    user_id = add_user(app, "Counter", branch_id=branch)
    subscribe_directly(app, user_id)

    def gone(subscription_info, payload):
        raise push.PushDeliveryError("410 gone", status=410)

    monkeypatch.setattr(push, "_dispatch", gone)
    with app.app_context():
        counts = push.send_to_users([user_id], {"title": "x", "url": "/customers/1"})

    assert counts["removed"] == 1
    assert counts["sent"] == 0
    assert rows(app) == [], "a browser that is gone is not retried forever"


def test_a_transient_failure_is_counted_and_the_row_kept(app, monkeypatch):
    branch = add_branch(app, "Alpha Depot")
    user_id = add_user(app, "Counter", branch_id=branch)
    subscribe_directly(app, user_id)

    def boom(subscription_info, payload):
        raise push.PushDeliveryError("500 server error", status=500)

    monkeypatch.setattr(push, "_dispatch", boom)
    with app.app_context():
        counts = push.send_to_users([user_id], {"title": "x"})

    assert counts["failed"] == 1
    stored = rows(app)
    assert len(stored) == 1
    assert stored[0]["failure_count"] == 1
    assert stored[0]["disabled_at"] is None


def test_repeated_failures_disable_the_subscription(app, monkeypatch):
    branch = add_branch(app, "Alpha Depot")
    user_id = add_user(app, "Counter", branch_id=branch)
    subscribe_directly(app, user_id)

    def boom(subscription_info, payload):
        raise push.PushDeliveryError("502", status=502)

    monkeypatch.setattr(push, "_dispatch", boom)
    with app.app_context():
        for _ in range(push.MAX_FAILURES):
            counts = push.send_to_users([user_id], {"title": "x"})
    assert counts["disabled"] == 1
    assert rows(app)[0]["disabled_at"]

    # A disabled row is not attempted again.
    with app.app_context():
        assert push.send_to_users([user_id], {"title": "x"})["sent"] == 0


def test_a_missing_library_is_reported_as_skipped(app, monkeypatch):
    branch = add_branch(app, "Alpha Depot")
    user_id = add_user(app, "Counter", branch_id=branch)
    subscribe_directly(app, user_id)
    monkeypatch.setattr(push, "library_available", lambda: False)
    with app.app_context():
        counts = push.send_to_users([user_id], {"title": "x"})
    assert counts["skipped"] == 1
    assert counts["sent"] == 0


def test_a_server_key_fault_does_not_disable_the_device(app, monkeypatch):
    """A bad VAPID key is a server problem: skip and leave the subscription alone."""
    branch = add_branch(app, "Alpha Depot")
    user_id = add_user(app, "Counter", branch_id=branch)
    subscribe_directly(app, user_id)

    def bad_key(subscription_info, payload):
        raise push.PushUnavailable("push key/configuration error: Could not deserialize key")

    monkeypatch.setattr(push, "_dispatch", bad_key)
    with app.app_context():
        counts = push.send_to_users([user_id], {"title": "x"})

    assert counts["skipped"] == 1
    assert counts["failed"] == 0
    stored = rows(app)
    assert stored[0]["failure_count"] == 0
    assert stored[0]["disabled_at"] is None


def test_there_is_no_test_send_endpoint(app, client):
    """Device setup no longer offers a test send: the route is gone, not just hidden."""
    branch = add_branch(app, "Alpha Depot")
    sign_in(client, add_user(app, "Counter", branch_id=branch))
    response = client.post("/push/test", headers={"X-CSRF-Token": token(client)})
    assert response.status_code == 404


# ------------------------------------------------------------ the portal trigger


def portal_submission(**overrides):
    data = {
        "customer_type": "individual",
        "own_use_declaration": "individual",
        "first_names": "Piet",
        "surname": "Mokoena",
        "name": "Piet Mokoena",
        "email": "piet.push@example.test",
        "phone": "083 777 4321",
        "address_line1": "1 Push Street",
        "suburb": "Randburg",
        "city": "Johannesburg",
        "province": "Gauteng",
        "postal_code": "2194",
        "alternative_contact_name": "Jan Mokoena",
        "alternative_contact_number": "082 111 2222",
        "alternative_contact_relationship": "Brother",
        "popia_consent": "1",
    }
    data.update(overrides)
    return data


@pytest.fixture()
def portal_client(app, monkeypatch):
    monkeypatch.setattr(portal_intake, "registration_is_open", lambda: True)
    client = app.test_client()
    body = client.get("/portal/register").get_data(as_text=True)
    match = re.search(r'name="request_key" value="([^"]+)"', body)
    return client, (match.group(1) if match else "")


def customer_ids(app):
    with app.app_context():
        return [
            row[0] for row in
            get_db().execute("SELECT id FROM customers ORDER BY id").fetchall()
        ]


def test_a_successful_portal_submission_notifies_once(portal_client, app, monkeypatch):
    client, request_key = portal_client
    calls = []
    monkeypatch.setattr(
        "app.services.push_notifications.notify_portal_submission",
        lambda customer_id: calls.append(customer_id),
    )

    response = client.post("/portal/register", data=portal_submission(request_key=request_key))
    assert response.status_code == 200
    created = customer_ids(app)
    assert len(created) == 1
    assert calls == created


def test_a_replayed_submission_does_not_notify_again(portal_client, app, monkeypatch):
    client, request_key = portal_client
    calls = []
    monkeypatch.setattr(
        "app.services.push_notifications.notify_portal_submission",
        lambda customer_id: calls.append(customer_id),
    )
    data = portal_submission(request_key=request_key)

    assert client.post("/portal/register", data=data).status_code == 200
    assert client.post("/portal/register", data=data).status_code == 200

    assert len(customer_ids(app)) == 1
    assert len(calls) == 1, "a double tap (or replay) is one notification"


def test_a_push_failure_never_breaks_the_portal_save(portal_client, app, monkeypatch):
    client, request_key = portal_client

    def explode(customer_id):
        raise RuntimeError("push exploded")

    monkeypatch.setattr("app.services.push_notifications.notify_portal_submission", explode)
    response = client.post("/portal/register", data=portal_submission(request_key=request_key))

    assert response.status_code == 200
    assert len(customer_ids(app)) == 1, "the client's submission is already durable"


def test_the_trigger_uses_the_persisted_branch_not_the_session(app, monkeypatch):
    branch = add_branch(app, "Persisted Depot")
    staff = add_user(app, "Persisted Counter", branch_id=branch)
    customer_id = make_customer(app, "Persisted Client", branch_id=branch)
    captured = []

    def fake_send(user_ids, payload):
        captured.append((list(user_ids), dict(payload)))
        return {"recipients": len(user_ids), "sent": 0}

    monkeypatch.setattr(push, "send_to_users", fake_send)
    with app.app_context():
        result = push.notify_portal_submission(customer_id)

    assert result["branch_id"] == branch
    assert captured[0][0] == [staff]
    assert captured[0][1]["url"] == f"/customers/{customer_id}"
