from app.db import get_db, now

# Day numbering follows the app's existing global `operating_hours` table
# (and `first_day_of_week` default): 0 = Sunday ... 6 = Saturday, which is what
# the settings "Operating hours seed" already stores - so both tables agree.
DAY_LABELS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
WEEKEND_DAYS = (0, 6)
DEFAULT_OPEN_TIME = "09:00"
DEFAULT_CLOSE_TIME = "17:00"


def _day_rows_for(branch_id):
    rows = get_db().execute(
        "SELECT * FROM branch_operating_hours WHERE branch_id = ? ORDER BY day_of_week",
        (branch_id,),
    ).fetchall()
    by_day = {}
    for row in rows:
        try:
            by_day[int(row["day_of_week"])] = row
        except (TypeError, ValueError):
            continue
    return by_day


def default_hours():
    """Seven rows of default trading hours, seeded from the global settings.

    Used for a branch that has never had hours saved (and for a brand-new
    branch) so the page always shows a full week without an empty state.
    """
    rows = get_db().execute(
        "SELECT day_of_week, open_time, close_time, closed FROM operating_hours ORDER BY day_of_week"
    ).fetchall()
    by_day = {}
    for row in rows:
        try:
            by_day[int(row["day_of_week"])] = row
        except (TypeError, ValueError):
            continue
    result = []
    for day in range(7):
        row = by_day.get(day)
        result.append({
            "day_of_week": day,
            "day_label": DAY_LABELS[day],
            "open_time": (row["open_time"] if row else None) or DEFAULT_OPEN_TIME,
            "close_time": (row["close_time"] if row else None) or DEFAULT_CLOSE_TIME,
            "closed": 1 if row and row["closed"] else (1 if not row and day in WEEKEND_DAYS else 0),
            "saved": 0,
        })
    return result


def list_branch_hours(branch_id):
    """The branch's saved week, or the global defaults when nothing is saved."""
    saved = _day_rows_for(branch_id)
    if not saved:
        return default_hours()
    defaults = {row["day_of_week"]: row for row in default_hours()}
    result = []
    for day in range(7):
        row = saved.get(day)
        if row:
            result.append({
                "day_of_week": day,
                "day_label": DAY_LABELS[day],
                "open_time": row["open_time"] or DEFAULT_OPEN_TIME,
                "close_time": row["close_time"] or DEFAULT_CLOSE_TIME,
                "closed": 1 if row["closed"] else 0,
                "saved": 1,
            })
        else:
            result.append(dict(defaults[day], saved=0))
    return result


def hours_saved(branch_id):
    return bool(_day_rows_for(branch_id))


def pickup_hours_error(branch_id, when):
    """Refuse a pickup outside the collection branch's saved trading hours.

    Returns None - meaning "no objection" - when the branch has no SAVED hours
    (enforcement only starts once the client has set real hours for that depot),
    when there is no branch/time, or when the time sits inside the branch's
    trading window for that weekday.

    Day numbering is Sunday-first to match the table (see DAY_LABELS), so a
    Monday pickup maps to day_of_week 1.
    """
    if not branch_id or when is None:
        return None
    saved = _day_rows_for(branch_id)
    if not saved:
        return None
    day = (when.weekday() + 1) % 7
    row = saved.get(day)
    if row is None:
        return None
    branch = get_branch(branch_id)
    label = (branch["name"] if branch else "") or "This branch"
    if row["closed"]:
        return f"{label} is closed on {DAY_LABELS[day]}. Choose another pickup date."
    open_time = row["open_time"] or DEFAULT_OPEN_TIME
    close_time = row["close_time"] or DEFAULT_CLOSE_TIME
    if not (open_time <= when.strftime("%H:%M") <= close_time):
        return (
            f"Pickup must be between {open_time} and {close_time} — {label} trades "
            f"{open_time} to {close_time} on {DAY_LABELS[day]}."
        )
    return None


def _summarize(hours):
    """Compact one-line summary for the branches table (e.g. 'Mon-Fri 09:00-17:00, Sat-Sun Closed')."""
    if not hours:
        return "Not set"
    groups = []
    for row in hours:
        if row["closed"]:
            key = ("Closed",)
        else:
            key = (row["open_time"], row["close_time"])
        if groups and groups[-1][0] == key:
            groups[-1][1].append(row["day_label"][:3])
        else:
            groups.append((key, [row["day_label"][:3]]))
    parts = []
    for key, days in groups:
        days_label = days[0] if len(days) == 1 else f"{days[0]}-{days[-1]}"
        if key == ("Closed",):
            parts.append(f"{days_label} Closed")
        else:
            parts.append(f"{days_label} {key[0]}-{key[1]}")
    return ", ".join(parts)


