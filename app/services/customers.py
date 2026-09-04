import json

from app.db import get_db, now

VALID_TYPES = {"individual", "company"}
HIDDEN_CUSTOM_FIELD_KEYS = {"id_or_license", "custom_question", "custom_answer_type", "custom_answer"}
VISIBLE_CUSTOM_FIELD_LABELS = {
    "vehicle_make": "Vehicle Make",
    "vehicle_color": "Vehicle Color",
    "vehicle_reg_no": "Veh Reg No",
    "alternative_contact_name": "Alternative Contact Name",
    "alternative_contact_number": "Alternative Contact Number",
    "alternative_contact_relationship": "Alternative Contact Relationship",
    "vat_number": "VAT No",
    "company_reg_no": "Company Reg No",
}
VISIBLE_CUSTOM_FIELD_ORDER = [
    "vehicle_make",
    "vehicle_color",
    "vehicle_reg_no",
    "alternative_contact_name",
    "alternative_contact_number",
    "alternative_contact_relationship",
    "vat_number",
    "company_reg_no",
]
CUSTOM_FIELD_FORM_KEYS = list(VISIBLE_CUSTOM_FIELD_ORDER)


def list_customers(query="", customer_type="", marketing=""):
    sql = "SELECT c.*, (SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.id) AS order_count FROM customers c WHERE 1=1"
    params = []
    if query:
        sql += " AND (LOWER(c.name) LIKE ? OR LOWER(c.email) LIKE ? OR LOWER(c.phone) LIKE ?)"
        needle = f"%{query.lower()}%"
        params.extend([needle, needle, needle])
    if customer_type in VALID_TYPES:
        sql += " AND c.customer_type = ?"
        params.append(customer_type)
    if marketing == "subscribed":
        sql += " AND c.marketing_opt_in = 1"
    elif marketing == "not_subscribed":
        sql += " AND c.marketing_opt_in = 0"
    sql += " ORDER BY c.created_at DESC, c.name"
    return get_db().execute(sql, params).fetchall()


def customer_counts():
    row = get_db().execute(
        "SELECT COUNT(*) total, SUM(customer_type='individual') individuals, SUM(customer_type='company') companies, SUM(marketing_opt_in) subscribed FROM customers"
    ).fetchone()
    return {
        "total": row["total"] or 0,
        "individuals": row["individuals"] or 0,
        "companies": row["companies"] or 0,
        "subscribed": row["subscribed"] or 0,
        "not_subscribed": (row["total"] or 0) - (row["subscribed"] or 0),
    }


def customer_filter_counts():
    counts = customer_counts()
    return {
        "customer_type": {"individual": counts["individuals"], "company": counts["companies"]},
        "marketing": {"subscribed": counts["subscribed"], "not_subscribed": counts["not_subscribed"]},
    }


def get_customer(customer_id):
    return get_db().execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()


def customer_orders(customer_id):
    return get_db().execute("SELECT * FROM orders WHERE customer_id = ? ORDER BY created_at DESC", (customer_id,)).fetchall()


def _clean(form, existing_custom_fields=None):
    name = form.get("name", "").strip()
    if not name:
        raise ValueError("Customer name is required")
    customer_type = form.get("customer_type", "individual")
    if customer_type not in VALID_TYPES:
        customer_type = "individual"
    custom_fields = dict(existing_custom_fields or {})
    submitted_custom_fields = {
        "vehicle_make": form.get("vehicle_make", "").strip() or form.get("vehicle_details", "").strip(),
        "vehicle_color": form.get("vehicle_color", "").strip(),
        "vehicle_reg_no": form.get("vehicle_reg_no", "").strip(),
        "alternative_contact_name": form.get("alternative_contact_name", "").strip() or form.get("alternative_contact", "").strip(),
        "alternative_contact_number": form.get("alternative_contact_number", "").strip(),
        "alternative_contact_relationship": form.get("alternative_contact_relationship", "").strip(),
        "vat_number": form.get("vat_number", "").strip(),
        "company_reg_no": form.get("company_reg_no", "").strip(),
    }
    for key, value in submitted_custom_fields.items():
        if value:
            custom_fields[key] = value
        else:
            custom_fields.pop(key, None)
    custom_fields.pop("vehicle_details", None)
    custom_fields.pop("alternative_contact", None)
    return {
        "customer_type": customer_type,
        "name": name,
        "email": form.get("email", "").strip().lower(),
        "phone": form.get("phone", "").strip(),
        "marketing_opt_in": 1 if form.get("marketing_opt_in") else 0,
        "address_line1": form.get("address_line1", "").strip(),
        "address_line2": form.get("address_line2", "").strip(),
        "suburb": form.get("suburb", "").strip(),
        "city": form.get("city", "").strip(),
        "province": form.get("province", "").strip(),
        "postal_code": form.get("postal_code", "").strip(),
        "country": form.get("country", "South Africa").strip() or "South Africa",
        "custom_fields_json": json.dumps(custom_fields, ensure_ascii=False),
    }


