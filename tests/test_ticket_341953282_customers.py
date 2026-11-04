"""Ticket ABI-341953282: the customer directory's Legacy balance column, filter and sorting.

Covers the three behaviours the ticket asks for:
* the Legacy balance column sits immediately after Balance due and shows what is
  still *outstanding* on active imported rows, never the original imported sums;
* "With legacy balance" filters both the rows and the reported total from one
  shared filter definition (count parity), and sorting keeps the filters;
* every data column header sorts ascending/descending on the server, in the list
  query before LIMIT/OFFSET, and only allowlisted columns/directions reach SQL.
"""

import os
import tempfile
from urllib.parse import parse_qs, urlparse

import pytest
from bs4 import BeautifulSoup

from app import create_app
from app.db import get_db
from app.services.customers import (
    customer_filtered_total,
    customer_outstanding_balances,
    list_customers,
    normalise_customer_sort,
)
from app.services.legacy_balances import legacy_balance_summary, record_legacy_payment, upsert_legacy_balance

DAY = "2026-10-04"


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


def login(client):
    with client.application.app_context():
        row = get_db().execute(
            "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
        ).fetchone()
    return client.post("/login", data={"user_id": str(row["id"]), "password": "admin123"},
                       follow_redirects=True)


def seed_customer(db, name, *, email=None, customer_type="individual", marketing=0,
                  created_at=f"{DAY}T08:00:00"):
    email = email or f"{name.lower().replace(' ', '.')}@example.test"
    db.execute(
        """INSERT INTO customers (customer_type, name, email, phone, marketing_opt_in,
            address_line1, address_line2, suburb, city, province, postal_code, country,
            custom_fields_json, balance_due, standard_discount_percent, client_verified,
            is_blocked, blocked_reason, created_by_user_id, branch_id, created_at)
            VALUES (?, ?, ?, '0710000000', ?, '', '', '', '', '', '', 'South Africa', '{}',
                    0, 0, NULL, 0, '', 1, 1, ?)""",
        (customer_type, name, email, marketing, created_at),
    )
    return db.execute("SELECT id FROM customers WHERE name = ?", (name,)).fetchone()["id"]


def add_legacy(db, customer_id, number, amount_due, *, settled=0, status="active"):
    """One imported legacy row; ``settled`` is money already collected against it."""
    upsert_legacy_balance(customer_id, "old-cust", number, amount_due)
    if settled or status != "active":
        db.execute(
            "UPDATE legacy_customer_balances SET settled_amount = ?, status = ? WHERE source_order_number = ?",
            (settled, status, number),
        )


def seed_order(db, customer_id, number, *, total=100):
    db.execute(
        """INSERT INTO orders (order_number, customer_id, booking_type, collect_branch_id,
            return_branch_id, status, payment_status, start_at, end_at, subtotal, tax_total,
            deposit_total, total, due_total, notes, created_at)
            VALUES (?, ?, 'return', 1, 1, 'returned', 'payment_due', ?, ?, ?, 0, 0, ?, ?, '', ?)""",
        (number, customer_id, f"{DAY}T09:00:00", f"{DAY}T17:00:00", total, total, total,
         f"{DAY}T09:00:00"),
    )
    return db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()["id"]


def row_names(body):
    soup = BeautifulSoup(body, "html.parser")
    return [a.get_text(strip=True) for a in soup.select(".customers-table .customer-name-cell strong")]


def table_headers(body):
    soup = BeautifulSoup(body, "html.parser")
    table = soup.select_one(".customers-table")
    assert table is not None
    return [th.get_text(" ", strip=True) for th in table.select("thead th")]


# --- Legacy balance column -------------------------------------------------

