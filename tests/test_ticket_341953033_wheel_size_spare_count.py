"""Ticket ABI-341953033 - wheel size on rental inventory + dashboard Spare Wheel Count.

Requested edit:

1. In the rental inventory form, an area called **Wheel size** with a dropdown of
   the five sizes the client listed (10" - 4H, 13" - 4H, 13" - 5H, 14" - 5H,
   14" - 6H).
2. A dashboard section **Spare Wheel Count**: the same five sizes with
   **Expected** (the total number of that wheel size on all trailers that have
   not been picked up - one wheel per trailer), an **Actual** box staff fill in,
   and the difference Actual - Expected: ``0`` gets a green tick, a negative
   number is red and says ``(shortage)``, a positive number is green and says
   ``(over)``.

These tests pin the whole path: the dropdown only for rentals and the value it
saves, the Expected maths (picked-up units excluded, reserved trailers counted,
blank wheel sizes / non-rental / inactive products ignored, never negative), the
branch scope, the per-depot-per-day storage with a read-only All-branches sum, the
variance readings and their colours, and the server-side guards (crafted depot,
negative or junk counts, future day).
"""

import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.db import get_db, run_migrations  # noqa: E402
from app.services import spare_wheels  # noqa: E402
from app.services.access import MODULE_KEYS  # noqa: E402
from app.services.cash import today_iso  # noqa: E402
from app.services.products import WHEEL_SIZES, create_product, update_product  # noqa: E402

SIZES = list(WHEEL_SIZES)
DAY = today_iso()
TEMPLATE_PATH = ROOT / "templates" / "admin" / "_dashboard_spare_wheels.html"


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


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def owner_id(app):
    with app.app_context():
        return get_db().execute(
            "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
        ).fetchone()["id"]


