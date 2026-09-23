"""Scan-to-return matching — programme phase 5 (feature D / D1).

Don's ask (2026-09-23): staff should be able to scan a licence disc — the
**trailer's** or the **towing car's** — and have the matching rental marked
returned on the admin side.

This module is only the *reverse lookup*: a scanned disc's identifiers in, the
open rental(s) they could belong to out, plus the two guards the screen needs.
All the actual return work stays in :mod:`app.services.orders` — the status move
is ``transition_order(order_id, "return")`` (`orders.py:1292`, reachable from
``TRANSITIONS["return"]`` at `orders.py:1133`), and the checklist / deposit /
charging flow stays exactly where it is. Nothing here duplicates a transition.

House rules this module is responsible for:

* **Nothing is guessed.** A scan either resolves to a returnable order, or it
  reports the candidates it *could* be for a human to choose between, or it
  reports nothing at all. There is no "best guess" and no auto-pick.
* **One candidate, one decision.** :func:`match_open_rentals` returns every
  candidate it found with its evidence; the caller must not act when there is
  more than one.
* **Only a picked-up rental can come back.** ``TRANSITIONS["return"]["from"]`` is
  ``{"started"}``, so a draft or reserved order that matches is returned as
  *not returnable yet* rather than offered.
* **Scope is respected.** A branch-limited account only ever matches its own
  depots' rentals (the same session scope every other list uses).
"""

from app.db import get_db
from app.services.access import session_branch_scope_ids
from app.services.orders import STATUS_LABELS, get_order, transition_order
from app.services.timezone import local_now_iso
from app.services.vehicles import fields_from_disc, normalise_registration, registration_key

#: Why an order matched. Order matters: the first is the strongest evidence, and
#: when the same order matches more than one way the strongest is the one kept.
MATCH_TRAILER_PLATE = "trailer_plate"
MATCH_CUSTOMER_VEHICLE_PLATE = "customer_vehicle_plate"
MATCH_VEHICLE_REGISTRATION_NUMBER = "customer_vehicle_registration_number"
MATCH_VEHICLE_VIN = "vehicle_vin"
MATCH_VEHICLE_ENGINE = "vehicle_engine"
MATCH_PRIORITY = (
    MATCH_TRAILER_PLATE,
    MATCH_CUSTOMER_VEHICLE_PLATE,
    MATCH_VEHICLE_REGISTRATION_NUMBER,
    MATCH_VEHICLE_VIN,
    MATCH_VEHICLE_ENGINE,
)

#: What was scanned, recorded on the order by :func:`mark_returned_via_scan`.
SOURCE_TRAILER_DISC = "trailer_disc"
SOURCE_VEHICLE_DISC = "vehicle_disc"

#: Statuses worth offering as a candidate. ``started`` is the only returnable one;
#: the other two are live but not picked up yet, so they are shown as
#: "not returnable yet" instead of being silently dropped.
LIVE_STATUSES = ("draft", "reserved", "started")

#: Product identity columns, in the order a match is preferred (plate first).
_PRODUCT_IDENTITY_FIELDS = ("registration", "licence_number", "registration_number")

#: Vehicle identity columns and the match each one produces.
_VEHICLE_MATCHES = (
    ("registration", MATCH_CUSTOMER_VEHICLE_PLATE),
    ("registration_number", MATCH_VEHICLE_REGISTRATION_NUMBER),
    ("vin", MATCH_VEHICLE_VIN),
    ("engine_number", MATCH_VEHICLE_ENGINE),
)


def scanned_identifiers(parsed):
    """The identifier values a scan carries, via feature A's field mapping.

    ``parse_disc_text()`` returns the **number plate** under ``licence_number``
    (it mirrors the reference TypeScript's ``licenceNumber``), while the disc's own
    licence number arrives as ``disc_licence_number``. :func:`fields_from_disc` is
    the one place that mapping is written down, so it is reused here rather than
    repeated — the A1/A2 trap (plate landing in the licence-number column) cannot
    come back through this module.
    """
    fields = fields_from_disc(parsed or {})
    return {
        "registration": fields["registration"],
        "licence_number": fields["licence_number"],
        "registration_number": fields["registration_number"],
        "vin": fields["vin"],
        "engine_number": fields["engine_number"],
        "make": fields["make"],
        "model": fields["model"],
    }


