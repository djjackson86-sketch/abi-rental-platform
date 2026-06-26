from app.db import get_db, now

VALID_DISCOUNT_TYPES = {"percent", "fixed"}


def list_coupons(query="", status=""):
    sql = "SELECT * FROM coupons WHERE 1=1"
    params = []
    if query:
        sql += " AND (LOWER(code) LIKE ? OR LOWER(description) LIKE ?)"
        needle = f"%{query.lower()}%"
        params.extend([needle, needle])
    if status == "active":
        sql += " AND active = 1"
    elif status == "inactive":
        sql += " AND active = 0"
    sql += " ORDER BY created_at DESC, code"
    return get_db().execute(sql, params).fetchall()


def get_coupon_by_code(code):
    cleaned = (code or "").strip().upper()
    if not cleaned:
        return None
    return get_db().execute("SELECT * FROM coupons WHERE code = ? AND active = 1", (cleaned,)).fetchone()


def format_discount(coupon):
    if coupon["discount_type"] == "percent":
        return f"{float(coupon['value']):.1f}%"
    return f"R{float(coupon['value']):.2f}"


def calculate_discount(coupon, subtotal):
    if not coupon:
        return 0
    subtotal = max(0, float(subtotal or 0))
    value = max(0, float(coupon["value"] or 0))
    if coupon["discount_type"] == "percent":
        discount = subtotal * min(value, 100) / 100
    else:
        discount = min(value, subtotal)
    return round(discount, 2)


def create_coupon(form):
    code = form.get("code", "").strip().upper()
    if not code:
        raise ValueError("Coupon code is required")
    discount_type = form.get("discount_type", "percent")
    if discount_type not in VALID_DISCOUNT_TYPES:
        discount_type = "percent"
    value = float(form.get("value") or 0)
    if value <= 0:
        raise ValueError("Coupon discount must be greater than zero")
    if discount_type == "percent" and value > 100:
        raise ValueError("Percentage coupons cannot exceed 100%")
    db = get_db()
    try:
        db.execute(
            """INSERT INTO coupons (code, description, discount_type, value, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (code, form.get("description", "").strip(), discount_type, value, 1 if form.get("active") else 0, now()),
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        if "UNIQUE" in str(exc).upper():
            raise ValueError("Coupon code already exists") from exc
        raise


def coupon_counts():
    row = get_db().execute("SELECT COUNT(*) total, COALESCE(SUM(active), 0) active FROM coupons").fetchone()
    total = row["total"] or 0
    active = row["active"] or 0
    return {"total": total, "active": active, "inactive": total - active}
