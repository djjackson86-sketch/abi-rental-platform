import difflib
import json
import math
import re
import unicodedata

from flask import g, has_request_context, session

from app.db import get_db, now
from app.services.access import current_session_user_id, is_main_session, session_active_branch_id, session_primary_branch_id
from app.services.legacy_balances import legacy_balance_writeoffs
from app.services.payments import _active_payment_clause, normalise_payment_date_filter
from app.services.portal_registrations import pending_exclusion_clause
from app.services.settings import get_company_settings

VALID_TYPES = {"individual", "company"}
HIDDEN_CUSTOM_FIELD_KEYS = {
    "id_or_license", "custom_question", "custom_answer_type", "custom_answer",
    # Booqable import audit trail (kept in the DB, never rendered - 2026-09-12)
    "booqable_id", "booqable_number", "booqable_balance_due_cents",
    "booqable_deposit_type", "booqable_deposit_value", "booqable_client_verification",
    "booqable_latest_order_at", "booqable_your_reference", "booqable_tags",
    "booqable_updated_at", "booqable_address_raw",
    # The internal pointer to the client's chosen main vehicle (feature 2026-10-10): it ties the
    # mirrored vehicle fields to a vehicle row and is never shown or edited as a custom field.
    "main_vehicle_id",
    # Ticket ABI-341953268: the fraud-suspect classification and the supplementary ID number
    # staff capture with it. Both live in custom_fields_json precisely so they stay hidden from
    # the custom-field cards, form summaries and public/CSV exports; the service reads them via
    # raw_custom_fields_for / customer_is_suspect only.
    "suspect_fraud", "customer_id_number", "suspect_reason", "lifecycle_history",
}
#: The custom-field keys the client's main vehicle is shown under. Deliberately the eight fields
#: staff capture on the scan screen — number plate, disc licence number, NaTIS registration number,
#: VIN, engine number, make, disk expiry and colour. Colour joined the original seven on request: the
#: licence disc decodes it, so staff see and correct it rather than leaving it to a hand-typed legacy
#: field. Other decoded disk data (model, category, registering authority, control number) lives on
#: the vehicle row only. ``app.services.vehicles`` owns the column↔key mapping; a test pins these
#: keys to ``vehicles.MAIN_VEHICLE_CUSTOM_KEYS``.
VEHICLE_CUSTOM_FIELD_KEYS = (
    "vehicle_reg_no",
    "vehicle_licence_number",
    "vehicle_registration_number",
    "vehicle_vin",
    "vehicle_engine_number",
    "vehicle_make",
    "vehicle_licence_disk_expiry",
    "vehicle_color",
)
VISIBLE_CUSTOM_FIELD_LABELS = {
    "other_email_addresses": "Other email addresses",
    "vehicle_make": "Vehicle Make",
    "vehicle_reg_no": "Veh Reg No",
    "vehicle_registration_number": "NaTIS registration number",
    "vehicle_licence_number": "Disk licence number",
    "vehicle_vin": "VIN",
    "vehicle_engine_number": "Engine number",
    "vehicle_licence_disk_expiry": "Licence disk expiry",
    "vehicle_color": "Vehicle Color",
    "alternative_contact_name": "Alternative Contact Name",
    "alternative_contact_number": "Alternative Contact Number",
    "alternative_contact_relationship": "Alternative Contact Relationship",
    "vat_number": "VAT No",
    "company_reg_no": "Company Reg No",
}
VISIBLE_CUSTOM_FIELD_ORDER = [
    "vehicle_make",
    "vehicle_reg_no",
    "vehicle_registration_number",
    "vehicle_licence_number",
    "vehicle_vin",
    "vehicle_engine_number",
    "vehicle_licence_disk_expiry",
    "vehicle_color",
    "alternative_contact_name",
    "alternative_contact_number",
    "alternative_contact_relationship",
    "vat_number",
    "company_reg_no",
]
CUSTOM_FIELD_FORM_KEYS = list(VISIBLE_CUSTOM_FIELD_ORDER) + ["other_email_addresses"]

# Ticket ABI-341953028: the "Block Customer" checkbox and its reason live on the
# customer form only. _clean() is shared — the new/edit ORDER form's attached
# customer card also posts through it — so the block fields are read ONLY when
# the form actually carries this marker. Without the marker a customer edit that
# knows nothing about blocking leaves the stored block state untouched instead of
# silently unblocking the customer.
BLOCKING_PANEL_MARKER = "blocking_panel"

# Ticket ABI-341953129: the credit facility (allow buying on account + its
# limit) is owner-only. Like the blocking panel above, the credit inputs live on
# the customer form only and are read ONLY when the form carries this marker, so
# the shared _clean() — also used by the order form's inline customer card and
# by the public storefront — can never change a stored facility.
CREDIT_PANEL_MARKER = "credit_panel"
CREDIT_FIELDS = ("credit_allowed", "credit_limit")
CREDIT_LIMIT_ERROR = "Credit limit must be a positive amount"


def _directory_visibility_clause(folder="active"):
    """Include disapproved portal clients in the directory, never in booking pickers.

    Disapproval sets is_blocked but leaves the registration pending. Deleted
    registrations remain hidden, even if their retained customer row is blocked.
    """
    retained = "(c.lifecycle_status IN ('deleted','archived') OR EXISTS (SELECT 1 FROM portal_registrations pr WHERE pr.customer_id=c.id AND pr.status='deleted'))"
    if folder == "retained":
        return " AND " + retained
    return " AND NOT " + retained + (
        " AND ((1=1" + pending_exclusion_clause("c") + ") OR (c.is_blocked = 1"
        " AND NOT EXISTS (SELECT 1 FROM portal_registrations pr"
        " WHERE pr.customer_id = c.id AND pr.status = 'deleted')))"
    )


def _legacy_outstanding_sql(alias="c"):
    """Outstanding on a client's *still-open* imported balances, not the original import.

    Ticket ABI-341953282: the directory's Legacy balance column and its sort/filter
    must agree with ``legacy_balances.legacy_balance_summary`` — only ``status =
    'active'`` rows count, each row contributes ``amount_due - settled_amount`` clamped
    at zero, so a row settled to nothing (or closed by hand / waived) contributes
    nothing. The imported ``amount_due`` is never summed on its own.
    """
    return f"""COALESCE((SELECT ROUND(SUM(MAX(ROUND(COALESCE(lb.amount_due, 0) - COALESCE(lb.settled_amount, 0), 2), 0)), 2)
            FROM legacy_customer_balances lb
            WHERE lb.customer_id = {alias}.id AND lb.status = 'active'), 0)"""


def _outstanding_balance_sql(alias="c"):
    """Live order debt for one customer, clamped per order — the list's Balance due.

    Same collectible-order rules as ``customer_outstanding_balances`` (rental stages
    and accepted quotes count, finalised invoices count even on draft/archived orders,
    cancelled orders never do, overpayments cannot hide another order's debt), phrased
    as one correlated expression so the displayed figure and the sort key are the same
    value instead of two definitions that can drift apart.
    """
    return f"""COALESCE((SELECT ROUND(SUM(MAX(ROUND(COALESCE(o.total, 0) - COALESCE(p.paid, 0), 2), 0)), 2)
            FROM orders o
            LEFT JOIN (
                SELECT p.order_id, SUM(p.amount) AS paid FROM payments p
                WHERE {_active_payment_clause('p')} AND LOWER(COALESCE(p.method, '')) <> 'account' GROUP BY p.order_id
            ) p ON p.order_id = o.id
            WHERE o.customer_id = {alias}.id
              AND COALESCE(o.status, '') NOT IN ('canceled', 'cancelled')
              AND (
                  EXISTS (SELECT 1 FROM documents d WHERE d.order_id = o.id
                          AND d.document_type = 'invoice' AND d.status = 'finalized')
                  OR (COALESCE(o.status, '') NOT IN ('draft', 'archived') AND (
                      o.status IN ('reserved', 'started', 'returned')
                      OR EXISTS (SELECT 1 FROM documents d WHERE d.order_id = o.id
                                 AND d.document_type = 'quote' AND d.status IN ('accepted', 'finalized'))
                  ))
              )), 0)"""