def test_legacy_column_sits_after_balance_due_and_sums_only_active_outstanding(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        cid = seed_customer(db, "Column Customer")
        add_legacy(db, cid, "L-1", 500, settled=200)      # outstanding 300
        add_legacy(db, cid, "L-2", 100)                   # outstanding 100
        add_legacy(db, cid, "L-3", 900, status="collected")   # settled in full elsewhere
        add_legacy(db, cid, "L-4", 250, status="waived")      # closed by hand
        add_legacy(db, cid, "L-5", 40, settled=40)            # active but nothing left
        db.commit()

        row = list_customers(query="Column Customer")[0]
        # 300 + 100 — not 500 + 100 + 900 + 250 + 40, and not the original 1 790.
        assert row["legacy_balance"] == 400.0
        # The detail page's own summary agrees with the directory figure.
        assert legacy_balance_summary(cid)["total"] == 400.0

    body = client.get("/customers?query=Column").get_data(as_text=True)
    headers = table_headers(body)
    assert headers.index("Legacy balance") == headers.index("Balance due") + 1
    soup = BeautifulSoup(body, "html.parser")
    cell = soup.select_one(".customers-table tbody tr .legacy-balance-cell")
    assert cell is not None and cell.get_text(strip=True) == "R400.00"
    # The original import total must not leak into the column.
    assert "R1790.00" not in body


def test_legacy_column_follows_payments_collected_after_the_import(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        cid = seed_customer(db, "Paying Customer")
        add_legacy(db, cid, "L-10", 500)
        db.commit()

    with app.app_context():
        assert list_customers(query="Paying Customer")[0]["legacy_balance"] == 500.0
        record_legacy_payment(cid, {"amount": "200", "method": "cash"})
        assert list_customers(query="Paying Customer")[0]["legacy_balance"] == 300.0


# --- "With legacy balance" filter and count parity -------------------------

def seed_mixed_directory(db):
    """A legacy balance, a fully collected one, an empty active one, and a clean client."""
    owing = seed_customer(db, "Legacy Owing", created_at=f"{DAY}T09:00:00")
    add_legacy(db, owing, "M-1", 750, settled=250)          # 500 open
    collected = seed_customer(db, "Legacy Collected", created_at=f"{DAY}T10:00:00")
    add_legacy(db, collected, "M-2", 400, status="collected")
    emptied = seed_customer(db, "Legacy Emptied", created_at=f"{DAY}T11:00:00")
    add_legacy(db, emptied, "M-3", 60, settled=60)          # active, zero left
    clean = seed_customer(db, "No Legacy", created_at=f"{DAY}T12:00:00")
    return owing, collected, emptied, clean


def test_with_legacy_filter_limits_rows_and_total_together(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        owing, collected, emptied, clean = seed_mixed_directory(db)
        db.commit()

        assert [row["id"] for row in list_customers(legacy="1")] == [owing]
        assert customer_filtered_total(legacy="1") == 1
        assert {row["id"] for row in list_customers(legacy="without")} == {collected, emptied, clean}
        assert customer_filtered_total(legacy="without") == 3
        # The filter never changes what "all" means.
        assert customer_filtered_total() == 4

    body = client.get("/customers?legacy=1").get_data(as_text=True)
    assert row_names(body) == ["Legacy Owing"]
    assert "of 1" in body
    # An active row settled to nothing is not a legacy balance.
    assert "Legacy Emptied" not in body


def test_filter_and_sort_keep_count_parity_with_each_other(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        owing = seed_customer(db, "Filter Owing", customer_type="company")
        add_legacy(db, owing, "F-1", 120)
        second = seed_customer(db, "Filter Owing Two", customer_type="company")
        add_legacy(db, second, "F-2", 240)
        clean = seed_customer(db, "Filter Clean", customer_type="company")
        seed_order(db, clean, "F-ORD", total=99)
        individual = seed_customer(db, "Filter Individual", customer_type="individual")
        add_legacy(db, individual, "F-3", 900)
        db.commit()

        filters = {"legacy": "1", "customer_type": "company"}
        assert [row["id"] for row in list_customers(**filters, sort="legacy_balance", direction="asc")] == [owing, second]
        assert customer_filtered_total(**filters) == 2

    body = client.get("/customers?legacy=1&customer_type=company&sort=legacy_balance&dir=asc").get_data(as_text=True)
    assert row_names(body) == ["Filter Owing", "Filter Owing Two"]
    assert "of 2" in body


# --- Allowlisted server-side sorting before pagination ---------------------

def seed_sortable_directory(db):
    values = [
        ("Alpha Sort", 10, f"{DAY}T01:00:00"),
        ("Bravo Sort", 50, f"{DAY}T02:00:00"),
        ("Charlie Sort", 30, f"{DAY}T03:00:00"),
        ("Delta Sort", 0, f"{DAY}T04:00:00"),
        ("Echo Sort", 70, f"{DAY}T05:00:00"),
    ]
    ids = {}
    for name, amount, created_at in values:
        cid = seed_customer(db, name, created_at=created_at)
        if amount:
            add_legacy(db, cid, f"S-{name.split()[0]}", amount)
        ids[name.split()[0]] = cid
    # Distinct live order debt so Balance due can be sorted too.
    seed_order(db, ids["Alpha"], "S-ORD-A", total=100)
    seed_order(db, ids["Delta"], "S-ORD-D", total=900)
    return ids


def test_legacy_balance_sort_orders_the_whole_directory_before_pagination(app):
    with app.app_context():
        db = get_db()
        ids = seed_sortable_directory(db)
        db.commit()

        names = lambda rows: [row["name"].split()[0] for row in rows]
        assert names(list_customers(sort="legacy_balance", direction="desc", limit=2, offset=0)) == ["Echo", "Bravo"]
        assert names(list_customers(sort="legacy_balance", direction="desc", limit=2, offset=2)) == ["Charlie", "Alpha"]
        assert names(list_customers(sort="legacy_balance", direction="desc", limit=2, offset=4)) == ["Delta"]
        assert names(list_customers(sort="legacy_balance", direction="asc", limit=2, offset=0)) == ["Delta", "Alpha"]
        assert names(list_customers(sort="legacy_balance", direction="asc", limit=3, offset=3)) == ["Bravo", "Echo"]
        # Paging never repeats or drops a row.
        paged = [row["id"] for offset in range(5)
                 for row in list_customers(sort="legacy_balance", direction="desc", limit=1, offset=offset)]
        assert paged == [ids[key] for key in ["Echo", "Bravo", "Charlie", "Alpha", "Delta"]]


def test_name_orders_and_balance_due_sort_server_side(app):
    with app.app_context():
        db = get_db()
        seed_sortable_directory(db)
        db.commit()

        assert [row["name"] for row in list_customers(sort="name", direction="asc")] == [
            "Alpha Sort", "Bravo Sort", "Charlie Sort", "Delta Sort", "Echo Sort"]
        assert [row["name"] for row in list_customers(sort="name", direction="desc")][0] == "Echo Sort"
        # Balance due sorts on live order debt, not the legacy column.
        by_due = list_customers(sort="balance_due", direction="desc")
        assert by_due[0]["name"] == "Delta Sort" and by_due[0]["outstanding_balance"] == 900
        assert by_due[1]["name"] == "Alpha Sort" and by_due[1]["outstanding_balance"] == 100
        assert all(row["outstanding_balance"] == 0 for row in by_due[2:])


def test_route_headers_apply_desc_and_asc_with_filters_and_total_intact(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        seed_sortable_directory(db)
        db.commit()

    desc = client.get("/customers?legacy=1&sort=legacy_balance&dir=desc").get_data(as_text=True)
    assert row_names(desc) == ["Echo Sort", "Bravo Sort", "Charlie Sort", "Alpha Sort"]
    assert "of 4" in desc

    asc = client.get("/customers?legacy=1&sort=legacy_balance&dir=asc").get_data(as_text=True)
    assert row_names(asc) == ["Alpha Sort", "Charlie Sort", "Bravo Sort", "Echo Sort"]
    assert "of 4" in asc


def test_sort_links_retain_filters_and_toggle_direction(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        cid = seed_customer(db, "Sort Target", marketing=1)
        add_legacy(db, cid, "T-1", 100)
        db.commit()

    body = client.get(
        "/customers?query=Sort&customer_type=individual&marketing=subscribed&legacy=1&sort=name&dir=asc"
    ).get_data(as_text=True)
    soup = BeautifulSoup(body, "html.parser")
    links = {a.get_text(" ", strip=True): str(a["href"]) for a in soup.select("a.table-sort")}

    # The active header shows its direction and announces it on the <th>.
    assert "Name ↑" in links
    name_header = soup.select_one(".customers-table thead th[aria-sort='ascending']")
    assert name_header is not None and "Name" in name_header.get_text()

    # Clicking the active column flips it; every other header starts ascending.
    assert parse_qs(urlparse(links["Name ↑"]).query)["dir"] == ["desc"]
    assert parse_qs(urlparse(links["Legacy balance"]).query)["dir"] == ["asc"]

    for label, href in links.items():
        query = parse_qs(urlparse(href).query)
        # Filters survive the sort, including the legacy-balance filter.
        assert query["query"] == ["Sort"], label
        assert query["customer_type"] == ["individual"], label
        assert query["marketing"] == ["subscribed"], label
        assert query["legacy"] == ["1"], label
        assert query["folder"] == ["active"], label
        assert query["sort"] in (["name"], ["legacy_balance"], ["balance_due"], ["orders"],
                                 ["created_at"], ["created_by"], ["id"], ["email"], ["client_verified"]), label


def test_unknown_sort_or_direction_cannot_reach_the_query(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        seed_sortable_directory(db)
        db.commit()
        default = [row["id"] for row in list_customers()]
        assert [row["id"] for row in list_customers(
            sort="legacy_balance; DROP TABLE customers;--", direction="sideways")] == default
        assert [row["id"] for row in list_customers(sort="nope", direction="DESC")] == default
        # A direction that is not exactly "asc" is the default descending order.
        assert [row["id"] for row in list_customers(sort="", direction="asc; DELETE FROM customers")] == default
        assert get_db().execute("SELECT COUNT(*) AS c FROM customers").fetchone()["c"] == 5

    for query in ["?sort=legacy_balance%3B%20DROP%20TABLE%20customers%3B--&dir=sideways",
                  "?sort=nope&dir=nope", "?sort=Legacy_Balance&dir=ASC"]:
        response = client.get("/customers" + query)
        assert response.status_code == 200

    assert normalise_customer_sort("balance_due", "ASC") == ("balance_due", "asc")
    assert normalise_customer_sort("Legacy_Balance", " desc ") == ("legacy_balance", "desc")
    assert normalise_customer_sort("evil", "asc") == ("created_at", "asc")


def test_outstanding_balance_summary_still_agrees_with_the_list_column(app):
    with app.app_context():
        db = get_db()
        ids = seed_sortable_directory(db)
        db.commit()
        balances = customer_outstanding_balances(list(ids.values()) + [-1])
        for customer_id in ids.values():
            listed = next(row for row in list_customers() if row["id"] == customer_id)
            assert listed["outstanding_balance"] == balances[customer_id] == customer_outstanding_balances([customer_id])[customer_id]
        assert balances[-1] == 0.0
