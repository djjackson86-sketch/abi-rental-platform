from app.db import get_db, now
from app.services.access import order_branch_clause, product_branch_clause, session_branch_scope_ids
from app.services.settings import global_tax_profile_id
# Trailer identity is stored exactly like a scanned customer vehicle's, so the two
# sides of a disc scan agree on one shape: the same plate typed on the inventory
# form and read off a disc must compare equal ("NB 72 XMGP" == "NB72XMGP"). The
# normalisers live with the vehicles model (feature A) and are reused here rather
# than copied, because two spellings of "same plate" is how a plate ends up on two
# trailers — which would make every scan ambiguous.
from app.services.vehicles import normalise_registration, registration_key

VALID_TYPES = {"rental", "sale", "service"}
VALID_UNITS = {"hour", "day", "week", "month", "fixed"}
VALID_TRACKING_METHODS = {"bulk", "individual", "none"}

TRACKING_LABELS = {
    "individual": "Track individually",
    "none": "Don\u2019t track quantities",
}


#: Wheel sizes a rental trailer can run (ticket ABI-341953033). The client gave
#: the list as 10” - 4H … 14” - 6H (inch mark); it is stored with a plain ASCII
#: quote so the value is identical everywhere it is written or compared — the
#: product form, the dashboard spare wheel panel and the spare_wheel_counts rows.
WHEEL_SIZES = (
    '10" - 4H',
    '13" - 4H',
    '13" - 5H',
    '14" - 5H',
    '14" - 6H',
)


def wheel_size_label(value):
    """The stored wheel size, or an em dash when a product has none."""
    text = str(value or "").strip()
    return text if text in WHEEL_SIZES else "\u2014"


def clean_wheel_size(value):
    """A submitted wheel size, or '' — anything outside the client's list is dropped."""
    text = str(value or "").strip()
    return text if text in WHEEL_SIZES else ""


def tracking_label(value):
    """customer-facing name of a tracking method ('none' = never blocks a booking)."""
    return TRACKING_LABELS.get(value, "Track quantities")


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


def list_products(query="", product_type="", visibility="", product_group_id="", branch_id=None):
    """Every product the session may see, narrowed by the screen's filters.

    ``branch_id`` is the inventory page's Branch filter. It is handed straight to
    ``product_branch_clause`` so the session scope stays authoritative: a filter
    can only ever narrow what a branch-limited account sees, never widen it.
    """
    sql = """SELECT p.*, t.name AS tax_name, t.rate AS tax_rate, b.name AS branch_name,
        g.name AS product_group_name, g.description AS product_group_description, g.sort_order AS product_group_sort_order
        FROM products p
        LEFT JOIN tax_profiles t ON p.tax_profile_id = t.id
        LEFT JOIN branches b ON b.id = p.branch_id
        LEFT JOIN product_groups g ON g.id = p.product_group_id
        WHERE 1=1"""
    params = []
    branch_sql, branch_params = product_branch_clause("p", include_unassigned=True, branch_id=branch_id)
    sql += branch_sql
    params.extend(branch_params)
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


def _quantity_from_form(form):
    try:
        return max(0, int(form.get("quantity") or 0))
    except (TypeError, ValueError):
        return 0


BRANCH_QTY_PREFIX = "qty_branch_"


def _branch_counts_from_form(form):
    """Read the per-branch stock inputs (``qty_branch_<branch id>``).

    Returns ``(counts, present)`` where ``counts`` maps branch id -> whole
    quantity for every box that was filled in, and ``present`` is True when the
    form carried the per-branch inputs at all. A blank box means "not
    configured for that branch" and is skipped; ``0`` is a real count (it gates
    bookings collected from that branch). Negative or non-whole values are
    refused.
    """
    counts = {}
    present = False
    try:
        keys = list(form.keys())
    except AttributeError:
        keys = []
    for key in keys:
        key = str(key)
        if not key.startswith(BRANCH_QTY_PREFIX):
            continue
        present = True
        suffix = key[len(BRANCH_QTY_PREFIX):]
        try:
            branch_id = int(suffix)
        except (TypeError, ValueError):
            continue
        if branch_id <= 0:
            continue
        raw = form.get(key)
        raw = "" if raw is None else str(raw).strip()
        if raw == "":
            continue
        try:
            amount = float(raw)
        except (TypeError, ValueError):
            raise ValueError("Stock counts must be whole numbers")
        if amount != int(amount):
            raise ValueError("Stock counts must be whole numbers")
        amount = int(amount)
        if amount < 0:
            raise ValueError("Stock counts cannot be negative")
        counts[branch_id] = amount
    return counts, present