#: The columns the Customers directory may be sorted by, keyed by the ``?sort=``
#: value. The request value only *selects* one of these fixed expressions; it is
#: never interpolated into SQL, so a crafted ``sort``/``dir`` cannot reach the
#: query. The chosen column is every table header the page offers.
CUSTOMER_SORTS = {
    "id": "c.id",
    "name": "LOWER(COALESCE(c.name, ''))",
    "email": "LOWER(COALESCE(c.email, ''))",
    "client_verified": "COALESCE(c.client_verified, 0)",
    "created_at": "COALESCE(c.created_at, '')",
    "created_by": "LOWER(COALESCE(CASE WHEN c.source_system = 'portal' AND c.created_by_user_id IS NULL"
                  " THEN 'Public' ELSE u.name END, ''))",
    "orders": "COALESCE(oc.order_count, 0)",
    "balance_due": _outstanding_balance_sql("c"),
    "legacy_balance": _legacy_outstanding_sql("c"),
}


def normalise_customer_sort(sort, direction):
    """Only an allowlisted column and direction ever reach the ORDER BY clause."""
    sort = str(sort or "").strip().lower()
    sort = sort if sort in CUSTOMER_SORTS else "created_at"
    direction = "asc" if str(direction or "").strip().lower() == "asc" else "desc"
    return sort, direction


def _customer_order_clause(sort="created_at", direction="desc"):
    """The directory's whole-result order: chosen column, then a stable tie-break.

    The tie-break (name, then id) keeps page boundaries deterministic when the
    chosen column holds equal values — without it a LIMIT/OFFSET page could repeat
    or skip rows.
    """
    sort = sort if sort in CUSTOMER_SORTS else "created_at"
    direction = "asc" if direction == "asc" else "desc"
    clauses = [f"{CUSTOMER_SORTS[sort]} {direction.upper()}"]
    if sort != "created_at":
        clauses.append("COALESCE(c.created_at, '') DESC")
    clauses.extend(["LOWER(COALESCE(c.name, '')) ASC", "c.id ASC"])
    return ", ".join(clauses)


def _customer_filter_where(query="", customer_type="", marketing="", blocked="", legacy="", folder="active"):
    """One filter definition shared by the rows and the count, so they cannot disagree.

    ``legacy`` is the "with a legacy balance" filter: ``'1'`` keeps only clients whose
    still-open imported balances total more than zero, ``'without'`` keeps the rest.
    """
    sql = " WHERE 1=1" + _directory_visibility_clause(folder)
    params = []
    legacy = str(legacy or "").strip().lower()
    if blocked == "1":
        sql += " AND c.is_blocked = 1"
    if legacy in ("1", "with"):
        sql += f" AND {_legacy_outstanding_sql('c')} > 0"
    elif legacy in ("without", "none"):
        sql += f" AND {_legacy_outstanding_sql('c')} <= 0"
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
    return sql, params


def list_customers(query="", customer_type="", marketing="", limit=None, offset=0, blocked="", folder="active",
                   legacy="", sort="created_at", direction="desc"):
    # Ticket ABI-341953196: ``order_count`` is a grouped LEFT JOIN, not a
    # correlated subquery per returned row. The Customers list is paged, but the
    # CSV export reads the whole book, where the per-row count billed one orders
    # scan per client on the remote libSQL database.
    #
    # Ticket ABI-341953282: the ORDER BY is part of this query (before LIMIT/OFFSET),
    # so a column header orders the *whole* filtered directory rather than the page
    # already loaded. Outstanding and legacy balances are selected here for the same
    # reason: the rendered figure is the value the sort compared.
    sort, direction = normalise_customer_sort(sort, direction)
    where, params = _customer_filter_where(
        query=query, customer_type=customer_type, marketing=marketing,
        blocked=blocked, legacy=legacy, folder=folder,
    )
    sql = f"""SELECT c.*, CASE WHEN c.source_system = 'portal' AND c.created_by_user_id IS NULL
            THEN 'Public' ELSE u.name END AS created_by_name, u.email AS created_by_email,
        COALESCE(oc.order_count, 0) AS order_count,
        {_outstanding_balance_sql('c')} AS outstanding_balance,
        {_legacy_outstanding_sql('c')} AS legacy_balance
        FROM customers c
        LEFT JOIN users u ON u.id = c.created_by_user_id
        LEFT JOIN (SELECT customer_id, COUNT(*) AS order_count FROM orders GROUP BY customer_id) oc
            ON oc.customer_id = c.id
        {where}
        ORDER BY {_customer_order_clause(sort, direction)}"""
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset or 0)])
    rows = get_db().execute(sql, params).fetchall()
    from app.services.reports import row_dict
    return [
        dict(
            row_dict(row),
            outstanding_balance=round(float(row["outstanding_balance"] or 0), 2),
            legacy_balance=round(float(row["legacy_balance"] or 0), 2),
        )
        for row in rows
    ]


def customer_outstanding_balances(customer_ids):
    """Read-time collectible order debt, separate from legacy and unused credit.

    Rental stages/accepted quotes are collectible. Finalised invoices remain
    debt even on draft or archived orders; cancelled orders never contribute.
    Use payment_summary's total-minus-active-payments basis, not cached totals.
    Refunds, applied deposits and allocated credit are payments counted once.
    Clamp per order so overpayments cannot hide another order's debt. Aggregate
    lists in batches rather than making a remote request for every customer.
    The expression is shared with the directory's Balance due column and sort.
    """
    ids = list(dict.fromkeys(customer_ids))
    balances = {customer_id: 0.0 for customer_id in ids}
    if not ids:
        return balances
    expr = _outstanding_balance_sql("c")
    for start in range(0, len(ids), 400):
        batch = ids[start:start + 400]
        marks = ",".join("?" for _ in batch)
        rows = get_db().execute(
            f"SELECT c.id, {expr} AS balance FROM customers c WHERE c.id IN ({marks})",
            batch,
        ).fetchall()
        for row in rows:
            balances[row["id"]] = round(float(row["balance"] or 0), 2)
    return balances


def customer_filtered_total(query="", customer_type="", marketing="", blocked="", folder="active", legacy=""):
    where, params = _customer_filter_where(
        query=query, customer_type=customer_type, marketing=marketing,
        blocked=blocked, legacy=legacy, folder=folder,
    )
    row = get_db().execute("SELECT COUNT(*) AS total FROM customers c" + where, params).fetchone()
    return int(row["total"] if row else 0)


def search_customers(query, limit=8, *, allow_pending=False):
    """Typeahead for the scan screen's "allocate to client" step: id, name and phone only.

    Deliberately narrow: the picker has to identify a client, so it gets the two things a
    staff member says out loud on the phone — never email, address, ID, balance or order
    history. A blank query returns nothing rather than the whole book.
    """
    needle = (query or "").strip()
    if not needle:
        return []
    like = f"%{needle.lower()}%"
    try:
        limit = max(1, min(25, int(limit)))
    except (TypeError, ValueError):
        limit = 8
    rows = get_db().execute(
        """SELECT id, name, phone FROM customers c
        WHERE 1=1""" + pending_exclusion_clause("c", allow_pending=allow_pending) + """
        AND (LOWER(name) LIKE ? OR LOWER(phone) LIKE ?)
        ORDER BY CASE WHEN LOWER(name) LIKE ? THEN 0 ELSE 1 END, LOWER(name), id
        LIMIT ?""",
        (like, like, f"{needle.lower()}%", limit),
    ).fetchall()
    return [{"id": row["id"], "name": row["name"] or "", "phone": row["phone"] or ""} for row in rows]


def _phone_digits(value):
    """Digits only, with the SA trunk/country prefix folded away, so ``+27 82…`` == ``082…``."""
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("27") and len(digits) > 10:
        digits = "0" + digits[2:]
    return digits


