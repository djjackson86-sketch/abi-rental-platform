"""Spare wheel count for the dashboard (ticket ABI-341953033).

Two client asks, one small model:

1. a **Wheel size** on a rental inventory item (``products.wheel_size``, chosen
   from the fixed five-value list in ``products.WHEEL_SIZES``);
2. a dashboard **Spare Wheel Count** panel listing those five sizes with
   **Expected**, an **Actual** count typed in by staff, and the difference
   (Actual − Expected) shown as a green tick, a red ``(shortage)`` or a green
   ``(over)``.

Expected counts **one wheel per trailer that has not been picked up**:

    expected(size) = SUM, over the active rental products carrying that wheel
                     size, of max(0, product.quantity - units picked up)

``products.quantity`` is the trailer's stock (one shared pool, or the per-branch
total for a split sales-style item) and "picked up" is the ordered quantity on
orders in the app's live collection stage (``started``) — exactly the figure the
inventory screen prints as *Picked up*. A trailer that is only reserved has not
left the yard, so it stays in Expected; returned, cancelled, draft and
sales/repair orders hold no trailer and count for nothing.

Actual counts are stored per depot per business day (``spare_wheel_counts``,
additive), mirroring the cash-up panel: a selected depot is editable, while the
"All branches" view is a read-only sum across the depots the session may already
see — so one save can never pretend to update several depots at once.

Every function returns plain scalars and dicts. Production rows are libsql
tuples, so no raw row object may leave this module.
"""

from app.db import get_db, now
from app.services.access import order_branch_clause, product_branch_clause, session_branch_scope_ids
from app.services.branches import branch_options
from app.services.cash import today_iso
from app.services.products import WHEEL_SIZES

#: Form field names are positional (``actual_0`` … ``actual_4``) so the inch mark
#: inside a wheel size never has to survive an HTML attribute or a query string.
ACTUAL_FIELD_PREFIX = "actual_"


def _value(row, key, default=None):
    """Read one column off any of the three row shapes this app sees."""
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError, ValueError):
        return default


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def visible_branch_ids():
    """Branch ids this session may count for: its own depots, or every depot.

    A branch-restricted account gets exactly its granted depots (an empty list
    when it is granted none), an all-branch viewer gets every active depot, so
    the aggregate view can never widen what the sign-in may already reach.
    """
    scope = session_branch_scope_ids()
    options = []
    for row in branch_options():
        branch_id = _as_int(_value(row, "id"), 0)
        if branch_id <= 0:
            continue
        if scope is not None and branch_id not in scope:
            continue
        options.append(branch_id)
    return options


def expected_spare_wheels(branch_id=None):
    """{wheel size: wheels on trailers still in the yard} — one query.

    ``branch_id`` is the dashboard's depot filter. The session scope always wins
    (``product_branch_clause``/``order_branch_clause`` can only narrow it), so a
    crafted filter can never reveal another depot's trailers. With no depot
    selected the figure covers every depot the session may see, unassigned
    trailers included, which is the existing inventory convention.
    """
    product_sql, product_params = product_branch_clause("p", include_unassigned=True, branch_id=branch_id)
    scope_sql, scope_params = order_branch_clause("o", branch_id=branch_id)
    sql = f"""
        SELECT p.id AS product_id, p.wheel_size AS wheel_size,
            p.quantity AS quantity, COALESCE(live.picked_up, 0) AS picked_up
        FROM products p
        LEFT JOIN (
            SELECT oi.product_id AS product_id, SUM(oi.quantity) AS picked_up
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            WHERE o.status = ?{scope_sql}
            GROUP BY oi.product_id
        ) live ON live.product_id = p.id
        WHERE p.product_type = 'rental' AND p.active = 1 AND p.wheel_size <> ''{product_sql}
    """
    params = ["started", *scope_params, *product_params]
    totals = {size: 0 for size in WHEEL_SIZES}
    for row in get_db().execute(sql, params).fetchall():
        size = str(_value(row, "wheel_size") or "").strip()
        if size not in WHEEL_SIZES:
            # A stored value outside the client's list is ignored rather than
            # added to a size nobody can count.
            continue
        in_yard = _as_int(_value(row, "quantity")) - _as_int(_value(row, "picked_up"))
        if in_yard > 0:
            totals[size] += in_yard
    return totals


def actual_counts(day, branch_id):
    """{wheel size: counted wheels} for one depot business day (only what was counted)."""
    branch_id = _as_int(branch_id, 0)
    if branch_id <= 0:
        return {}
    rows = get_db().execute(
        """SELECT wheel_size, actual_count FROM spare_wheel_counts
        WHERE branch_id = ? AND business_day = ?""",
        (branch_id, str(day)),
    ).fetchall()
    counts = {}
    for row in rows:
        size = str(_value(row, "wheel_size") or "").strip()
        if size in WHEEL_SIZES:
            counts[size] = _as_int(_value(row, "actual_count"))
    return counts


