"""ABI-341953196: batch the order-form customer picker's per-customer aggregates.

Live symptom (measured against the deployed Turso book: ~4 800 clients, 390
orders, 544 payments): one render of the new-order form billed ~4,000,000
``rows_read``. The picker ran five correlated subqueries *per customer*, so
``orders`` was fully scanned once for every client and ``payments`` once for
every matched order. On a remote libSQL database that is ``clients x tables`` and
it is invisible in a local test suite — only the real endpoint shows it.

The fix has two halves:
  * the three hot-path child indexes (``orders(customer_id)``,
    ``payments(order_id)``, ``documents(order_id)``) added additively to SCHEMA and
    ``run_migrations``;
  * the picker/``list_customers`` aggregates rewritten as grouped derived tables.

These tests pin the batched values to the exact original correlated projection
(the oracle below is the pre-fix SQL, kept verbatim), prove the additive indexes
exist and are idempotent, prove the child lookups are actually chosen by the
planner, and guard against the projection growing scalar subqueries again.
"""
import os
import tempfile

import pytest

from app import create_app
from app.db import get_db, run_migrations
from app.routes.orders import _customer_picker_sql, _customer_summary_by_id, _customers
from app.services.credit_limits import debt_sql
from app.services.customers import list_customers
from app.services.portal_registrations import pending_exclusion_clause

TS = "2026-01-01T00:00:00"


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


def _oracle_sql():
    """The pre-fix per-customer correlated projection — the parity oracle."""
    return f"""
        SELECT id, customer_type, name, email, phone, marketing_opt_in,
               address_line1, address_line2, suburb, city, province, postal_code, country, custom_fields_json, standard_discount_percent,
               client_verified, is_blocked, blocked_reason, credit_allowed, credit_limit,
               CASE WHEN credit_allowed = 1 THEN {debt_sql("customers.id")} ELSE 0 END AS account_credit_debt,
               (SELECT COALESCE(SUM(o.total - COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.order_id=o.id AND p.status='paid' AND COALESCE(p.deleted_at,'')='' AND LOWER(COALESCE(p.method,'')) <> 'account'), 0)), 0)
                FROM orders o WHERE o.customer_id = customers.id AND o.status NOT IN ('canceled','cancelled','archived')) AS previous_orders_balance,
               (SELECT COALESCE(SUM(cc.amount), 0) FROM customer_credits cc WHERE cc.customer_id = customers.id AND cc.status = 'active') AS customer_credit_balance,
               (SELECT COALESCE(SUM(l.amount_due - l.settled_amount), 0) FROM legacy_customer_balances l WHERE l.customer_id = customers.id AND l.status = 'active' AND l.amount_due > l.settled_amount) AS legacy_balance,
               (SELECT COUNT(*) FROM orders o WHERE o.customer_id = customers.id AND o.status NOT IN ('canceled','cancelled','archived') AND o.payment_status IN ('paid','overpaid')) AS orders_to_date
        FROM customers
        WHERE 1=1{pending_exclusion_clause("customers")}
        ORDER BY name
    """


def _aggregates(db, sql):
    out = {}
    for row in db.execute(sql).fetchall():
        out[row["id"]] = (
            round(float(row["account_credit_debt"] or 0), 2),
            round(float(row["previous_orders_balance"] or 0), 2),
            round(float(row["customer_credit_balance"] or 0), 2),
            round(float(row["legacy_balance"] or 0), 2),
            int(row["orders_to_date"] or 0),
        )
    return out