def find_customer_id_by_text(text, *, allow_pending=False):
    """Resolve the scan screen's "Allocate to client" text to a customer id, or ``None``.

    Staff type a name, a cellphone number, or pick the typeahead's own label
    (``Name · 082 123 4567``). Only an **unambiguous** match is accepted; two clients with
    the same number, or a name that matches more than one record, return ``None`` so the
    caller keeps its refusal rather than allocating a vehicle to the wrong person.

    The typeahead label is resolved *first* and by exact string: the label the suggestion
    list rendered is ``name · <the client's own stored phone>``, so two clients who share a
    name and the same digits stored with different SA phone prefixes show two
    different labels. The label the staff member picked therefore identifies exactly one of
    them, where the digit-normalised comparison below cannot tell them apart and refuses. A
    label that is genuinely not unique still yields ``None``, so the ambiguity guard is
    unchanged.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    label_name, _, label_phone = raw.partition("·")
    label_name = label_name.strip()
    label_phone = label_phone.strip()
    if not label_phone and re.fullmatch(r"[\d\s()+\-]{9,}", raw):
        label_phone, label_name = raw, ""
    rows = get_db().execute(
        "SELECT id, name, phone FROM customers c WHERE 1=1" + pending_exclusion_clause("c", allow_pending=allow_pending)
    ).fetchall()

    if label_name and label_phone:
        want_name = " ".join(label_name.lower().split())
        want_phone = " ".join(label_phone.lower().split())
        label_hits = [
            row["id"]
            for row in rows
            if " ".join(str(row["name"] or "").lower().split()) == want_name
            and " ".join(str(row["phone"] or "").lower().split()) == want_phone
        ]
        if label_hits:
            return label_hits[0] if len(label_hits) == 1 else None

    phone_hits = []
    if label_phone:
        wanted = _phone_digits(label_phone)[-9:]
        if len(wanted) == 9:
            phone_hits = [row["id"] for row in rows if _phone_digits(row["phone"])[-9:] == wanted]
    name_hits = []
    if label_name:
        key = " ".join(label_name.lower().split())
        name_hits = [row["id"] for row in rows if " ".join(str(row["name"] or "").lower().split()) == key]

    if len(phone_hits) == 1:
        return phone_hits[0]
    if len(phone_hits) > 1:
        narrowed = [customer_id for customer_id in name_hits if customer_id in phone_hits]
        return narrowed[0] if len(narrowed) == 1 else None
    if len(name_hits) == 1:
        return name_hits[0]
    return None


def customer_counts(folder="active"):
    row = get_db().execute(
        "SELECT COUNT(*) total, SUM(customer_type='individual') individuals, SUM(customer_type='company') companies, SUM(marketing_opt_in) subscribed FROM customers c WHERE 1=1"
        + _directory_visibility_clause(folder)
    ).fetchone()
    return {
        "total": row["total"] or 0,
        "individuals": row["individuals"] or 0,
        "companies": row["companies"] or 0,
        "subscribed": row["subscribed"] or 0,
        "not_subscribed": (row["total"] or 0) - (row["subscribed"] or 0),
    }


def customer_filter_counts(folder="active"):
    counts = customer_counts(folder)
    return {
        "customer_type": {"individual": counts["individuals"], "company": counts["companies"]},
        "marketing": {"subscribed": counts["subscribed"], "not_subscribed": counts["not_subscribed"]},
    }


def get_customer(customer_id):
    return get_db().execute(
        """SELECT c.*, CASE WHEN c.source_system = 'portal' AND c.created_by_user_id IS NULL
            THEN 'Public' ELSE u.name END AS created_by_name, u.email AS created_by_email, b.name AS branch_name
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
    """Retain a deleted client and all children; existing order history still blocks deletion."""
    if customer_has_history(customer_id):
        raise ValueError("Customer has existing orders/history and cannot be deleted")
    return retain_customer(customer_id, "deleted")


def retain_customer(customer_id, status="archived", reason=""):
    """Retain identity and every financial/vehicle link; never overwrite prior audit."""
    if status not in ("archived", "deleted"):
        raise ValueError("Invalid customer lifecycle status")
    db = get_db()
    cur = db.execute(
        "UPDATE customers SET lifecycle_status=?, lifecycle_changed_at=?, lifecycle_reason=?, "
        "lifecycle_changed_by_user_id=?, lifecycle_source='staff' WHERE id=? AND lifecycle_status='active'",
        (status, now(), str(reason or "").strip()[:2000], current_session_user_id(), customer_id),
    )
    db.commit()
    _clear_suspect_cache()
    return cur.rowcount > 0


def customer_is_deleted(customer):
    """Include legacy portal tombstones, but never archived customers."""
    if customer is None or customer['lifecycle_status'] == 'archived':
        return False
    return customer['lifecycle_status'] == 'deleted' or bool(get_db().execute(
        "SELECT 1 FROM portal_registrations WHERE customer_id=? AND status='deleted' "
        "AND NOT EXISTS (SELECT 1 FROM portal_registrations WHERE customer_id=? AND status IN ('pending','approved'))",
        (customer['id'], customer['id']),
    ).fetchone())


def _lock_deleted_customer(customer_id):
    # Acquire the write lock before reading eligibility, on SQLite and Turso alike.
    db = get_db()
    db.execute("UPDATE customers SET lifecycle_status=lifecycle_status WHERE id=?", (customer_id,))
    customer = get_customer(customer_id)
    if customer is None or not customer_is_deleted(customer):
        raise ValueError("Only a deleted customer can use this action.")
    from app.services.portal_registrations import branch_in_session_scope
    if not branch_in_session_scope(customer):
        raise ValueError("This customer is outside the branches you can access.")
    return customer


def restore_customer(customer_id):
    """Restore identity in place; never grant verification or remove rental blocks."""
    from app.db import write_transaction
    from app.services import portal_registrations as registrations
    with write_transaction():
        customer = _lock_deleted_customer(customer_id)
        db = get_db()
        rows = db.execute("SELECT * FROM portal_registrations WHERE customer_id=? ORDER BY id", (customer_id,)).fetchall()
        # A portal sign-up needs saved approval evidence, not merely an ID tick.
        was_approved = any(r['status'] == 'approved' or r['approved_at'] for r in rows)
        active = registrations.id_verified(customer) and (
            was_approved or (not rows and customer['source_system'] != 'portal_pending')
        )
        status = 'approved' if active else 'pending'
        if rows:
            # Collapse legacy multiple pending/tombstone states without losing evidence.
            db.execute("UPDATE portal_registrations SET status='superseded' WHERE customer_id=?", (customer_id,))
            chosen = next((r for r in rows if r['status'] == 'approved' or r['approved_at']), rows[-1]) if active else rows[-1]
            db.execute("UPDATE portal_registrations SET status=? WHERE id=?", (status, chosen['id']))

        elif not active:
            registrations.record_pending_registration(customer_id, customer['branch_id'],
                f'RESTORED-{customer_id}', customer['name'], customer['email'], customer['phone'], commit=False)
        fields = raw_custom_fields_for(customer)
        history = fields.get('lifecycle_history')
        if not isinstance(history, list):
            history = []
        restored_at = now()
        restored_by = current_session_user_id()
        history.append({key: customer[key] for key in (
            'lifecycle_status', 'lifecycle_changed_at', 'lifecycle_changed_by_user_id',
            'lifecycle_reason', 'lifecycle_source')})
        history.append({'action': 'restore', 'at': restored_at, 'by_user_id': restored_by, 'destination': status})
        fields['lifecycle_history'] = history
        db.execute("UPDATE customers SET lifecycle_status='active', lifecycle_changed_at=?, "
                   "lifecycle_changed_by_user_id=?, lifecycle_reason='Restored from deleted', lifecycle_source='staff', "
                   "custom_fields_json=?, source_system=CASE WHEN ? AND source_system='portal_pending' THEN 'portal' ELSE source_system END WHERE id=?",
                   (restored_at, restored_by, json.dumps(fields), int(active), customer_id))
    _clear_suspect_cache()
    return 'Active' if active else 'Awaiting Verification'


