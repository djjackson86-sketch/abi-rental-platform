"""Per-account notification settings: staff reach, persistence, defaults and dispatch.

Synthetic fixtures only; no network and no production data. Delivery tests stand in for
``pywebpush`` at the same seam the real library sits behind, so the preference filter is
exercised without sending anything.
"""

import base64
import json
import re
import sqlite3

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from app.db import get_db, now
from app.services import branches as branches_service
from app.services import customers as customers_service
from app.services import notification_preferences as prefs
from app.services import push_notifications as push
from app.services import shop_enquiries


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
        }
    )


@pytest.fixture()
def app(tmp_path):
    return build_app(str(tmp_path / "notification-settings.sqlite"))


@pytest.fixture()
def client(app):
    return app.test_client()


def add_branch(app, name):
    with app.app_context():
        return branches_service.create_branch({"name": name, "active": 1})


def add_user(app, name, branch_id=None, role="staff", active=1, modules=None):
    with app.app_context():
        db = get_db()
        cursor = db.execute(
            """INSERT INTO users (name, email, password_hash, initials, role, branch_id,
                                  can_view_all_branches, active, modules_json, created_at)
               VALUES (?, ?, ?, 'ST', ?, ?, 0, ?, ?, ?)""",
            (
                name,
                f"{name.lower().replace(' ', '.')}@example.test",
                generate_password_hash("staff123"),
                role,
                branch_id,
                active,
                json.dumps(modules) if modules is not None else None,
                now(),
            ),
        )
        db.commit()
        return cursor.lastrowid


def sign_in(client, user_id, role="staff", modules=None, branch_id=None):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["user_name"] = "Staff Member"
        sess["user_role"] = role
        sess["staff_modules"] = list(modules or [])
        sess["branch_id"] = branch_id
        sess["can_view_all_branches"] = role == "owner"


def make_customer(app, name, branch_id):
    with app.app_context():
        return customers_service.create_customer(
            {"customer_type": "individual", "name": name, "phone": "082 555 0001"},
            branch_id=branch_id,
            notify=False,
        )


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def subscription(endpoint):
    return {
        "endpoint": endpoint,
        "keys": {"p256dh": b64url(b"\x04" + b"k" * 64), "auth": b64url(b"a" * 16)},
    }


def subscribe(app, user_id, endpoint):
    with app.app_context():
        push.subscribe(user_id, subscription(endpoint))


def page_csrf(client):
    page = client.get("/settings/notifications").get_data(as_text=True)
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match, "the settings form must carry the scoped CSRF token"
    return match.group(1), page


def checkbox_checked(page, key=prefs.CUSTOMER_NOTIFICATIONS):
    match = re.search(rf'<input[^>]*name="{re.escape(key)}"[^>]*>', page)
    assert match, f"the {key} checkbox is missing"
    return "checked" in match.group(0)


def stored_rows(app, user_id):
    with app.app_context():
        return get_db().execute(
            """SELECT notification_type, enabled FROM notification_preferences
               WHERE user_id = ? ORDER BY notification_type""",
            (user_id,),
        ).fetchall()


# ------------------------------------------------------------------ staff can reach it


def test_a_staff_member_without_the_settings_module_can_reach_their_own_page(app, client):
    branch = add_branch(app, "Branch A")
    staff = add_user(app, "Counter Staff", branch_id=branch, modules=["customers", "dashboard"])
    sign_in(client, staff, modules=["customers", "dashboard"], branch_id=branch)

    # Company settings stay locked behind the module grant...
    assert client.get("/settings/general").status_code == 403

    # ...but the personal notification page is reachable.
    response = client.get("/settings/notifications")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "Customer notifications" in page
    assert 'name="customer_notifications"' in page
    assert "data-push-controls" in page
    assert "Test notification" not in page
    assert "data-push-test" not in page


def test_the_page_is_reachable_from_settings_and_the_global_button(app, client):
    branch = add_branch(app, "Branch A")
    owner = add_user(app, "Main Profile", branch_id=branch, role="owner")
    sign_in(client, owner, role="owner", branch_id=branch)

    general = client.get("/settings/general").get_data(as_text=True)
    assert 'href="/settings/notifications"' in general
    assert ">Notifications</a>" in general

    dashboard = client.get("/dashboard").get_data(as_text=True)
    assert 'class="btn ghost" href="/settings/notifications"' in dashboard
    assert "data-push-controls" not in dashboard
    assert "Test notification" not in dashboard


