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
    "Mechanism repairs",
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


def create_service_history(product_id, service_type, custom_description="", branch_id=None, user_id=None, service_date=None):
    product = get_eligible_trailer_product(product_id, branch_id=branch_id)
    if not product:
        raise ValueError("Choose an active rental trailer from inventory")
    service_type = clean_service_type(service_type)
    custom_description = (custom_description or "").strip()
    if service_type == "Custom" and not custom_description:
        raise ValueError("Enter the custom service or maintenance done")
    if service_type != "Custom":
        custom_description = ""
    timestamp = now()
    service_date = (service_date or timestamp[:10]).strip()[:10]
    db = get_db()
    cur = db.execute(
        """INSERT INTO trailer_service_history
        (product_id, service_type, custom_description, service_date, branch_id, created_by_user_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (int(product["id"]), service_type, custom_description, service_date, branch_id, user_id, timestamp),
    )
    db.commit()
    return cur.lastrowid


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