def _scope_ids(session_scope=None):
    """Branch ids this caller may match against, or None when unrestricted.

    An explicit ``session_scope`` (the tests, or a caller that already resolved it)
    wins; otherwise the live session scope is used, exactly like every other list
    query. ``None`` means every branch, an empty list means none.
    """
    if session_scope is None:
        return session_branch_scope_ids()
    return list(session_scope)


def _in_scope(order, scope_ids):
    if scope_ids is None:
        return True
    for branch_id in (order["collect_branch_id"], order["return_branch_id"]):
        if branch_id is not None and branch_id in scope_ids:
            return True
    return False


def _key_index(ident, names):
    """``{comparison key: (field, raw value)}`` for the identifiers that are set."""
    index = {}
    for name in names:
        value = str(ident.get(name) or "").strip()
        key = registration_key(value)
        if key:
            index.setdefault(key, (name, value))
    return index


def _orders_holding_product(product_id):
    return get_db().execute(
        """SELECT o.id, o.order_number, o.status, o.customer_id, o.collect_branch_id,
                  o.return_branch_id, c.name AS customer_name, p.name AS product_name
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.id
        LEFT JOIN customers c ON c.id = o.customer_id
        LEFT JOIN products p ON p.id = oi.product_id
        WHERE oi.product_id = ? AND o.status IN (?, ?, ?)
        ORDER BY o.id""",
        (product_id, *LIVE_STATUSES),
    ).fetchall()


def _orders_for_customer(customer_id):
    return get_db().execute(
        """SELECT o.id, o.order_number, o.status, o.customer_id, o.collect_branch_id,
                  o.return_branch_id, c.name AS customer_name, '' AS product_name
        FROM orders o
        LEFT JOIN customers c ON c.id = o.customer_id
        WHERE o.customer_id = ? AND o.status IN (?, ?, ?)
        ORDER BY o.id""",
        (customer_id, *LIVE_STATUSES),
    ).fetchall()


def _trailers_matching(keys):
    """Products whose recorded identity matches one of the scanned keys."""
    rows = get_db().execute(
        """SELECT id, name, registration, licence_number, registration_number
        FROM products
        WHERE registration <> '' OR licence_number <> '' OR registration_number <> ''"""
    ).fetchall()
    matches = []
    for row in rows:
        for field in _PRODUCT_IDENTITY_FIELDS:
            value = str(row[field] or "").strip()
            key = registration_key(value)
            if key and key in keys:
                matches.append((row, field, value))
    return matches


def _vehicles_matching(keys):
    """Customer vehicles whose recorded identity matches one of the scanned keys."""
    rows = get_db().execute(
        """SELECT v.id, v.customer_id, v.registration, v.registration_number, v.vin,
                  v.engine_number, c.name AS customer_name
        FROM vehicles v LEFT JOIN customers c ON c.id = v.customer_id"""
    ).fetchall()
    matches = []
    for row in rows:
        for field, matched_on in _VEHICLE_MATCHES:
            value = str(row[field] or "").strip()
            key = registration_key(value)
            if key and key in keys:
                matches.append((row, field, matched_on, value))
    return matches


def _candidate(order, matched_on, evidence, source):
    status = order["status"]
    returnable = status == "started"
    label = STATUS_LABELS.get(status, status)
    reason = ""
    if not returnable:
        reason = (
            f"Order {order['order_number']} is {label} — it must be picked up before "
            "it can be returned"
        )
    return {
        "order_id": int(order["id"]),
        "order_number": order["order_number"],
        "customer_id": order["customer_id"],
        "customer_name": (order["customer_name"] or "").strip(),
        "product_name": (order["product_name"] or "").strip(),
        "status": status,
        "status_label": label,
        "returnable": returnable,
        "matched_on": matched_on,
        "evidence": evidence,
        "source": source,
        "reason": reason,
        "also_matched_on": [],
    }


