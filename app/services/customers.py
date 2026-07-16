from app.db import get_db, now

VALID_TYPES = {"individual", "company"}


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


def _clean(form):
    name = form.get("name", "").strip()
    if not name:
        raise ValueError("Customer name is required")
    customer_type = form.get("customer_type", "individual")
    if customer_type not in VALID_TYPES:
        customer_type = "individual"
    return {
        "customer_type": customer_type,
        "name": name,
        "email": form.get("email", "").strip().lower(),
        "phone": form.get("phone", "").strip(),
        "marketing_opt_in": 1 if form.get("marketing_opt_in") else 0,
    }


def create_customer(form):
    data = _clean(form)
    db = get_db()
    cur = db.execute(
        """INSERT INTO customers (customer_type, name, email, phone, marketing_opt_in, balance_due, created_at)
        VALUES (:customer_type, :name, :email, :phone, :marketing_opt_in, 0, :created_at)""",
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
    data = _clean(form)
    data["id"] = customer_id
    get_db().execute(
        """UPDATE customers SET customer_type=:customer_type, name=:name, email=:email, phone=:phone, marketing_opt_in=:marketing_opt_in WHERE id=:id""",
        data,
    )
    get_db().commit()
