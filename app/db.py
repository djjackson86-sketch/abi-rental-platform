import sqlite3
from datetime import datetime
from app.services.timezone import local_now_iso
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
    -- NULL = inherit the shared default set in company_settings.staff_permissions_json
    modules_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    company_name TEXT NOT NULL DEFAULT 'Sano Trailers',
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    vat_number TEXT NOT NULL DEFAULT '',
    company_reg_no TEXT NOT NULL DEFAULT '',
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
    vat_rate REAL NOT NULL DEFAULT 15,
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
    store_title TEXT NOT NULL DEFAULT 'Sano Trailers Rentals',
    store_intro TEXT NOT NULL DEFAULT 'Browse our rental catalogue and request a booking online.',
    store_hero_text TEXT NOT NULL DEFAULT 'Select your rental period and we will confirm availability.',
    checkout_instructions TEXT NOT NULL DEFAULT 'Submit your booking request and our team will confirm availability before payment.',
    store_contact_email TEXT NOT NULL DEFAULT '',
    store_contact_phone TEXT NOT NULL DEFAULT '',
    invoice_email_message TEXT NOT NULL DEFAULT 'Dear {customer_name},

Please find attached {document_label} {document_number} for order {order_number}.

Kind regards,
{company_name}',
    invoice_email_signature TEXT NOT NULL DEFAULT '',
    invoice_email_signature_include_logo INTEGER NOT NULL DEFAULT 0,
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
    bank_name TEXT NOT NULL DEFAULT '',
    bank_account_name TEXT NOT NULL DEFAULT '',
    bank_account_number TEXT NOT NULL DEFAULT '',
    bank_branch_code TEXT NOT NULL DEFAULT '',
    bank_account_type TEXT NOT NULL DEFAULT '',
    bank_reference_note TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS branch_operating_hours (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    day_of_week INTEGER NOT NULL,
    open_time TEXT NOT NULL DEFAULT '09:00',
    close_time TEXT NOT NULL DEFAULT '17:00',
    closed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE(branch_id, day_of_week)
);

