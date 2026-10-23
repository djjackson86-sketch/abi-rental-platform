"""Branch enquiries from the online store.

Synthetic fixtures only: no network, no production data. The public path is the
interesting one — these tests exist to prove that a stranger filling in the form
can create an enquiry and **nothing else**, that the branch's own staff are the
only people notified, and that a staff conversion goes through the existing
customer/order rules rather than around them.
"""

import json
import sqlite3
from datetime import timedelta

import pytest

from app import create_app
from app.db import get_db, now
from app.services import branches as branches_service
from app.services import csrf
from app.services import push_notifications
from app.services import shop_enquiries
from app.services.timezone import local_now_iso
from app.services.reports import row_dict


def build_app(database_path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": database_path,
            "SECRET_KEY": "test",
            "ADMIN_EMAIL": "admin@abi.local",
            "ADMIN_PASSWORD": "admin123",
            "TELEGRAM_NOTIFICATIONS_ENABLED": "",
            "VAPID_PUBLIC_KEY": "",
            "VAPID_PRIVATE_KEY": "",
        }
    )
    # The parent wires this blueprint into the real app; tests register it
    # explicitly so this file never depends on an edit outside its own scope.
    from app.routes.shop_enquiries import bp as shop_enquiries_bp

    if 'shop_enquiries' not in app.blueprints:
        app.register_blueprint(shop_enquiries_bp)
    return app


@pytest.fixture()
def app(tmp_path):
    application = build_app(str(tmp_path / "shop-enquiries.sqlite"))
    # One app context for the whole test: the service helpers are also called
    # directly from assertions, and those reads must see the same database the
    # test client just wrote to.
    context = application.app_context()
    context.push()
    yield application
    context.pop()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def reset_enquiry_limits():
    shop_enquiries.reset_rate_limits()
    yield
    shop_enquiries.reset_rate_limits()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def add_branch(app, name):
    with app.app_context():
        return branches_service.create_branch({"name": name, "active": 1})