def login(client, app, name=None, password="admin123"):
    """Sign in the way the UI does: pick a name, then the password."""
    with client.application.app_context():
        if name is None:
            row = get_db().execute(
                "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
        else:
            row = get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
    return client.post("/login", data={"user_id": str(row["id"]), "password": password},
                       follow_redirects=True)


def add_staff(client, app, name, branch_id, password="staff123"):
    """Owner creates an additional (non-main) account tied to one depot."""
    login(client, app)
    client.post("/settings/users/permissions", data={"module": list(MODULE_KEYS)},
                follow_redirects=True)
    client.post("/settings/users/add", data={"name": name, "password": password,
                                            "branch_id": str(branch_id)},
                follow_redirects=True)
    with app.app_context():
        return get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()["id"]


def make_product(app, name, quantity, wheel_size="", product_type="rental", active=1, branch_id=1):
    with app.app_context():
        return create_product({
            "name": name,
            "product_type": product_type,
            "tracking_method": "bulk",
            "quantity": str(quantity),
            "wheel_size": wheel_size,
            "branch_id": "" if branch_id is None else str(branch_id),
            "active": "1" if active else "",
        })


def add_order(app, status, lines, collect_branch=1, return_branch=1, number=None):
    """One order with catalog lines: ``lines`` = [(product_id, quantity), ...]."""
    with app.app_context():
        db = get_db()
        number = number or f"ORD-{status.upper()}-{collect_branch}-{len(lines)}"
        db.execute(
            """INSERT INTO orders (order_number, booking_type, collect_branch_id, return_branch_id,
            status, created_at) VALUES (?, 'return', ?, ?, ?, ?)""",
            (number, collect_branch, return_branch, status, f"{DAY}T09:00:00"),
        )
        order_id = db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()["id"]
        for product_id, quantity in lines:
            db.execute(
                """INSERT INTO order_items (order_id, product_id, custom_name, quantity,
                unit_price, line_subtotal, line_total) VALUES (?, ?, '', ?, 100, 100, 100)""",
                (order_id, product_id, quantity),
            )
        db.commit()
        return order_id


def variance_kinds(html):
    return re.findall(r'class="spare-wheel-variance is-(\w+)"', html)


def variance_texts(html):
    cells = re.findall(r'class="spare-wheel-variance is-\w+"[^>]*>(.*?)</td>', html, flags=re.S)
    out = []
    for cell in cells:
        cell = re.sub(r"<[^>]*>", " ", cell)
        out.append(re.sub(r"\s+", " ", cell).strip())
    return out


# --------------------------------------------------------------------------- #
# 1. The wheel size list and the product form
# --------------------------------------------------------------------------- #

def test_the_wheel_size_list_is_exactly_what_the_client_asked_for():
    assert SIZES == ['10" - 4H', '13" - 4H', '13" - 5H', '14" - 5H', '14" - 6H']


def test_the_rental_product_form_offers_a_wheel_size_dropdown(client, app):
    login(client, app)
    html = client.get("/inventory/new").data.decode()
    assert 'data-wheel-size-field' in html
    assert 'name="wheel_size"' in html
    assert '<option value="">Not set</option>' in html
    # The inch mark is HTML-escaped inside the attribute, which is correct.
    for size in SIZES:
        assert size.replace('"', "&#34;") in html
    # One dropdown, not a text box: nothing can be typed outside the client's list.
    assert 'input name="wheel_size"' not in html


def test_a_sale_or_service_product_hides_and_disables_the_wheel_size(client, app):
    sale = make_product(app, "Flatbed For Sale", 2, SIZES[0], product_type="sale")
    login(client, app)
    html = client.get(f"/inventory/{sale}/edit").data.decode()
    assert 'data-wheel-size-field hidden' in html
    assert re.search(r'<select name="wheel_size" disabled', html)


def test_the_stored_wheel_size_is_selected_when_editing(client, app):
    product = make_product(app, "Box Trailer", 4, SIZES[2])
    login(client, app)
    html = client.get(f"/inventory/{product}/edit").data.decode()
    assert re.search(r'<option value="13&#34; - 5H" selected>', html)


def test_the_form_saves_the_wheel_size_and_a_blank_choice_clears_it(client, app):
    login(client, app)
    client.post("/inventory/new", data={
        "name": "Wheel Sized Trailer", "product_type": "rental", "tracking_method": "bulk",
        "quantity": "3", "active": "1", "wheel_size": SIZES[3],
    }, follow_redirects=True)
    with app.app_context():
        row = get_db().execute("SELECT id, wheel_size FROM products WHERE name = 'Wheel Sized Trailer'").fetchone()
    assert row["wheel_size"] == SIZES[3]
    product_id = row["id"]

    client.post(f"/inventory/{product_id}/edit", data={
        "name": "Wheel Sized Trailer", "product_type": "rental", "tracking_method": "bulk",
        "quantity": "3", "active": "1", "wheel_size": "",
    }, follow_redirects=True)
    with app.app_context():
        assert get_db().execute("SELECT wheel_size FROM products WHERE id = ?", (product_id,)).fetchone()["wheel_size"] == ""


def test_a_wheel_size_outside_the_list_is_never_stored(client, app):
    login(client, app)
    client.post("/inventory/new", data={
        "name": "Junk Wheel", "product_type": "rental", "tracking_method": "bulk",
        "quantity": "1", "active": "1", "wheel_size": "20\" - 9H",
    }, follow_redirects=True)
    with app.app_context():
        assert get_db().execute("SELECT wheel_size FROM products WHERE name = 'Junk Wheel'").fetchone()["wheel_size"] == ""


def test_a_post_without_the_field_keeps_the_stored_wheel_size(app):
    """The field is disabled (so absent) for non-rentals and legacy callers.

    An absent field must never silently wipe a stored size, and switching a
    product back to rental must bring it back.
    """
    product = make_product(app, "Switchable Trailer", 2, SIZES[1])
    with app.app_context():
        update_product(product, {"name": "Switchable Trailer", "product_type": "sale",
                                 "tracking_method": "bulk", "quantity": "2", "active": "1"})
        assert get_db().execute("SELECT wheel_size FROM products WHERE id = ?", (product,)).fetchone()["wheel_size"] == SIZES[1]
        update_product(product, {"name": "Switchable Trailer", "product_type": "rental",
                                 "tracking_method": "bulk", "quantity": "2", "active": "1"})
        assert get_db().execute("SELECT wheel_size FROM products WHERE id = ?", (product,)).fetchone()["wheel_size"] == SIZES[1]


def test_duplicating_a_product_copies_its_wheel_size(client, app):
    product = make_product(app, "Original Trailer", 3, SIZES[4])
    login(client, app)
    client.post(f"/inventory/{product}/duplicate", follow_redirects=True)
    with app.app_context():
        copy = get_db().execute(
            "SELECT wheel_size FROM products WHERE name = 'Original Trailer (copy)'").fetchone()
    assert copy["wheel_size"] == SIZES[4]


# --------------------------------------------------------------------------- #
# 2. Expected - one wheel per trailer that has not been picked up
# --------------------------------------------------------------------------- #

def test_expected_counts_only_trailers_still_in_the_yard(app):
    in_yard = make_product(app, "In Yard", 3, SIZES[0])
    picked = make_product(app, "Picked Up", 4, SIZES[0])
    add_order(app, "started", [(picked, 2)])
    with app.app_context():
        expected = spare_wheels.expected_spare_wheels()
    # 3 in the yard + (4 - 2 picked up) = 5
    assert expected[SIZES[0]] == 5
    assert expected[SIZES[1]] == 0


def test_a_reserved_trailer_still_counts_but_a_returned_one_does_not(app):
    reserved = make_product(app, "Reserved", 2, SIZES[2])
    returned = make_product(app, "Returned", 3, SIZES[2])
    canceled = make_product(app, "Canceled", 2, SIZES[2])
    draft = make_product(app, "Draft", 2, SIZES[2])
    add_order(app, "reserved", [(reserved, 1)])
    add_order(app, "returned", [(returned, 3)])
    add_order(app, "canceled", [(canceled, 2)])
    add_order(app, "draft", [(draft, 1)])
    with app.app_context():
        expected = spare_wheels.expected_spare_wheels()
    # Reserved has not been collected, so it is still in the yard: 2. Returned,
    # cancelled and draft orders hold no trailer, so their whole stock counts.
    assert expected[SIZES[2]] == 2 + 3 + 2 + 2


def test_expected_ignores_non_rentals_blank_sizes_and_inactive_products(app):
    make_product(app, "Sale With Wheel", 5, SIZES[1], product_type="sale")
    make_product(app, "Service With Wheel", 5, SIZES[1], product_type="service")
    make_product(app, "No Wheel Size", 5, "")
    make_product(app, "Archived Trailer", 5, SIZES[1], active=0)
    with app.app_context():
        expected = spare_wheels.expected_spare_wheels()
    assert expected == {size: 0 for size in SIZES}


def test_a_trailer_with_more_units_out_than_in_stock_never_counts_below_zero(app):
    trailer = make_product(app, "Over Booked", 1, SIZES[3])
    add_order(app, "started", [(trailer, 3)])
    with app.app_context():
        assert spare_wheels.expected_spare_wheels()[SIZES[3]] == 0


def test_a_per_branch_split_product_uses_its_total(app):
    with app.app_context():
        product = create_product({
            "name": "Split Trailer", "product_type": "rental", "tracking_method": "bulk",
            "wheel_size": SIZES[1], "active": "1", "branch_id": "1",
            "qty_branch_1": "2", "qty_branch_2": "3",
        })
        assert spare_wheels.expected_spare_wheels()[SIZES[1]] == 5
        assert product


def test_expected_follows_the_branch_filter_and_the_session_scope(app):
    make_product(app, "Depot One Trailer", 2, SIZES[0], branch_id=1)
    make_product(app, "Depot Two Trailer", 4, SIZES[0], branch_id=2)
    make_product(app, "Unassigned Trailer", 1, SIZES[0], branch_id=None)
    add_order(app, "started", [(make_product(app, "Depot One Picked", 3, SIZES[0], branch_id=1), 1)],
              collect_branch=1)
    with app.app_context():
        assert spare_wheels.expected_spare_wheels(branch_id=1)[SIZES[0]] == 2 + 2
        assert spare_wheels.expected_spare_wheels(branch_id=2)[SIZES[0]] == 4
        # No filter: every depot plus the unassigned trailer.
        assert spare_wheels.expected_spare_wheels()[SIZES[0]] == 2 + 2 + 4 + 1


def test_a_branch_limited_account_only_ever_sees_its_own_depot(client, app):
    make_product(app, "Depot One Trailer", 2, SIZES[0], branch_id=1)
    make_product(app, "Depot Two Trailer", 4, SIZES[0], branch_id=2)
    add_staff(client, app, "Depot Two Staff", 2)
    login(client, app, "Depot Two Staff", "staff123")
    # The panel is read through a real request, because the branch scope comes
    # from the session (and a crafted ?branch= can only ever narrow it).
    own = client.get("/dashboard").data.decode()
    assert re.findall(r'data-expected="(\d+)"', own)[0] == "4"
    forced = client.get("/dashboard?branch=1").data.decode()
    assert re.findall(r'data-expected="(\d+)"', forced)[0] == "4"


# --------------------------------------------------------------------------- #
# 3. The dashboard panel
# --------------------------------------------------------------------------- #

def test_the_dashboard_panel_lists_the_sizes_in_order_with_their_expected(client, app):
    make_product(app, "Trailer A", 3, SIZES[0])
    make_product(app, "Trailer B", 2, SIZES[4])
    login(client, app)
    html = client.get("/dashboard?branch=1").data.decode()
    assert "Spare Wheel Count" in html
    rows = re.findall(r'<td>(1[0-4]&#34; - \dH)</td>\s*<td class="spare-wheel-count"[^>]*>(\d+)</td>', html)
    assert [r[0] for r in rows] == [s.replace('"', "&#34;") for s in SIZES]
    assert [r[1] for r in rows] == ["3", "0", "0", "0", "2"]


def test_the_panel_sits_on_the_dashboard_and_is_editable_for_one_depot(client, app):
    make_product(app, "Trailer A", 3, SIZES[0])
    login(client, app)
    html = client.get("/dashboard?branch=1").data.decode()
    assert 'action="/dashboard/spare-wheels"' in html
    assert 'name="branch" value="1"' in html
    assert f'name="day" value="{DAY}"' in html
    assert 'Save spare wheel count' in html


def test_the_all_branches_panel_is_read_only(app, client):
    make_product(app, "Depot One Trailer", 2, SIZES[0], branch_id=1)
    login(client, app)
    html = client.get("/dashboard").data.decode()
    assert 'action="/dashboard/spare-wheels"' not in html
    assert "Select a branch in the top Branch filter to type in the actual counts." in html
    assert re.search(r'name="actual_0"', html) is None


def test_the_variance_readings_mark_zero_shortage_and_over(client, app):
    make_product(app, "Size A", 4, SIZES[0])   # actual 4 -> 0 -> green tick
    make_product(app, "Size B", 3, SIZES[1])   # actual 1 -> -2 -> red shortage
    make_product(app, "Size C", 1, SIZES[2])   # actual 5 -> +4 -> green over
    login(client, app)
    client.post("/dashboard/spare-wheels", data={
        "day": DAY, "branch": "1", "actual_0": "4", "actual_1": "1", "actual_2": "5",
    }, follow_redirects=True)
    html = client.get("/dashboard?branch=1").data.decode()
    assert variance_kinds(html) == ["ok", "short", "over", "none", "none"]
    assert variance_texts(html)[:3] == ["&#10004; 0", "-2 (shortage)", "+4 (over)"]
    assert variance_texts(html)[3] == "Not counted yet"


def test_the_variance_colours_are_real_css():
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert ".spare-wheel-variance.is-ok{color:var(--success)}" in template
    assert ".spare-wheel-variance.is-over{color:var(--success)}" in template
    assert ".spare-wheel-variance.is-short{color:var(--danger)}" in template


# --------------------------------------------------------------------------- #
# 4. Saving the Actual counts
# --------------------------------------------------------------------------- #

def test_saving_actual_counts_stores_them_for_the_depot_and_day(client, app):
    login(client, app)
    response = client.post("/dashboard/spare-wheels", data={
        "day": DAY, "branch": "1", "actual_0": "2", "actual_1": "0",
    }, follow_redirects=True)
    assert f"Spare wheel count saved for {DAY}" in response.data.decode()
    with app.app_context():
        rows = get_db().execute(
            """SELECT branch_id, business_day, wheel_size, actual_count, reported_by_user_id
            FROM spare_wheel_counts ORDER BY wheel_size""").fetchall()
    assert [(r["branch_id"], r["business_day"], r["wheel_size"], r["actual_count"]) for r in rows] == [
        (1, DAY, SIZES[0], 2), (1, DAY, SIZES[1], 0),
    ]
    assert rows[0]["reported_by_user_id"] == owner_id(app)


def test_a_saved_count_comes_back_in_the_boxes(client, app):
    login(client, app)
    client.post("/dashboard/spare-wheels", data={"day": DAY, "branch": "1", "actual_3": "6"},
                follow_redirects=True)
    html = client.get("/dashboard?branch=1").data.decode()
    assert re.search(r'name="actual_3" value="6"', html)
    assert variance_kinds(html)[3] == "over"
    assert "+6 (over)" in html


def test_a_blank_box_clears_the_count_again(client, app):
    login(client, app)
    client.post("/dashboard/spare-wheels", data={"day": DAY, "branch": "1", "actual_0": "2"},
                follow_redirects=True)
    client.post("/dashboard/spare-wheels", data={"day": DAY, "branch": "1", "actual_0": ""},
                follow_redirects=True)
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) AS c FROM spare_wheel_counts").fetchone()["c"] == 0