def seed(app):
    """One client per financial shape the picker has to report.

    Triggers are dropped first: the credit/finalisation guards legitimately refuse
    the *historical* shapes under test (an ``account`` payment row, an order that
    overshoots a limit), and a trigger never affects a SELECT anyway.
    """
    ids = {}
    with app.app_context():
        db = get_db()
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
            db.execute('DROP TRIGGER IF EXISTS "%s"' % name)

        def customer(label, **cols):
            cols.setdefault("name", label)
            cols.setdefault("created_at", TS)
            fields = ["customer_type", "name", "email", "phone", "marketing_opt_in",
                      "client_verified", "is_blocked", "blocked_reason", "credit_allowed",
                      "credit_limit", "source_system", "created_at"]
            defaults = {"customer_type": "individual", "email": "", "phone": "",
                        "marketing_opt_in": 0, "client_verified": 1, "is_blocked": 0,
                        "blocked_reason": "", "credit_allowed": 0, "credit_limit": 0,
                        "source_system": ""}
            defaults.update(cols)
            cur = db.execute(
                "INSERT INTO customers (%s) VALUES (%s)" % (", ".join(fields), ", ".join("?" * len(fields))),
                [defaults.get(f) for f in fields],
            )
            ids[label] = cur.lastrowid
            return cur.lastrowid

        seq = {"n": 0}

        def order(cid, total, status="started", payment_status="payment_due"):
            seq["n"] += 1
            cur = db.execute(
                "INSERT INTO orders (order_number, customer_id, status, payment_status, total, created_at) VALUES (?,?,?,?,?,?)",
                ("ORD-%04d" % seq["n"], cid, status, payment_status, total, TS),
            )
            return cur.lastrowid

        def payment(oid, amount, status="paid", method="cash", deleted_at=""):
            db.execute(
                "INSERT INTO payments (order_id, amount, method, status, deleted_at, created_at) VALUES (?,?,?,?,?,?)",
                (oid, amount, method, status, deleted_at, TS),
            )

        def credit(cid, amount, status="active"):
            db.execute(
                "INSERT INTO customer_credits (customer_id, amount, status, created_at) VALUES (?,?,?,?)",
                (cid, amount, status, TS),
            )

        def legacy(cid, amount_due, settled=0, status="active"):
            seq["n"] += 1
            db.execute(
                "INSERT INTO legacy_customer_balances (customer_id, source_order_number, amount_due, settled_amount, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (cid, "OLD-%04d" % seq["n"], amount_due, settled, status, TS, TS),
            )

        def registration(cid, status):
            db.execute(
                "INSERT INTO portal_registrations (customer_id, status, created_at) VALUES (?,?,?)",
                (cid, status, TS),
            )

        customer("None")

        mixed = customer("Mixed")
        o1 = order(mixed, 100, payment_status="partially_paid")
        o2 = order(mixed, 150, payment_status="paid")
        payment(o1, 60)
        payment(o2, 150)

        customer("Canceled")
        order(ids["Canceled"], 200, status="canceled")

        customer("Archived")
        order(ids["Archived"], 500, status="archived")

        filters = customer("Filters")
        of = order(filters, 300, payment_status="partially_paid")
        payment(of, 100)
        payment(of, 50, deleted_at="2026-02-01T00:00:00")
        payment(of, 25, method="account")
        payment(of, 30, status="pending")

        overpaid = customer("Overpaid")
        order(overpaid, 100, payment_status="overpaid")
        payment(db.execute("SELECT id FROM orders WHERE customer_id=?", (overpaid,)).fetchone()["id"], 150)

        facility = customer("Facility")
        order(facility, 400, payment_status="payment_due")
        order(facility, 999, status="canceled")
        db.execute("UPDATE customers SET credit_allowed=1, credit_limit=10000 WHERE id=?", (facility,))

        customer("Blocked", is_blocked=1, blocked_reason="test block")

        rejected = customer("PendingSignUp")
        registration(rejected, "pending")
        removed = customer("DeletedSignUp")
        registration(removed, "deleted")

        credits = customer("Credits")
        credit(credits, 50)
        credit(credits, 999, status="used")

        legacyc = customer("Legacy")
        legacy(legacyc, 100, 0)
        legacy(legacyc, 50, 50)          # fully settled -> excluded
        legacy(legacyc, 30, 0, status="waived")  # not active -> excluded

        legacypt = customer("LegacyPartial")
        legacy(legacypt, 200, 50)

        nopay = customer("DeletePayment")
        onp = order(nopay, 250, payment_status="paid")
        payment(onp, 250, deleted_at="2026-03-01T00:00:00")

        db.commit()
    return ids