# ---------------------------------------------------------------------- persistence


def test_the_checkbox_persists_per_user_and_self_only(app, client):
    branch = add_branch(app, "Branch A")
    me = add_user(app, "Me", branch_id=branch)
    other = add_user(app, "Other", branch_id=branch)
    sign_in(client, me, branch_id=branch)

    token, page = page_csrf(client)
    # Default ON for an existing account with no stored row.
    assert checkbox_checked(page)

    # Turned off: the absent checkbox is what "off" means.
    response = client.post(
        "/settings/notifications",
        data={"csrf_token": token, "user_id": str(other)},
        follow_redirects=False,
    )
    assert response.status_code == 302
    rows = stored_rows(app, me)
    assert [(row["notification_type"], row["enabled"]) for row in rows] == [
        (prefs.CUSTOMER_NOTIFICATIONS, 0)
    ]
    assert stored_rows(app, other) == [], "a posted user id cannot write another account"

    token, page = page_csrf(client)
    assert not checkbox_checked(page)

    # Turned back on.
    client.post(
        "/settings/notifications",
        data={"csrf_token": token, prefs.CUSTOMER_NOTIFICATIONS: "1"},
        follow_redirects=False,
    )
    assert [row["enabled"] for row in stored_rows(app, me)] == [1]
    token, page = page_csrf(client)
    assert checkbox_checked(page)


def test_an_unknown_type_is_ignored(app, client):
    branch = add_branch(app, "Branch A")
    user_id = add_user(app, "Counter", branch_id=branch)
    sign_in(client, user_id, branch_id=branch)
    token, _page = page_csrf(client)

    client.post(
        "/settings/notifications",
        data={
            "csrf_token": token,
            prefs.CUSTOMER_NOTIFICATIONS: "1",
            "marketing_alerts": "1",
        },
        follow_redirects=False,
    )
    keys = [row["notification_type"] for row in stored_rows(app, user_id)]
    assert keys == [prefs.CUSTOMER_NOTIFICATIONS]


def test_a_missing_csrf_token_writes_nothing(app, client):
    branch = add_branch(app, "Branch A")
    user_id = add_user(app, "Counter", branch_id=branch)
    sign_in(client, user_id, branch_id=branch)

    response = client.post("/settings/notifications", data={prefs.CUSTOMER_NOTIFICATIONS: "0"})
    assert response.status_code == 400
    assert stored_rows(app, user_id) == []


def test_default_is_on_so_existing_subscribers_keep_their_alerts(app):
    branch = add_branch(app, "Branch A")
    user_id = add_user(app, "Counter", branch_id=branch)
    subscribe(app, user_id, "https://fcm.googleapis.com/fcm/send/default-on")

    with app.app_context():
        assert prefs.stored_preferences(user_id) == {}
        assert prefs.is_enabled(user_id, prefs.CUSTOMER_NOTIFICATIONS) is True
        assert prefs.preferences_for(user_id)[prefs.CUSTOMER_NOTIFICATIONS] is True
        assert prefs.enabled_user_ids([user_id], prefs.CUSTOMER_NOTIFICATIONS) == [user_id]


# --------------------------------------------------------------- dispatch honouring


def capture_dispatches(monkeypatch):
    endpoints = []
    monkeypatch.setattr(push, "library_available", lambda: True)

    def fake_dispatch(subscription_info, payload_json):
        endpoints.append(subscription_info["endpoint"])

    monkeypatch.setattr(push, "_dispatch", fake_dispatch)
    return endpoints


def test_an_opted_out_staff_member_is_excluded_from_portal_alerts(app, monkeypatch):
    branch = add_branch(app, "Branch A")
    keep = add_user(app, "Keeps Alerts", branch_id=branch)
    opted_out = add_user(app, "Opted Out", branch_id=branch)
    keep_endpoint = "https://fcm.googleapis.com/fcm/send/portal-keep"
    off_endpoint = "https://fcm.googleapis.com/fcm/send/portal-off"
    subscribe(app, keep, keep_endpoint)
    subscribe(app, opted_out, off_endpoint)
    with app.app_context():
        prefs.save_preferences(opted_out, {})

    customer_id = make_customer(app, "Portal Client", branch_id=branch)
    endpoints = capture_dispatches(monkeypatch)
    with app.app_context():
        result = push.notify_portal_submission(customer_id)

    assert endpoints == [keep_endpoint], "only the staff member who wants alerts is sent one"
    assert result["recipients"] == 1
    assert result["sent"] == 1
    # The deep link is unchanged by the preference filter.
    assert result["branch_id"] == branch


