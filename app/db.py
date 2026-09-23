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
    -- The absolute base for every public link the app hands out (feature B §B1, decision D5).
    -- Blank means "use the host of the request that is asking", which is right on localhost and
    -- wrong the moment a QR code is printed, so staff set it once per environment.
    public_base_url TEXT NOT NULL DEFAULT '',
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
    -- Per-branch public portal (programme phase 8 / feature B §B1, decision D5). The slug is
    -- STORED, not derived at request time: a QR code printed on an A4 sheet has to keep working
    -- after the branch is renamed, so ensure_slug() only ever fills a blank one. Existing rows
    -- are backfilled from the branch name once, by run_migrations() below.
    public_slug TEXT NOT NULL DEFAULT '',
    portal_enabled INTEGER NOT NULL DEFAULT 1,
    portal_intro TEXT NOT NULL DEFAULT '',
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

-- Vehicles recorded against a client (programme phase 2 / feature A, 2026-09-23).
-- Mostly scanned from a NaTIS licence disc (see app/services/vehicle_disk.py), and the
-- columns follow decision D3 exactly: ``registration`` is the NUMBER PLATE (e.g. NB72XMGP),
-- ``registration_number`` is the NaTIS registration number and ``licence_number`` is the
-- disc's own licence number. ``tare_kg`` / ``gvm_kg`` are nullable REAL because the real
-- modern disc payload carries no masses at all — a missing mass stays NULL, never 0 — and
-- there is deliberately no towing-capacity column (decision D3b).
CREATE TABLE IF NOT EXISTS vehicles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    registration TEXT NOT NULL DEFAULT '',
    make TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    year TEXT NOT NULL DEFAULT '',
    vin TEXT NOT NULL DEFAULT '',
    engine_number TEXT NOT NULL DEFAULT '',
    colour TEXT NOT NULL DEFAULT '',
    licence_number TEXT NOT NULL DEFAULT '',
    registration_number TEXT NOT NULL DEFAULT '',
    control_number TEXT NOT NULL DEFAULT '',
    registering_authority TEXT NOT NULL DEFAULT '',
    vehicle_type TEXT NOT NULL DEFAULT '',
    tare_kg REAL,
    gvm_kg REAL,
    licence_disk_expiry TEXT NOT NULL DEFAULT '',
    raw_scan_text TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vehicles_customer ON vehicles(customer_id);
-- One owner per recorded registration; a blank registration (a vehicle typed in without a
-- plate) is allowed as often as staff need it, hence the partial index.
CREATE UNIQUE INDEX IF NOT EXISTS idx_vehicles_registration ON vehicles(registration) WHERE registration <> '';

-- POPIA consent records (programme phase 7 / feature P, §P1). Decision D10: every
-- client-facing capture point (the branch portal and the public booking form) shows a
-- required, unticked-by-default acceptance, and the acceptance is *recorded* so Sano can
-- evidence it — the reference app keeps it in component state only, which evidences
-- nothing. The columns are deliberately the minimum: who, which notice version, which
-- channel, when. There is **no IP address, no user agent and no device fingerprint** —
-- POPIA data minimisation, and a test asserts this column set so it cannot creep back.
CREATE TABLE IF NOT EXISTS consent_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    consent_type TEXT NOT NULL DEFAULT 'popia_privacy',
    notice_version TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT '',
    accepted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_consent_records_customer ON consent_records(customer_id);

