from app.db import get_db, now


def get_company_settings():
    return get_db().execute("SELECT * FROM company_settings WHERE id = 1").fetchone()


def update_company_settings(form):
    fields = [
        "company_name", "email", "phone", "website", "country", "address_line1", "address_line2",
        "city", "province", "postcode", "additional_detail1", "additional_detail2", "timezone",
        "first_day_of_week", "date_format", "units", "currency", "currency_symbol", "currency_position",
        "tax_mode", "default_pickup_time", "default_return_time", "time_increment_minutes", "deposit_mode", "deposit_value"
    ]
    values = {f: form.get(f, "") for f in fields}
    values["use_ampm"] = 1 if form.get("use_ampm") else 0
    values["pricing_enabled"] = 1 if form.get("pricing_enabled") else 0
    values["enable_time_selection"] = 1 if form.get("enable_time_selection") else 0
    values["enable_operating_hours"] = 1 if form.get("enable_operating_hours") else 0
    values["time_increment_minutes"] = int(values["time_increment_minutes"] or 60)
    values["deposit_value"] = float(values["deposit_value"] or 0)
    values["updated_at"] = now()
    set_clause = ", ".join([f"{k} = :{k}" for k in values])
    get_db().execute(f"UPDATE company_settings SET {set_clause} WHERE id = 1", values)
    get_db().commit()


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
        "store_title": form.get("store_title", "").strip() or "ABI Solutions Rentals",
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