def match_open_rentals(parsed, session_scope=None):
    """Every open rental a scanned disc could belong to, strongest evidence first.

    Resolution order (decision D9):

    1. **trailer plate** — a ``products`` row whose ``registration`` /
       ``licence_number`` / ``registration_number`` equals the scanned value →
       the live orders holding that product;
    2. **customer vehicle plate** — a ``vehicles`` row from feature A → that
       customer's live orders;
    3. **VIN / engine number** — the same vehicle lookup as a secondary signal.

    The same order matched several ways is returned once, carrying the strongest
    ``matched_on`` and remembering the others in ``also_matched_on``. Candidates
    are ordered returnable-first, then by order id. An unknown disc returns an
    **empty list** — inventing a match is never an option.
    """
    ident = scanned_identifiers(parsed)
    keys = _key_index(ident, ("registration", "licence_number", "registration_number", "vin", "engine_number"))
    scope_ids = _scope_ids(session_scope)
    found = {}

    def remember(order, matched_on, evidence, source):
        candidate = _candidate(order, matched_on, evidence, source)
        if not _in_scope(order, scope_ids):
            return
        previous = found.get(candidate["order_id"])
        if previous is None:
            found[candidate["order_id"]] = candidate
            return
        previous_rank = MATCH_PRIORITY.index(previous["matched_on"])
        if MATCH_PRIORITY.index(matched_on) < previous_rank:
            candidate["also_matched_on"] = previous["also_matched_on"] + [
                {"matched_on": previous["matched_on"], "evidence": previous["evidence"]}
            ]
            found[candidate["order_id"]] = candidate
        else:
            previous["also_matched_on"].append({"matched_on": matched_on, "evidence": evidence})

    for product, field, value in _trailers_matching(keys):
        if not str(ident.get(field) or "").strip():
            # Never match a trailer on a field the scan did not actually carry.
            continue
        for order in _orders_holding_product(int(product["id"])):
            remember(order, MATCH_TRAILER_PLATE, value, SOURCE_TRAILER_DISC)

    for vehicle, field, matched_on, value in _vehicles_matching(keys):
        if not str(ident.get(field) or "").strip():
            continue
        for order in _orders_for_customer(int(vehicle["customer_id"])):
            remember(order, matched_on, value, SOURCE_VEHICLE_DISC)

    return sorted(
        found.values(),
        key=lambda item: (0 if item["returnable"] else 1, item["order_id"]),
    )


def recently_returned(parsed, session_scope=None, limit=3):
    """Returned rentals the scanned identifiers still point at — the "already returned" hint.

    Once an order is ``returned`` it is no longer a live order, so
    :func:`match_open_rentals` correctly stops offering it. Staff scanning the same
    disc a second time then need to be told *why* nothing is on offer, so the
    screen adds this read-only lookup: the same trailer / customer-vehicle identity
    as the matcher uses, but for orders that have already come back. Nothing is
    written and nothing is offered as returnable.

    A returned rental that was **itself** closed by a disc scan is listed first
    (``scanned`` is True and ``returned_at`` carries the scan time), so the
    sentence staff read is "it already came back this way".
    """
    ident = scanned_identifiers(parsed)
    keys = _key_index(
        ident, ("registration", "licence_number", "registration_number", "vin", "engine_number")
    )
    if not keys:
        return []
    scope_ids = _scope_ids(session_scope)
    found = {}

    def remember(row, registration, source):
        if not _in_scope(row, scope_ids):
            return
        hint = {
            "order_id": int(row["id"]),
            "order_number": row["order_number"],
            "customer_name": (row["customer_name"] or "").strip(),
            "registration": (registration or "").strip(),
            "returned_at": str(row["return_scan_at"] or "").strip(),
            "scanned": bool(str(row["return_scan_at"] or "").strip()),
            "scan_registration": str(row["return_scan_registration"] or "").strip(),
            "source": str(row["return_scan_source"] or "").strip(),
        }
        previous = found.get(hint["order_id"])
        if previous is None or (not previous["scanned"] and hint["scanned"]):
            found[hint["order_id"]] = hint

    orders_sql = """
        SELECT o.id, o.order_number, o.status, o.return_scan_at, o.return_scan_registration,
               o.return_scan_source, o.collect_branch_id, o.return_branch_id,
               c.name AS customer_name
        FROM orders o %s
        LEFT JOIN customers c ON c.id = o.customer_id
        WHERE %s AND o.status = 'returned'
        ORDER BY o.id DESC"""
    for product, field, value in _trailers_matching(keys):
        if not str(ident.get(field) or "").strip():
            continue
        rows = get_db().execute(
            orders_sql % ("JOIN order_items oi ON oi.order_id = o.id", "oi.product_id = ?"),
            (int(product["id"]),),
        ).fetchall()
        for row in rows:
            remember(row, row["return_scan_registration"] or value, SOURCE_TRAILER_DISC)
    for vehicle, field, matched_on, value in _vehicles_matching(keys):
        if not str(ident.get(field) or "").strip():
            continue
        rows = get_db().execute(
            orders_sql % ("", "o.customer_id = ?"), (int(vehicle["customer_id"]),)
        ).fetchall()
        for row in rows:
            remember(row, row["return_scan_registration"] or value, SOURCE_VEHICLE_DISC)
    return sorted(
        found.values(),
        key=lambda item: (item["scanned"], item["returned_at"], item["order_id"]),
        reverse=True,
    )[:limit]