def branch_hours_summaries():
    """{branch_id: summary} for every branch that has saved hours."""
    rows = get_db().execute("SELECT * FROM branch_operating_hours ORDER BY branch_id, day_of_week").fetchall()
    by_branch = {}
    for row in rows:
        try:
            branch_id = int(row["branch_id"])
            day = int(row["day_of_week"])
        except (TypeError, ValueError):
            continue
        by_branch.setdefault(branch_id, {})[day] = row
    summaries = {}
    for branch_id, by_day in by_branch.items():
        hours = []
        for day in range(7):
            row = by_day.get(day)
            if not row:
                continue
            hours.append({
                "day_of_week": day,
                "day_label": DAY_LABELS[day],
                "open_time": row["open_time"] or DEFAULT_OPEN_TIME,
                "close_time": row["close_time"] or DEFAULT_CLOSE_TIME,
                "closed": 1 if row["closed"] else 0,
            })
        summaries[branch_id] = _summarize(hours)
    return summaries


def _time_value(raw, fallback):
    value = (raw or "").strip()
    if not value:
        return fallback
    parts = value.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError("Trading hours must be entered as HH:MM")
    hour, minute = int(parts[0]), int(parts[1])
    if hour > 23 or minute > 59:
        raise ValueError("Trading hours must be entered as HH:MM")
    return f"{hour:02d}:{minute:02d}"


def save_branch_hours(branch_id, form):
    """Upsert the seven day rows for one branch from the submitted form."""
    if not get_branch(branch_id):
        raise ValueError("Branch not found")
    db = get_db()
    ts = now()
    saved = _day_rows_for(branch_id)
    for day in range(7):
        closed = 1 if form.get(f"closed_{day}") else 0
        open_time = _time_value(form.get(f"open_time_{day}"), DEFAULT_OPEN_TIME)
        close_time = _time_value(form.get(f"close_time_{day}"), DEFAULT_CLOSE_TIME)
        if not closed and open_time >= close_time:
            raise ValueError(f"{DAY_LABELS[day]}: closing time must be after opening time")
        row = saved.get(day)
        if row:
            db.execute(
                """UPDATE branch_operating_hours SET open_time = ?, close_time = ?, closed = ?, updated_at = ?
                WHERE branch_id = ? AND day_of_week = ?""",
                (open_time, close_time, closed, ts, branch_id, day),
            )
        else:
            db.execute(
                """INSERT INTO branch_operating_hours (branch_id, day_of_week, open_time, close_time, closed, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (branch_id, day, open_time, close_time, closed, ts),
            )
    db.commit()


def seed_branch_hours(branch_id):
    """Give a brand-new branch its own copy of the default week."""
    if _day_rows_for(branch_id):
        return
    db = get_db()
    ts = now()
    for row in default_hours():
        db.execute(
            """INSERT INTO branch_operating_hours (branch_id, day_of_week, open_time, close_time, closed, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (branch_id, row["day_of_week"], row["open_time"], row["close_time"], row["closed"], ts),
        )
    db.commit()


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
    branch_id = cur.lastrowid
    # A brand-new branch starts with an editable copy of the default week so the
    # trading-hours section is never blank.
    seed_branch_hours(branch_id)
    return branch_id


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
    # Per-branch stock counts belong to the depot that is being deleted.
    stock_products = [row["product_id"] for row in db.execute(
        "SELECT DISTINCT product_id FROM product_branch_stock WHERE branch_id = ?", (branch_id,)
    ).fetchall()]
    db.execute("DELETE FROM product_branch_stock WHERE branch_id = ?", (branch_id,))
    # Trading hours belong to the depot that is being deleted (libsql
    # autocommits, so children are deleted explicitly).
    db.execute("DELETE FROM branch_operating_hours WHERE branch_id = ?", (branch_id,))
    for product_id in stock_products:
        # products.quantity stays the computed total of the rows that are left.
        total = db.execute(
            "SELECT COALESCE(SUM(quantity), 0) AS total FROM product_branch_stock WHERE product_id = ?",
            (product_id,),
        ).fetchone()["total"]
        db.execute("UPDATE products SET quantity = ? WHERE id = ?", (int(total or 0), product_id))
    db.execute(
        "UPDATE orders SET collect_branch_id = CASE WHEN collect_branch_id = ? THEN NULL ELSE collect_branch_id END, return_branch_id = CASE WHEN return_branch_id = ? THEN NULL ELSE return_branch_id END WHERE collect_branch_id = ? OR return_branch_id = ?",
        (branch_id, branch_id, branch_id, branch_id),
    )
    db.execute("UPDATE users SET branch_id = NULL WHERE branch_id = ?", (branch_id,))
    # Multi-branch rows for the deleted depot go with it (libsql autocommits, so
    # the delete is explicit). An account left with no rows would fall back to
    # the historic "blank branch = all branches" rule, which would silently WIDEN
    # its visibility — so such an account is explicitly made all-branches instead.
    affected_users = [row["user_id"] for row in db.execute(
        "SELECT user_id FROM user_branch_access WHERE branch_id = ?", (branch_id,)
    ).fetchall()]
    db.execute("DELETE FROM user_branch_access WHERE branch_id = ?", (branch_id,))
    for user_id in affected_users:
        remaining = db.execute(
            "SELECT COUNT(*) AS c FROM user_branch_access WHERE user_id = ?", (user_id,)
        ).fetchone()["c"]
        if int(remaining or 0) == 0:
            db.execute("UPDATE users SET can_view_all_branches = 1 WHERE id = ?", (user_id,))
    db.execute("DELETE FROM branches WHERE id = ?", (branch_id,))
    db.commit()
