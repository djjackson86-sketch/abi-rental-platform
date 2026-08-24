from app.db import get_db, now

VALID_TYPES = {"rental", "sale", "service"}
VALID_UNITS = {"hour", "day", "week", "month", "fixed"}
VALID_TRACKING_METHODS = {"bulk", "individual"}


def tracking_label(value):
    return "Track individually" if value == "individual" else "Track quantities"


def list_product_groups(include_inactive=True):
    sql = "SELECT * FROM product_groups"
    params = []
    if not include_inactive:
        sql += " WHERE active = 1"
    sql += " ORDER BY sort_order ASC, name ASC"
    return get_db().execute(sql, params).fetchall()


def get_product_group(group_id):
    return get_db().execute("SELECT * FROM product_groups WHERE id = ?", (group_id,)).fetchone()


def _clean_group(form):
    name = form.get("name", "").strip()
    if not name:
        raise ValueError("Product group name is required")
    try:
        sort_order = int(form.get("sort_order") or 0)
    except ValueError:
        sort_order = 0
    return {
        "name": name,
        "description": form.get("description", "").strip(),
        "active": 1 if form.get("active") else 0,
        "sort_order": sort_order,
    }


def create_product_group(form):
    data = _clean_group(form)
    db = get_db()
    try:
        cur = db.execute(
            """INSERT INTO product_groups (name, description, active, sort_order, created_at, updated_at)
            VALUES (:name, :description, :active, :sort_order, :created_at, :updated_at)""",
            {**data, "created_at": now(), "updated_at": now()},
        )
        db.commit()
        return cur.lastrowid
    except Exception as exc:
        db.rollback()
        if "UNIQUE" in str(exc).upper():
            raise ValueError("A product group with that name already exists") from exc
        raise


