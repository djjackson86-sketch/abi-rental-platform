"""Shared DB plumbing for the one-off Booqable import scripts.

Kept out of app/services on purpose: these scripts are maintenance tools, and
app/services/booqable_import.py stays free of database access so it can be
unit-tested against real export rows.
"""
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:          # so callers can always import the app package
    sys.path.insert(0, str(ROOT))


class Connection:
    """Uniform wrapper over sqlite3 and the Turso adapter."""

    def __init__(self, conn, kind, describe):
        self.conn = conn
        self.kind = kind
        self.describe = describe

    def query(self, sql, params=()):
        if self.kind == "turso":
            return self.conn.execute(sql, params).fetchall()
        return self.conn.execute(sql, params).fetchall()

    def exec(self, sql, params=()):
        if self.kind == "turso":
            return self.conn.execute(sql, params)
        cur = self.conn.execute(sql, params)
        try:
            self.conn.commit()
        except Exception:
            pass
        return cur

    def table_columns(self, table):
        return {row["name"] for row in self.query(f"PRAGMA table_info({table})")}

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def _live_credentials():
    cred = ROOT / "turso-access.txt"
    if not cred.exists():
        sys.exit("turso-access.txt not found and TURSO_DATABASE_URL is unset")
    text = cred.read_text()
    url = re.search(r"(libsql://\S+)", text)
    token = re.search(r"(eyJ[\w.\-]+)", text)
    return (url.group(1) if url else ""), (token.group(1) if token else "")


def connect(database=None, live=False):
    """local sqlite when a path is given, otherwise Turso (live creds with --live)."""
    if database:
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return Connection(conn, "sqlite", "local sqlite:%s" % database)

    url = os.environ.get("TURSO_DATABASE_URL", "")
    token = os.environ.get("TURSO_AUTH_TOKEN", "")
    if not url and live:
        url, token = _live_credentials()

    if not url:
        default = os.path.join(ROOT, "instance", "abi_rental.db")
        conn = sqlite3.connect(default)
        conn.row_factory = sqlite3.Row
        return Connection(conn, "sqlite", "local sqlite:%s" % default)

    from app.turso_db import connect_turso
    return Connection(connect_turso(url, token), "turso",
                      "LIVE Turso" if live else "Turso")


def ensure_source_columns(db, log=print):
    """Additive only - mirrors app.db.run_migrations()."""
    for table in ("customers", "orders", "products"):
        existing = db.table_columns(table)
        if not existing:
            continue
        for column in ("source_system", "source_id"):
            if column not in existing:
                db.exec(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
                log(f"  + {table}.{column}")
        db.exec(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_source "
            f"ON {table}(source_system, source_id) WHERE source_id <> ''"
        )


def finish(code=0):
    """Exit without waiting on the libsql client's background session thread."""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(code)