def product_branch_stock(product_id):
    """{branch_id: quantity} rows for a product — empty when it is not split."""
    rows = get_db().execute(
        "SELECT branch_id, quantity FROM product_branch_stock WHERE product_id = ? ORDER BY branch_id",
        (product_id,),
    ).fetchall()
    return {int(row["branch_id"]): int(row["quantity"] or 0) for row in rows}


def stock_breakdown_rows(product_ids=None):
    """{product_id: [{'branch_name','quantity'}, ...]} for the inventory list/CSV."""
    sql = """SELECT s.product_id, s.branch_id, s.quantity,
            COALESCE(b.name, 'Branch ' || s.branch_id) AS branch_name
        FROM product_branch_stock s
        LEFT JOIN branches b ON b.id = s.branch_id"""
    params = []
    if product_ids is not None:
        ids = [int(pid) for pid in product_ids]
        if not ids:
            return {}
        sql += " WHERE s.product_id IN (%s)" % ",".join("?" for _ in ids)
        params.extend(ids)
    sql += " ORDER BY branch_name, s.branch_id"
    breakdown = {}
    for row in get_db().execute(sql, params).fetchall():
        breakdown.setdefault(int(row["product_id"]), []).append({
            "branch_id": int(row["branch_id"]),
            "branch_name": row["branch_name"],
            "quantity": int(row["quantity"] or 0),
        })
    return breakdown


def format_stock_breakdown(rows):
    """One-line 'Midrand 5 · Wonderboom 2' summary of a product's branch stock."""
    return " \u00b7 ".join(f"{row['branch_name']} {row['quantity']}" for row in (rows or []))


#: Order statuses that mean a unit is physically out or promised. "Picked up"
#: is the live collection stage (``started``); "reserved" is a booking that has
#: not been collected yet. Everything else (draft, sales/repairs, returned,
#: canceled, archived) holds no live unit.
LIVE_RENTAL_STATUSES = ("started", "reserved")


def live_rental_status(product_ids, branch_id=None):
    """{product_id: {'picked_up': n, 'reserved': n}} live rented units.

    Counts the ordered quantity still held by an open booking — orders in
    ``started`` (picked up) or ``reserved`` — for every product in one query.
    Branch scoping reuses the shared ``order_branch_clause``, so a
    branch-limited session (or the inventory branch filter) can only ever
    narrow the figures, never widen them. One query for the whole page.
    """
    ids = [int(pid) for pid in (product_ids or [])]
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    status_marks = ",".join("?" for _ in LIVE_RENTAL_STATUSES)
    sql = f"""
        SELECT oi.product_id AS product_id, o.status AS status,
            COALESCE(SUM(oi.quantity), 0) AS quantity
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        WHERE oi.product_id IN ({marks}) AND o.status IN ({status_marks})
    """
    params = [*ids, *LIVE_RENTAL_STATUSES]
    scope_sql, scope_params = order_branch_clause("o", branch_id=branch_id)
    sql += scope_sql
    params.extend(scope_params)
    sql += " GROUP BY oi.product_id, o.status"
    live = {}
    for row in get_db().execute(sql, params).fetchall():
        entry = live.setdefault(int(row["product_id"]), {"picked_up": 0, "reserved": 0})
        if row["status"] == "started":
            entry["picked_up"] += int(row["quantity"] or 0)
        else:
            entry["reserved"] += int(row["quantity"] or 0)
    return live


def live_rental_status_label(entry):
    """'Picked up 2 · Reserved 1' for the inventory status column, or '—'."""
    if not entry:
        return "\u2014"
    parts = []
    if entry.get("picked_up"):
        parts.append(f"Picked up {entry['picked_up']}")
    if entry.get("reserved"):
        parts.append(f"Reserved {entry['reserved']}")
    return " \u00b7 ".join(parts) if parts else "\u2014"