def aggregated_actual_counts(day, branch_ids=None):
    """{wheel size: counted wheels} summed over several depots (read-only view).

    A size with no row in any of those depots is absent from the result, so the
    panel can say "not counted yet" instead of showing a misleading zero.
    """
    ids = [int(bid) for bid in (branch_ids if branch_ids is not None else visible_branch_ids()) if _as_int(bid, 0) > 0]
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = get_db().execute(
        f"""SELECT wheel_size, SUM(actual_count) AS actual_count FROM spare_wheel_counts
        WHERE business_day = ? AND branch_id IN ({marks})
        GROUP BY wheel_size""",
        [str(day), *ids],
    ).fetchall()
    counts = {}
    for row in rows:
        size = str(_value(row, "wheel_size") or "").strip()
        if size in WHEEL_SIZES:
            counts[size] = _as_int(_value(row, "actual_count"))
    return counts


def variance_reading(expected, actual):
    """``(variance, kind)`` for one size: kind is ok / short / over / none.

    ``none`` means nothing was counted yet, so no difference is claimed.
    """
    if actual is None:
        return None, "none"
    difference = _as_int(actual) - _as_int(expected)
    if difference == 0:
        return 0, "ok"
    return difference, ("short" if difference < 0 else "over")


def spare_wheel_rows(day, branch_id=None, editable=False):
    """One row per wheel size for the dashboard panel, in the ticket's order.

    Each row carries the wheel size, ``expected``, the counted ``actual`` (None
    when that size has not been counted), the ``variance`` and how it should read
    (``ok`` / ``short`` / ``over`` / ``none``), plus a ready-made ``variance_text``
    so the template stays free of arithmetic.
    """
    expected = expected_spare_wheels(branch_id=branch_id if editable else None)
    if editable:
        counted = actual_counts(day, branch_id)
    else:
        counted = aggregated_actual_counts(day)
    rows = []
    for size in WHEEL_SIZES:
        expected_count = _as_int(expected.get(size, 0))
        actual = counted.get(size)
        variance, kind = variance_reading(expected_count, actual)
        if kind == "none":
            variance_text = ""
        elif kind == "ok":
            variance_text = "0"
        elif kind == "short":
            variance_text = f"{variance} (shortage)"
        else:
            variance_text = f"+{variance} (over)"
        rows.append({
            "wheel_size": size,
            "expected": expected_count,
            "actual": actual,
            "counted": actual is not None,
            "variance": variance,
            "kind": kind,
            "variance_text": variance_text,
        })
    return rows


def guard_countable_day(day):
    """Today or an earlier business day only.

    Same rule (and the same business-day definition, ``cash.today_iso``) the
    cash-up day uses, with a message that fits this panel: a crafted hidden
    ``day`` must not be able to park a count in a day that has not happened yet.
    """
    if str(day) > today_iso():
        raise ValueError("Spare wheels can be counted for today or a past day only")
    return day


def parse_actual_count(value, label=""):
    """A posted count: ``None`` for a blank box, else a whole number >= 0.

    Raises ``ValueError`` on anything else so the route can flash a message and
    leave the stored counts untouched.
    """
    text = "" if value is None else str(value).strip()
    if text == "":
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        raise ValueError(f"{label or 'Spare wheel counts'} must be whole numbers")
    if number != int(number):
        raise ValueError(f"{label or 'Spare wheel counts'} must be whole numbers")
    number = int(number)
    if number < 0:
        raise ValueError(f"{label or 'Spare wheel counts'} cannot be negative")
    return number


def actual_counts_from_form(form):
    """{wheel size: count or None} read from the panel's positional inputs.

    Only the five canonical sizes can be addressed, so a crafted form cannot
    invent a wheel size; a blank box means "clear this count".
    """
    values = {}
    for index, size in enumerate(WHEEL_SIZES):
        key = f"{ACTUAL_FIELD_PREFIX}{index}"
        if key not in form:
            continue
        values[size] = parse_actual_count(form.get(key), size)
    return values


def save_actual_counts(day, branch_id, values, user_id=None):
    """Save the counted wheels for one depot business day.

    A size with a number is stored (replacing any earlier count for the same
    depot/day/size); a size submitted blank has its row removed, so a count can
    be cleared again. Returns the number of sizes saved.
    """
    branch_id = _as_int(branch_id, 0)
    if branch_id <= 0:
        raise ValueError("No depot is available for the spare wheel count")
    day = str(day)
    db = get_db()
    timestamp = now()
    saved = 0
    for size, count in (values or {}).items():
        if size not in WHEEL_SIZES:
            continue
        existing = db.execute(
            """SELECT id FROM spare_wheel_counts
            WHERE branch_id = ? AND business_day = ? AND wheel_size = ?""",
            (branch_id, day, size),
        ).fetchone()
        if count is None:
            if existing is not None:
                db.execute("DELETE FROM spare_wheel_counts WHERE id = ?", (_as_int(_value(existing, "id")),))
            continue
        if existing is None:
            db.execute(
                """INSERT INTO spare_wheel_counts
                (branch_id, business_day, wheel_size, actual_count, reported_by_user_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (branch_id, day, size, int(count), user_id, timestamp),
            )
        else:
            db.execute(
                """UPDATE spare_wheel_counts SET actual_count = ?, reported_by_user_id = ?, updated_at = ?
                WHERE id = ?""",
                (int(count), user_id, timestamp, _as_int(_value(existing, "id"))),
            )
        saved += 1
    db.commit()
    return saved