def test_picker_values_match_correlated_oracle(app):
    ids = seed(app)
    with app.app_context():
        db = get_db()
        oracle = _aggregates(db, _oracle_sql())
        batched = _aggregates(db, _customer_picker_sql(pending_exclusion_clause("customers"), " ORDER BY customers.name"))

    # The batched form returns exactly the same clients with exactly the same five
    # aggregates as the pre-fix correlated projection.
    assert batched == oracle
    assert ids["PendingSignUp"] not in batched, "pending self-registration must stay hidden"
    assert ids["DeletedSignUp"] not in batched, "deleted self-registration must stay hidden"
    assert ids["Blocked"] in batched, "a blocked client still appears in the picker"

    # Non-vacuous sanity checks so a broken oracle cannot make parity trivially true.
    assert oracle[ids["Mixed"]][1] == 40.0 and oracle[ids["Mixed"]][4] == 1
    assert oracle[ids["Filters"]][1] == 200.0
    assert oracle[ids["Overpaid"]][1] == -50.0 and oracle[ids["Overpaid"]][4] == 1
    assert oracle[ids["Facility"]][0] == 1399.0          # all orders, incl. canceled
    assert oracle[ids["Credits"]][2] == 50.0
    assert oracle[ids["Legacy"]][3] == 100.0
    assert oracle[ids["LegacyPartial"]][3] == 150.0
    assert oracle[ids["DeletePayment"]][1] == 250.0 and oracle[ids["DeletePayment"]][4] == 1


def test_picker_and_selected_summary_agree(app):
    ids = seed(app)
    with app.app_context():
        listed = {c["id"]: (c["account_credit_used"], c["previous_orders_balance"],
                            c["customer_credit_balance"], c["legacy_balance"], c["orders_to_date"])
                  for c in _customers() if c}
        for label, cid in ids.items():
            if label == "DeletedSignUp":
                assert _customer_summary_by_id(cid) is None, label
                continue
            selected = _customer_summary_by_id(cid)
            assert selected is not None, label
            assert (selected["account_credit_used"], selected["previous_orders_balance"],
                    selected["customer_credit_balance"], selected["legacy_balance"],
                    selected["orders_to_date"]) == listed[cid], label


def test_list_customers_order_count_is_batched(app):
    ids = seed(app)
    with app.app_context():
        db = get_db()
        rows = {r["id"]: r for r in list_customers()}
        counts = {cid: db.execute("SELECT COUNT(*) AS c FROM orders WHERE customer_id=?", (cid,)).fetchone()["c"]
                  for cid in rows}
    for cid, row in rows.items():
        assert int(row["order_count"]) == counts[cid]
    assert int(rows[ids["Mixed"]]["order_count"]) == 2
    assert int(rows[ids["None"]]["order_count"]) == 0
    assert ids["PendingSignUp"] not in rows and ids["DeletedSignUp"] not in rows


def test_projection_has_no_scalar_subqueries(app):
    """Regression guard: the picker projection must stay batched, not N+1."""
    sql = _customer_picker_sql(pending_exclusion_clause("customers"), " ORDER BY customers.name")
    projection = sql.split("FROM customers", 1)[0]
    assert "(SELECT" not in projection and "SELECT" not in projection.replace("SELECT ", "", 1)


def test_hot_path_indexes_exist_and_migrations_idempotent(app):
    wanted = {"idx_orders_customer", "idx_payments_order", "idx_documents_order"}
    with app.app_context():
        db = get_db()

        def present():
            return {r["name"] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name IN (?,?,?)",
                tuple(sorted(wanted)),
            ).fetchall()}
        assert wanted <= present()
        before = db.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
        run_migrations(db)
        run_migrations(db)          # idempotent: a second/third run is a no-op
        assert wanted <= present()
        assert db.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"] == before


def test_child_lookups_use_the_new_indexes(app):
    seeded = seed(app)
    with app.app_context():
        db = get_db()
        cid = seeded["Mixed"]
        oid = db.execute("SELECT id FROM orders WHERE customer_id=? LIMIT 1", (cid,)).fetchone()["id"]

        def plan(sql, params=()):
            return " ".join(str(r[3]) for r in db.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall())

        assert "idx_orders_customer" in plan("SELECT COUNT(*) FROM orders WHERE customer_id = ?", (cid,))
        assert "idx_payments_order" in plan("SELECT SUM(amount) FROM payments WHERE order_id = ?", (oid,))
        assert "idx_documents_order" in plan("SELECT COUNT(*) FROM documents WHERE order_id = ?", (oid,))
        # The customer page's outstanding-balance query probes documents per order.
        assert "idx_documents_order" in plan(
            "SELECT o.customer_id FROM orders o WHERE o.customer_id IN (?) "
            "AND EXISTS (SELECT 1 FROM documents d WHERE d.order_id = o.id "
            "AND d.document_type='invoice' AND d.status='finalized') GROUP BY o.customer_id",
            (cid,),
        )
