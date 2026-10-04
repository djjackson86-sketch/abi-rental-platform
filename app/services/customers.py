import json

from app.db import get_db, now
from app.services.access import current_session_user_id, session_active_branch_id, session_primary_branch_id
from app.services.payments import _active_payment_clause, normalise_payment_date_filter
from app.services.settings import get_company_settings

VALID_TYPES = {"individual", "company"}
HIDDEN_CUSTOM_FIELD_KEYS = {
    "id_or_license", "custom_question", "custom_answer_type", "custom_answer",
    # Booqable import audit trail (kept in the DB, never rendered - 2026-09-12)
    "booqable_id", "booqable_number", "booqable_balance_due_cents",
    "booqable_deposit_type", "booqable_deposit_value", "booqable_client_verification",
    "booqable_latest_order_at", "booqable_your_reference", "booqable_tags",
    "booqable_updated_at", "booqable_address_raw",
}
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

# Ticket ABI-341953028: the "Block Customer" checkbox and its reason live on the
# customer form only. _clean() is shared — the new/edit ORDER form's attached
# customer card also posts through it — so the block fields are read ONLY when
# the form actually carries this marker. Without the marker a customer edit that
# knows nothing about blocking leaves the stored block state untouched instead of
# silently unblocking the customer.
BLOCKING_PANEL_MARKER = "blocking_panel"


def list_customers(query="", customer_type="", marketing="", limit=None, offset=0):
    sql = """SELECT c.*, u.name AS created_by_name, u.email AS created_by_email,
        (SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.id) AS order_count
        FROM customers c
        LEFT JOIN users u ON u.id = c.created_by_user_id
        WHERE 1=1"""
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
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset or 0)])
    return get_db().execute(sql, params).fetchall()


def customer_filtered_total(query="", customer_type="", marketing=""):
    sql = "SELECT COUNT(*) AS total FROM customers c WHERE 1=1"
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
    row = get_db().execute(sql, params).fetchone()
    return int(row["total"] if row else 0)


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
    return get_db().execute(
        """SELECT c.*, u.name AS created_by_name, u.email AS created_by_email, b.name AS branch_name
        FROM customers c
        LEFT JOIN users u ON u.id = c.created_by_user_id
        LEFT JOIN branches b ON b.id = c.branch_id
        WHERE c.id = ?""",
        (customer_id,),
    ).fetchone()


def customer_orders(customer_id):
    return get_db().execute("SELECT * FROM orders WHERE customer_id = ? ORDER BY created_at DESC", (customer_id,)).fetchall()


def customer_has_history(customer_id):
    row = get_db().execute(
        "SELECT COUNT(*) AS count FROM orders WHERE customer_id = ?",
        (customer_id,),
    ).fetchone()
    return bool(row and row["count"])


def delete_customer(customer_id):
    if customer_has_history(customer_id):
        raise ValueError("Customer has existing orders/history and cannot be deleted")
    db = get_db()
    cur = db.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
    db.commit()
    return cur.rowcount > 0


def _client_verified_value(form):
    """Yes/No client verification: 1, 0, or None when nobody has answered yet.

    None (NULL) is deliberately kept distinct from 0 so the thousands of
    imported clients with no verification value are never shown as "No".
    """
    raw = form.get("client_verified")
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if value in ("1", "yes", "true", "on"):
        return 1
    if value in ("0", "no", "false", "off"):
        return 0
    return None


def _form_value(form, key):
    try:
        return form.get(key)
    except AttributeError:
        return None


def blocking_panel_present(form):
    """True when the submitted form is the customer form's own blocking panel."""
    return str(_form_value(form, BLOCKING_PANEL_MARKER) or "").strip() == "1"


def _row_flag(customer, key):
    """1/0 for a nullable integer flag on a customer row, tolerant of absence."""
    try:
        value = customer[key]
    except (KeyError, IndexError, TypeError):
        return 0
    if value is None or value == "":
        return 0
    try:
        return 1 if int(value) else 0
    except (TypeError, ValueError):
        return 0


def customer_is_blocked(customer):
    """True when the customer is blocked (ticket ABI-341953028)."""
    return bool(_row_flag(customer, "is_blocked"))