def test_junk_and_negative_counts_are_refused_without_writing(client, app):
    login(client, app)
    negative = client.post("/dashboard/spare-wheels",
                           data={"day": DAY, "branch": "1", "actual_0": "-3"}, follow_redirects=True)
    assert "cannot be negative" in negative.data.decode()
    junk = client.post("/dashboard/spare-wheels",
                       data={"day": DAY, "branch": "1", "actual_0": "two"}, follow_redirects=True)
    assert "must be whole numbers" in junk.data.decode()
    fraction = client.post("/dashboard/spare-wheels",
                           data={"day": DAY, "branch": "1", "actual_1": "1.5"}, follow_redirects=True)
    assert "must be whole numbers" in fraction.data.decode()
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) AS c FROM spare_wheel_counts").fetchone()["c"] == 0


def test_a_crafted_wheel_size_is_ignored(client, app):
    login(client, app)
    client.post("/dashboard/spare-wheels", data={
        "day": DAY, "branch": "1", "actual_0": "1", "wheel_size": "20\" - 9H",
        "actual_9": "99", "actual_0_wheel_size": "junk",
    }, follow_redirects=True)
    with app.app_context():
        rows = get_db().execute("SELECT wheel_size FROM spare_wheel_counts").fetchall()
    assert [r["wheel_size"] for r in rows] == [SIZES[0]]