-- Extra depots an additional account may see. No rows = the historic single
-- branch (users.branch_id) or all branches when can_view_all_branches is set.
CREATE TABLE IF NOT EXISTS user_branch_access (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    UNIQUE(user_id, branch_id)
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
    custom_fields_json TEXT NOT NULL DEFAULT '{}',
    balance_due REAL NOT NULL DEFAULT 0,
    standard_discount_percent REAL NOT NULL DEFAULT 0,
    client_verified INTEGER,
    is_blocked INTEGER NOT NULL DEFAULT 0,
    blocked_reason TEXT NOT NULL DEFAULT '',
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
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
    hourly_extra_rate REAL NOT NULL DEFAULT 0,
    tax_profile_id INTEGER REFERENCES tax_profiles(id) ON DELETE SET NULL,
    product_group_id INTEGER REFERENCES product_groups(id) ON DELETE SET NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    tracking_method TEXT NOT NULL DEFAULT 'bulk',
    branch_id INTEGER REFERENCES branches(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_branch_stock (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    quantity INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE(product_id, branch_id)
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
    discount_mode TEXT NOT NULL DEFAULT '',
    discount_value REAL NOT NULL DEFAULT 0,
    coupon_code TEXT NOT NULL DEFAULT '',
    tax_total REAL NOT NULL DEFAULT 0,
    deposit_total REAL NOT NULL DEFAULT 0,
    deposit_option TEXT NOT NULL DEFAULT 'security_deposit',
    damage_waiver_amount REAL NOT NULL DEFAULT 0,
    extra_hours REAL NOT NULL DEFAULT 0,
    deposit_applied_amount REAL NOT NULL DEFAULT 0,
    deposit_refund_amount REAL NOT NULL DEFAULT 0,
    deposit_process_method TEXT NOT NULL DEFAULT '',
    deposit_processed_at TEXT NOT NULL DEFAULT '',
    deposit_note TEXT NOT NULL DEFAULT '',
    no_damages INTEGER NOT NULL DEFAULT 0,
    no_revision_required INTEGER NOT NULL DEFAULT 0,
    return_revised_at TEXT NOT NULL DEFAULT '',
    total REAL NOT NULL DEFAULT 0,
    due_total REAL NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    picked_up_at TEXT,
    new_order_notified_at TEXT,
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
    payment_date TEXT NOT NULL DEFAULT '',
    deleted_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- Day-end cash reconciliation: one row per depot per business day. Purely
-- additive — nothing reads these tables until a user cashes a day up.
CREATE TABLE IF NOT EXISTS cash_ups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    business_day TEXT NOT NULL,
    opening_cash REAL NOT NULL DEFAULT 0,
    counted_cash REAL,
    notes TEXT NOT NULL DEFAULT '',
    interaction_calls INTEGER NOT NULL DEFAULT 0,
    interaction_whatsapp INTEGER NOT NULL DEFAULT 0,
    interaction_emails INTEGER NOT NULL DEFAULT 0,
    interaction_walk_in INTEGER NOT NULL DEFAULT 0,
    interaction_notes TEXT NOT NULL DEFAULT '',
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(branch_id, business_day)
);

-- Cash taken out of the drawer during the day, each line saying what it was for.
CREATE TABLE IF NOT EXISTS cash_used (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cash_up_id INTEGER NOT NULL REFERENCES cash_ups(id) ON DELETE CASCADE,
    amount REAL NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- Cash taken out of the drawer and dropped off at the bank (amount only —
-- the client asked for "just the amount"). It reduces what is expected to be
-- left in the drawer at the end of the day.
CREATE TABLE IF NOT EXISTS cash_bank_drops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cash_up_id INTEGER NOT NULL REFERENCES cash_ups(id) ON DELETE CASCADE,
    amount REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trailer_service_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    service_type TEXT NOT NULL,
    custom_description TEXT NOT NULL DEFAULT '',
    service_date TEXT NOT NULL,
    branch_id INTEGER REFERENCES branches(id) ON DELETE SET NULL,
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
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
    revision_of_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    revision_number INTEGER NOT NULL DEFAULT 0,
    revised_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- Every use (and failed attempt) of the master recovery password, so emergency
-- access to the main profile is always attributable.
CREATE TABLE IF NOT EXISTS recovery_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    ip TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    user_id INTEGER,
    note TEXT NOT NULL DEFAULT ''
);
"""


def now():
    return local_now_iso(timespec="seconds")


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

    db.execute("""CREATE TABLE IF NOT EXISTS product_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
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
    ensure_column(db, "company_settings", "store_title", "TEXT NOT NULL DEFAULT 'Sano Trailers Rentals'")
    ensure_column(db, "company_settings", "store_intro", "TEXT NOT NULL DEFAULT 'Browse our rental catalogue and request a booking online.'")
    ensure_column(db, "company_settings", "store_hero_text", "TEXT NOT NULL DEFAULT 'Select your rental period and we will confirm availability.'")
    ensure_column(db, "company_settings", "checkout_instructions", "TEXT NOT NULL DEFAULT 'Submit your booking request and our team will confirm availability before payment.'")
    ensure_column(db, "company_settings", "store_contact_email", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "company_settings", "store_contact_phone", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "company_settings", "vat_number", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "company_settings", "company_reg_no", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "company_settings", "invoice_email_message", "TEXT NOT NULL DEFAULT 'Dear {customer_name}\n\nPlease find attached {document_label} {document_number} for order {order_number}.\n\nKind regards,\n{company_name}'")
    ensure_column(db, "company_settings", "invoice_email_signature", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "company_settings", "invoice_email_signature_include_logo", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(db, "company_settings", "vat_rate", "REAL NOT NULL DEFAULT 15")
    ensure_column(db, "company_settings", "staff_permissions_json", "TEXT NOT NULL DEFAULT '[\"new_order\",\"dashboard\",\"calendar\",\"orders\",\"customers\"]'")
    ensure_column(db, "branches", "bank_name", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "branches", "bank_account_name", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "branches", "bank_account_number", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "branches", "bank_branch_code", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "branches", "bank_account_type", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "branches", "bank_reference_note", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "users", "branch_id", "INTEGER REFERENCES branches(id) ON DELETE SET NULL")
    ensure_column(db, "users", "can_view_all_branches", "INTEGER NOT NULL DEFAULT 1")
    ensure_column(db, "products", "tracking_method", "TEXT NOT NULL DEFAULT 'bulk'")
    ensure_column(db, "products", "product_group_id", "INTEGER REFERENCES product_groups(id) ON DELETE SET NULL")
    ensure_column(db, "products", "hourly_extra_rate", "REAL NOT NULL DEFAULT 0")
    ensure_column(db, "products", "branch_id", "INTEGER REFERENCES branches(id) ON DELETE SET NULL")
    ensure_column(db, "orders", "booking_type", "TEXT NOT NULL DEFAULT 'return'")
    ensure_column(db, "orders", "collect_branch_id", "INTEGER REFERENCES branches(id) ON DELETE SET NULL")
    ensure_column(db, "orders", "return_branch_id", "INTEGER REFERENCES branches(id) ON DELETE SET NULL")
    ensure_column(db, "orders", "coupon_code", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "orders", "deposit_option", "TEXT NOT NULL DEFAULT 'security_deposit'")
    ensure_column(db, "orders", "damage_waiver_amount", "REAL NOT NULL DEFAULT 0")
    ensure_column(db, "orders", "extra_hours", "REAL NOT NULL DEFAULT 0")
    ensure_column(db, "orders", "deposit_applied_amount", "REAL NOT NULL DEFAULT 0")
    ensure_column(db, "orders", "deposit_refund_amount", "REAL NOT NULL DEFAULT 0")
    ensure_column(db, "orders", "deposit_process_method", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "orders", "deposit_processed_at", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "orders", "deposit_note", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "orders", "no_damages", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(db, "orders", "no_revision_required", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(db, "orders", "return_revised_at", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "orders", "discount_mode", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "orders", "discount_value", "REAL NOT NULL DEFAULT 0")
    ensure_column(db, "orders", "created_by_user_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL")
    # When the order was actually collected. Nullable: orders that predate the
    # column fall back to their scheduled pickup where a "picked up today"
    # figure needs a date (see reports.dashboard_day_metrics).
    ensure_column(db, "orders", "picked_up_at", "TEXT")
    # --- "New order" announced once per order (additive, 2026-09-19) ----------
    # Ticket ABI-341952993 makes "revert a live order to draft" a normal step, so
    # an order can leave draft more than once. Without a marker that would
    # re-announce the same order to the client Telegram group every time it is
    # reserved again. NULL = never announced (an admin draft untouched since
    # ABI-341952988 keeps its single announcement when it finally moves on).
    ensure_column(db, "orders", "new_order_notified_at", "TEXT")
    # One-off backfill: an order that has already left draft has already been
    # announced (admin drafts announce on that move, public bookings announce at
    # creation), so it must never announce again. Drafts stay NULL so the live
    # drafts that are still waiting announce exactly once, as documented.
    db.execute(
        "UPDATE orders SET new_order_notified_at = COALESCE(NULLIF(created_at, ''), ?) "
        "WHERE new_order_notified_at IS NULL AND status <> 'draft'",
        (now(),),
    )
    ensure_column(db, "customers", "address_line1", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "customers", "address_line2", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "customers", "suburb", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "customers", "city", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "customers", "province", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "customers", "postal_code", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "customers", "country", "TEXT NOT NULL DEFAULT 'South Africa'")
    ensure_column(db, "customers", "custom_fields_json", "TEXT NOT NULL DEFAULT '{}'")
    ensure_column(db, "customers", "standard_discount_percent", "REAL NOT NULL DEFAULT 0")
    # Ticket ABI-341953028: a blocked customer cannot be used to create an order.
    # Purely additive with defaults, so every existing customer stays unblocked.
    ensure_column(db, "customers", "is_blocked", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(db, "customers", "blocked_reason", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "customers", "created_by_user_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL")
    # The branch a customer was created at, so a depot's "New customers for the
    # day" figure is the customers added by THAT branch (ticket ABI-341952962).
    # Additive and nullable: imported/legacy rows claim no branch.
    ensure_column(db, "customers", "branch_id", "INTEGER REFERENCES branches(id) ON DELETE SET NULL")
    ensure_column(db, "order_items", "billing_mode", "TEXT NOT NULL DEFAULT 'catalog'")
    ensure_column(db, "payments", "payment_date", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "payments", "deleted_at", "TEXT NOT NULL DEFAULT ''")

    # --- Booqable import source keys (additive, 2026-09-12) -------------------
    # Lets the one-off Booqable importer re-run idempotently and lets orders /
    # line items join back to their source record. Partial unique indexes so the
    # many existing rows with an empty source_id never collide.
    for _table in ("customers", "orders", "products"):
        ensure_column(db, _table, "source_system", "TEXT NOT NULL DEFAULT ''")
        ensure_column(db, _table, "source_id", "TEXT NOT NULL DEFAULT ''")
        db.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{_table}_source "
            f"ON {_table}(source_system, source_id) WHERE source_id <> ''"
        )
    ensure_column(db, "documents", "sent_at", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "documents", "sent_to", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "documents", "email_status", "TEXT NOT NULL DEFAULT 'not_sent'")
    ensure_column(db, "documents", "email_error", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "documents", "revision_of_id", "INTEGER REFERENCES documents(id) ON DELETE SET NULL")
    ensure_column(db, "documents", "revision_number", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(db, "documents", "revised_at", "TEXT NOT NULL DEFAULT ''")
    db.execute("DROP TABLE IF EXISTS email_open_tokens")
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

    # --- Per-branch stock counts (additive, 2026-09-12) ----------------------
    # A tracked product may hold one stock count per branch. When it has rows
    # here it is branch-managed (a booking collected at a branch with no row, or
    # a 0 row, is not available from that branch); with no rows it keeps the
    # legacy single shared pool in products.quantity. products.quantity stays the
    # computed total (sum of the rows) so reports, calendar totals and the
    # order-form picker keep working unchanged.
    db.execute("""CREATE TABLE IF NOT EXISTS product_branch_stock (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
        quantity INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        UNIQUE(product_id, branch_id)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_product_branch_stock_product ON product_branch_stock(product_id)")

    # --- Per-branch trading hours (additive, 2026-09-13) ----------------------
    # Informational only: they are shown on the Branches page and never enforced
    # on availability, the calendar or public bookings. A branch with no rows
    # falls back to the global operating_hours defaults for display, so nothing
    # has to be written for an existing branch to show sensible hours.
    db.execute("""CREATE TABLE IF NOT EXISTS branch_operating_hours (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
        day_of_week INTEGER NOT NULL,
        open_time TEXT NOT NULL DEFAULT '09:00',
        close_time TEXT NOT NULL DEFAULT '17:00',
        closed INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        UNIQUE(branch_id, day_of_week)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_branch_operating_hours_branch ON branch_operating_hours(branch_id)")
    # NULL means "not answered yet", so the thousands of imported clients that
    # carry no verification value are never rendered as "No".
    ensure_column(db, "customers", "client_verified", "INTEGER")

    # --- Per-account modules + multi-branch access (additive, 2026-09-13) -----
    # modules_json NULL keeps the account on the shared default set, so the
    # existing global permission panel keeps working exactly as before; a saved
    # value gives that one account its own (editable after the account exists).
    ensure_column(db, "users", "modules_json", "TEXT")
    # Extra depots per account. No rows = the historic single-branch behaviour
    # (users.branch_id, or all branches when can_view_all_branches is set), so
    # every existing account keeps its current visibility.
    db.execute("""CREATE TABLE IF NOT EXISTS user_branch_access (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
        UNIQUE(user_id, branch_id)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_user_branch_access_user ON user_branch_access(user_id)")

    # --- Day-end cash reconciliation (additive, 2026-09-13) -------------------
    # One cash-up row per depot per business day (``counted_cash`` NULL means the
    # day is not cashed up yet, so its lines can be captured before the count),
    # plus the cash-taken-out lines belonging to it. Nothing else in the app
    # changes: no payment, order or report row is read or written by this feature.
    db.execute("""CREATE TABLE IF NOT EXISTS cash_ups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
        business_day TEXT NOT NULL,
        opening_cash REAL NOT NULL DEFAULT 0,
        counted_cash REAL,
        notes TEXT NOT NULL DEFAULT '',
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(branch_id, business_day)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_cash_ups_branch_day ON cash_ups(branch_id, business_day)")
    ensure_column(db, "cash_ups", "interaction_calls", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(db, "cash_ups", "interaction_whatsapp", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(db, "cash_ups", "interaction_emails", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(db, "cash_ups", "interaction_walk_in", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(db, "cash_ups", "interaction_notes", "TEXT NOT NULL DEFAULT ''")
    db.execute("""CREATE TABLE IF NOT EXISTS cash_used (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cash_up_id INTEGER NOT NULL REFERENCES cash_ups(id) ON DELETE CASCADE,
        amount REAL NOT NULL DEFAULT 0,
        description TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_cash_used_cash_up ON cash_used(cash_up_id)")
    # Cash dropped off at the bank (ABI-341952952). Additive: an existing day
    # simply has no drop lines, so no historical figure changes.
    db.execute("""CREATE TABLE IF NOT EXISTS cash_bank_drops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cash_up_id INTEGER NOT NULL REFERENCES cash_ups(id) ON DELETE CASCADE,
        amount REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_cash_bank_drops_cash_up ON cash_bank_drops(cash_up_id)")

    # --- Trailer service and maintenance history (additive, ABI-341953013) ----
    # Dashboard capture for active rental trailers, with each entry visible on the
    # product itself. Deletes cascade only when a product is permanently removed.
    db.execute("""CREATE TABLE IF NOT EXISTS trailer_service_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        service_type TEXT NOT NULL,
        custom_description TEXT NOT NULL DEFAULT '',
        service_date TEXT NOT NULL,
        branch_id INTEGER REFERENCES branches(id) ON DELETE SET NULL,
        created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_trailer_service_history_product ON trailer_service_history(product_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_trailer_service_history_date ON trailer_service_history(service_date)")


def init_db():
    db = get_db()
    db.executescript(SCHEMA)
    run_migrations(db)
    ts = now()
    db.execute("INSERT OR IGNORE INTO company_settings (id, company_name, email, updated_at) VALUES (1, 'Sano Trailers', 'info@abi-solutions.local', ?)", (ts,))
    db.execute("INSERT OR IGNORE INTO tax_profiles (id, name, rate, is_default, active, created_at) VALUES (1, 'No VAT', 0, 1, 1, ?)", (ts,))
    branch_count = db.execute("SELECT COUNT(*) AS c FROM branches").fetchone()
    if branch_count and branch_count["c"] == 0:
        for branch_name, code in [('Branch 1', 'BR1'), ('Branch 2', 'BR2'), ('Branch 3', 'BR3')]:
            db.execute("INSERT INTO branches (name, code, created_at, updated_at) VALUES (?, ?, ?, ?)", (branch_name, code, ts, ts))
    default_branch = db.execute("SELECT id FROM branches ORDER BY id LIMIT 1").fetchone()
    for day in range(7):
        db.execute("INSERT OR IGNORE INTO operating_hours (day_of_week, open_time, close_time, closed) VALUES (?, '09:00', '17:00', ?)", (day, 1 if day in (0,6) else 0))
    admin_email = current_app.config["ADMIN_EMAIL"]
    owner = db.execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
    if owner is None:
        existing = db.execute("SELECT id FROM users WHERE email = ?", (admin_email,)).fetchone()
        if existing:
            db.execute("UPDATE users SET role = 'owner', active = 1 WHERE id = ?", (existing["id"],))
        else:
            db.execute(
                "INSERT INTO users (email, password_hash, name, initials, role, branch_id, can_view_all_branches, active, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?)",
                (admin_email, generate_password_hash(current_app.config["ADMIN_PASSWORD"]), "Head office admin", "HO", "owner", default_branch['id'] if default_branch else None, ts),
            )
    db.commit()


def init_app(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()