def add_user(app, name, branch_id=None, role="staff", active=1, modules=None,
             extra_branches=(), can_view_all=0):
    with app.app_context():
        db = get_db()
        email = name.lower().replace(" ", ".") + "@test.local"
        cursor = db.execute(
            """INSERT INTO users
            (email, password_hash, name, initials, role, branch_id, can_view_all_branches,
             active, modules_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                email, "x", name, "".join(part[0] for part in name.split())[:2].upper(),
                role, branch_id, can_view_all, active,
                json.dumps(modules) if modules is not None else None, now(),
            ),
        )
        user_id = cursor.lastrowid
        for branch in extra_branches:
            db.execute(
                "INSERT INTO user_branch_access (user_id, branch_id) VALUES (?, ?)",
                (user_id, branch),
            )
        db.commit()
        return user_id


def add_group(app, name, active=1, store_visible=1):
    with app.app_context():
        cursor = get_db().execute(
            """INSERT INTO product_groups (name, description, active, becomes_store_visible,
               sort_order, created_at, updated_at)
            VALUES (?, '', ?, ?, 0, ?, ?)""",
            (name, active, store_visible, now(), now()),
        )
        get_db().commit()
        return cursor.lastrowid


def add_product(app, name, branch_id=None, price=450.0, quantity=3, active=1,
                public_visible=1, maintenance=0, group_id=None, sku=""):
    with app.app_context():
        cursor = get_db().execute(
            """INSERT INTO products
            (name, product_type, description, sku, active, public_visible, price_amount,
             price_unit, security_deposit, quantity, tracking_method, under_maintenance,
             branch_id, product_group_id, created_at)
            VALUES (?, 'rental', '', ?, ?, ?, ?, 'day', 0, ?, 'bulk', ?, ?, ?, ?)""",
            (name, sku, active, public_visible, price, quantity, maintenance, branch_id,
             group_id, now()),
        )
        get_db().commit()
        product_id = cursor.lastrowid
        if branch_id:
            get_db().execute(
                """INSERT OR IGNORE INTO product_branch_stock (product_id, branch_id, quantity, updated_at)
                VALUES (?, ?, 0, ?)""",
                (product_id, branch_id, now()),
            )
            get_db().commit()
        return product_id


def publish_notice(app, version="1.1"):
    with app.app_context():
        get_db().execute(
            """INSERT INTO popia_notice_versions
            (notice_version, notice_content_hash, notice_text, published_at)
            VALUES (?, ?, ?, ?)""",
            (version, "hash-" + version, "Privacy notice " + version, now()),
        )
        get_db().commit()


def public_csrf(client):
    return _session_token(client, "shop_public")


def admin_csrf(client, scope="shop_enquiry_admin"):
    return _session_token(client, scope)


def _session_token(client, scope):
    """Seed the session's scoped token the way ``csrf.issue_token`` would."""
    with client.session_transaction() as session:
        tokens = session.get("_csrf_tokens")
        if not isinstance(tokens, dict):
            tokens = {}
        token = "test-" + scope
        tokens[scope] = token
        session["_csrf_tokens"] = tokens
        return token


def login(client, user_id, role="staff", branch_id=None, modules=(), branch_ids=(),
          can_view_all=False):
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["user_name"] = "Test user"
        session["user_role"] = role
        session["branch_id"] = branch_id
        session["can_view_all_branches"] = bool(can_view_all)
        session["staff_modules"] = list(modules)
        session["branch_ids"] = list(branch_ids)


def next_days():
    today = local_now_iso()[:10]
    from datetime import date

    start = date.fromisoformat(today) + timedelta(days=1)
    # Ordinary enquiry tests need an open weekday, not tomorrow's incidental
    # weekend. Closed-day behaviour has dedicated branch-hours tests.
    while start.weekday() >= 5:
        start += timedelta(days=1)
    return start.isoformat(), (start + timedelta(days=2)).isoformat()


def rental_form(branch_id, product_id, **overrides):
    start, end = next_days()
    form = {
        "enquiry_type": "rental",
        "branch_id": str(branch_id),
        "customer_name": "Thandi Mokoena",
        "customer_email": "thandi@example.com",
        "customer_phone": "082 555 0101",
        "notes": "Please call before 17:00",
        "privacy_accepted": "1",
        "notice_version": "1.1",
        "product_id": str(product_id),
        "quantity": "2",
        "start_date": start,
        "start_time": "09:00",
        "end_date": end,
        "end_time": "15:00",
    }
    form.update(overrides)
    return form


def post_enquiry(client, form, follow=False):
    form = dict(form)
    form.setdefault("csrf_token", public_csrf(client))
    return client.post("/store/enquiries/request", data=form, follow_redirects=follow)


def table_count(app, table):
    with app.app_context():
        try:
            return int(get_db().execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
        except sqlite3.OperationalError:
            # This feature's table is created lazily on first use; before that
            # a refused submission legitimately leaves no table and no rows.
            return 0


class Capture:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __call__(self, user_ids, payload):
        self.calls.append({"ids": list(user_ids), "payload": payload})
        if self.error:
            raise self.error
        return {"recipients": len(user_ids), "sent": 0, "failed": 0, "removed": 0,
                "skipped": 0, "disabled": 0}


# ---------------------------------------------------------------------------
# Public submission
# ---------------------------------------------------------------------------

def test_public_submit_writes_enquiry_and_snapshot_lines_only(app, client):
    branch = add_branch(app, "Enquiry Branch A")
    product = add_product(app, "Enquiry Half Ton", branch_id=branch, price=450.0, sku="ENQ-HT")
    publish_notice(app)

    before = {
        table: table_count(app, table)
        for table in ("customers", "orders", "order_items", "payments", "documents",
                      "product_branch_stock", "shop_enquiries")
    }
    response = post_enquiry(client, rental_form(branch, product, request_key="submit-once"))
    assert response.status_code == 303
    assert "/store/request/" in response.headers["Location"]

    with app.app_context():
        enquiry = row_dict(get_db().execute("SELECT * FROM shop_enquiries").fetchone())
        items = [row_dict(row) for row in get_db().execute("SELECT * FROM shop_enquiry_items").fetchall()]
    assert enquiry["status"] == "new"
    assert enquiry["enquiry_type"] == "rental"
    assert enquiry["branch_id"] == branch
    assert enquiry["email"] == "thandi@example.com"
    assert enquiry["notice_version"] == "1.1"
    assert enquiry["consent_at"]
    assert enquiry["start_at"].startswith(next_days()[0])
    assert len(items) == 1
    assert items[0]["product_id"] == product
    assert items[0]["product_name"] == "Enquiry Half Ton"
    assert items[0]["sku"] == "ENQ-HT"
    assert items[0]["quantity"] == 2
    assert items[0]["unit_price"] == 450.0
    assert items[0]["line_total"] == 900.0

    for table in ("customers", "orders", "order_items", "payments", "documents", "product_branch_stock"):
        assert table_count(app, table) == before[table], f"{table} must not change on public submit"


def test_public_submit_accepts_purchase_items_json_without_dates(app, client):
    branch = add_branch(app, "Enquiry Sales Branch")
    first = add_product(app, "Enquiry New Trailer", branch_id=branch, price=12999.0)
    second = add_product(app, "Enquiry Spare Wheel", branch_id=branch, price=950.0)
    get_db().execute("UPDATE products SET product_type='sale',price_unit='fixed' WHERE id IN (?,?)", (first,second))
    get_db().commit()
    publish_notice(app)

    form = {
        "enquiry_type": "purchase",
        "branch_id": str(branch),
        "customer_name": "Sipho Dlamini",
        "customer_email": "sipho@example.com",
        "customer_phone": "+27 83 555 0199",
        "privacy_accepted": "1",
        "notice_version": "1.1",
        "items_json": json.dumps([
            {"product_id": first, "quantity": 1},
            {"product_id": second, "quantity": 4},
        ]),
    }
    response = post_enquiry(client, form)
    assert response.status_code == 303
    with app.app_context():
        enquiry = row_dict(get_db().execute("SELECT * FROM shop_enquiries").fetchone())
        lines = [row_dict(row) for row in get_db().execute(
            "SELECT product_id, quantity FROM shop_enquiry_items ORDER BY id"
        ).fetchall()]
    assert enquiry["enquiry_type"] == "purchase"
    assert enquiry["start_at"] is None and enquiry["end_at"] is None
    assert [(line["product_id"], line["quantity"]) for line in lines] == [(first, 1), (second, 4)]


def test_public_submit_requires_privacy_acceptance_and_published_notice(app, client):
    branch = add_branch(app, "Enquiry Consent Branch")
    product = add_product(app, "Enquiry Consent Trailer", branch_id=branch)
    publish_notice(app)

    response = post_enquiry(client, rental_form(branch, product, privacy_accepted="0"))
    assert response.status_code == 303
    assert table_count(app, "shop_enquiries") == 0

    # Stale notice version: the box was ticked against wording that changed.
    response = post_enquiry(client, rental_form(branch, product, notice_version="0.9"))
    assert response.status_code == 303
    assert table_count(app, "shop_enquiries") == 0

    # No published notice at all: fail closed, even with a ticked box.
    with app.app_context():
        get_db().execute("DELETE FROM popia_notice_versions")
        get_db().commit()
    response = post_enquiry(client, rental_form(branch, product, notice_version=""))
    assert response.status_code == 303
    assert table_count(app, "shop_enquiries") == 0


def test_public_submit_refuses_invalid_type_dates_and_quantities(app, client):
    branch = add_branch(app, "Enquiry Validation Branch")
    product = add_product(app, "Enquiry Validation Trailer", branch_id=branch)
    publish_notice(app)

    start, end = next_days()
    cases = [
        {"enquiry_type": "hire"},
        {"start_date": "2020-01-01"},
        {"start_date": end, "end_date": start},
        {"start_time": "25:00"},
        {"quantity": "1.5"},
        {"quantity": "0"},
        {"customer_email": "not-an-email"},
        {"customer_phone": "12"},
        {"customer_name": "   "},
    ]
    for overrides in cases:
        response = post_enquiry(client, rental_form(branch, product, **overrides))
        assert response.status_code == 303, overrides
    assert table_count(app, "shop_enquiries") == 0
    assert table_count(app, "shop_enquiry_items") == 0


def test_public_submit_refuses_honeypot(app, client):
    branch = add_branch(app, "Enquiry Bot Branch")
    product = add_product(app, "Enquiry Bot Trailer", branch_id=branch)
    publish_notice(app)

    response = post_enquiry(client, rental_form(branch, product, company_website="http://spam"))
    assert response.status_code == 303
    assert table_count(app, "shop_enquiries") == 0


def test_public_submit_refuses_a_blocked_client_without_revealing_why(app, client):
    branch = add_branch(app, "Enquiry Blocked Branch")
    product = add_product(app, "Enquiry Blocked Trailer", branch_id=branch)
    publish_notice(app)
    with app.app_context():
        get_db().execute(
            """INSERT INTO customers (customer_type, name, email, phone, is_blocked,
               blocked_reason, created_at)
            VALUES ('individual', 'Thandi Mokoena', 'thandi@example.com', '082 555 0101', 1,
                    'Unpaid damages — internal only', ?)""",
            (now(),),
        )
        get_db().commit()

    response = post_enquiry(client, rental_form(branch, product, request_key="blocked-1"))
    assert response.status_code == 303
    assert table_count(app, "shop_enquiries") == 0
    with client.session_transaction() as session:
        flashes = dict(session.get("_flashes") or [])
    message = " ".join(flashes.values())
    assert "contact the branch" in message.lower()
    assert "unpaid" not in message.lower()
    assert "blocked" not in message.lower()


def test_public_submit_replay_returns_same_receipt_and_one_row(app, client, monkeypatch):
    branch = add_branch(app, "Enquiry Replay Branch")
    product = add_product(app, "Enquiry Replay Trailer", branch_id=branch)
    publish_notice(app)
    add_user(app, "Replay Staff", branch_id=branch, modules=["orders"])
    capture = Capture()
    monkeypatch.setattr(push_notifications, "send_to_users", capture)

    form = rental_form(branch, product, request_key="browser-retry-1")
    first = post_enquiry(client, form)
    second = post_enquiry(client, form)
    assert first.status_code == second.status_code == 303
    assert first.headers["Location"] == second.headers["Location"]
    assert table_count(app, "shop_enquiries") == 1
    assert table_count(app, "shop_enquiry_items") == 1
    assert len(capture.calls) == 1, "a replay must not send a second push"


def test_public_submit_refuses_product_eligibility_and_branch(app, client):
    branch = add_branch(app, "Enquiry Eligibility Branch")
    other_branch = add_branch(app, "Enquiry Other Branch")
    hidden_group = add_group(app, "Hidden Enquiry Group", store_visible=0)
    inactive_group = add_group(app, "Inactive Enquiry Group", active=0)

    maintenance = add_product(app, "Enquiry Maintenance", branch_id=branch, maintenance=1)
    unpublished = add_product(app, "Enquiry Unpublished", branch_id=branch, public_visible=0)
    inactive = add_product(app, "Enquiry Inactive", branch_id=branch, active=0)
    hidden = add_product(app, "Enquiry Hidden Category", branch_id=branch, group_id=hidden_group)
    in_inactive_group = add_product(app, "Enquiry Inactive Category", branch_id=branch, group_id=inactive_group)
    foreign = add_product(app, "Enquiry Foreign Branch", branch_id=other_branch)
    publish_notice(app)

    for product in (maintenance, unpublished, inactive, hidden, in_inactive_group):
        response = post_enquiry(client, rental_form(branch, product))
        assert response.status_code == 303
    # Product that belongs to another depot and is not stocked at this one.
    response = post_enquiry(client, rental_form(branch, foreign))
    assert response.status_code == 303

    # Inactive branch id.
    with app.app_context():
        get_db().execute("UPDATE branches SET active = 0 WHERE id = ?", (branch,))
        get_db().commit()
    response = post_enquiry(client, rental_form(branch, add_product(app, "Enquiry Any", branch_id=branch)))
    assert response.status_code == 303

    assert table_count(app, "shop_enquiries") == 0


def test_public_submit_does_not_move_stock(app, client):
    branch = add_branch(app, "Enquiry Stock Branch")
    product = add_product(app, "Enquiry Zero Stock Trailer", branch_id=branch, quantity=0)
    publish_notice(app)

    with app.app_context():
        get_db().execute(
            "UPDATE product_branch_stock SET quantity = 0 WHERE product_id = ? AND branch_id = ?",
            (product, branch),
        )
        get_db().commit()

    response = post_enquiry(client, rental_form(branch, product, quantity="3"))
    assert response.status_code == 303

    with app.app_context():
        stock = row_dict(get_db().execute(
            "SELECT quantity FROM product_branch_stock WHERE product_id = ? AND branch_id = ?",
            (product, branch),
        ).fetchone())
        product_row = row_dict(get_db().execute(
            "SELECT quantity FROM products WHERE id = ?", (product,)
        ).fetchone())
    assert stock["quantity"] == 0
    assert product_row["quantity"] == 0
    assert table_count(app, "shop_enquiries") == 1


def test_receipt_is_minimal_and_has_no_pii(app, client):
    branch = add_branch(app, "Enquiry Receipt Branch")
    product = add_product(app, "Enquiry Receipt Trailer", branch_id=branch)
    publish_notice(app)

    response = post_enquiry(client, rental_form(branch, product, request_key="receipt-1"))
    location = response.headers["Location"]
    page = client.get(location)
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Reference" in html
    assert "Enquiry Receipt Branch" in html
    assert "thandi@example.com" not in html
    assert "Thandi Mokoena" not in html
    assert "082 555 0101" not in html
    assert "Please call before 17:00" not in html

    assert client.get("/store/request/not-a-real-token").status_code == 404


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def test_only_the_branchs_own_active_staff_are_notified(app, client, monkeypatch):
    branch_a = add_branch(app, "Enquiry Notify A")
    branch_b = add_branch(app, "Enquiry Notify B")
    product = add_product(app, "Enquiry Notify Trailer", branch_id=branch_a)
    publish_notice(app)

    primary = add_user(app, "Notify Primary", branch_id=branch_a, modules=["online_store"])
    extra = add_user(app, "Notify Extra", branch_id=None, modules=["online_store"],
                     extra_branches=[branch_a])
    add_user(app, "Notify All View", branch_id=branch_b, modules=["online_store"], can_view_all=1)
    add_user(app, "Notify Inactive", branch_id=branch_a, modules=["online_store"], active=0)
    add_user(app, "Notify Other Depot", branch_id=branch_b, modules=["online_store"])
    # The seeded main profile is an owner; put it on branch A to prove it is still excluded.
    with app.app_context():
        get_db().execute("UPDATE users SET branch_id = ? WHERE role = 'owner'", (branch_a,))
        get_db().commit()

    capture = Capture()
    monkeypatch.setattr(push_notifications, "send_to_users", capture)

    response = post_enquiry(client, rental_form(branch_a, product, request_key="notify-1"))
    assert response.status_code == 303
    assert len(capture.calls) == 1
    assert set(capture.calls[0]["ids"]) == {primary, extra}

    payload = capture.calls[0]["payload"]
    assert payload["url"].startswith("/online-store/enquiries/")
    serialised = json.dumps(payload)
    assert "Thandi" not in serialised
    assert "thandi@example.com" not in serialised
    assert "082 555 0101" not in serialised


def test_notification_failure_never_loses_the_enquiry(app, client, monkeypatch):
    branch = add_branch(app, "Enquiry Notify Failure")
    product = add_product(app, "Enquiry Notify Failure Trailer", branch_id=branch)
    publish_notice(app)
    add_user(app, "Failure Staff", branch_id=branch, modules=["online_store"])
    monkeypatch.setattr(push_notifications, "send_to_users", Capture(error=RuntimeError("push down")))

    response = post_enquiry(client, rental_form(branch, product, request_key="notify-fail-1"))
    assert response.status_code == 303
    assert table_count(app, "shop_enquiries") == 1


# ---------------------------------------------------------------------------
# Staff access
# ---------------------------------------------------------------------------

def test_staff_screens_require_live_module_and_branch_scope(app, client):
    branch = add_branch(app, "Enquiry Access A")
    other = add_branch(app, "Enquiry Access B")
    product = add_product(app, "Enquiry Access Trailer", branch_id=branch)
    publish_notice(app)
    post_enquiry(client, rental_form(branch, product, request_key="access-1"))
    with app.app_context():
        enquiry_id = int(get_db().execute("SELECT id FROM shop_enquiries").fetchone()["id"])

    own_staff = add_user(app, "Access Own", branch_id=branch, modules=["online_store"])
    foreign_staff = add_user(app, "Access Foreign", branch_id=other, modules=["online_store"])
    orders_staff = add_user(app, "Access Orders Only", branch_id=branch, modules=["orders"])
    no_module = add_user(app, "Access No Module", branch_id=branch, modules=["customers"])
    inactive = add_user(app, "Access Inactive", branch_id=branch, modules=["online_store"], active=0)

    login(client, foreign_staff, branch_id=other, modules=["online_store"])
    assert client.get("/online-store/enquiries").status_code == 200
    assert client.get(f"/online-store/enquiries/{enquiry_id}").status_code == 404

    # Depot staff who work from Orders must be able to open the alert they get.
    login(client, orders_staff, branch_id=branch, modules=["orders"])
    assert client.get("/online-store/enquiries").status_code == 200
    assert client.get(f"/online-store/enquiries/{enquiry_id}").status_code == 200

    login(client, no_module, branch_id=branch, modules=["customers"])
    assert client.get("/online-store/enquiries").status_code == 403
    assert client.get(f"/online-store/enquiries/{enquiry_id}").status_code == 403

    login(client, inactive, branch_id=branch, modules=["online_store"])
    assert client.get("/online-store/enquiries").status_code == 403

    login(client, own_staff, branch_id=branch, modules=["online_store"])
    response = client.get("/online-store/enquiries")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Thandi Mokoena" in html
    detail = client.get(f"/online-store/enquiries/{enquiry_id}")
    assert detail.status_code == 200
    assert "Enquiry Access Trailer" in detail.get_data(as_text=True)


def test_status_assignment_and_notes_are_audited(app, client):
    branch = add_branch(app, "Enquiry Workflow")
    other = add_branch(app, "Enquiry Workflow Other")
    product = add_product(app, "Enquiry Workflow Trailer", branch_id=branch)
    publish_notice(app)

    staff = add_user(app, "Workflow Staff", branch_id=branch, modules=["online_store"])
    extra = add_user(app, "Workflow Extra", branch_id=None, modules=["online_store"],
                     extra_branches=[branch])
    foreign = add_user(app, "Workflow Foreign", branch_id=other, modules=["online_store"])
    owner_id = None
    with app.app_context():
        owner_id = int(get_db().execute("SELECT id FROM users WHERE role = 'owner'").fetchone()["id"])

    login(client, staff, branch_id=branch, modules=["online_store"])
    post_enquiry(client, rental_form(branch, product, request_key="workflow-1"))
    with app.app_context():
        enquiry_id = int(get_db().execute("SELECT id FROM shop_enquiries").fetchone()["id"])
    token = admin_csrf(client)

    response = client.post(f"/online-store/enquiries/{enquiry_id}/status",
                           data={"status": "contacted", "csrf_token": token})
    assert response.status_code == 303
    assert shop_enquiries.get_enquiry(enquiry_id)["status"] == "contacted"

    # Invalid status is refused and leaves the row alone.
    client.post(f"/online-store/enquiries/{enquiry_id}/status",
                data={"status": "sold", "csrf_token": token})
    assert shop_enquiries.get_enquiry(enquiry_id)["status"] == "contacted"

    # A staff member from another branch cannot be assigned.
    client.post(f"/online-store/enquiries/{enquiry_id}/assignment",
                data={"assigned_user_id": str(foreign), "csrf_token": token})
    assert shop_enquiries.get_enquiry(enquiry_id)["assigned_user_id"] is None

    # The owner is never assignable, even when attached to the branch.
    with app.app_context():
        get_db().execute("UPDATE users SET branch_id = ? WHERE id = ?", (branch, owner_id))
        get_db().commit()
    client.post(f"/online-store/enquiries/{enquiry_id}/assignment",
                data={"assigned_user_id": str(owner_id), "csrf_token": token})
    assert shop_enquiries.get_enquiry(enquiry_id)["assigned_user_id"] is None

    client.post(f"/online-store/enquiries/{enquiry_id}/assignment",
                data={"assigned_user_id": str(extra), "csrf_token": token})
    assert shop_enquiries.get_enquiry(enquiry_id)["assigned_user_id"] == extra

    client.post(f"/online-store/enquiries/{enquiry_id}/note",
                data={"note": "Phoned — customer will collect Friday.", "csrf_token": token})
    enquiry = shop_enquiries.get_enquiry(enquiry_id)
    assert "Phoned — customer will collect Friday." in enquiry["internal_notes"]
    event_types = [event["event_type"] for event in enquiry["events"]]
    assert "status_changed" in event_types
    assert "assignment" in event_types
    assert "note" in event_types
    assert any(event["user_id"] == staff for event in enquiry["events"])


def test_admin_post_requires_csrf(app, client):
    branch = add_branch(app, "Enquiry CSRF")
    product = add_product(app, "Enquiry CSRF Trailer", branch_id=branch)
    publish_notice(app)
    staff = add_user(app, "CSRF Staff", branch_id=branch, modules=["online_store"])
    post_enquiry(client, rental_form(branch, product, request_key="csrf-1"))
    with app.app_context():
        enquiry_id = int(get_db().execute("SELECT id FROM shop_enquiries").fetchone()["id"])

    login(client, staff, branch_id=branch, modules=["online_store"])
    response = client.post(f"/online-store/enquiries/{enquiry_id}/status", data={"status": "closed"})
    assert response.status_code == 400
    assert shop_enquiries.get_enquiry(enquiry_id)["status"] == "new"


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def _seed_order(app, branch_id, customer_id, order_number="ENQ-ORDER-1"):
    with app.app_context():
        cursor = get_db().execute(
            """INSERT INTO orders (order_number, customer_id, booking_type, collect_branch_id,
               return_branch_id, status, created_at)
            VALUES (?, ?, 'return', ?, ?, 'draft', ?)""",
            (order_number, customer_id, branch_id, branch_id, now()),
        )
        get_db().commit()
        return cursor.lastrowid


def _seed_customer(app, name, email, phone, branch_id):
    from app.services.customers import create_customer

    with app.app_context():
        return create_customer(
            {"customer_type": "individual", "name": name, "email": email, "phone": phone},
            branch_id=branch_id,
            notify=False,
        )


def _request_context_session(app, user_id, branch_id):
    return app.test_request_context("/online-store/enquiries")


def test_order_prefill_and_link_order_convert_idempotently(app, client):
    branch = add_branch(app, "Enquiry Convert")
    other = add_branch(app, "Enquiry Convert Other")
    product = add_product(app, "Enquiry Convert Trailer", branch_id=branch, price=500.0)
    publish_notice(app)

    staff = add_user(app, "Convert Staff", branch_id=branch, modules=["online_store"])
    customer_id = _seed_customer(app, "Thandi Mokoena", "thandi@example.com", "082 555 0101", branch)
    login(client, staff, branch_id=branch, modules=["online_store"])
    post_enquiry(client, rental_form(branch, product, request_key="convert-1"))
    with app.app_context():
        enquiry_id = int(get_db().execute("SELECT id FROM shop_enquiries").fetchone()["id"])

    with app.test_request_context("/"):
        from flask import session

        session["user_id"] = staff
        session["user_role"] = "staff"
        session["branch_id"] = branch
        session["can_view_all_branches"] = False
        session["branch_ids"] = []
        prefill = shop_enquiries.order_prefill(enquiry_id)
    assert prefill["customer_id"] == customer_id
    assert prefill["collect_branch_id"] == branch
    assert prefill["lines"][0]["product_id"] == product
    assert prefill["lines"][0]["quantity"] == 2
    assert prefill["start_date"] == next_days()[0]

    order_id = _seed_order(app, branch, customer_id, "ENQ-CONVERT-1")
    with app.test_request_context("/"):
        from flask import session

        session["user_id"] = staff
        session["user_role"] = "staff"
        session["branch_id"] = branch
        session["can_view_all_branches"] = False
        session["branch_ids"] = []
        result = shop_enquiries.link_order(enquiry_id, order_id)
        replay = shop_enquiries.link_order(enquiry_id, order_id)

    assert result["already"] is False
    assert replay["already"] is True
    converted = shop_enquiries.get_enquiry(enquiry_id)
    assert converted["status"] == "converted"
    assert converted["linked_order_id"] == order_id
    assert converted["converted_at"]
    assert any(event["event_type"] == "converted" for event in converted["events"])

    # One order can satisfy only one enquiry.
    post_enquiry(client, rental_form(branch, product, request_key="convert-2"))
    with app.app_context():
        second_id = int(get_db().execute(
            "SELECT id FROM shop_enquiries WHERE id <> ?", (enquiry_id,)
        ).fetchone()["id"])
    with app.test_request_context("/"):
        from flask import session

        session["user_id"] = staff
        session["user_role"] = "staff"
        session["branch_id"] = branch
        session["can_view_all_branches"] = False
        session["branch_ids"] = []
        with pytest.raises(ValueError):
            shop_enquiries.link_order(second_id, order_id)

    # A wrong-branch order is refused (the branch gate runs before the match
    # checks, so it can be a permission refusal) and the enquiry stays unconverted.
    foreign_order = _seed_order(app, other, customer_id, "ENQ-CONVERT-2")
    with app.test_request_context("/"):
        from flask import session

        session["user_id"] = staff
        session["user_role"] = "staff"
        session["branch_id"] = branch
        session["can_view_all_branches"] = False
        session["branch_ids"] = []
        with pytest.raises((PermissionError, ValueError)):
            shop_enquiries.link_order(second_id, foreign_order)
    assert shop_enquiries.get_enquiry(second_id)["status"] == "new"


def test_link_order_requires_a_signed_in_staff_account(app):
    branch = add_branch(app, "Enquiry Convert Auth")
    product = add_product(app, "Enquiry Convert Auth Trailer", branch_id=branch)
    publish_notice(app)
    customer_id = _seed_customer(app, "Auth Customer", "auth@example.com", "082 555 0199", branch)
    with app.app_context():
        from app.services.shop_enquiries import submit_enquiry

        form = rental_form(branch, product)
        result = submit_enquiry(form, notify=False)
        enquiry_id = result["enquiry_id"]
        order_id = _seed_order(app, branch, customer_id, "ENQ-AUTH-1")

    with app.test_request_context("/"):
        with pytest.raises(PermissionError):
            shop_enquiries.link_order(enquiry_id, order_id)


def test_prepare_order_creates_an_unverified_client_or_uses_existing(app, client):
    branch = add_branch(app, "Enquiry Prepare")
    product = add_product(app, "Enquiry Prepare Trailer", branch_id=branch)
    publish_notice(app)
    staff = add_user(app, "Prepare Staff", branch_id=branch, modules=["online_store"])
    login(client, staff, branch_id=branch, modules=["online_store"])
    post_enquiry(client, rental_form(branch, product, request_key="prepare-1"))
    with app.app_context():
        enquiry_id = int(get_db().execute("SELECT id FROM shop_enquiries").fetchone()["id"])
    token = admin_csrf(client)

    response = client.post(f"/online-store/enquiries/{enquiry_id}/prepare-order",
                           data={"customer_action": "create_customer", "csrf_token": token})
    assert response.status_code == 303
    assert "/orders/new" in response.headers["Location"]
    assert "enquiry_id=" + str(enquiry_id) in response.headers["Location"]
    assert "customer_id=" in response.headers["Location"]

    with app.app_context():
        customer = row_dict(get_db().execute("SELECT * FROM customers").fetchone())
    assert customer["name"] == "Thandi Mokoena"
    assert customer["email"] == "thandi@example.com"
    assert customer["client_verified"] is None, "creation must not auto-verify the client"
    # The enquiry is not converted until a real order is saved and linked.
    assert shop_enquiries.get_enquiry(enquiry_id)["status"] == "new"

    # A second enquiry with the same details resolves the existing client.
    post_enquiry(client, rental_form(branch, product, request_key="prepare-2",
                                     customer_email="thandi@example.com"))
    with app.app_context():
        second_id = int(get_db().execute(
            "SELECT id FROM shop_enquiries WHERE id <> ?", (enquiry_id,)
        ).fetchone()["id"])
    token = admin_csrf(client)
    response = client.post(f"/online-store/enquiries/{second_id}/prepare-order",
                           data={"customer_action": "prepare", "csrf_token": token})
    assert response.status_code == 303
    assert f"customer_id={customer['id']}" in response.headers["Location"]
    assert table_count(app, "customers") == 1


def test_new_badge_and_list_filters(app, client):
    branch_a = add_branch(app, "Enquiry Badge A")
    branch_b = add_branch(app, "Enquiry Badge B")
    product_a = add_product(app, "Enquiry Badge Trailer A", branch_id=branch_a)
    product_b = add_product(app, "Enquiry Badge Trailer B", branch_id=branch_b)
    publish_notice(app)
    staff = add_user(app, "Badge Staff", branch_id=branch_a, modules=["online_store"])

    login(client, staff, branch_id=branch_a, modules=["online_store"])
    post_enquiry(client, rental_form(branch_a, product_a, request_key="badge-a"))
    post_enquiry(client, rental_form(branch_b, product_b, request_key="badge-b",
                                     customer_email="other@example.com"))

    with app.app_context():
        assert shop_enquiries.new_enquiry_count() == 2
        assert shop_enquiries.new_enquiry_count([branch_a]) == 1

    page = client.get("/online-store/enquiries")
    html = page.get_data(as_text=True)
    assert "1 new" in html
    assert "Thandi Mokoena" in html
    # Branch scope hides branch B's enquiry from branch A's staff.
    assert "other@example.com" not in html

    with app.app_context():
        first_id = int(get_db().execute(
            "SELECT id FROM shop_enquiries WHERE branch_id = ?", (branch_a,)
        ).fetchone()["id"])
        shop_enquiries.set_status(first_id, "contacted", staff)
        assert shop_enquiries.count_new_enquiries([branch_a]) == 0
