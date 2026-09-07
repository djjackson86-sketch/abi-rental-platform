from app.db import get_db, now


def list_branches(active_only=False):
    sql = """
        SELECT b.*,
               (SELECT COUNT(*) FROM products p WHERE p.branch_id = b.id) AS product_count,
               (SELECT COUNT(*) FROM orders o WHERE o.collect_branch_id = b.id OR o.return_branch_id = b.id) AS order_count,
               (SELECT COUNT(*) FROM users u WHERE u.branch_id = b.id) AS user_count
        FROM branches b"""
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
        "bank_name": (form.get("bank_name") or "").strip(),
        "bank_account_name": (form.get("bank_account_name") or "").strip(),
        "bank_account_number": (form.get("bank_account_number") or "").strip(),
        "bank_branch_code": (form.get("bank_branch_code") or "").strip(),
        "bank_account_type": (form.get("bank_account_type") or "").strip(),
        "bank_reference_note": (form.get("bank_reference_note") or "").strip(),
        "active": 1 if form.get("active") else 0,
    }


def create_branch(form):
    data = _clean(form)
    ts = now()
    cur = get_db().execute("""INSERT INTO branches (name, code, phone, email, address_line1, address_line2, city, province, postal_code, bank_name, bank_account_name, bank_account_number, bank_branch_code, bank_account_type, bank_reference_note, active, created_at, updated_at)
        VALUES (:name, :code, :phone, :email, :address_line1, :address_line2, :city, :province, :postal_code, :bank_name, :bank_account_name, :bank_account_number, :bank_branch_code, :bank_account_type, :bank_reference_note, :active, :created_at, :updated_at)""", {**data, "created_at": ts, "updated_at": ts})
    get_db().commit()
    return cur.lastrowid


def update_branch(branch_id, form):
    data = _clean(form)
    data.update({"id": branch_id, "updated_at": now()})
    get_db().execute("""UPDATE branches SET name=:name, code=:code, phone=:phone, email=:email, address_line1=:address_line1, address_line2=:address_line2, city=:city, province=:province, postal_code=:postal_code, bank_name=:bank_name, bank_account_name=:bank_account_name, bank_account_number=:bank_account_number, bank_branch_code=:bank_branch_code, bank_account_type=:bank_account_type, bank_reference_note=:bank_reference_note, active=:active, updated_at=:updated_at WHERE id=:id""", data)
    get_db().commit()


def branch_options():
    return list_branches(active_only=True)


def delete_branch(branch_id):
    """Delete a depot branch and detach its references.

    Products, orders and users keep working after the delete: their branch_id is
    set to NULL first (no orphaned pointers), then the branch row is removed.
    Deleting the final remaining branch is not allowed.
    """
    db = get_db()
    branch = get_branch(branch_id)
    if not branch:
        raise ValueError("Branch not found")
    remaining = db.execute("SELECT COUNT(*) AS c FROM branches").fetchone()["c"]
    if int(remaining) <= 1:
        raise ValueError("Cannot delete the last branch; keep at least one depot branch")
    db.execute("UPDATE products SET branch_id = NULL WHERE branch_id = ?", (branch_id,))
    db.execute(
        "UPDATE orders SET collect_branch_id = CASE WHEN collect_branch_id = ? THEN NULL ELSE collect_branch_id END, return_branch_id = CASE WHEN return_branch_id = ? THEN NULL ELSE return_branch_id END WHERE collect_branch_id = ? OR return_branch_id = ?",
        (branch_id, branch_id, branch_id, branch_id),
    )
    db.execute("UPDATE users SET branch_id = NULL WHERE branch_id = ?", (branch_id,))
    db.execute("DELETE FROM branches WHERE id = ?", (branch_id,))
    db.commit()