def test_an_opted_out_staff_member_is_excluded_from_shop_alerts(app, monkeypatch):
    branch = add_branch(app, "Branch A")
    keep = add_user(app, "Keeps Alerts", branch_id=branch)
    opted_out = add_user(app, "Opted Out", branch_id=branch)
    keep_endpoint = "https://fcm.googleapis.com/fcm/send/shop-keep"
    off_endpoint = "https://fcm.googleapis.com/fcm/send/shop-off"
    subscribe(app, keep, keep_endpoint)
    subscribe(app, opted_out, off_endpoint)
    with app.app_context():
        prefs.save_preferences(opted_out, {})
        db = get_db()
        cursor = db.execute(
            """INSERT INTO shop_enquiries
               (receipt_token, request_key, payload_digest, enquiry_type, branch_id,
                customer_name, email, phone, notes, status, notice_version, consent_at,
                created_at, updated_at)
               VALUES (?, ?, ?, 'rental', ?, ?, ?, ?, '', 'new', '', '', ?, ?)""",
            (
                "receipt-pref-1",
                "request-pref-1",
                "digest-pref-1",
                branch,
                "Shop Client",
                "shop@example.test",
                "082 555 0002",
                now(),
                now(),
            ),
        )
        enquiry_id = cursor.lastrowid
        db.commit()

    endpoints = capture_dispatches(monkeypatch)
    with app.app_context():
        shop_enquiries.notify_new_enquiry(enquiry_id)
        enquiry = shop_enquiries.get_enquiry(enquiry_id)
        assert enquiry is not None and enquiry["branch_id"] == branch

    assert endpoints == [keep_endpoint]
    # A real payload still points at the enquiry detail screen.


def test_opted_out_users_do_not_break_the_branch_scope(app, monkeypatch):
    """Turning alerts off must not widen or empty another depot's alert."""
    branch_a = add_branch(app, "Branch A")
    branch_b = add_branch(app, "Branch B")
    a_staff = add_user(app, "A Staff", branch_id=branch_a)
    b_staff = add_user(app, "B Staff", branch_id=branch_b)
    subscribe(app, a_staff, "https://fcm.googleapis.com/fcm/send/a")
    subscribe(app, b_staff, "https://fcm.googleapis.com/fcm/send/b")
    with app.app_context():
        prefs.save_preferences(a_staff, {})

    customer_id = make_customer(app, "A Client", branch_id=branch_a)
    endpoints = capture_dispatches(monkeypatch)
    with app.app_context():
        push.notify_portal_submission(customer_id)

    assert endpoints == [], "the only subscriber at this depot opted out; branch B is not a fallback"


# --------------------------------------------------------------- libSQL-row tolerance


def test_preferences_work_with_real_sdk_row_objects(tmp_path, monkeypatch):
    from libsql_client.result import Row

    def sdk_rows(cursor, values):
        return Row({item[0]: i for i, item in enumerate(cursor.description)}, tuple(values))

    monkeypatch.setattr(sqlite3, "Row", sdk_rows)
    app = build_app(str(tmp_path / "sdk.sqlite"))
    client = app.test_client()
    with app.app_context():
        db = get_db()
        branch = branches_service.create_branch({"name": "SDK Branch", "active": 1})
        cursor = db.execute(
            """INSERT INTO users (name, email, password_hash, initials, role, branch_id,
                                  can_view_all_branches, active, modules_json, created_at)
               VALUES ('SDK Staff', 'sdk@example.test', ?, 'SS', 'staff', ?, 0, 1,
                       '["customers"]', ?)""",
            (generate_password_hash("staff123"), branch, now()),
        )
        user_id = cursor.lastrowid
        db.commit()
        row = db.execute(
            "SELECT notification_type, enabled FROM notification_preferences WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        assert row is None
        assert prefs.is_enabled(user_id, prefs.CUSTOMER_NOTIFICATIONS) is True
        prefs.save_preferences(user_id, {})
        assert prefs.is_enabled(user_id, prefs.CUSTOMER_NOTIFICATIONS) is False

    sign_in(client, user_id, modules=["customers"], branch_id=branch)
    page = client.get("/settings/notifications")
    assert page.status_code == 200
    assert "Customer notifications" in page.get_data(as_text=True)