def set_product_branch_stock(product_id, counts, restrict_branch_id=None):
    """Replace a product's per-branch counts and keep products.quantity the total.

    ``counts`` maps branch id -> whole quantity; an empty mapping clears the
    split and returns the product to the single shared pool. ``restrict_branch_id``
    narrows the write to the branches a session may touch — a single id or a list
    of ids — and preserves every other branch's row. That is the server-side guard
    for branch-limited staff, so a scoped user can only ever edit their own
    depot's count, whatever the form posts.
    """
    db = get_db()
    cleaned = {}
    for branch_id, amount in (counts or {}).items():
        try:
            branch_id = int(branch_id)
            amount = int(amount)
        except (TypeError, ValueError):
            continue
        if branch_id > 0 and amount >= 0:
            cleaned[branch_id] = amount
    allowed = []
    if restrict_branch_id:
        raw = restrict_branch_id if isinstance(restrict_branch_id, (list, tuple, set)) else [restrict_branch_id]
        for value in raw:
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if value and value not in allowed:
                allowed.append(value)
        if not allowed:
            # A scoped caller with no usable branch id must never fall through to
            # the unrestricted branch and clear every other depot's rows.
            return {}
    if allowed:
        cleaned = {b: q for b, q in cleaned.items() if b in allowed}
        marks = ",".join("?" for _ in allowed)
        db.execute(
            f"DELETE FROM product_branch_stock WHERE product_id = ? AND branch_id IN ({marks})",
            [product_id, *allowed],
        )
    else:
        db.execute("DELETE FROM product_branch_stock WHERE product_id = ?", (product_id,))
    timestamp = now()
    for branch_id, amount in sorted(cleaned.items()):
        db.execute(
            """INSERT INTO product_branch_stock (product_id, branch_id, quantity, updated_at)
            VALUES (?, ?, ?, ?)""",
            (product_id, branch_id, amount, timestamp),
        )
    if cleaned or not allowed:
        # products.quantity stays the computed total of the branch rows.
        total = sum(product_branch_stock(product_id).values())
        db.execute("UPDATE products SET quantity = ? WHERE id = ?", (total, product_id))
    db.commit()
    return dict(cleaned)


#: The three identifiers a NaTIS licence disc carries, in the order the inventory
#: form asks for them. Column meanings are decision D3 and are byte-identical to
#: the ``vehicles`` model (feature A) so a disc scanned on one side matches a
#: trailer typed on the other: registration = number plate, registration_number =
#: NaTIS number, licence_number = the disc's own licence number.
TRAILER_IDENTITY_FIELDS = ("registration", "licence_number", "registration_number")

#: Hidden marker the identification panel posts (the ``maintenance_panel`` trick).
#: It tells a real inventory post from a caller that never carried the fields, so
#: a sale/service save, an import or a legacy post can never blank a trailer's
#: plate.
TRAILER_IDENTITY_MARKER = "trailer_identity_panel"


def _stored(value, field):
    """Read ``field`` from a stored product row/dict, or '' when absent."""
    try:
        return value[field]
    except (KeyError, IndexError, TypeError):
        return ""


def trailer_identity_from_form(form, existing=None):
    """The disc identifiers a post carries, or the ones already stored.

    Normalised on the way in (upper case, inner whitespace collapsed) so one plate
    has exactly one stored shape and the partial unique index is a real
    constraint. A post that never carried the panel keeps every stored value.
    """
    try:
        keys = {str(key) for key in form.keys()}
    except AttributeError:
        keys = set()
    panel_posted = TRAILER_IDENTITY_MARKER in keys
    identity = {}
    for field in TRAILER_IDENTITY_FIELDS:
        if field in keys:
            identity[field] = normalise_registration(form.get(field))
        elif panel_posted:
            identity[field] = ""
        else:
            identity[field] = normalise_registration(_stored(existing, field))
    return identity


def trailer_registration_owner(registration, exclude_product_id=None):
    """The OTHER product already carrying this plate, or None when it is free.

    Matched on the whitespace-free upper-case key, so spacing and case cannot put
    one plate on two trailers — which would make every disc scan ambiguous.
    """
    key = registration_key(registration)
    if not key:
        return None
    sql = "SELECT * FROM products WHERE REPLACE(UPPER(registration), ' ', '') = ?"
    params = [key]
    if exclude_product_id is not None:
        sql += " AND id <> ?"
        params.append(exclude_product_id)
    return get_db().execute(sql + " ORDER BY id LIMIT 1", params).fetchone()


def _assert_trailer_registration_is_free(registration, exclude_product_id=None):
    owner = trailer_registration_owner(registration, exclude_product_id)
    if owner is not None:
        plate = normalise_registration(registration)
        raise ValueError(
            f"Trailer {plate} is already recorded on {owner['name']} — open that "
            "trailer and edit it instead of adding the same plate a second time"
        )