def blocked_reason_for(customer):
    return (_customer_row_value(customer, "blocked_reason", "") or "").strip()


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
    try:
        standard_discount_percent = max(0, min(100, float(form.get("standard_discount_percent") or 0)))
    except ValueError as exc:
        raise ValueError("Standard discount must be a percentage between 0 and 100") from exc
    data = {
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
        "standard_discount_percent": standard_discount_percent,
        "client_verified": _client_verified_value(form),
    }
    if blocking_panel_present(form):
        # Ticket ABI-341953028: block/unblock only ever comes from the customer
        # form's own panel. The typed reason is stored as-is when the customer is
        # unblocked too, so the record keeps the audit trail; enforcement keys off
        # is_blocked alone.
        data["is_blocked"] = 1 if form.get("is_blocked") else 0
        data["blocked_reason"] = (form.get("blocked_reason") or "").strip()
    return data


def customer_branch_id():
    """The branch a customer being created right now belongs to.

    Ticket ABI-341952962: "New customers for the day" for a depot means the
    customers added by THAT branch, so every customer row records where it was
    created — the depot the sign-in chose to manage, otherwise the account's own
    primary branch. An all-branch account (head office) with no branch of its own
    stores NULL: it claims no depot, so no depot's figure is inflated by it.
    """
    return session_active_branch_id() or session_primary_branch_id()


