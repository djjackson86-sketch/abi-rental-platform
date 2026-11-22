from app.db import get_db, now
from app.services.access import product_branch_clause

TRAILER_SERVICE_TYPES = [
    "Bearing service",
    "Trailer plug change",
    "Re-wiring",
    "Light repair/change",
    "Tyre change",
    "Rim & tyre change",
    "Minor body repairs",
    "Major body repairs",
    "Trailer repaint",
    "Brake lining change",
    "Damper change",
    "Brake adjustment",
    "Brake drum change",
    "Front axle change",
    "Back axle change",
    "Custom",
]

OTHER_RENTAL_PRODUCTS_GROUP = "other rental products"


def eligible_trailer_products(branch_id=None, query=""):
    sql = """SELECT p.*, b.name AS branch_name, g.name AS product_group_name
        FROM products p
        LEFT JOIN branches b ON b.id = p.branch_id
        LEFT JOIN product_groups g ON g.id = p.product_group_id
        WHERE p.product_type = 'rental' AND p.active = 1
          AND LOWER(COALESCE(g.name, '')) <> ?"""
    params = [OTHER_RENTAL_PRODUCTS_GROUP]
    branch_sql, branch_params = product_branch_clause("p", include_unassigned=True, branch_id=branch_id)
    sql += branch_sql
    params.extend(branch_params)
    if query:
        sql += " AND (LOWER(p.name) LIKE ? OR LOWER(p.sku) LIKE ? OR LOWER(p.description) LIKE ?)"
        needle = f"%{query.lower()}%"
        params.extend([needle, needle, needle])
    sql += " ORDER BY COALESCE(g.sort_order, 999999), COALESCE(g.name, 'ZZZ Ungrouped'), p.name LIMIT 200"
    return get_db().execute(sql, params).fetchall()


def get_eligible_trailer_product(product_id, branch_id=None):
    try:
        product_id = int(product_id)
    except (TypeError, ValueError):
        return None
    for product in eligible_trailer_products(branch_id=branch_id):
        if int(product["id"]) == product_id:
            return product
    return None


def clean_service_type(service_type):
    service_type = (service_type or "").strip()
    if service_type not in TRAILER_SERVICE_TYPES:
        raise ValueError("Choose the service or maintenance type")
    return service_type


def clean_service_types(values):
    """Validate a multi-select submission of service / maintenance types.

    Ticket ABI-341953342: one visit to the workshop often covers several jobs, so
    the dashboard form now sends ``service_type`` more than once.  Blank values
    are ignored (an empty ``getlist`` entry), duplicates collapse, and the result
    is returned in ``TRAILER_SERVICE_TYPES`` order so the day panel, the report
    and the product history always read the jobs in the order the list offers
    them rather than the order the boxes happened to be ticked.

    One off-list label rejects the whole submission: the caller writes nothing.
    """
    if isinstance(values, str):
        values = [values]
    chosen = []
    for value in values or []:
        service_type = (value or "").strip()
        if not service_type:
            continue
        if service_type not in TRAILER_SERVICE_TYPES:
            raise ValueError("Choose the service or maintenance type")
        if service_type not in chosen:
            chosen.append(service_type)
    if not chosen:
        raise ValueError("Choose the service or maintenance type")
    return [service_type for service_type in TRAILER_SERVICE_TYPES if service_type in chosen]


def _prepare_service_entry(product_id, service_types, custom_description, branch_id, service_date):
    """Everything ``create_service_history_many`` validates before writing."""
    product = get_eligible_trailer_product(product_id, branch_id=branch_id)
    if not product:
        raise ValueError("Choose an active rental trailer from inventory")
    service_types = clean_service_types(service_types)
    custom_description = (custom_description or "").strip()
    if "Custom" in service_types and not custom_description:
        raise ValueError("Enter the custom service or maintenance done")
    timestamp = now()
    service_date = (service_date or timestamp[:10]).strip()[:10]
    return int(product["id"]), service_types, custom_description, service_date, timestamp