def returnable_order(order_id, session_scope=None):
    """The order behind a scan, or a plain ``ValueError`` saying why it cannot come back.

    This is the guard the screen calls before it posts anything: it refuses an
    order that does not exist, is already returned, is not picked up (only
    ``started`` is in ``TRANSITIONS["return"]["from"]``), or belongs to a depot
    this account cannot see.
    """
    order = get_order(order_id)
    if not order:
        raise ValueError("Order not found")
    number = order["order_number"]
    status = order["status"]
    if status == "returned":
        stamp = str(order["return_scan_at"] or "").replace("T", " ").strip()
        if stamp:
            raise ValueError(f"Order {number} is already returned (returned via disc scan on {stamp})")
        raise ValueError(f"Order {number} is already returned — nothing to do")
    if status != "started":
        label = STATUS_LABELS.get(status, status)
        raise ValueError(
            f"Order {number} is {label} — only a picked-up (Started) order can be returned"
        )
    if not _in_scope(order, _scope_ids(session_scope)):
        raise ValueError(
            f"Order {number} belongs to another depot — you cannot return it from here"
        )
    return order


def _scan_identity_for_order(order_id, ident):
    """``(registration, source)`` to record: was this disc the trailer or the car?

    The registration is stored **normalised** (upper case, single spaces — the same
    ``normalise_registration`` every product and vehicle plate goes through), so the
    audit line on the order page reads like every other plate in the app even when
    the disc payload arrived in loose lower case.
    """
    plates = _key_index(ident, _PRODUCT_IDENTITY_FIELDS)
    registration = normalise_registration(
        str(ident.get("registration") or "")
        or str(ident.get("registration_number") or "")
        or str(ident.get("licence_number") or "")
        or str(ident.get("vin") or "")
    )
    if plates:
        rows = get_db().execute(
            """SELECT p.registration, p.licence_number, p.registration_number
            FROM order_items oi JOIN products p ON p.id = oi.product_id
            WHERE oi.order_id = ?""",
            (order_id,),
        ).fetchall()
        for row in rows:
            for field in _PRODUCT_IDENTITY_FIELDS:
                value = str(row[field] or "").strip()
                key = registration_key(value)
                if key and key in plates:
                    return registration or normalise_registration(value), SOURCE_TRAILER_DISC
    return registration, SOURCE_VEHICLE_DISC


def mark_returned_via_scan(order_id, user_id=None, parsed=None, session_scope=None):
    """Mark the scanned rental returned, through the existing return flow.

    Writes the audit trail (``orders.return_scan_at`` / ``return_scan_registration``
    / ``return_scan_source`` / ``return_scan_user_id``) and then calls
    ``transition_order(order_id, "return")`` — the same call the order screen's
    Return button makes, so stock, notifications and the return checklist behave
    identically. No transition logic lives here; if the existing call refuses
    (the invoice or the checklist is not ready), its message is surfaced verbatim
    and **nothing** is written.

    ``session_scope`` is only for a caller that already resolved the branch scope
    (the screen lets the live session resolve it, like every other list).
    """
    parsed = parsed or {}
    order = returnable_order(order_id, session_scope=session_scope)
    ident = scanned_identifiers(parsed)
    registration, source = _scan_identity_for_order(order_id, ident)
    message = transition_order(order_id, "return")
    db = get_db()
    db.execute(
        """UPDATE orders SET return_scan_at = ?, return_scan_registration = ?,
                  return_scan_source = ?, return_scan_user_id = ? WHERE id = ?""",
        (local_now_iso(), registration, source, user_id, order_id),
    )
    db.commit()
    return {
        "order_id": int(order_id),
        "order_number": order["order_number"],
        "registration": registration,
        "source": source,
        "message": message,
    }