def _clean(form, existing_quantity=None, existing_wheel_size=None, existing_under_maintenance=None,
           existing_trailer_identity=None):
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
    branch_id = int(form.get("branch_id") or 0) or None
    # Wheel size (rental trailers only, ticket ABI-341953033). The form field is
    # hidden and disabled for anything that is not a rental, so a post that does
    # not carry the field at all keeps whatever is stored rather than wiping it —
    # the same guard the product type/tracking method change uses.
    try:
        form_keys = {str(key) for key in form.keys()}
    except AttributeError:
        form_keys = set()
    if "wheel_size" in form_keys:
        wheel_size = clean_wheel_size(form.get("wheel_size"))
    else:
        wheel_size = clean_wheel_size(existing_wheel_size)

    # "Trailer under maintenance" (ticket ABI-341953038(3)). A checkbox only
    # submits when it is TICKED, so the panel carries a hidden `maintenance_panel`
    # marker (the same trick the customer-blocking panel uses) to tell a real
    # post from a caller that never carried the field — a sale/service product, or
    # a legacy/API caller. Without the marker the stored flag is kept, so nothing
    # can silently release a flagged trailer.
    if "maintenance_panel" in form_keys or "under_maintenance" in form_keys:
        under_maintenance = 1 if form.get("under_maintenance") else 0
    else:
        under_maintenance = 1 if existing_under_maintenance else 0

    # Trailer identity (programme phase 5 / feature D): the plate / licence number /
    # NaTIS number that a scanned licence disc is matched against when staff return
    # a rental. Rental trailers only — the panel is hidden and disabled for anything
    # else — and its marker decides whether a post may change those three fields, so
    # a sale/service save or a legacy caller can never blank a recorded plate.
    identity = trailer_identity_from_form(form, existing_trailer_identity)

    # Services and untracked products keep no stock count at all.
    untracked = product_type == "service" or tracking_method == "none"
    branch_counts, branch_form_present = _branch_counts_from_form(form)
    if untracked:
        quantity = 0
        branch_counts = {}
    elif branch_counts:
        quantity = sum(branch_counts.values())
    elif branch_form_present:
        # Every per-branch box was left blank: the product keeps one shared
        # pool. Never silently zero an existing count — keep the stored total,
        # or the previous default of 1 for a brand-new product.
        quantity = _quantity_from_form(form) if "quantity" in list(form.keys()) else (
            max(0, int(existing_quantity)) if existing_quantity is not None else 1
        )
    else:
        quantity = _quantity_from_form(form)
    return {
        "name": name,
        "product_type": product_type,
        "tracking_method": tracking_method,
        "wheel_size": wheel_size,
        "under_maintenance": under_maintenance,
        "registration": identity["registration"],
        "licence_number": identity["licence_number"],
        "registration_number": identity["registration_number"],
        "description": form.get("description", "").strip(),
        "sku": form.get("sku", "").strip(),
        "active": 1 if form.get("active") else 0,
        "public_visible": 1 if form.get("public_visible") else 0,
        "price_amount": float(form.get("price_amount") or 0),
        "price_unit": price_unit,
        "security_deposit": float(form.get("security_deposit") or 0),
        "hourly_extra_rate": float(form.get("hourly_extra_rate") or 0),
        # VAT is global: every product uses the one profile, never a per-product choice
        "tax_profile_id": global_tax_profile_id(),
        "product_group_id": product_group_id,
        "quantity": quantity,
        "branch_id": branch_id,
        "branch_counts": branch_counts,
        "branch_form_present": branch_form_present,
        "untracked": untracked,
    }


def create_product(form):
    data = _clean(form)
    branch_counts = data.pop("branch_counts")
    data.pop("branch_form_present")
    data.pop("untracked")
    # One plate belongs to one trailer (programme phase 5 / feature D): a second
    # product claiming it would make every disc scan ambiguous, so it is refused
    # with the owning trailer named — the partial unique index is the backstop.
    _assert_trailer_registration_is_free(data["registration"])
    db = get_db()
    cur = db.execute(
        """INSERT INTO products
        (name, product_type, tracking_method, wheel_size, under_maintenance, registration, licence_number, registration_number, description, sku, active, public_visible, price_amount, price_unit, security_deposit, hourly_extra_rate, tax_profile_id, product_group_id, quantity, branch_id, created_at)
        VALUES (:name, :product_type, :tracking_method, :wheel_size, :under_maintenance, :registration, :licence_number, :registration_number, :description, :sku, :active, :public_visible, :price_amount, :price_unit, :security_deposit, :hourly_extra_rate, :tax_profile_id, :product_group_id, :quantity, :branch_id, :created_at)""",
        {**data, "created_at": now()},
    )
    db.commit()
    product_id = cur.lastrowid
    if branch_counts:
        set_product_branch_stock(
            product_id, branch_counts, restrict_branch_id=session_branch_scope_ids()
        )
    return product_id