def test_counts_are_kept_per_depot_and_never_leak_between_them(client, app):
    login(client, app)
    client.post("/dashboard/spare-wheels", data={"day": DAY, "branch": "1", "actual_0": "2"},
                follow_redirects=True)
    client.post("/dashboard/spare-wheels", data={"day": DAY, "branch": "2", "actual_0": "7"},
                follow_redirects=True)
    one = client.get("/dashboard?branch=1").data.decode()
    two = client.get("/dashboard?branch=2").data.decode()
    assert re.search(r'name="actual_0" value="2"', one)
    assert re.search(r'name="actual_0" value="7"', two)


def test_a_branch_limited_account_cannot_write_another_depot(client, app):
    add_staff(client, app, "Depot Two Staff", 2)
    login(client, app, "Depot Two Staff", "staff123")
    client.post("/dashboard/spare-wheels", data={"day": DAY, "branch": "1", "actual_0": "5"},
                follow_redirects=True)
    with app.app_context():
        rows = get_db().execute("SELECT branch_id, actual_count FROM spare_wheel_counts").fetchall()
    assert [(r["branch_id"], r["actual_count"]) for r in rows] == [(2, 5)]


def test_a_future_day_cannot_be_counted(client, app):
    login(client, app)
    response = client.post("/dashboard/spare-wheels",
                           data={"day": "2099-01-01", "branch": "1", "actual_0": "2"},
                           follow_redirects=True)
    assert "today or a past day only" in response.data.decode()
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) AS c FROM spare_wheel_counts").fetchone()["c"] == 0


