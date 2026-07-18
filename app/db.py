import sqlite3
from datetime import datetime
from flask import current_app, g
from werkzeug.security import generate_password_hash

from app.turso_db import connect_turso

SCHEMA = r"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    name TEXT NOT NULL,
    initials TEXT NOT NULL DEFAULT 'AD',
    role TEXT NOT NULL DEFAULT 'owner',
    branch_id INTEGER REFERENCES branches(id) ON DELETE SET NULL,
    can_view_all_branches INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    company_name TEXT NOT NULL DEFAULT 'ABI Solutions',
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    logo_path TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT 'South Africa',
    address_line1 TEXT NOT NULL DEFAULT '',
    address_line2 TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    province TEXT NOT NULL DEFAULT '',
    postcode TEXT NOT NULL DEFAULT '',
    additional_detail1 TEXT NOT NULL DEFAULT '',
    additional_detail2 TEXT NOT NULL DEFAULT '',
    timezone TEXT NOT NULL DEFAULT 'Africa/Johannesburg',
    first_day_of_week TEXT NOT NULL DEFAULT 'Sunday',
    date_format TEXT NOT NULL DEFAULT 'dd-mm-yyyy',
    use_ampm INTEGER NOT NULL DEFAULT 0,
    units TEXT NOT NULL DEFAULT 'metric',
    pricing_enabled INTEGER NOT NULL DEFAULT 1,
    currency TEXT NOT NULL DEFAULT 'ZAR',
    currency_symbol TEXT NOT NULL DEFAULT 'R',
    currency_position TEXT NOT NULL DEFAULT 'before',
    tax_mode TEXT NOT NULL DEFAULT 'exclusive',
    default_pickup_time TEXT NOT NULL DEFAULT '09:00',
    default_return_time TEXT NOT NULL DEFAULT '15:00',
    enable_time_selection INTEGER NOT NULL DEFAULT 1,
    time_increment_minutes INTEGER NOT NULL DEFAULT 60,
    enable_operating_hours INTEGER NOT NULL DEFAULT 0,
    deposit_mode TEXT NOT NULL DEFAULT 'product_specific',
    deposit_value REAL NOT NULL DEFAULT 0,
    store_enabled INTEGER NOT NULL DEFAULT 1,
    show_prices INTEGER NOT NULL DEFAULT 1,
    show_availability INTEGER NOT NULL DEFAULT 1,
    store_title TEXT NOT NULL DEFAULT 'ABI Solutions Rentals',
    store_intro TEXT NOT NULL DEFAULT 'Browse our rental catalogue and request a booking online.',
    store_hero_text TEXT NOT NULL DEFAULT 'Select your rental period and we will confirm availability.',
    checkout_instructions TEXT NOT NULL DEFAULT 'Submit your booking request and our team will confirm availability before payment.',
    store_contact_email TEXT NOT NULL DEFAULT '',
    store_contact_phone TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tax_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    rate REAL NOT NULL DEFAULT 0,
    is_default INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operating_hours (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day_of_week INTEGER NOT NULL UNIQUE,
    open_time TEXT NOT NULL DEFAULT '09:00',
    close_time TEXT NOT NULL DEFAULT '17:00',
    closed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS app_store_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 0,
    config_json TEXT NOT NULL DEFAULT '{}',
    installed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS branches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    code TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    address_line1 TEXT NOT NULL DEFAULT '',
    address_line2 TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    province TEXT NOT NULL DEFAULT '',
    postal_code TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_type TEXT NOT NULL DEFAULT 'individual',
    name TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    marketing_opt_in INTEGER NOT NULL DEFAULT 0,
    address_line1 TEXT NOT NULL DEFAULT '',
    address_line2 TEXT NOT NULL DEFAULT '',
    suburb TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    province TEXT NOT NULL DEFAULT '',
    postal_code TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT 'South Africa',
    balance_due REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    product_type TEXT NOT NULL DEFAULT 'rental',
    description TEXT NOT NULL DEFAULT '',
    sku TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    public_visible INTEGER NOT NULL DEFAULT 1,
    price_amount REAL NOT NULL DEFAULT 0,
    price_unit TEXT NOT NULL DEFAULT 'day',
    security_deposit REAL NOT NULL DEFAULT 0,
    tax_profile_id INTEGER REFERENCES tax_profiles(id) ON DELETE SET NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    tracking_method TEXT NOT NULL DEFAULT 'bulk',
    branch_id INTEGER REFERENCES branches(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coupons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    discount_type TEXT NOT NULL DEFAULT 'percent',
    value REAL NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_number TEXT NOT NULL UNIQUE,
    customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
    booking_type TEXT NOT NULL DEFAULT 'return',
    collect_branch_id INTEGER REFERENCES branches(id) ON DELETE SET NULL,
    return_branch_id INTEGER REFERENCES branches(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    payment_status TEXT NOT NULL DEFAULT 'payment_due',
    start_at TEXT,
    end_at TEXT,
    subtotal REAL NOT NULL DEFAULT 0,
    discount_total REAL NOT NULL DEFAULT 0,
    coupon_code TEXT NOT NULL DEFAULT '',
    tax_total REAL NOT NULL DEFAULT 0,
    deposit_total REAL NOT NULL DEFAULT 0,
    total REAL NOT NULL DEFAULT 0,
    due_total REAL NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    custom_name TEXT NOT NULL DEFAULT '',
    quantity INTEGER NOT NULL DEFAULT 1,
    unit_price REAL NOT NULL DEFAULT 0,
    line_subtotal REAL NOT NULL DEFAULT 0,
    line_tax REAL NOT NULL DEFAULT 0,
    line_total REAL NOT NULL DEFAULT 0,
    billing_mode TEXT NOT NULL DEFAULT 'catalog'
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    amount REAL NOT NULL,
    method TEXT NOT NULL DEFAULT 'manual',
    reference TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'paid',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER REFERENCES orders(id) ON DELETE CASCADE,
    document_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    number TEXT NOT NULL DEFAULT '',
    pdf_path TEXT NOT NULL DEFAULT '',
    sent_at TEXT NOT NULL DEFAULT '',
    sent_to TEXT NOT NULL DEFAULT '',
    email_status TEXT NOT NULL DEFAULT 'not_sent',
    email_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def now():
    return datetime.utcnow().isoformat(timespec="seconds")


def get_db():
    if "db" not in g:
        turso_url = current_app.config.get("TURSO_DATABASE_URL")
        if turso_url:
            g.db = connect_turso(turso_url, current_app.config.get("TURSO_AUTH_TOKEN"))
        else:
            g.db = sqlite3.connect(current_app.config["DATABASE"])
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()



def ensure_column(db, table, column, definition):
    existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def rename_column(db, table, old_name, new_name):
    existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if old_name in existing and new_name not in existing:
        db.execute(f"ALTER TABLE {table} RENAME COLUMN {old_name} TO {new_name}")


def run_migrations(db):

    db.execute("""CREATE TABLE IF NOT EXISTS branches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        code TEXT NOT NULL DEFAULT '',
        phone TEXT NOT NULL DEFAULT '',
        email TEXT NOT NULL DEFAULT '',
        address_line1 TEXT NOT NULL DEFAULT '',
        address_line2 TEXT NOT NULL DEFAULT '',
        city TEXT NOT NULL DEFAULT '',
        province TEXT NOT NULL DEFAULT '',
        postal_code TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    rename_column(db, "operating_hours", "checked", "closed")
    ensure_column(db, "company_settings", "store_enabled", "INTEGER NOT NULL DEFAULT 1")
    ensure_column(db, "company_settings", "show_prices", "INTEGER NOT NULL DEFAULT 1")
    ensure_column(db, "company_settings", "show_availability", "INTEGER NOT NULL DEFAULT 1")
    ensure_column(db, "company_settings", "store_title", "TEXT NOT NULL DEFAULT 'ABI Solutions Rentals'")
    ensure_column(db, "company_settings", "store_intro", "TEXT NOT NULL DEFAULT 'Browse our rental catalogue and request a booking online.'")
    ensure_column(db, "company_settings", "store_hero_text", "TEXT NOT NULL DEFAULT 'Select your rental period and we will confirm availability.'")
    ensure_column(db, "company_settings", "checkout_instructions", "TEXT NOT NULL DEFAULT 'Submit your booking request and our team will confirm availability before payment.'")
    ensure_column(db, "company_settings", "store_contact_email", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "company_settings", "store_contact_phone", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "users", "branch_id", "INTEGER REFERENCES branches(id) ON DELETE SET NULL")
    ensure_column(db, "users", "can_view_all_branches", "INTEGER NOT NULL DEFAULT 1")
    ensure_column(db, "products", "tracking_method", "TEXT NOT NULL DEFAULT 'bulk'")
    ensure_column(db, "products", "branch_id", "INTEGER REFERENCES branches(id) ON DELETE SET NULL")
    ensure_column(db, "orders", "booking_type", "TEXT NOT NULL DEFAULT 'return'")
    ensure_column(db, "orders", "collect_branch_id", "INTEGER REFERENCES branches(id) ON DELETE SET NULL")
    ensure_column(db, "orders", "return_branch_id", "INTEGER REFERENCES branches(id) ON DELETE SET NULL")
    ensure_column(db, "orders", "coupon_code", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "customers", "address_line1", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "customers", "address_line2", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "customers", "suburb", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "customers", "city", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "customers", "province", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "customers", "postal_code", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "customers", "country", "TEXT NOT NULL DEFAULT 'South Africa'")
    ensure_column(db, "order_items", "billing_mode", "TEXT NOT NULL DEFAULT 'catalog'")
    ensure_column(db, "documents", "sent_at", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "documents", "sent_to", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "documents", "email_status", "TEXT NOT NULL DEFAULT 'not_sent'")
    ensure_column(db, "documents", "email_error", "TEXT NOT NULL DEFAULT ''")
    db.execute("""CREATE TABLE IF NOT EXISTS app_store_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        provider TEXT NOT NULL DEFAULT '',
        is_active INTEGER NOT NULL DEFAULT 0,
        config_json TEXT NOT NULL DEFAULT '{}',
        installed_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")

def init_db():
    db = get_db()
    db.executescript(SCHEMA)
    run_migrations(db)
    ts = now()
    db.execute("INSERT OR IGNORE INTO company_settings (id, company_name, email, updated_at) VALUES (1, 'ABI Solutions', 'info@abi-solutions.local', ?)", (ts,))
    db.execute("INSERT OR IGNORE INTO tax_profiles (id, name, rate, is_default, active, created_at) VALUES (1, 'No VAT', 0, 1, 1, ?)", (ts,))
    for branch_name, code in [('Branch 1', 'BR1'), ('Branch 2', 'BR2'), ('Branch 3', 'BR3')]:
        db.execute("INSERT OR IGNORE INTO branches (name, code, created_at, updated_at) VALUES (?, ?, ?, ?)", (branch_name, code, ts, ts))
    default_branch = db.execute("SELECT id FROM branches ORDER BY id LIMIT 1").fetchone()
    if default_branch:
        db.execute("UPDATE products SET branch_id = COALESCE(branch_id, ?) WHERE branch_id IS NULL", (default_branch['id'],))
        db.execute("UPDATE orders SET collect_branch_id = COALESCE(collect_branch_id, ?), return_branch_id = COALESCE(return_branch_id, ?) WHERE collect_branch_id IS NULL OR return_branch_id IS NULL", (default_branch['id'], default_branch['id']))
    for day in range(7):
        db.execute("INSERT OR IGNORE INTO operating_hours (day_of_week, open_time, close_time, closed) VALUES (?, '09:00', '17:00', ?)", (day, 1 if day in (0,6) else 0))
    admin_email = current_app.config["ADMIN_EMAIL"]
    existing = db.execute("SELECT id FROM users WHERE email = ?", (admin_email,)).fetchone()
    if not existing:
        db.execute(
            "INSERT INTO users (email, password_hash, name, initials, role, branch_id, can_view_all_branches, active, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?)",
            (admin_email, generate_password_hash(current_app.config["ADMIN_PASSWORD"]), "ABI Admin", "AA", "owner", default_branch['id'] if default_branch else None, ts),
        )
    db.commit()


def init_app(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()