def permanently_delete_customer(customer_id):
    """Erase only an eligible deleted identity, never cascade business/audit history."""
    from app.db import write_transaction
    with write_transaction():
        _lock_deleted_customer(customer_id)
        db = get_db()
        for table in ('orders', 'customer_credits', 'legacy_customer_balances',
                      'legacy_balance_payments', 'prepaid_fundings', 'consent_records'):
            if db.execute(f'SELECT 1 FROM {table} WHERE customer_id=? LIMIT 1', (customer_id,)).fetchone():
                raise ValueError("Permanent deletion blocked: customer has business or protected audit history. Restore or retain the record instead.")
        # Explicit children also work when a remote connection has FK enforcement disabled.
        db.execute("DELETE FROM portal_submission_claims WHERE customer_id=? OR registration_id IN "
                   "(SELECT id FROM portal_registrations WHERE customer_id=?)", (customer_id, customer_id))
        for table in ('portal_registrations', 'vehicles'):
            db.execute(f'DELETE FROM {table} WHERE customer_id=?', (customer_id,))
        db.execute("DELETE FROM customers WHERE id=?", (customer_id,))
    _clear_suspect_cache()
    return True


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


def can_set_customer_credit():
    """Ticket ABI-341953129: only the main profile may grant/change credit.

    Reuses the existing main-profile check (role 'owner'), the same boundary the
    Settings user management uses. Outside a request (background jobs, service
    unit tests) there is no signed-in main user, so it is False — the safe
    default, which is what keeps a forged field from ever being honoured.
    """
    if not has_request_context():
        return False
    return is_main_session(session)


def credit_panel_present(form):
    """True when the customer form's own owner-only credit panel was submitted."""
    return str(_form_value(form, CREDIT_PANEL_MARKER) or "").strip() == "1"


def credit_fields_submitted(form):
    """True when any credit input (or its panel marker) rode along on the POST.

    Lets the customer route refuse a crafted staff POST outright, while the
    service still ignores stray fields on every other path (order form card,
    public storefront) because they are keyed off the panel marker.
    """
    if credit_panel_present(form):
        return True
    return any(_form_value(form, key) not in (None, "") for key in CREDIT_FIELDS)


