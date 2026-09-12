from app.db import get_db, now


def get_company_settings():
    return get_db().execute("SELECT * FROM company_settings WHERE id = 1").fetchone()


def _row_has_key(row, key):
    if not row:
        return False
    if hasattr(row, 'keys'):
        return key in row.keys()
    if hasattr(row, '_fields'):
        return key in row._fields
    if hasattr(row, 'asdict'):
        return key in row.asdict()
    return False


def _row_value(row, key, default=''):
    return row[key] if _row_has_key(row, key) else default


def update_company_settings(form):
    fields = [
        "company_name", "email", "phone", "website", "country", "address_line1", "address_line2",
        "city", "province", "postcode", "additional_detail1", "additional_detail2", "units",
        "currency", "currency_symbol", "currency_position",
        "tax_mode", "default_pickup_time", "default_return_time", "time_increment_minutes", "deposit_mode", "deposit_value",
        "invoice_email_message", "invoice_email_signature"
    ]
    current = get_company_settings()
    values = {f: form.get(f, _row_value(current, f, "")) for f in fields}
    values["timezone"] = "Africa/Johannesburg"
    values["date_format"] = "dd-mm-yyyy"
    values["first_day_of_week"] = _row_value(current, "first_day_of_week", "Sunday") or "Sunday"
    values["use_ampm"] = 1 if form.get("use_ampm") else 0
    values["pricing_enabled"] = 1 if form.get("pricing_enabled") else 0
    values["enable_time_selection"] = 1 if form.get("enable_time_selection") else 0
    values["enable_operating_hours"] = 1 if form.get("enable_operating_hours") else 0
    values["invoice_email_signature_include_logo"] = 1 if form.get("invoice_email_signature_include_logo") else 0
    values["time_increment_minutes"] = int(values["time_increment_minutes"] or 60)
    values["deposit_value"] = float(values["deposit_value"] or 0)
    values["updated_at"] = now()
    set_clause = ", ".join([f"{k} = :{k}" for k in values])
    get_db().execute(f"UPDATE company_settings SET {set_clause} WHERE id = 1", values)
    get_db().commit()


DEFAULT_VAT_RATE = 15.0


def global_vat_rate():
    """The single VAT rate for the whole app."""
    try:
        return float(_row_value(get_company_settings(), "vat_rate", DEFAULT_VAT_RATE))
    except (TypeError, ValueError):
        return DEFAULT_VAT_RATE


def global_tax_profile_id():
    """The one tax profile every product uses, so VAT is never a per-product choice."""
    db = get_db()
    row = db.execute("SELECT id FROM tax_profiles WHERE is_default = 1 ORDER BY id LIMIT 1").fetchone()
    if row:
        return row["id"]
    row = db.execute("SELECT id FROM tax_profiles ORDER BY id LIMIT 1").fetchone()
    if row:
        db.execute("UPDATE tax_profiles SET is_default = 1 WHERE id = ?", (row["id"],))
        db.commit()
        return row["id"]
    cur = db.execute(
        "INSERT INTO tax_profiles (name, rate, is_default, active, created_at) VALUES (?, ?, 1, 1, ?)",
        ("VAT", global_vat_rate(), now()))
    db.commit()
    return cur.lastrowid


def update_vat_settings(form):
    """Save the global VAT rate and whether the prices already include it."""
    db = get_db()
    raw = form.get("vat_rate")
    if raw is None or str(raw).strip() == "":
        rate = global_vat_rate()          # no rate posted: keep the current one
    else:
        try:
            rate = max(0.0, float(raw))
        except (TypeError, ValueError):
            rate = global_vat_rate()
    mode = "inclusive" if form.get("prices_include_vat") else "exclusive"
    db.execute("UPDATE company_settings SET vat_rate = ?, tax_mode = ?, updated_at = ? WHERE id = 1",
               (rate, mode, now()))
    profile_id = global_tax_profile_id()
    db.execute("UPDATE tax_profiles SET rate = ?, name = ?, active = 1 WHERE id = ?",
               (rate, f"VAT {rate:g}%", profile_id))
    db.commit()


def list_tax_profiles():
    return get_db().execute("SELECT * FROM tax_profiles ORDER BY is_default DESC, name").fetchall()


def create_tax_profile(name, rate, is_default=False):
    db = get_db()
    if is_default:
        db.execute("UPDATE tax_profiles SET is_default = 0")
    db.execute("INSERT INTO tax_profiles (name, rate, is_default, active, created_at) VALUES (?, ?, ?, 1, ?)", (name, float(rate or 0), 1 if is_default else 0, now()))
    db.commit()


def list_operating_hours():
    return get_db().execute("SELECT * FROM operating_hours ORDER BY day_of_week").fetchall()


def update_online_store_settings(form):
    values = {
        "store_title": form.get("store_title", "").strip() or "Sano Trailers Rentals",
        "store_intro": form.get("store_intro", "").strip(),
        "store_hero_text": form.get("store_hero_text", "").strip(),
        "checkout_instructions": form.get("checkout_instructions", "").strip(),
        "store_contact_email": form.get("store_contact_email", "").strip().lower(),
        "store_contact_phone": form.get("store_contact_phone", "").strip(),
        "store_enabled": 1 if form.get("store_enabled") else 0,
        "show_prices": 1 if form.get("show_prices") else 0,
        "show_availability": 1 if form.get("show_availability") else 0,
        "updated_at": now(),
    }
    set_clause = ", ".join([f"{key} = :{key}" for key in values])
    get_db().execute(f"UPDATE company_settings SET {set_clause} WHERE id = 1", values)
    get_db().commit()
