import sqlite3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mark_finalized_invoices_sent import affected_invoices, mark_invoices_sent, remaining_count


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            order_number TEXT NOT NULL DEFAULT '',
            customer_id INTEGER
        );
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            order_id INTEGER,
            document_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            number TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            sent_at TEXT NOT NULL DEFAULT '',
            sent_to TEXT NOT NULL DEFAULT '',
            email_status TEXT NOT NULL DEFAULT 'not_sent',
            email_error TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO customers (id, name) VALUES (1, 'Hidden Customer');
        INSERT INTO orders (id, order_number, customer_id) VALUES
            (1, 'ORD-OLD', 1),
            (2, 'ORD-CUTOFF', 1),
            (3, 'ORD-AFTER', 1),
            (4, 'ORD-QUOTE', 1),
            (5, 'ORD-DRAFT', 1),
            (6, 'ORD-ALREADY-SENT', 1),
            (7, 'ORD-FAILED', 1),
            (8, 'ORD-BLANK-STATUS', 1);
        INSERT INTO documents (id, order_id, document_type, status, number, created_at, email_status, sent_at, email_error) VALUES
            (101, 1, 'invoice', 'finalized', 'INV-OLD', '2026-09-26T10:00:00+02:00', 'not_sent', '', 'old error'),
            (102, 2, 'invoice', 'finalized', 'INV-CUTOFF', '2026-09-27T23:59:59+02:00', 'not_sent', '', ''),
            (103, 3, 'invoice', 'finalized', 'INV-AFTER', '2026-09-28T00:00:00+02:00', 'not_sent', '', ''),
            (104, 4, 'quote', 'finalized', 'QUO-OLD', '2026-09-26T10:00:00+02:00', 'not_sent', '', ''),
            (105, 5, 'invoice', 'draft', '', '2026-09-26T10:00:00+02:00', 'not_sent', '', ''),
            (106, 6, 'invoice', 'finalized', 'INV-SENT', '2026-09-26T10:00:00+02:00', 'sent', '2026-09-27T10:00:00Z', ''),
            (107, 7, 'invoice', 'finalized', 'INV-FAILED', '2026-09-26T10:00:00+02:00', 'failed', '', 'SMTP error'),
            (108, 8, 'invoice', 'finalized', 'INV-BLANK', '2026-09-27', '', '', 'old error');
        """
    )

    class DB:
        def query(self, sql, params=()):
            return conn.execute(sql, params).fetchall()

        def exec(self, sql, params=()):
            cur = conn.execute(sql, params)
            conn.commit()
            return cur

    return conn, DB()


def test_selection_predicate_only_finds_historical_finalized_unsent_invoices():
    _conn, db = _db()

    rows = affected_invoices(db, "2026-09-27")

    assert [row["id"] for row in rows] == [101, 102, 108]
    assert [row["order_number"] for row in rows] == ["ORD-OLD", "ORD-CUTOFF", "ORD-BLANK-STATUS"]
    assert all("name" not in row for row in rows)  # no customer data in audit output
    assert remaining_count(db, "2026-09-27") == 3


def test_mark_invoices_sent_updates_only_matching_rows_and_clears_errors():
    conn, db = _db()

    updated = mark_invoices_sent(db, "2026-09-27", "2026-09-30T08:00:00Z")

    assert updated == 3
    assert remaining_count(db, "2026-09-27") == 0
    changed = conn.execute(
        "SELECT id, email_status, sent_at, sent_to, email_error FROM documents WHERE id IN (101, 102, 108) ORDER BY id"
    ).fetchall()
    assert [(row["id"], row["email_status"], row["sent_at"], row["sent_to"], row["email_error"]) for row in changed] == [
        (101, "sent", "2026-09-30T08:00:00Z", "", ""),
        (102, "sent", "2026-09-30T08:00:00Z", "", ""),
        (108, "sent", "2026-09-30T08:00:00Z", "", ""),
    ]
    untouched = conn.execute(
        "SELECT id, email_status, sent_at, email_error FROM documents WHERE id NOT IN (101, 102, 108) ORDER BY id"
    ).fetchall()
    assert [(row["id"], row["email_status"], row["sent_at"], row["email_error"]) for row in untouched] == [
        (103, "not_sent", "", ""),
        (104, "not_sent", "", ""),
        (105, "not_sent", "", ""),
        (106, "sent", "2026-09-27T10:00:00Z", ""),
        (107, "failed", "", "SMTP error"),
    ]
