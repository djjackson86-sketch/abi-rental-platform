from app.db import get_db, now


def list_branches(active_only=False):
    sql = "SELECT b.*, (SELECT COUNT(*) FROM products p WHERE p.branch_id = b.id) AS product_count FROM branches b"
    if active_only:
        sql += " WHERE b.active = 1"
    sql += " ORDER BY b.active DESC, b.name"
    return get_db().execute(sql).fetchall()


def get_branch(branch_id):
    return get_db().execute("SELECT * FROM branches WHERE id = ?", (branch_id,)).fetchone()


def default_branch_id():
    row = get_db().execute("SELECT id FROM branches WHERE active = 1 ORDER BY id LIMIT 1").fetchone()
    return row["id"] if row else None


def _clean(form):
    name = (form.get("name") or "").strip()
    if not name:
        raise ValueError("Branch name is required")
    return {
        "name": name,
        "code": (form.get("code") or "").strip(),
        "phone": (form.get("phone") or "").strip(),
        "email": (form.get("email") or "").strip().lower(),
        "address_line1": (form.get("address_line1") or "").strip(),
        "address_line2": (form.get("address_line2") or "").strip(),
        "city": (form.get("city") or "").strip(),
        "province": (form.get("province") or "").strip(),
        "postal_code": (form.get("postal_code") or "").strip(),
        "active": 1 if form.get("active") else 0,
    }


def create_branch(form):
    data = _clean(form)
    ts = now()
    cur = get_db().execute("""INSERT INTO branches (name, code, phone, email, address_line1, address_line2, city, province, postal_code, active, created_at, updated_at)
        VALUES (:name, :code, :phone, :email, :address_line1, :address_line2, :city, :province, :postal_code, :active, :created_at, :updated_at)""", {**data, "created_at": ts, "updated_at": ts})
    get_db().commit()
    return cur.lastrowid


def update_branch(branch_id, form):
    data = _clean(form)
    data.update({"id": branch_id, "updated_at": now()})
    get_db().execute("""UPDATE branches SET name=:name, code=:code, phone=:phone, email=:email, address_line1=:address_line1, address_line2=:address_line2, city=:city, province=:province, postal_code=:postal_code, active=:active, updated_at=:updated_at WHERE id=:id""", data)
    get_db().commit()


def branch_options():
    return list_branches(active_only=True)