def create_customer(form):
    data = _clean(form)
    db = get_db()
    # Ticket ABI-341953028: a customer created from the customer form can be
    # blocked on the spot. Any other caller (public storefront booking, inline
    # order customer, inline AJAX create) carries no blocking panel, so its new
    # customer starts unblocked.
    cur = db.execute(
        """INSERT INTO customers (customer_type, name, email, phone, marketing_opt_in, address_line1, address_line2, suburb, city, province, postal_code, country, custom_fields_json, balance_due, standard_discount_percent, client_verified, is_blocked, blocked_reason, created_by_user_id, branch_id, created_at)
        VALUES (:customer_type, :name, :email, :phone, :marketing_opt_in, :address_line1, :address_line2, :suburb, :city, :province, :postal_code, :country, :custom_fields_json, 0, :standard_discount_percent, :client_verified, :is_blocked, :blocked_reason, :created_by_user_id, :branch_id, :created_at)""",
        {
            **data,
            "is_blocked": int(data.get("is_blocked") or 0),
            "blocked_reason": data.get("blocked_reason") or "",
            "created_by_user_id": current_session_user_id(),
            "branch_id": customer_branch_id(),
            "created_at": now(),
        },
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
    # Ticket ABI-341953028: the block columns are only written when the customer
    # form's own blocking panel was submitted, so the order form's attached
    # customer card can never clear a block by saving other details.
    sets = """customer_type=:customer_type, name=:name, email=:email, phone=:phone, marketing_opt_in=:marketing_opt_in, address_line1=:address_line1, address_line2=:address_line2, suburb=:suburb, city=:city, province=:province, postal_code=:postal_code, country=:country, custom_fields_json=:custom_fields_json, standard_discount_percent=:standard_discount_percent, client_verified=:client_verified"""
    if blocking_panel_present(form):
        sets += ", is_blocked=:is_blocked, blocked_reason=:blocked_reason"
    get_db().execute(f"UPDATE customers SET {sets} WHERE id=:id", data)
    get_db().commit()



def client_verified_label(value):
    """'Yes' / 'No' / '—' for a stored client_verified value (None = not answered)."""
    if value is None or value == "":
        return "—"
    try:
        return "Yes" if int(value) else "No"
    except (TypeError, ValueError):
        return "—"


def client_verified_form_value(value):
    """'1' / '0' / '' for the Yes/No control ('' = not answered)."""
    if value is None or value == "":
        return ""
    try:
        return "1" if int(value) else "0"
    except (TypeError, ValueError):
        return ""


def _customer_credit_balance_value(customer):
    try:
        return round(float(_customer_row_value(customer, "customer_credit_balance", 0) or 0), 2)
    except (TypeError, ValueError):
        customer_id = _customer_row_value(customer, "id", None)
        if not customer_id:
            return 0.0
        try:
            from app.services.customer_credits import customer_credit_balance
            return customer_credit_balance(customer_id)
        except Exception:
            return 0.0


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
    credit_balance = _customer_credit_balance_value(customer)
    previous_orders_balance = round(float(_customer_row_value(customer, "previous_orders_balance", 0) or 0), 2)
    legacy_balance = round(float(_customer_row_value(customer, "legacy_balance", 0) or 0), 2)
    return {
        "id": customer["id"],
        "customer_type": customer["customer_type"] or "individual",
        "name": customer["name"] or "—",
        "email": email or "—",
        "phone": customer["phone"] or "—",
        "address": ", ".join(str(part).strip() for part in address_parts if part and str(part).strip()) or "—",
        "custom_fields": custom_fields_for(customer),
        "form": customer_form_values_for(customer),
        "standard_discount_percent": float(_customer_row_value(customer, "standard_discount_percent", 0) or 0),
        "previous_orders_balance": previous_orders_balance,
        "customer_credit_balance": credit_balance,
        "previous_orders_balance_display": credit_balance if credit_balance > 0 else previous_orders_balance,
        # Imported opening balance from the previous Sano system plus where staff
        # settle it. The order form shows this beside Previous Orders Balance and
        # links straight to the customer page's payment panel.
        "legacy_balance": legacy_balance,
        "legacy_balance_url": f"/customers/{customer['id']}#legacy-balance",
        "unpaid_orders_total": round(previous_orders_balance + legacy_balance, 2),
        "orders_to_date": int(_customer_row_value(customer, "orders_to_date", 0) or 0),
        "client_verified": _customer_row_value(customer, "client_verified", None),
        "client_verified_label": client_verified_label(_customer_row_value(customer, "client_verified", None)),
        # Ticket ABI-341953028: the order form renders the blocked banner from
        # this summary (the same payload the customer picker's datalist carries),
        # so it needs no second request when a blocked customer is chosen.
        "is_blocked": customer_is_blocked(customer),
        "blocked_reason": blocked_reason_for(customer),
        "display": display,
    }


# --- Customer statement (ticket ABI-341953085) -------------------------------
#
# A read-only statement of account: one row per FINALISED invoice and one row per
# ACTIVE payment (archived/deleted rows never appear), each dated by its own
# transaction date — the invoice's own (finalised) date and the payment date —
# so a caller can ask "what happened between these dates".
#
# The range still reconciles: everything dated before ``date_from`` becomes the
# opening balance, then invoiced in range - paid in range = closing balance. With
# no range at all the closing balance is simply the customer's live outstanding
# position on this document ledger (invoiced - paid).
#
# Nothing here writes: no schema change, no totals recomputed, no document
# touched. Draft invoices are excluded because only finalised invoices are
# invoiced money, and cancelled orders are excluded so a cancelled booking's
# lines never inflate the statement. Archived orders ARE included: archiving
# hides an order from the working lists, it does not un-invoice it, and hiding
# real debt from a statement of account would be the worse lie.
STATEMENT_EXCLUDED_ORDER_STATUSES = ("canceled", "cancelled")
STATEMENT_EMPTY_LABEL = "None in this period"
# The merged ACTIVITY table (ticket ABI-341953086) sorts on these ranks: on the
# same date an invoice comes before a payment, then the row's own id breaks the
# remaining ties.
STATEMENT_ACTIVITY_INVOICE_RANK = 0
STATEMENT_ACTIVITY_PAYMENT_RANK = 1


def _statement_day(value):
    """The YYYY-MM-DD the transaction belongs to (dates compare as text)."""
    return str(value or "").strip()[:10]


def _statement_money(value):
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _statement_money_display(value):
    return f"R{_statement_money(value):.2f}"


def _statement_setting(settings, key, default=""):
    if not settings:
        return default
    try:
        value = settings[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _statement_branch(customer):
    """The customer's own branch row, when the record claims one."""
    try:
        branch_id = customer["branch_id"]
    except (KeyError, IndexError, TypeError):
        return None
    if not branch_id:
        return None
    return get_db().execute("SELECT * FROM branches WHERE id = ?", (branch_id,)).fetchone()


def customer_statement(customer_id, date_from="", date_to="", generated_at=None):
    """The data behind the customer statement PDF (or ``None`` if unknown).

    ``activity`` is the invoices and payments merged into one date-sorted table —
    that is what the statement prints (ticket ABI-341953086); ``invoices`` and
    ``payments`` stay as-is because the summary totals are built from them.

    ``date_from``/``date_to`` are normalised with the same helper the payments
    screen uses, so junk is ignored rather than raising, and the two bounds are
    inclusive. A reversed range is a typo, not a request for an empty statement,
    so the bounds are swapped.
    """
    customer = get_customer(customer_id)
    if not customer:
        return None
    date_from = normalise_payment_date_filter(date_from)
    date_to = normalise_payment_date_filter(date_to)
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from

    db = get_db()
    invoice_rows = db.execute(
        """SELECT d.id AS document_id, d.number AS number, d.created_at AS invoice_at,
                  o.order_number AS order_number, o.total AS total
           FROM documents d
           JOIN orders o ON o.id = d.order_id
           WHERE o.customer_id = ?
             AND d.document_type = 'invoice'
             AND d.status = 'finalized'
             AND LOWER(COALESCE(o.status, '')) NOT IN (?, ?)
           ORDER BY d.created_at, d.id""",
        (customer_id, *STATEMENT_EXCLUDED_ORDER_STATUSES),
    ).fetchall()
    payment_rows = db.execute(
        f"""SELECT p.id AS payment_id, p.amount AS amount, p.method AS method,
                   p.reference AS reference,
                   COALESCE(NULLIF(p.payment_date, ''), p.created_at) AS paid_at,
                   o.order_number AS order_number
            FROM payments p
            JOIN orders o ON o.id = p.order_id
            WHERE o.customer_id = ? AND {_active_payment_clause('p')}
            ORDER BY paid_at, p.id""",
        (customer_id,),
    ).fetchall()

    invoices = []
    payments = []
    activity = []
    opening_invoiced = 0.0
    opening_paid = 0.0
    invoiced_total = 0.0
    paid_total = 0.0
    lifetime_invoiced = 0.0
    lifetime_paid = 0.0

    for row in invoice_rows:
        amount = _statement_money(row["total"])
        lifetime_invoiced += amount
        day = _statement_day(row["invoice_at"])
        if date_from and day < date_from:
            opening_invoiced += amount
            continue
        if date_to and day > date_to:
            continue
        invoiced_total += amount
        number = (row["number"] or "").strip() or "Unnumbered"
        order_number = row["order_number"] or "—"
        amount_display = _statement_money_display(amount)
        invoices.append({
            "date": day,
            "number": number,
            "order_number": order_number,
            "amount": amount,
            "amount_display": amount_display,
        })
        activity.append({
            "date": day,
            "type": "Invoice",
            "detail": f"{number} ({order_number})",
            "amount": amount,
            "amount_display": amount_display,
            "type_rank": STATEMENT_ACTIVITY_INVOICE_RANK,
            "source_id": row["document_id"],
        })

    for row in payment_rows:
        amount = _statement_money(row["amount"])
        lifetime_paid += amount
        day = _statement_day(row["paid_at"])
        if date_from and day < date_from:
            opening_paid += amount
            continue
        if date_to and day > date_to:
            continue
        paid_total += amount
        method = (row["method"] or "manual").replace("_", " ").title()
        reference = (row["reference"] or "").strip()
        amount_display = _statement_money_display(amount)
        payments.append({
            "date": day,
            "method": method,
            "reference": reference,
            "order_number": row["order_number"] or "—",
            "amount": amount,
            "amount_display": amount_display,
        })
        activity.append({
            "date": day,
            "type": "Payment",
            "detail": f"{method} · {reference}" if reference else method,
            "amount": amount,
            "amount_display": amount_display,
            "type_rank": STATEMENT_ACTIVITY_PAYMENT_RANK,
            "source_id": row["payment_id"],
        })

    # Ticket ABI-341953086: the statement prints ONE chronological activity table
    # instead of separate INVOICES / PAYMENTS tables. The two source lists above
    # stay (the summary block's totals are built from them). Rows sort by date,
    # then invoices before payments on the same date, then the row's own id —
    # a deterministic tie-break so a page break landing mid-date can never
    # shuffle the order.
    activity.sort(key=lambda item: (item["date"], item["type_rank"], item["source_id"]))

    opening_balance = round(opening_invoiced - opening_paid, 2)
    invoiced_total = round(invoiced_total, 2)
    paid_total = round(paid_total, 2)
    closing_balance = round(opening_balance + invoiced_total - paid_total, 2)

    settings = get_company_settings()
    branch = _statement_branch(customer)
    issuer_name = _statement_setting(branch, "name", "") or _statement_setting(settings, "company_name", "")
    issuer_phone = _statement_setting(branch, "phone", "") or _statement_setting(settings, "phone", "")
    issuer_email = _statement_setting(branch, "email", "") or _statement_setting(settings, "email", "")
    issuer_vat_number = str(_statement_setting(settings, "vat_number", "") or "").strip()
    issuer_company_reg_no = str(_statement_setting(settings, "company_reg_no", "") or "").strip()
    issuer_address = [
        _statement_setting(branch, "address_line1", "") or _statement_setting(settings, "address_line1", ""),
        _statement_setting(branch, "address_line2", "") or _statement_setting(settings, "address_line2", ""),
        _statement_setting(branch, "city", "") or _statement_setting(settings, "city", ""),
        " ".join(part for part in [
            _statement_setting(branch, "province", "") or _statement_setting(settings, "province", ""),
            _statement_setting(branch, "postal_code", "") or _statement_setting(settings, "postcode", ""),
        ] if part),
    ]

    custom_fields = custom_fields_for(customer)
    customer_lines = [customer["name"] or "-"]
    if customer["email"]:
        customer_lines.append(customer["email"])
    if customer["phone"]:
        customer_lines.append(customer["phone"])
    if (customer["customer_type"] or "") == "company":
        vat_number = str(custom_fields.get("vat_number") or "").strip()
        company_reg_no = str(custom_fields.get("company_reg_no") or "").strip()
        if vat_number:
            customer_lines.append(f"VAT No: {vat_number}")
        if company_reg_no:
            customer_lines.append(f"Company Reg No: {company_reg_no}")
    customer_lines.extend(line for line in [
        customer["address_line1"],
        customer["address_line2"],
        customer["suburb"],
        customer["city"],
        " ".join(part for part in [customer["province"], customer["postal_code"]] if part),
        customer["country"],
    ] if line)

    if date_from and date_to:
        period_label = f"{date_from} to {date_to}"
    elif date_from:
        period_label = f"From {date_from}"
    elif date_to:
        period_label = f"Up to {date_to}"
    else:
        period_label = "Full history"

    return {
        "customer_id": customer["id"],
        "customer_name": customer["name"] or "-",
        "customer_lines": customer_lines,
        "statement_number": f"CUSTOMER-STATEMENT-{customer['id']}",
        "issuer_name": issuer_name,
        "issuer_phone": issuer_phone,
        "issuer_email": issuer_email,
        "issuer_vat_number": issuer_vat_number,
        "issuer_company_reg_no": issuer_company_reg_no,
        "issuer_address": issuer_address,
        "date_from": date_from,
        "date_to": date_to,
        "period_label": period_label,
        "generated_at": generated_at or now(),
        "invoices": invoices,
        "payments": payments,
        "activity": activity,
        "invoice_count": len(invoices),
        "payment_count": len(payments),
        "activity_count": len(activity),
        "opening_balance": opening_balance,
        "invoiced_total": invoiced_total,
        "paid_total": paid_total,
        "closing_balance": closing_balance,
        "outstanding_balance": round(lifetime_invoiced - lifetime_paid, 2),
        "opening_balance_display": _statement_money_display(opening_balance),
        "invoiced_total_display": _statement_money_display(invoiced_total),
        "paid_total_display": _statement_money_display(paid_total),
        "closing_balance_display": _statement_money_display(closing_balance),
        "outstanding_balance_display": _statement_money_display(lifetime_invoiced - lifetime_paid),
    }


def _customer_row_value(customer, key, default=None):
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
        "standard_discount_percent": _customer_row_value(customer, "standard_discount_percent", 0) or 0,
        "client_verified": client_verified_form_value(_customer_row_value(customer, "client_verified", None)),
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