CREATE TABLE IF NOT EXISTS product_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    -- Category photo (programme phase 11 / feature C §C1, decision D4). The image bytes live in
    -- the database, never on disk (Render's filesystem is ephemeral). ``image_source`` records
    -- where the bytes came from: '' = no image, 'default:sano' = a shipped Sano default (set by
    -- ``scripts/seed_default_group_images.py``), 'upload' = a photo staff chose. ``becomes_store_visible``
    -- is the on/off switch for the whole category section on the public store, so a category can be
    -- hidden without deleting its photo or its products.
    image_blob BLOB,
    image_mime TEXT NOT NULL DEFAULT '',
    image_filename TEXT NOT NULL DEFAULT '',
    image_source TEXT NOT NULL DEFAULT '',
    becomes_store_visible INTEGER NOT NULL DEFAULT 1,
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
    wheel_size TEXT NOT NULL DEFAULT '',
    -- Trailer identity (programme phase 5 / feature D). What a scanned NaTIS
    -- licence disc is matched against to find the open rental it belongs to:
    -- registration = number plate, registration_number = NaTIS number,
    -- licence_number = the disc's own licence number. Blank means "not recorded",
    -- so every existing product is unaffected and only ever gains a plate when
    -- staff type one in on the inventory form.
    registration TEXT NOT NULL DEFAULT '',
    licence_number TEXT NOT NULL DEFAULT '',
    registration_number TEXT NOT NULL DEFAULT '',
    -- Ticket ABI-341953038(3): a trailer flagged here cannot be rented and is
    -- hidden from the online store until it is released.
    under_maintenance INTEGER NOT NULL DEFAULT 0,
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
    -- Ticket ABI-341953038(1): the status an archived order held immediately
    -- before it was archived, so the main profile can unarchive it back to where
    -- it was. Blank on rows archived before this column existed.
    status_before_archive TEXT NOT NULL DEFAULT '',
    -- Return-by-disc-scan audit (programme phase 5 / feature D). Written only by
    -- ``returns.mark_returned_via_scan``: when a staff member scans a licence
    -- disc and the matching rental is marked returned, these record the moment,
    -- the scanned identifier and whether the disc was the trailer's or the towing
    -- car's. Blank on every order returned the normal way.
    return_scan_at TEXT NOT NULL DEFAULT '',
    return_scan_registration TEXT NOT NULL DEFAULT '',
    return_scan_source TEXT NOT NULL DEFAULT '',
    return_scan_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
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

-- Spare wheel counts typed in on the dashboard, one row per depot per business
-- day per wheel size (ticket ABI-341953033). Additive only: a day with no rows
-- simply has nothing counted yet.
CREATE TABLE IF NOT EXISTS spare_wheel_counts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    business_day TEXT NOT NULL,
    wheel_size TEXT NOT NULL,
    actual_count INTEGER NOT NULL DEFAULT 0,
    reported_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(branch_id, business_day, wheel_size)
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


def _backfill_branch_slugs(db):
    """Give every branch a public slug, once, deterministically (programme phase 8 / §B1).

    Only BLANK slugs are filled, so this is a no-op on every later app start — a slug that staff
    have already printed on an A4 sheet is never silently re-derived from a renamed branch.
    Collisions take the first free ``-2``, ``-3`` … suffix in branch-id order, so two machines
    running the same migration on the same data end up with the same links.

    ``app.services.portal`` is imported inside the function on purpose: it reads ``app.db``, so a
    module-level import here would be circular.
    """
    from app.services.portal import slugify

    taken = {
        row["public_slug"]
        for row in db.execute("SELECT public_slug FROM branches WHERE public_slug <> ''").fetchall()
    }
    for row in db.execute("SELECT id, name, public_slug FROM branches ORDER BY id").fetchall():
        if (row["public_slug"] or "").strip():
            continue
        base = slugify(row["name"]) or f"branch-{row['id']}"
        candidate, suffix = base, 2
        while candidate in taken:
            candidate = f"{base}-{suffix}"
            suffix += 1
        db.execute("UPDATE branches SET public_slug = ? WHERE id = ?", (candidate, row["id"]))
        taken.add(candidate)


def run_migrations(db):

    db.execute("""CREATE TABLE IF NOT EXISTS product_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        sort_order INTEGER NOT NULL DEFAULT 0,
        image_blob BLOB,
        image_mime TEXT NOT NULL DEFAULT '',
        image_filename TEXT NOT NULL DEFAULT '',
        image_source TEXT NOT NULL DEFAULT '',
        becomes_store_visible INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    # --- Category photos + store visibility (programme phase 11 / feature C §C1) ---
    # Additive with defaults, so every existing group keeps behaving as it does today: no photo,
    # and visible on the store. The image bytes stay in the database (decision D4).
    ensure_column(db, "product_groups", "image_blob", "BLOB")
    ensure_column(db, "product_groups", "image_mime", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "product_groups", "image_filename", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "product_groups", "image_source", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "product_groups", "becomes_store_visible", "INTEGER NOT NULL DEFAULT 1")
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
    # Which wheel size a rental trailer runs (ticket ABI-341953033). Additive with
    # a blank default, so every existing product simply has no wheel size and is
    # left out of the dashboard spare wheel Expected total.
    ensure_column(db, "products", "wheel_size", "TEXT NOT NULL DEFAULT ''")
    # Ticket ABI-341953038(3): "Trailer under maintenance" — blocks rental and
    # hides the trailer from the online store until it is released. Additive with
    # a default, so every existing product stays available.
    ensure_column(db, "products", "under_maintenance", "INTEGER NOT NULL DEFAULT 0")
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
    # Ticket ABI-341953038(1): the status an order held before it was archived.
    # Additive with a blank default, so an order archived before this column
    # existed simply has no remembered status (unarchive falls back to Returned).
    ensure_column(db, "orders", "status_before_archive", "TEXT NOT NULL DEFAULT ''")
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

    # --- Spare wheel counts (additive, ABI-341953033) -------------------------
    # One count per depot per business day per wheel size, typed in from the
    # dashboard panel. Nothing existing is read or written by this table: a day
    # with no rows simply has nothing counted yet.
    db.execute("""CREATE TABLE IF NOT EXISTS spare_wheel_counts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
        business_day TEXT NOT NULL,
        wheel_size TEXT NOT NULL,
        actual_count INTEGER NOT NULL DEFAULT 0,
        reported_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(branch_id, business_day, wheel_size)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_spare_wheel_counts_day ON spare_wheel_counts(business_day, branch_id)")

    # --- Vehicles recorded against a client (additive, programme phase 2 / feature A) ---
    # The NaTIS licence disc scanner's home. Column meanings are fixed by decision D3
    # (registration = number plate, registration_number = NaTIS number, licence_number =
    # the disc's licence number); tare_kg/gvm_kg are nullable REAL because the real modern
    # payload carries no masses — never store 0 as a stand-in for "not on the disc" — and
    # decision D3b removed towing capacity, so there is no column for it. On an existing
    # database this only adds an empty table: no customer, order or product row is touched.
    db.execute("""CREATE TABLE IF NOT EXISTS vehicles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
        registration TEXT NOT NULL DEFAULT '',
        make TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        year TEXT NOT NULL DEFAULT '',
        vin TEXT NOT NULL DEFAULT '',
        engine_number TEXT NOT NULL DEFAULT '',
        colour TEXT NOT NULL DEFAULT '',
        licence_number TEXT NOT NULL DEFAULT '',
        registration_number TEXT NOT NULL DEFAULT '',
        control_number TEXT NOT NULL DEFAULT '',
        registering_authority TEXT NOT NULL DEFAULT '',
        vehicle_type TEXT NOT NULL DEFAULT '',
        tare_kg REAL,
        gvm_kg REAL,
        licence_disk_expiry TEXT NOT NULL DEFAULT '',
        raw_scan_text TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'manual',
        created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_vehicles_customer ON vehicles(customer_id)")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_vehicles_registration "
        "ON vehicles(registration) WHERE registration <> ''"
    )

    # --- Trailer identity on a rental product (additive, programme phase 5 / feature D) ---
    # Feature D returns a rental by scanning a licence disc, so a trailer has to
    # carry the same three identifiers a disc does: registration = number plate,
    # registration_number = NaTIS number, licence_number = the disc's own licence
    # number (decision D3). Blank defaults, so on an existing database every
    # product simply has no identity recorded and nothing else changes — the plate
    # is only ever typed in on the inventory form. The partial unique index makes
    # "one plate belongs to one trailer" a database-level fact (an empty plate may
    # repeat, so products without a plate are never blocked), which is what keeps
    # a scan unambiguous.
    ensure_column(db, "products", "registration", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "products", "licence_number", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "products", "registration_number", "TEXT NOT NULL DEFAULT ''")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_products_registration "
        "ON products(registration) WHERE registration <> ''"
    )

    # --- Return-by-disc-scan audit on an order (additive, programme phase 5 / feature D) ---
    # Written only by returns.mark_returned_via_scan(); blank on every order that
    # was returned the normal way, so no existing row or report is affected.
    ensure_column(db, "orders", "return_scan_at", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "orders", "return_scan_registration", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "orders", "return_scan_source", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "orders", "return_scan_user_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL")

    # --- POPIA consent records (additive, programme phase 7 / feature P §P1) ---
    # A new, empty table on an existing database: every client simply has no consent
    # recorded yet, which is what the admin customer page reports ("No POPIA consent
    # recorded"). Nothing existing changes. The column set is deliberately minimal —
    # who / which notice version / which channel / when, and no IP or user agent
    # (see the note on the same table in SCHEMA).
    db.execute(
        """CREATE TABLE IF NOT EXISTS consent_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
            consent_type TEXT NOT NULL DEFAULT 'popia_privacy',
            notice_version TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL DEFAULT '',
            accepted_at TEXT NOT NULL
        )"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_consent_records_customer ON consent_records(customer_id)"
    )

    # --- Per-branch public portal (additive, programme phase 8 / feature B §B1) ---
    # Every branch gets a stable public link of the shape /portal/<slug> and a QR that encodes the
    # absolute URL (decision D5). All four columns are additive with defaults, so on an existing
    # database every branch keeps behaving exactly as it does today; the backfill fills only the
    # blank slugs, once. The partial unique index is the database-level half of "one slug, one
    # branch": an empty slug may repeat (so a branch that has never been given a link never blocks
    # anything), which is the same shape as idx_vehicles_registration / idx_products_registration.
    ensure_column(db, "branches", "public_slug", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "branches", "portal_enabled", "INTEGER NOT NULL DEFAULT 1")
    ensure_column(db, "branches", "portal_intro", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "company_settings", "public_base_url", "TEXT NOT NULL DEFAULT ''")
    _backfill_branch_slugs(db)
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_branches_slug "
        "ON branches(public_slug) WHERE public_slug <> ''"
    )


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
        # These three starter branches are created *after* run_migrations(), so the migration's
        # slug backfill never sees them: fill them here as well, so a brand-new install has working
        # portal links from the first start (feature B §B1). Idempotent — it only fills blank slugs.
        _backfill_branch_slugs(db)
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