def create_customer(form):
    data = _clean(form)
    db = get_db()
    cur = db.execute(
        """INSERT INTO customers (customer_type, name, email, phone, marketing_opt_in, address_line1, address_line2, suburb, city, province, postal_code, country, custom_fields_json, balance_due, created_at)
        VALUES (:customer_type, :name, :email, :phone, :marketing_opt_in, :address_line1, :address_line2, :suburb, :city, :province, :postal_code, :country, :custom_fields_json, 0, :created_at)""",
        {**data, "created_at": now()},
    )
    db.commit()
    customer_id = cur.lastrowid
    try:
        from app.services.telegram import send_new_customer_notification
        send_new_customer_notification(customer_id)
    except Exception:
        pass
    return customer_id


def update_customer(customer_id, form):
    data = _clean(form, existing_custom_fields=raw_custom_fields_for(get_customer(customer_id)))
    data["id"] = customer_id
    get_db().execute(
        """UPDATE customers SET customer_type=:customer_type, name=:name, email=:email, phone=:phone, marketing_opt_in=:marketing_opt_in, address_line1=:address_line1, address_line2=:address_line2, suburb=:suburb, city=:city, province=:province, postal_code=:postal_code, country=:country, custom_fields_json=:custom_fields_json WHERE id=:id""",
        data,
    )
    get_db().commit()



def customer_summary_for(customer):
    if not customer:
        return None
    address_parts = [
        customer["address_line1"],
        customer["address_line2"],
        customer["suburb"],
        customer["city"],
        customer["province"],
        customer["postal_code"],
        customer["country"],
    ]
    email = customer["email"] or ""
    display = customer["name"] or ""
    if email:
        display += f" — {email}"
    return {
        "id": customer["id"],
        "customer_type": customer["customer_type"] or "individual",
        "name": customer["name"] or "—",
        "email": email or "—",
        "phone": customer["phone"] or "—",
        "address": ", ".join(str(part).strip() for part in address_parts if part and str(part).strip()) or "—",
        "custom_fields": custom_fields_for(customer),
        "form": customer_form_values_for(customer),
        "display": display,
    }


def _customer_row_value(customer, key, default=""):
    try:
        value = customer[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def customer_form_values_for(customer):
    """Raw editable customer values keyed like the customer form inputs.

    Used to prefill the editable attached-customer card on the new-order form
    and to simulate an untouched customer form for change detection.
    """
    raw = raw_custom_fields_for(customer)
    if raw.get("vehicle_details") and not raw.get("vehicle_make"):
        raw["vehicle_make"] = raw["vehicle_details"]
    if raw.get("alternative_contact") and not raw.get("alternative_contact_name"):
        raw["alternative_contact_name"] = raw["alternative_contact"]
    values = {
        "customer_type": _customer_row_value(customer, "customer_type") or "individual",
        "name": _customer_row_value(customer, "name") or "",
        "email": _customer_row_value(customer, "email") or "",
        "phone": _customer_row_value(customer, "phone") or "",
        "marketing_opt_in": 1 if _customer_row_value(customer, "marketing_opt_in") else 0,
        "address_line1": _customer_row_value(customer, "address_line1") or "",
        "address_line2": _customer_row_value(customer, "address_line2") or "",
        "suburb": _customer_row_value(customer, "suburb") or "",
        "city": _customer_row_value(customer, "city") or "",
        "province": _customer_row_value(customer, "province") or "",
        "postal_code": _customer_row_value(customer, "postal_code") or "",
        "country": _customer_row_value(customer, "country") or "South Africa",
    }
    for key in CUSTOM_FIELD_FORM_KEYS:
        values[key] = raw.get(key) or ""
    return values


def customer_fields_changed(customer, form):
    """True when the order-form editable customer fields differ from the stored record.

    Raises ValueError for the same validation failures update_customer would
    (e.g. a blanked-out customer name), so callers can surface the message
    before creating the draft.
    """
    existing = raw_custom_fields_for(customer)
    untouched = _customer_form_from_record(customer)
    unchanged = _clean(untouched, existing_custom_fields=existing)
    submitted = _clean(form, existing_custom_fields=existing)
    return submitted != unchanged


def _customer_form_from_record(customer):
    values = customer_form_values_for(customer)
    values["marketing_opt_in"] = "1" if values["marketing_opt_in"] else ""
    return values


def custom_fields_for(customer):
    raw = raw_custom_fields_for(customer)
    visible = {key: value for key, value in raw.items() if key not in HIDDEN_CUSTOM_FIELD_KEYS}
    if visible.get("vehicle_details") and not visible.get("vehicle_make"):
        visible["vehicle_make"] = visible["vehicle_details"]
    if visible.get("alternative_contact") and not visible.get("alternative_contact_name"):
        visible["alternative_contact_name"] = visible["alternative_contact"]
    visible.pop("vehicle_details", None)
    visible.pop("alternative_contact", None)
    ordered = {}
    for key in VISIBLE_CUSTOM_FIELD_ORDER:
        value = visible.get(key)
        if value:
            ordered[key] = value
    for key, value in visible.items():
        if key not in ordered and value:
            ordered[key] = value
    return ordered


def custom_field_label(key):
    return VISIBLE_CUSTOM_FIELD_LABELS.get(key, key.replace("_", " ").title())


def raw_custom_fields_for(customer):
    if not customer:
        return {}
    try:
        data = json.loads(customer["custom_fields_json"] or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}