def test_the_all_branches_panel_sums_the_depots(client, app):
    login(client, app)
    client.post("/dashboard/spare-wheels", data={"day": DAY, "branch": "1", "actual_0": "2"},
                follow_redirects=True)
    client.post("/dashboard/spare-wheels", data={"day": DAY, "branch": "2", "actual_0": "3"},
                follow_redirects=True)
    html = client.get("/dashboard").data.decode()
    assert variance_kinds(html)[0] == "over"
    assert "+5 (over)" in html


def test_a_count_for_another_day_does_not_show_today(client, app):
    login(client, app)
    client.post("/dashboard/spare-wheels", data={"day": "2026-01-01", "branch": "1", "actual_0": "9"},
                follow_redirects=True)
    html = client.get("/dashboard?branch=1").data.decode()
    assert variance_kinds(html)[0] == "none"


# --------------------------------------------------------------------------- #
# 5. Schema / guards
# --------------------------------------------------------------------------- #

def test_the_migration_adds_the_column_and_table_again_when_they_are_missing(app):
    with app.app_context():
        db = get_db()
        db.execute("ALTER TABLE products DROP COLUMN wheel_size")
        db.execute("DROP TABLE spare_wheel_counts")
        db.commit()
        run_migrations(db)
        db.commit()
        columns = {row["name"] for row in db.execute("PRAGMA table_info(products)").fetchall()}
        tables = {row["name"] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        spare_columns = {row["name"] for row in db.execute("PRAGMA table_info(spare_wheel_counts)").fetchall()}
    assert "wheel_size" in columns
    assert "spare_wheel_counts" in tables
    assert {"branch_id", "business_day", "wheel_size", "actual_count",
            "reported_by_user_id", "updated_at"} <= spare_columns


def test_an_existing_product_keeps_a_blank_wheel_size(app):
    with app.app_context():
        get_db().execute(
            """INSERT INTO products (name, product_type, active, public_visible, price_amount,
            price_unit, quantity, tracking_method, created_at)
            VALUES ('Legacy Trailer', 'rental', 1, 1, 100, 'day', 4, 'bulk', ?)""", (DAY,))
        get_db().commit()
        assert get_db().execute(
            "SELECT wheel_size FROM products WHERE name = 'Legacy Trailer'").fetchone()["wheel_size"] == ""


def test_a_staff_account_without_the_dashboard_module_cannot_reach_the_panel(client, app):
    add_staff(client, app, "Orders Only", 1)
    with app.app_context():
        user_id = get_db().execute("SELECT id FROM users WHERE name = 'Orders Only'").fetchone()["id"]
        get_db().execute("UPDATE users SET modules_json = ? WHERE id = ?", ('["orders"]', user_id))
        get_db().commit()
    login(client, app, "Orders Only", "staff123")
    assert client.get("/dashboard").status_code == 403
    assert client.post("/dashboard/spare-wheels",
                       data={"day": DAY, "branch": "1", "actual_0": "1"}).status_code == 403