def create_service_history_many(product_id, service_types, custom_description="", branch_id=None, user_id=None, service_date=None):
    """Record one trailer service entry per selected type - one row each.

    One row per type (no schema change) keeps every existing reader working: the
    dashboard day panel, the day report PDF and CSV, and the product's own
    service history all loop the ``trailer_service_history`` rows, so several
    jobs ticked in one submission appear as several lines for the same trailer
    on the same date and depot.

    Every type is validated first; nothing is written if any of them is refused.
    Returns the inserted row ids.
    """
    product_id, service_types, custom_description, service_date, timestamp = _prepare_service_entry(
        product_id, service_types, custom_description, branch_id, service_date
    )
    db = get_db()
    row_ids = []
    for service_type in service_types:
        cur = db.execute(
            """INSERT INTO trailer_service_history
            (product_id, service_type, custom_description, service_date, branch_id, created_by_user_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                product_id,
                service_type,
                custom_description if service_type == "Custom" else "",
                service_date,
                branch_id,
                user_id,
                timestamp,
            ),
        )
        row_ids.append(cur.lastrowid)
    db.commit()
    return row_ids


def create_service_history(product_id, service_type, custom_description="", branch_id=None, user_id=None, service_date=None):
    """Single-type entry; kept for the callers and tests that still pass one type."""
    row_ids = create_service_history_many(
        product_id,
        [service_type],
        custom_description,
        branch_id=branch_id,
        user_id=user_id,
        service_date=service_date,
    )
    return row_ids[0]


def list_service_history(product_id, limit=100):
    return get_db().execute(
        """SELECT h.*, b.name AS branch_name, u.name AS created_by_name
        FROM trailer_service_history h
        LEFT JOIN branches b ON b.id = h.branch_id
        LEFT JOIN users u ON u.id = h.created_by_user_id
        WHERE h.product_id = ?
        ORDER BY h.service_date DESC, h.created_at DESC, h.id DESC
        LIMIT ?""",
        (int(product_id), int(limit)),
    ).fetchall()


def service_history_label(row):
    if row["service_type"] == "Custom" and row["custom_description"]:
        return f"Custom — {row['custom_description']}"
    return row["service_type"]


def _value(row, key, default=None):
    """Read one column off any of the three row shapes this app sees."""
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError, ValueError):
        return default


def services_for_day(day, branch_id=None):
    """What was serviced or maintained on one business day, as plain dicts.

    Ticket ABI-341953042 ask 3: the dashboard's trailer service panel has to show
    the day it is looking at, not just offer a form.  Rows come from
    ``trailer_service_history.service_date`` (which ``create_service_history``
    writes from the resolved business day), so a report for a past day reads the
    work logged against that day.

    The depot filter is the **same** ``product_branch_clause`` the panel's
    trailer picker already uses (``eligible_trailer_products``), where the
    session scope always wins - so a crafted ``?branch=`` can narrow this list
    but never widen it.

    Plain dicts only: production rows are libsql tuples, where ``row[key]``
    works but ``row.count`` is a method (see ``app/services/cash.py``).
    """
    clause, clause_params = product_branch_clause("p", include_unassigned=True, branch_id=branch_id)
    sql = f"""SELECT h.id AS id, h.service_type AS service_type,
        h.custom_description AS custom_description, h.service_date AS service_date,
        p.name AS product_name, p.sku AS product_sku, b.name AS branch_name
        FROM trailer_service_history h
        JOIN products p ON p.id = h.product_id
        LEFT JOIN branches b ON b.id = h.branch_id
        WHERE h.service_date = ?{clause}
        ORDER BY p.name, h.id"""
    entries = []
    for row in get_db().execute(sql, [str(day or "")[:10], *clause_params]).fetchall():
        service_type = str(_value(row, "service_type") or "")
        custom = str(_value(row, "custom_description") or "")
        if service_type == "Custom" and custom:
            label = f"Custom — {custom}"
        else:
            label = service_type
        entries.append({
            "id": int(_value(row, "id") or 0),
            "product_name": str(_value(row, "product_name") or ""),
            "product_sku": str(_value(row, "product_sku") or ""),
            "branch_name": str(_value(row, "branch_name") or ""),
            "service_date": str(_value(row, "service_date") or ""),
            "label": label,
        })
    return entries