def _clean_credit(form):
    """(credit_allowed, credit_limit) for an owner-submitted credit panel.

    Disabled stores 0/0. Enabled requires a positive, finite monetary limit:
    zero, negative, NaN and infinity are all refused (a limit of R0 would let
    the customer onto account with no ceiling at all).
    """
    raw = str(form.get("credit_allowed") or "").strip().lower()
    allowed = 1 if raw in ("1", "true", "on", "yes") else 0
    if not allowed:
        return 0, 0.0
    try:
        limit = float(str(form.get("credit_limit") or "").strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(CREDIT_LIMIT_ERROR) from exc
    if not math.isfinite(limit) or round(limit, 2) <= 0:
        raise ValueError(CREDIT_LIMIT_ERROR)
    return 1, round(limit, 2)


# --- Suspected-fraud classification (ticket ABI-341953268) -------------------
#
# A client can be classified by staff as a suspected fraud. The classification is
# expressed as the normal ``is_blocked`` block (so every existing block guard —
# ordering, directory badge, order form banner — applies unchanged), and it is
# created in the SAME INSERT as the customer: a suspect is never briefly orderable.
# The suspect marker and the optional supplementary ID number ride in the already
# hidden ``custom_fields_json`` blob, so no customer-table migration is needed and
# neither value can reach a custom-field card, form summary or public export
# (both keys are in HIDDEN_CUSTOM_FIELD_KEYS above).
#
# Like the block panel (ABI-341953028) and the credit panel (ABI-341953129), the
# suspect inputs are read ONLY from the customer form's own blocking panel, and
# only when the form was submitted by its ``save_suspect`` action by a signed-in
# staff session. A public storefront post, an inline order-form customer card or
# any other caller can therefore never forge either field.
SUSPECT_FLAG_KEY = "suspect_fraud"
CUSTOMER_ID_NUMBER_KEY = "customer_id_number"
SUSPECT_REASON_KEY = "suspect_reason"
SUSPECT_REASON_MAX_LENGTH = 2000
SUSPECT_FORM_ACTION = "save_suspect"
SUSPECT_DEFAULT_BLOCK_REASON = "Suspected fraud"
#: Stable match reasons returned by ``suspect_matches`` — never the matched value.
SUSPECT_MATCH_REASONS = ("email", "phone", "id_number", "name")
#: The form action the suspect panel posts. The field is named ``action`` in the
#: customer form; ``form_action`` is accepted too so a caller's template can use
#: either name without silently losing the protection.
SUSPECT_ACTION_FIELDS = ("action", "form_action")


def _truthy(value):
    """1/True/'1'/'true'/'yes'/'on' are true; everything else (including None) is false."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def staff_request_active():
    """True inside a signed-in staff request; False for public/background work.

    The same boundary the rest of the service uses (``current_session_user_id``):
    a request with no session — the public storefront, the client portal, a
    background job or a unit test outside a request — is never staff.
    """
    return current_session_user_id() is not None


def suspect_panel_present(form):
    """True when the customer form's own suspect panel was submitted by staff.

    All three conditions are required, so a forged field on any other path is
    ignored rather than honoured: the blocking panel marker, the suspect form's
    own ``save_suspect`` action, and a signed-in staff session.
    """
    if not blocking_panel_present(form):
        return False
    action = ""
    for key in SUSPECT_ACTION_FIELDS:
        raw = _form_value(form, key)
        if raw not in (None, ""):
            action = str(raw)
            break
    if action.strip().lower() != SUSPECT_FORM_ACTION:
        return False
    return staff_request_active()


def suspect_fields_submitted(form):
    """True when a suspect flag or ID number rode along on the POST.

    Lets the customer route refuse a crafted POST outright (like
    ``credit_fields_submitted``); the service itself ignores those fields on
    every unauthorised path because they are keyed off the suspect panel.
    """
    if suspect_panel_present(form):
        return True
    return any(_form_value(form, key) not in (None, "") for key in (SUSPECT_FLAG_KEY, CUSTOMER_ID_NUMBER_KEY, SUSPECT_REASON_KEY))


def suspect_flag_submitted(form):
    """True when an authorised suspect panel submission classifies the client."""
    return suspect_panel_present(form) and _truthy(_form_value(form, SUSPECT_FLAG_KEY))


def _clean_suspect(form, custom_fields):
    """Update the hidden suspect metadata in ``custom_fields``; return the flag.

    Only call this for a submission ``suspect_panel_present`` accepted. Unticking
    the box clears the classification; the ID number is never deleted by a blank
    field (an omitted partial post must not wipe evidence staff recorded), and a
    supplied value is stored trimmed.
    """
    flagged = _truthy(_form_value(form, SUSPECT_FLAG_KEY))
    if flagged:
        custom_fields[SUSPECT_FLAG_KEY] = 1
    else:
        custom_fields.pop(SUSPECT_FLAG_KEY, None)
    submitted_id = str(_form_value(form, CUSTOMER_ID_NUMBER_KEY) or "").strip()
    if len(submitted_id) > 64 or any(ord(char) < 32 for char in submitted_id):
        raise ValueError("Customer ID number must be at most 64 characters without control characters")
    if submitted_id:
        custom_fields[CUSTOMER_ID_NUMBER_KEY] = submitted_id
    # Omitted fields preserve existing evidence; an explicit blank clears the optional reason.
    if SUSPECT_REASON_KEY in form:
        reason = str(_form_value(form, SUSPECT_REASON_KEY) or "").replace(chr(13) + chr(10), chr(10)).replace(chr(13), chr(10)).strip()
        if len(reason) > SUSPECT_REASON_MAX_LENGTH:
            raise ValueError("Suspect/Fraud reason must be at most 2000 characters")
        if any(ord(char) < 32 and char not in "\n\t" for char in reason):
            raise ValueError("Suspect/Fraud reason contains unsupported control characters")
        custom_fields[SUSPECT_REASON_KEY] = reason
    return flagged


def customer_suspect_reason_for(customer):
    """Private staff note; never included in custom-field summaries or match payloads."""
    return str(raw_custom_fields_for(customer).get(SUSPECT_REASON_KEY) or "")


def customer_is_suspect(customer):
    """True when the client is classified as a suspected fraud (ticket ABI-341953268)."""
    return _truthy(raw_custom_fields_for(customer).get(SUSPECT_FLAG_KEY))


def customer_id_number_for(customer):
    """The hidden supplementary ID number staff captured, or '' (staff surfaces only)."""
    return str(raw_custom_fields_for(customer).get(CUSTOMER_ID_NUMBER_KEY) or "").strip()


def _mapping_value(mapping, key, default=None):
    """Read ``key`` from a form (MultiDict/dict) or a customer row (sqlite3.Row)."""
    if mapping is None:
        return default
    try:
        value = mapping[key]
    except (KeyError, IndexError, TypeError, AttributeError):
        try:
            value = mapping.get(key, default)
        except AttributeError:
            return default
    return default if value is None else value


def _mapping_custom_fields(mapping):
    raw = _mapping_value(mapping, "custom_fields_json", None)
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}
    nested = _mapping_value(mapping, "custom_fields", None)
    return nested if isinstance(nested, dict) else {}


def _normalised_email(value):
    """Lower-cased email, or '' when it is blank/not an address (never matches on blank)."""
    text = str(value or "").strip().lower()
    return text if "@" in text and len(text) >= 3 else ""


def _normalised_phone(value):
    """The last nine digits of an SA number, so +27 82…, 082… and 0027 82… agree."""
    digits = _phone_digits(value)
    return digits[-9:] if len(digits) >= 9 else ""


def _normalised_id_number(value):
    """Whitespace-free ID number for exact comparison (never fuzzy-matched)."""
    return re.sub(r"\s+", "", str(value or "")).strip().upper()


def _name_tokens(value):
    """Lower-case alphanumeric name tokens with accents folded and punctuation split."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return [token for token in re.split(r"[^0-9a-z]+", text.lower()) if token]


def _names_match(left, right):
    """Conservative full-name comparison: both sides need a first AND last name.

    A blank name or a lone first name never matches (two different "John"s are
    not evidence). Otherwise the tokens are compared order-insensitively — so
    "Smith John" matches "John Smith" — and each token may differ by one typo or
    one character, but never by a different initial. Middle names must agree:
    "John Peter Smith" is not "John Smith".
    """
    left_tokens = sorted(_name_tokens(left))
    right_tokens = sorted(_name_tokens(right))
    if len(left_tokens) < 2 or len(left_tokens) != len(right_tokens):
        return False
    for left_token, right_token in zip(left_tokens, right_tokens):
        if left_token == right_token:
            continue
        if left_token[:1] != right_token[:1]:
            return False
        if abs(len(left_token) - len(right_token)) > 1:
            return False
        if difflib.SequenceMatcher(None, left_token, right_token).ratio() < 0.7:
            return False
    return True


def _suspect_identity(mapping):
    """Normalised identity values from a form or a customer row, for matching."""
    custom_fields = _mapping_custom_fields(mapping)
    id_number = _mapping_value(mapping, CUSTOMER_ID_NUMBER_KEY, None)
    if id_number in (None, ""):
        id_number = custom_fields.get(CUSTOMER_ID_NUMBER_KEY)
    return {
        "name": str(_mapping_value(mapping, "name", "") or "").strip(),
        "email": _normalised_email(_mapping_value(mapping, "email", "")),
        "phone": _normalised_phone(_mapping_value(mapping, "phone", "")),
        "id_number": _normalised_id_number(id_number),
    }


def _customer_identity_rows():
    """Blocked Suspect/Fraud identities, cached for the life of one request.

    ``customer_summary_for`` may classify several rows in one request (the
    order-form customer picker summarises the whole book), so the scans must not
    multiply. Writes clear the cache (``_clear_suspect_cache``).
    """
    if has_request_context():
        cached = getattr(g, "_suspect_identity_rows", None)
        if cached is not None:
            return cached
    rows = get_db().execute("SELECT id, name, email, phone, custom_fields_json FROM customers WHERE is_blocked = 1").fetchall()
    result = [
        {
            "id": row["id"],
            "name": row["name"] or "",
            "email": row["email"] or "",
            "phone": row["phone"] or "",
            "custom_fields_json": row["custom_fields_json"],
            # Ticket ABI-341953269: the ID number staff captured when this client was
            # flagged, carried on the cached row so the similarity popup can show it
            # beside the candidate's number for comparison (no second query).
            "id_number": str(_mapping_custom_fields(row).get(CUSTOMER_ID_NUMBER_KEY) or "").strip(),
        }
        for row in rows if customer_is_suspect(row)
    ]
    if has_request_context():
        g._suspect_identity_rows = result
    return result


def _clear_suspect_cache():
    if not has_request_context():
        return
    try:
        delattr(g, "_suspect_identity_rows")
    except AttributeError:
        pass


def _customers_with_id_number(wanted):
    """{id: row} for customers whose hidden ID number exactly equals ``wanted``.

    Only classified records are decoded and compared exactly; a number that
    merely appears in another custom field never counts as an ID match.
    """
    if not wanted:
        return {}
    rows = _customer_identity_rows()
    hits = {}
    for row in rows:
        try:
            fields = json.loads(row["custom_fields_json"] or "{}")
        except Exception:
            continue
        if not isinstance(fields, dict):
            continue
        if _normalised_id_number(fields.get(CUSTOMER_ID_NUMBER_KEY)) == wanted:
            hits[row["id"]] = {"id": row["id"], "name": row["name"] or "", "id_number": row["id_number"]}
    return hits


def suspect_matches(form_or_customer, exclude_id=None):
    """Other clients whose identity overlaps this one, with the ID they were flagged on.

    Accepts a submitted form (name/email/phone/customer_id_number) or a customer
    row/dict (its hidden custom fields are read for the ID number). Each entry is
    ``{"id", "name", "id_number", "reasons"}`` where ``reasons`` is a subset of
    ``SUSPECT_MATCH_REASONS``: a case-insensitive email, an SA-normalised phone
    (+27/0/0027 agree), an exact supplementary ID number, or a conservative full
    name match (never a blank or a lone first name). ``exclude_id`` skips the
    client being edited. Returns [] when there is nothing to compare.

    Ticket ABI-341953269: ``id_number`` is the number staff captured when *that*
    matched client was flagged (``""`` when none was recorded) so the staff
    similarity popup can put it beside the number being entered for comparison.
    Only the hidden ``customer_id_number`` value is read — never any other custom
    field — and it never leaves a staff surface: the summary that carries these
    matches is served to staff requests only, and the key stays hidden from
    custom-field cards, CSV exports and public pages (``HIDDEN_CUSTOM_FIELD_KEYS``).
    """
    identity = _suspect_identity(form_or_customer)
    try:
        excluded = int(exclude_id) if exclude_id not in (None, "") else None
    except (TypeError, ValueError):
        excluded = None
    matches = {}
    for row in _customer_identity_rows():
        if excluded is not None and row["id"] == excluded:
            continue
        reasons = []
        if identity["email"] and _normalised_email(row["email"]) == identity["email"]:
            reasons.append("email")
        if identity["phone"] and _normalised_phone(row["phone"]) == identity["phone"]:
            reasons.append("phone")
        if identity["name"] and _names_match(identity["name"], row["name"]):
            reasons.append("name")
        if reasons:
            matches[row["id"]] = {
                "id": row["id"],
                "name": row["name"],
                "id_number": row["id_number"],
                "reasons": reasons,
            }
    if identity["id_number"]:
        for customer_id, row in _customers_with_id_number(identity["id_number"]).items():
            if excluded is not None and customer_id == excluded:
                continue
            entry = matches.setdefault(
                customer_id,
                {"id": customer_id, "name": row["name"], "id_number": row["id_number"], "reasons": []},
            )
            if "id_number" not in entry["reasons"]:
                entry["reasons"].append("id_number")
    for entry in matches.values():
        entry["reasons"] = [reason for reason in SUSPECT_MATCH_REASONS if reason in entry["reasons"]]
    return sorted(matches.values(), key=lambda entry: (entry["name"].lower(), entry["id"]))


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


def _form_has(form, key):
    """True when the submitted form actually carries this key (a MultiDict or a dict)."""
    try:
        return key in form
    except TypeError:
        return False


def _clean(form, existing_custom_fields=None):
    name = form.get("name", "").strip()
    if not name:
        raise ValueError("Customer name is required")
    customer_type = form.get("customer_type", "individual")
    if customer_type not in VALID_TYPES:
        customer_type = "individual"
    custom_fields = dict(existing_custom_fields or {})
    if customer_type != "company":
        custom_fields.pop("other_email_addresses", None)
    elif "other_email_addresses" in form:
        from app.services.customer_emails import additional_emails
        copies = additional_emails(form.get("other_email_addresses"), form.get("email", ""), max_addresses=3)
        if copies:
            custom_fields["other_email_addresses"] = "; ".join(copies)
        else:
            custom_fields.pop("other_email_addresses", None)
    submitted_custom_fields = {
        "vehicle_make": form.get("vehicle_make", "").strip() or form.get("vehicle_details", "").strip(),
        "vehicle_model": form.get("vehicle_model", "").strip(),
        "vehicle_color": form.get("vehicle_color", "").strip(),
        "vehicle_reg_no": form.get("vehicle_reg_no", "").strip(),
        "vehicle_registration_number": form.get("vehicle_registration_number", "").strip(),
        "vehicle_licence_number": form.get("vehicle_licence_number", "").strip(),
        "vehicle_type": form.get("vehicle_type", "").strip(),
        "vehicle_vin": form.get("vehicle_vin", "").strip(),
        "vehicle_engine_number": form.get("vehicle_engine_number", "").strip(),
        "vehicle_licence_disk_expiry": form.get("vehicle_licence_disk_expiry", "").strip(),
        "vehicle_registering_authority": form.get("vehicle_registering_authority", "").strip(),
        "vehicle_control_number": form.get("vehicle_control_number", "").strip(),
        "alternative_contact_name": form.get("alternative_contact_name", "").strip() or form.get("alternative_contact", "").strip(),
        "alternative_contact_number": form.get("alternative_contact_number", "").strip(),
        "alternative_contact_relationship": form.get("alternative_contact_relationship", "").strip(),
        "vat_number": form.get("vat_number", "").strip(),
        "company_reg_no": form.get("company_reg_no", "").strip(),
    }
    for key, value in submitted_custom_fields.items():
        if key.startswith("vehicle_") and not _form_has(form, key):
            # A partial post (the public portal, or the order card's cached snapshot) omits the
            # main-vehicle fields it does not show. Leaving the stored value alone keeps a later
            # partial save from wiping the vehicle details a scan just captured — and it preserves
            # any legacy decoded field (e.g. vehicle_color) a document still prints.
            if not (key == "vehicle_make" and _form_has(form, "vehicle_details")):
                continue
        if value:
            custom_fields[key] = value
        else:
            custom_fields.pop(key, None)
    custom_fields.pop("vehicle_details", None)
    custom_fields.pop("alternative_contact", None)
    # Ticket ABI-341953268: the suspect classification and its optional ID number
    # are written only from the customer form's own panel (blocking-panel marker,
    # ``save_suspect`` action, signed-in staff). Every other caller leaves both
    # values exactly as stored — a public/inline post cannot forge or clear them.
    suspect_flagged = False
    if suspect_panel_present(form):
        suspect_flagged = _clean_suspect(form, custom_fields)
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
    if suspect_flagged:
        # Ticket ABI-341953268: a suspect classification IS a block. Forcing the
        # flag here (rather than relying on the checkbox) means the atomic create
        # insert below writes is_blocked=1 in the same INSERT as the customer, so
        # the record is never briefly orderable, and a later suspect-panel save
        # can never leave a classified client unblocked by forgetting the tick.
        data["is_blocked"] = 1
        data["blocked_reason"] = data.get("blocked_reason") or SUSPECT_DEFAULT_BLOCK_REASON
    if credit_panel_present(form) and can_set_customer_credit():
        # Ticket ABI-341953129: the credit facility is set ONLY through the
        # customer form's own panel AND only by the main profile. Any other
        # caller — a staff customer edit, the order form's inline customer card,
        # the public storefront — leaves the credit keys out of the payload, so
        # update_customer never touches the stored columns and a forged field
        # cannot change (or clear) an existing facility.
        data["credit_allowed"], data["credit_limit"] = _clean_credit(form)
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


def create_customer(form, branch_id=None, notify=True, defer_commit=False, pending_portal=False):
    """Create a client and record where it was captured.

    ``branch_id`` overrides the branch the client is recorded against. The public portal
    passes the branch its link belongs to (so the client keeps that branch attribution — in
    the customer list and in the Telegram message — instead of the session-derived default,
    which is nobody on a public request). Any other caller leaves it ``None`` and keeps the
    historic ``customer_branch_id()`` behaviour.

    ``notify`` lets a caller that will write more of the record afterwards (the portal, which
    also records the pending registration and its reference) send the single notification
    itself once the record is complete, rather than fire a premature one here.

    ``defer_commit`` lets a caller that must write more rows in the SAME transaction hold the
    commit back and own it itself, so a later failure rolls the customer row back instead of
    leaking an approved-looking client. Default ``False`` keeps every other caller committing
    immediately.
    """
    data = _clean(form)
    db = get_db()
    # Ticket ABI-341953028: a customer created from the customer form can be
    # blocked on the spot. Any other caller (public storefront booking, inline
    # order customer, inline AJAX create) carries no blocking panel, so its new
    # customer starts unblocked.
    cur = db.execute(
        """INSERT INTO customers (customer_type, name, email, phone, marketing_opt_in, address_line1, address_line2, suburb, city, province, postal_code, country, custom_fields_json, balance_due, standard_discount_percent, client_verified, is_blocked, blocked_reason, credit_allowed, credit_limit, created_by_user_id, branch_id, created_at, source_system)
        VALUES (:customer_type, :name, :email, :phone, :marketing_opt_in, :address_line1, :address_line2, :suburb, :city, :province, :postal_code, :country, :custom_fields_json, 0, :standard_discount_percent, :client_verified, :is_blocked, :blocked_reason, :credit_allowed, :credit_limit, :created_by_user_id, :branch_id, :created_at, :source_system)""",
        {
            **data,
            "is_blocked": int(data.get("is_blocked") or 0),
            "blocked_reason": data.get("blocked_reason") or "",
            # Ticket ABI-341953129: only the main profile's customer form carries
            # the credit keys (see _clean); every other creator — staff customer
            # form, inline order card, public storefront — starts at 0/0.
            "credit_allowed": int(data.get("credit_allowed") or 0),
            "credit_limit": float(data.get("credit_limit") or 0),
            "source_system": "portal_pending" if pending_portal else "",
            "created_by_user_id": current_session_user_id(),
            "branch_id": branch_id if branch_id is not None else customer_branch_id(),
            "created_at": now(),
        },
    )
    _clear_suspect_cache()
    if not defer_commit:
        db.commit()
    customer_id = cur.lastrowid
    if notify:
        try:
            from app.services.telegram import send_new_customer_notification
            send_new_customer_notification(customer_id)
        except Exception:
            pass
    return customer_id


def update_customer(customer_id, form):
    data = _clean(form, existing_custom_fields=raw_custom_fields_for(get_customer(customer_id)))
    data["id"] = customer_id
    # Feature 2026-10-10: the "Main client vehicle" fields are mirrored from a vehicle row. When
    # this client has one linked, a save from the customer form is authoritative over that row (a
    # plate another client owns is refused, never swapped in) and the row's stored values are
    # mirrored back. A partial post from the portal/order card is not authoritative and is ignored
    # here, so a stale snapshot can never overwrite the vehicle that a scan captured.
    from app.services.vehicles import apply_main_vehicle_form_sync
    data["custom_fields_json"] = apply_main_vehicle_form_sync(customer_id, form, data["custom_fields_json"])
    # Ticket ABI-341953028: the block columns are only written when the customer
    # form's own blocking panel was submitted, so the order form's attached
    # customer card can never clear a block by saving other details.
    sets = """customer_type=:customer_type, name=:name, email=:email, phone=:phone, marketing_opt_in=:marketing_opt_in, address_line1=:address_line1, address_line2=:address_line2, suburb=:suburb, city=:city, province=:province, postal_code=:postal_code, country=:country, custom_fields_json=:custom_fields_json, standard_discount_percent=:standard_discount_percent, client_verified=:client_verified"""
    if blocking_panel_present(form):
        sets += ", is_blocked=:is_blocked, blocked_reason=:blocked_reason"
    # Ticket ABI-341953129: the credit columns are only written when _clean
    # actually produced them — i.e. the customer form's own panel was submitted
    # AND the session is the main profile. Every other writer (staff customer
    # edit, order form's inline customer card posting through this same service,
    # public storefront) leaves both columns exactly as they were.
    if "credit_allowed" in data:
        sets += ", credit_allowed=:credit_allowed, credit_limit=:credit_limit"
    get_db().execute(f"UPDATE customers SET {sets} WHERE id=:id", data)
    get_db().commit()
    _clear_suspect_cache()



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
    credit_allowed = bool(_row_flag(customer, "credit_allowed"))
    credit_limit = round(float(_customer_row_value(customer, "credit_limit", 0) or 0), 2)
    account_debt = _customer_row_value(customer, "account_credit_debt", None)
    if credit_allowed and account_debt is None:
        from app.services.credit_limits import outstanding_debt
        account_debt = outstanding_debt(customer["id"])
    # Ticket ABI-341953166: surface the facility's used figure beside the limit
    # and the available credit. It rides on the same credit_limits basis the
    # enforcement uses, and both callers precompute account_credit_debt, so this
    # adds no query (the one-SQL-query customer picker stays intact).
    account_credit_used = round(max(float(account_debt or 0), 0), 2) if credit_allowed else 0.0
    available_account_credit = round(max(credit_limit - float(account_debt or 0), 0), 2) if credit_allowed else 0.0
    summary = {
        "credit_allowed": credit_allowed,
        "credit_limit": credit_limit if credit_allowed else 0.0,
        "available_account_credit": available_account_credit,
        "account_credit_used": account_credit_used,
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
    # Ticket ABI-341953268: suspected-fraud classification is staff-only. The flag
    # and its possible duplicate matches ride on this summary (the same payload the
    # order form and the customer picker already carry) so no second request is
    # needed — but never for a public/background caller, and never with an ID
    # number: ``suspect_matches`` entries carry id/name/reasons only.
    # Ticket ABI-341953269: they now also carry the *matched* client's captured ID
    # number (``id_number``) so staff can compare it; the staff-only gate above is
    # what keeps it off public/background payloads.
    if staff_request_active():
        is_suspect = customer_is_suspect(customer)
        summary["is_suspect"] = is_suspect
        summary["suspect_matches"] = suspect_matches(
            customer, exclude_id=_customer_row_value(customer, "id", None)
        )
    return summary


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


def _statement_signed_money_display(value):
    """Money with the sign in front of the R: ``-R300.00``, never ``R-300.00``.

    Ticket ABI-341953265: the statement prints a payment as a negative (credit)
    amount, and the South African convention puts the minus before the currency.
    """
    value = _statement_money(value)
    return f"-R{abs(value):.2f}" if value < 0 else f"R{value:.2f}"


def _statement_setting(settings, key, default=""):
    if not settings:
        return default
    try:
        value = settings[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _statement_branch(customer):
    """Prefer the customer branch; otherwise use an unambiguous invoice issuer.

    Imported customers may have no branch even though their invoices do. Never
    substitute the signed-in user's branch or pick one of several issuers.
    """
    branch_id = _statement_setting(customer, "branch_id", "")
    db = get_db()
    if not branch_id:
        issuers = db.execute(
            """SELECT DISTINCT o.collect_branch_id AS branch_id
               FROM documents d JOIN orders o ON o.id = d.order_id
               WHERE o.customer_id = ? AND d.document_type = 'invoice'
                 AND d.status = 'finalized'
                 AND LOWER(COALESCE(o.status, '')) NOT IN (?, ?)""",
            (customer["id"], *STATEMENT_EXCLUDED_ORDER_STATUSES),
        ).fetchall()
        if len(issuers) != 1 or not issuers[0]["branch_id"]:
            return None
        branch_id = issuers[0]["branch_id"]
    return db.execute("SELECT * FROM branches WHERE id = ?", (branch_id,)).fetchone()


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
            WHERE o.customer_id = ? AND {_active_payment_clause('p')} AND LOWER(COALESCE(p.method, '')) <> 'account'
            ORDER BY paid_at, p.id""",
        (customer_id,),
    ).fetchall()

    invoices = []
    payments = []
    activity = []
    opening_invoiced = 0.0
    opening_paid = 0.0
    opening_writeoff = 0.0
    invoiced_total = 0.0
    paid_total = 0.0
    writeoff_total = 0.0
    lifetime_invoiced = 0.0
    lifetime_paid = 0.0
    lifetime_writeoff = 0.0

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
        # Ticket ABI-341953265: a payment is a credit on the account, so the
        # activity table prints it negative. ``amount`` stays positive exactly as
        # the ledger holds it, so the running balance and summary totals reconcile.
        activity.append({
            "date": day,
            "type": "Payment",
            "detail": f"{method} · {reference}" if reference else method,
            "amount": amount,
            "amount_display": _statement_signed_money_display(-amount),
            # A payment takes money off the balance (a refund's negative amount
            # puts it back), so the running total subtracts ``amount``.
            "is_credit": True,
            "type_rank": STATEMENT_ACTIVITY_PAYMENT_RANK,
            "source_id": row["payment_id"],
        })

    # ABI-341953141: imported debt is amount_due, NOT source_total and NOT
    # amount_due - settled_amount. Receipts below subtract the settlement once.
    # Keep collected rows: fully settled history belongs on a statement too.
    legacy_rows = db.execute("""
        SELECT id, source_order_number, amount_due, settled_amount, status, source_started_at,
               source_created_at, created_at
        FROM legacy_customer_balances WHERE customer_id = ?
    """, (customer_id,)).fetchall()
    legacy_receipts = db.execute("""
        SELECT id, amount, method, reference,
               COALESCE(NULLIF(payment_date, ''), created_at) AS paid_at
        FROM legacy_balance_payments WHERE customer_id = ?
          AND COALESCE(status, 'active') = 'active'
          AND COALESCE(deleted_at, '') = ''
    """, (customer_id,)).fetchall()
    # Ticket ABI-341953292: an approved noncash write-off of imported debt is its
    # own audit record, not a receipt. The statement keeps the imported debt whole
    # and prints the write-off as a separate credit, so a reader can see what was
    # owed and that the business chose not to collect it. Grouped per imported row
    # because that row's own date decides whether the debt is in range or opening.
    writeoffs_by_balance = {}
    for row in legacy_balance_writeoffs(customer_id):
        writeoffs_by_balance.setdefault(row['legacy_balance_id'], []).append(row)

    legacy_balances = []
    legacy_writeoffs = []
    legacy_total = 0.0
    for row in legacy_rows:
        # Use the source order date, then source creation date. Undated imports
        # use their import date, explicitly labelled (never imply an invoice date).
        source_day = (_statement_day(row['source_started_at'])
                      or _statement_day(row['source_created_at']))
        day = source_day or _statement_day(row['created_at'])
        # ABI-341953218: a waiver closes only the unpaid remainder. Retain the
        # already-settled portion so existing receipts still offset it exactly;
        # collected rows continue to carry the full imported debt as before.
        waived = row['status'] == 'waived'
        balance_writeoffs = writeoffs_by_balance.get(row['id']) or []
        if balance_writeoffs:
            # ABI-341953292: when the waiver came from an approved noncash
            # write-off, keep the WHOLE imported debt on the statement and let
            # the write-off credit below clear it. This is the audit trail the
            # older bare-waiver shortcut never had.
            amount = _statement_money(row['amount_due'])
        else:
            amount = _statement_money(row['settled_amount'] if waived else row['amount_due'])
        lifetime_invoiced += amount
        if date_from and day < date_from:
            opening_invoiced += amount
        elif not (date_to and day > date_to):
            legacy_total += amount
            detail = str(row['source_order_number'] or 'Unnumbered')
            if not source_day:
                detail += ' (import date)'
            if waived and not balance_writeoffs:
                detail += ' (unpaid remainder waived)'
            entry = {
                'date': day, 'type': 'Legacy balance',
                'detail': detail,
                'amount': amount, 'amount_display': _statement_money_display(amount),
                'type_rank': STATEMENT_ACTIVITY_INVOICE_RANK,
                'source_kind': 'legacy_balance', 'source_id': row['id'],
            }
            legacy_balances.append(entry)
            activity.append(entry)
        # A write-off is dated by its own approval date, not the debt's date, so
        # it is collected here and filtered against the range further down.
        legacy_writeoffs.extend((row, writeoff) for writeoff in balance_writeoffs)

    for row in legacy_receipts:
        day = _statement_day(row['paid_at'])
        amount = _statement_money(row['amount'])
        lifetime_paid += amount
        if date_from and day < date_from:
            opening_paid += amount
            continue
        if date_to and day > date_to:
            continue
        paid_total += amount
        method = (row['method'] or 'manual').replace('_', ' ').title()
        reference = (row['reference'] or '').strip()
        # Customer-level receipt, possibly spanning several orders. There is no
        # persisted per-order allocation to print; never invent one here.
        entry = {
            'date': day, 'method': method, 'reference': reference,
            'order_number': '—', 'amount': amount,
            'amount_display': _statement_money_display(amount),
            'source_kind': 'legacy_payment',
        }
        payments.append(entry)
        activity.append({
            **entry, 'type': 'Legacy payment',
            'detail': f"{method} · {reference or 'Receipt #' + str(row['id'])} (customer-level)",
            # Ticket ABI-341953265: an imported receipt is a credit too.
            'amount_display': _statement_signed_money_display(-amount),
            'is_credit': True,
            'type_rank': STATEMENT_ACTIVITY_PAYMENT_RANK, 'source_id': row['id'],
        })

    # Ticket ABI-341953292: the noncash write-off rows. They read as credits in
    # the activity table but are NOT payments: ``payments``/``paid_total`` stay
    # money actually collected, and the write-off is subtracted on its own line
    # of the balance arithmetic (opening, period and lifetime).
    for row, writeoff in legacy_writeoffs:
        amount = _statement_money(writeoff['amount'])
        lifetime_writeoff += amount
        day = (_statement_day(writeoff['writeoff_date'])
               or _statement_day(writeoff['created_at']))
        if date_from and day < date_from:
            opening_writeoff += amount
            continue
        if date_to and day > date_to:
            continue
        writeoff_total += amount
        reason = str(writeoff['reason'] or '').strip()
        ticket = str(writeoff['ticket_id'] or '').strip()
        detail_parts = [str(row['source_order_number'] or 'Unnumbered')]
        if ticket:
            detail_parts.append(f'Ticket {ticket}')
        if reason:
            detail_parts.append(reason)
        activity.append({
            'date': day, 'type': 'Legacy write-off',
            'detail': ' · '.join(detail_parts),
            'amount': amount,
            'amount_display': _statement_signed_money_display(-amount),
            # A write-off takes the debt off the balance without cash moving.
            'is_credit': True,
            'type_rank': STATEMENT_ACTIVITY_PAYMENT_RANK,
            'source_kind': 'legacy_writeoff', 'source_id': writeoff['id'],
        })
    payments.sort(key=lambda item: (item['date'], item.get('source_kind', 'current')))

    # Ticket ABI-341953086: the statement prints ONE chronological activity table
    # instead of separate INVOICES / PAYMENTS tables. The two source lists above
    # stay (the summary block's totals are built from them). Rows sort by date,
    # then invoices before payments on the same date, then the row's own id —
    # a deterministic tie-break so a page break landing mid-date can never
    # shuffle the order.
    activity.sort(key=lambda item: (item["date"], item["type_rank"],
                                    item.get("source_kind", "current"), item["source_id"]))

    opening_balance = round(opening_invoiced - opening_paid - opening_writeoff, 2)
    invoiced_total = round(invoiced_total, 2)
    paid_total = round(paid_total, 2)
    legacy_total = round(legacy_total, 2)
    writeoff_total = round(writeoff_total, 2)
    # Ticket ABI-341953292: a noncash write-off closes debt without money being
    # received, so it is subtracted here — and never inside ``paid_total``.
    closing_balance = round(opening_balance + invoiced_total + legacy_total
                            - paid_total - writeoff_total, 2)
    outstanding_balance = round(lifetime_invoiced - lifetime_paid - lifetime_writeoff, 2)

    # Ticket ABI-341953265: every activity row carries the balance it leaves
    # behind (the running total from the statement's opening balance). Invoices
    # and imported debt add; payments (and imported receipts) subtract, which is
    # the same arithmetic behind ``closing_balance`` above — so the last row
    # closes exactly on it.
    running_balance = opening_balance
    for row in activity:
        movement = -row["amount"] if row.get("is_credit") else row["amount"]
        running_balance = round(running_balance + movement, 2)
        row["balance"] = running_balance
        row["balance_display"] = _statement_money_display(running_balance)

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

    # Ticket ABI-341953166: the customer-facing statement reports the shared
    # credit facility summary (used / available). The definition lives in
    # app.services.credit_limits.facility_summary so every surface — statement,
    # customer page, new order, order detail — shows one consistent figure.
    from app.services.credit_limits import facility_summary
    facility = facility_summary(customer_id)

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
        # Payment instructions belong only to this customer's statement branch.
        "banking_details": {
            key: _statement_setting(branch, key, "")
            for key in ("bank_name", "bank_account_name", "bank_account_number",
                        "bank_branch_code", "bank_account_type", "bank_reference_note")
        },
        "date_from": date_from,
        "date_to": date_to,
        "period_label": period_label,
        "generated_at": generated_at or now(),
        "invoices": invoices,
        "legacy_balances": legacy_balances,
        "legacy_balance_count": len(legacy_balances),
        "legacy_total": legacy_total,
        "legacy_total_display": _statement_money_display(legacy_total),
        "payments": payments,
        "activity": activity,
        "invoice_count": len(invoices),
        "payment_count": len(payments),
        "activity_count": len(activity),
        "opening_balance": opening_balance,
        "invoiced_total": invoiced_total,
        "paid_total": paid_total,
        "writeoff_total": writeoff_total,
        "closing_balance": closing_balance,
        "outstanding_balance": outstanding_balance,
        "opening_balance_display": _statement_money_display(opening_balance),
        "invoiced_total_display": _statement_money_display(invoiced_total),
        "paid_total_display": _statement_money_display(paid_total),
        "writeoff_total_display": _statement_money_display(writeoff_total),
        "closing_balance_display": _statement_money_display(closing_balance),
        "outstanding_balance_display": _statement_money_display(outstanding_balance),
        "facility": facility,
        "facility_enabled": facility["enabled"],
        "facility_limit_display": _statement_money_display(facility["limit"]),
        "facility_used_display": _statement_money_display(facility["used"]),
        "facility_available_display": _statement_money_display(facility["available"]),
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
    if _customer_row_value(customer, "customer_type") != "company":
        visible.pop("other_email_addresses", None)
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