def update_product(product_id, form):
    """Save a product.

    Product type and tracking method may only change while the product has never
    been used on an order — the same guard that protects permanent delete. Once an
    order (and therefore a quote or invoice) references the product, those two
    fields stay locked, and the blocked attempt is reported back to the route.

    The guard lives here, in the service layer: hiding or disabling the form
    inputs is presentation, not a permission boundary.
    """
    existing = get_product(product_id)
    data = _clean(
        form,
        existing_quantity=(existing["quantity"] if existing else None),
        existing_wheel_size=(existing["wheel_size"] if existing else None),
        existing_under_maintenance=(existing["under_maintenance"] if existing else None),
        existing_trailer_identity=existing,
    )
    # A plate moved onto a second trailer is refused (and the trailer that holds it
    # is named) — excluding this row, so re-saving a trailer is never a clash with
    # itself.
    _assert_trailer_registration_is_free(data["registration"], exclude_product_id=product_id)
    branch_counts = data.pop("branch_counts")
    branch_form_present = data.pop("branch_form_present")
    untracked = data.pop("untracked")
    blocked_change = False
    if existing:
        blocked_change = (
            data["product_type"] != existing["product_type"]
            or data["tracking_method"] != existing["tracking_method"]
        )
        if blocked_change and product_order_item_count(product_id) > 0:
            data["product_type"] = existing["product_type"]
            data["tracking_method"] = existing["tracking_method"]
            # _clean() zeroes the quantity of an untracked product; re-read the
            # submitted count so a refused change cannot wipe a tracked stock count.
            untracked = existing["product_type"] == "service" or existing["tracking_method"] == "none"
            data["quantity"] = 0 if untracked else _submitted_quantity(form, existing["quantity"])
        else:
            blocked_change = False
    # Per-branch counts are written before the product row so the total stored in
    # products.quantity is the one we intend: the sum of the rows for a split
    # product, or the single shared pool when every branch box was left blank.
    scope = session_branch_scope_ids()
    if untracked:
        set_product_branch_stock(product_id, {})
        data["quantity"] = 0
    elif branch_counts:
        set_product_branch_stock(product_id, branch_counts, restrict_branch_id=scope)
        data["quantity"] = sum(product_branch_stock(product_id).values())
    elif branch_form_present:
        set_product_branch_stock(product_id, {}, restrict_branch_id=scope)
        remaining = product_branch_stock(product_id)
        if remaining:
            # A branch-limited user only cleared their own depot's row, so the
            # shared total stays the sum of the rows that are left.
            data["quantity"] = sum(remaining.values())
    else:
        # No per-branch boxes in this post (a legacy/API caller) but the product
        # is split: the total must stay the sum of the rows, never the posted box.
        existing_rows = product_branch_stock(product_id)
        if existing_rows:
            data["quantity"] = sum(existing_rows.values())
    data["id"] = product_id
    get_db().execute(
        """UPDATE products SET
        name=:name, product_type=:product_type, tracking_method=:tracking_method, wheel_size=:wheel_size, under_maintenance=:under_maintenance, registration=:registration, licence_number=:licence_number, registration_number=:registration_number, description=:description, sku=:sku, active=:active, public_visible=:public_visible,
        price_amount=:price_amount, price_unit=:price_unit, security_deposit=:security_deposit, hourly_extra_rate=:hourly_extra_rate, tax_profile_id=:tax_profile_id, product_group_id=:product_group_id, quantity=:quantity, branch_id=:branch_id
        WHERE id=:id""",
        data,
    )
    get_db().commit()
    return blocked_change


def _submitted_quantity(form, fallback):
    """Total a form asks for: the sum of any per-branch boxes, else the legacy box."""
    branch_counts, present = _branch_counts_from_form(form)
    if branch_counts:
        return sum(branch_counts.values())
    if present:
        return max(0, int(fallback or 0))
    return _quantity_from_form(form)


def archive_product(product_id):
    get_db().execute("UPDATE products SET active = 0, public_visible = 0 WHERE id = ?", (product_id,))
    get_db().commit()