def update_product_group(group_id, form):
    data = _clean_group(form)
    data["id"] = group_id
    data["updated_at"] = now()
    db = get_db()
    try:
        db.execute(
            """UPDATE product_groups SET
            name=:name, description=:description, active=:active, sort_order=:sort_order, updated_at=:updated_at
            WHERE id=:id""",
            data,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        if "UNIQUE" in str(exc).upper():
            raise ValueError("A product group with that name already exists") from exc
        raise


def list_products(query="", product_type="", visibility="", product_group_id=""):
    sql = """SELECT p.*, t.name AS tax_name, t.rate AS tax_rate, b.name AS branch_name,
        g.name AS product_group_name, g.description AS product_group_description, g.sort_order AS product_group_sort_order
        FROM products p
        LEFT JOIN tax_profiles t ON p.tax_profile_id = t.id
        LEFT JOIN branches b ON b.id = p.branch_id
        LEFT JOIN product_groups g ON g.id = p.product_group_id
        WHERE 1=1"""
    params = []
    if query:
        sql += " AND (LOWER(p.name) LIKE ? OR LOWER(p.sku) LIKE ? OR LOWER(p.description) LIKE ?)"
        needle = f"%{query.lower()}%"
        params.extend([needle, needle, needle])
    if product_type in VALID_TYPES:
        sql += " AND p.product_type = ?"
        params.append(product_type)
    if visibility == "public":
        sql += " AND p.public_visible = 1 AND p.active = 1"
    elif visibility == "hidden":
        sql += " AND (p.public_visible = 0 OR p.active = 0)"
    if product_group_id == "ungrouped":
        sql += " AND p.product_group_id IS NULL"
    elif product_group_id:
        try:
            group_id = int(product_group_id)
        except ValueError:
            group_id = 0
        if group_id:
            sql += " AND p.product_group_id = ?"
            params.append(group_id)
    sql += " ORDER BY COALESCE(g.sort_order, 999999) ASC, COALESCE(g.name, 'ZZZ Ungrouped') ASC, p.name ASC"
    return get_db().execute(sql, params).fetchall()


def group_products_for_display(products):
    grouped = []
    current_key = object()
    for product in products:
        group_id = product["product_group_id"] if product["product_group_id"] is not None else "ungrouped"
        if group_id != current_key:
            grouped.append({
                "id": group_id,
                "name": product["product_group_name"] or "Ungrouped products",
                "description": product["product_group_description"] or "",
                "products": [],
            })
            current_key = group_id
        grouped[-1]["products"].append(product)
    return grouped


def get_product(product_id):
    return get_db().execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()


def product_counts():
    row = get_db().execute(
        "SELECT COUNT(*) total, SUM(active) active, SUM(public_visible) public_visible FROM products"
    ).fetchone()
    return {"total": row["total"] or 0, "active": row["active"] or 0, "public_visible": row["public_visible"] or 0}


def product_filter_counts():
    db = get_db()
    type_rows = db.execute("SELECT product_type, COUNT(*) count FROM products GROUP BY product_type").fetchall()
    visibility = db.execute(
        """SELECT
            SUM(CASE WHEN public_visible = 1 AND active = 1 THEN 1 ELSE 0 END) public,
            SUM(CASE WHEN public_visible = 0 OR active = 0 THEN 1 ELSE 0 END) hidden
        FROM products"""
    ).fetchone()
    return {
        "product_type": {row["product_type"]: row["count"] for row in type_rows},
        "visibility": {"public": visibility["public"] or 0, "hidden": visibility["hidden"] or 0},
    }


def _default_branch_id():
    row = get_db().execute("SELECT id FROM branches WHERE active = 1 ORDER BY id LIMIT 1").fetchone()
    return row["id"] if row else None


def _clean(form):
    name = form.get("name", "").strip()
    if not name:
        raise ValueError("Product name is required")
    product_type = form.get("product_type", "rental")
    if product_type not in VALID_TYPES:
        product_type = "rental"
    price_unit = form.get("price_unit", "day")
    if price_unit not in VALID_UNITS:
        price_unit = "day"
    tracking_method = form.get("tracking_method", "bulk")
    if tracking_method not in VALID_TRACKING_METHODS:
        tracking_method = "bulk"
    product_group_id = int(form.get("product_group_id") or 0) or None
    return {
        "name": name,
        "product_type": product_type,
        "tracking_method": tracking_method,
        "description": form.get("description", "").strip(),
        "sku": form.get("sku", "").strip(),
        "active": 1 if form.get("active") else 0,
        "public_visible": 1 if form.get("public_visible") else 0,
        "price_amount": float(form.get("price_amount") or 0),
        "price_unit": price_unit,
        "security_deposit": float(form.get("security_deposit") or 0),
        "tax_profile_id": int(form.get("tax_profile_id") or 1),
        "product_group_id": product_group_id,
        "quantity": max(0, int(form.get("quantity") or 0)),
        "branch_id": int(form.get("branch_id") or 0) or _default_branch_id(),
    }


def create_product(form):
    data = _clean(form)
    db = get_db()
    cur = db.execute(
        """INSERT INTO products
        (name, product_type, tracking_method, description, sku, active, public_visible, price_amount, price_unit, security_deposit, tax_profile_id, product_group_id, quantity, branch_id, created_at)
        VALUES (:name, :product_type, :tracking_method, :description, :sku, :active, :public_visible, :price_amount, :price_unit, :security_deposit, :tax_profile_id, :product_group_id, :quantity, :branch_id, :created_at)""",
        {**data, "created_at": now()},
    )
    db.commit()
    return cur.lastrowid


def update_product(product_id, form):
    data = _clean(form)
    existing = get_product(product_id)
    immutable_change_requested = False
    if existing:
        immutable_change_requested = (
            data["product_type"] != existing["product_type"]
            or data["tracking_method"] != existing["tracking_method"]
        )
        data["product_type"] = existing["product_type"]
        data["tracking_method"] = existing["tracking_method"]
    data["id"] = product_id
    get_db().execute(
        """UPDATE products SET
        name=:name, product_type=:product_type, tracking_method=:tracking_method, description=:description, sku=:sku, active=:active, public_visible=:public_visible,
        price_amount=:price_amount, price_unit=:price_unit, security_deposit=:security_deposit, tax_profile_id=:tax_profile_id, product_group_id=:product_group_id, quantity=:quantity, branch_id=:branch_id
        WHERE id=:id""",
        data,
    )
    get_db().commit()
    return immutable_change_requested


def archive_product(product_id):
    get_db().execute("UPDATE products SET active = 0, public_visible = 0 WHERE id = ?", (product_id,))
    get_db().commit()