def product_order_item_count(product_id):
    """How many order lines reference this product."""
    row = get_db().execute(
        "SELECT COUNT(*) AS c FROM order_items WHERE product_id = ?", (product_id,)
    ).fetchone()
    return int(row["c"] or 0)


def product_has_order_history(product_id):
    """True when an order (and therefore a quote/invoice) references the product."""
    return product_order_item_count(product_id) > 0


def duplicate_product(product_id):
    """Copy a product into a brand-new row (ABI-341952945).

    Additive by design: nothing on the source product changes. The copy is named
    "<name> (copy)", keeps the type, tracking method, description, pricing,
    deposit, hourly rate, group, branch and visibility, and inherits the source's
    stock — one shared count (``quantity``) and any per-branch counts.

    Two fields are deliberately NOT copied:

    * ``sku`` is blanked. A SKU identifies a real item, so two rows sharing one
      would be ambiguous in the fleet list and in every order/document lookup.
    * ``source_system`` / ``source_id`` are left empty, so the copy is never
      mistaken for an imported Booqable record (and cannot collide with the
      partial unique index on those columns).

    ``under_maintenance`` is also deliberately NOT copied (ticket
    ABI-341953038(3)): the flag means one physical trailer is off the road, and
    the copy is a different row. A duplicate therefore starts available; only the
    trailer the client actually ticked stays blocked.

    The trailer identity (plate / licence number / NaTIS number, programme phase 5)
    is NOT copied either, for the same reason: those three values identify one
    physical trailer (``idx_products_registration`` allows a plate on exactly one
    row), and the copy is a different trailer that has not been registered yet.

    Per-branch counts are written through ``set_product_branch_stock`` with the
    session's branch scope, so branch-limited staff cannot create another depot's
    stock row. If the source is split, the copy's total is the sum of the rows
    that were actually written, keeping ``products.quantity`` == the row total.
    """
    source = get_product(product_id)
    if not source:
        raise ValueError("Product not found")
    db = get_db()
    data = {
        "name": f"{source['name']} (copy)",
        "product_type": source["product_type"],
        "tracking_method": source["tracking_method"],
        "wheel_size": source["wheel_size"],
        "description": source["description"],
        "sku": "",
        "active": source["active"],
        "public_visible": source["public_visible"],
        "price_amount": source["price_amount"],
        "price_unit": source["price_unit"],
        "security_deposit": source["security_deposit"],
        "hourly_extra_rate": source["hourly_extra_rate"],
        # VAT is global: the copy joins the one profile rather than the source's row.
        "tax_profile_id": global_tax_profile_id(),
        "product_group_id": source["product_group_id"],
        "quantity": source["quantity"],
        "branch_id": source["branch_id"],
        "created_at": now(),
    }
    cur = db.execute(
        """INSERT INTO products
        (name, product_type, tracking_method, wheel_size, description, sku, active, public_visible, price_amount, price_unit, security_deposit, hourly_extra_rate, tax_profile_id, product_group_id, quantity, branch_id, created_at)
        VALUES (:name, :product_type, :tracking_method, :wheel_size, :description, :sku, :active, :public_visible, :price_amount, :price_unit, :security_deposit, :hourly_extra_rate, :tax_profile_id, :product_group_id, :quantity, :branch_id, :created_at)""",
        data,
    )
    db.commit()
    new_id = cur.lastrowid
    rows = product_branch_stock(product_id)
    if rows:
        set_product_branch_stock(new_id, rows, restrict_branch_id=session_branch_scope_ids())
        copied = product_branch_stock(new_id)
        db.execute("UPDATE products SET quantity = ? WHERE id = ?", (sum(copied.values()), new_id))
        db.commit()
    return new_id


def delete_product(product_id):
    """Permanently delete a product that no order has ever used.

    Refuses when the product has order history and points staff at Archive
    instead. That guard is what protects live documents: ``order_items.product_id``
    is ON DELETE SET NULL and the order/document templates plus the invoice PDF
    render ``product_name or custom_name`` (catalogue lines store an empty
    custom_name), so deleting an in-use product would erase the item description
    on that order, its quote and its invoice.
    """
    product = get_product(product_id)
    if not product:
        raise ValueError("Product not found")
    if product_has_order_history(product_id):
        raise ValueError("This product has been used on orders — archive it instead")
    get_db().execute("DELETE FROM products WHERE id = ?", (product_id,))
    get_db().commit()
