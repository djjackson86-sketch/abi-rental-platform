"""Branch enquiries from the online store (rental / purchase / repair).

A branch enquiry is a **request**, never a transaction. The public submit path
writes three things and nothing else:

* one ``shop_enquiries`` row (the request itself),
* its ``shop_enquiry_items`` lines (an inventory *snapshot*: name, SKU and price
  at the moment the customer asked), and
* nothing at all on replay of the same request.

It deliberately touches **no** customer, order, stock, payment, invoice or
document row. A stranger filling in a public form cannot create bookkeeping, and
cannot reserve or decrement a trailer. Turning an enquiry into money is an
explicit staff action (``order_prefill()`` → the normal New order screen →
``link_order()`` after the order is saved by the existing order service).

Two database backends run this code: local sqlite3 and libsql/Turso. Rows are
therefore normalised with ``reports.row_dict`` before any column is read —
``libsql_client.Row`` is a tuple subclass and ``row.keys``/``row["x"]`` differ
between the two.
"""

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta

from flask import current_app, has_request_context, request, session

from app.db import get_db, now, write_transaction
from app.services import access
from app.services.reports import row_dict
from app.services.timezone import local_now_iso

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

ENQUIRY_TYPES = ("rental", "purchase", "repair")

TYPE_LABELS = {
    "rental": "Rental",
    "purchase": "Purchase",
    "repair": "Repair",
}

STATUSES = ("new", "contacted", "quote_sent", "converted", "closed")

STATUS_LABELS = {
    "new": "New",
    "contacted": "Contacted",
    "quote_sent": "Quote sent",
    "converted": "Converted",
    "closed": "Closed",
}

#: The invisible field the public form carries. A real customer leaves it blank.
HONEYPOT_FIELD = "company_website"

#: A line is one product. Quantities are whole units — a trailer enquiry for
#: "1.5 trailers" is a typo, not an order.
MAX_LINE_QUANTITY = 99
MAX_LINES = 20
MAX_NOTE_LENGTH = 2000
MAX_NAME_LENGTH = 120
#: Longest rental an online enquiry may ask for. Longer stays are arranged with
#: the branch directly, so a mistyped year cannot create a 300-year request.
MAX_RENTAL_DAYS = 365

#: One address may raise this many enquiries per window. In-process only (a
#: dict, cleared on restart) and the address itself is never stored — only a
#: salted digest, so there is no log of who asked what.
RATE_LIMIT = 25
RATE_WINDOW_SECONDS = 3600
_RATE_SALT = "abi-shop-enquiries"
_rate_hits = {}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^[0-9+()\-\s]{6,32}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_schema(db):
    """Create this feature's tables. Safe to call on every request/start-up."""
    db.execute(
        """CREATE TABLE IF NOT EXISTS shop_enquiries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_token TEXT NOT NULL UNIQUE,
            request_key TEXT NOT NULL,
            payload_digest TEXT NOT NULL,
            enquiry_type TEXT NOT NULL,
            branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
            customer_name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            start_at TEXT,
            end_at TEXT,
            status TEXT NOT NULL DEFAULT 'new',
            assigned_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            internal_notes TEXT NOT NULL DEFAULT '',
            notice_version TEXT NOT NULL DEFAULT '',
            consent_at TEXT NOT NULL DEFAULT '',
            linked_order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL,
            converted_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(request_key, payload_digest)
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS shop_enquiry_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            enquiry_id INTEGER NOT NULL REFERENCES shop_enquiries(id) ON DELETE CASCADE,
            product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
            product_name TEXT NOT NULL DEFAULT '',
            sku TEXT NOT NULL DEFAULT '',
            unit_price REAL NOT NULL DEFAULT 0,
            quantity INTEGER NOT NULL DEFAULT 1,
            line_total REAL NOT NULL DEFAULT 0
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS shop_enquiry_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            enquiry_id INTEGER NOT NULL REFERENCES shop_enquiries(id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            event_type TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_shop_enquiries_request_key ON shop_enquiries(request_key)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_shop_enquiries_one_order ON shop_enquiries(linked_order_id) WHERE linked_order_id IS NOT NULL")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_shop_enquiries_branch_status "
        "ON shop_enquiries(branch_id, status, created_at)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_shop_enquiries_linked_order "
        "ON shop_enquiries(linked_order_id)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_shop_enquiry_items_enquiry "
        "ON shop_enquiry_items(enquiry_id)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_shop_enquiry_events_enquiry "
        "ON shop_enquiry_events(enquiry_id)"
    )
    db.commit()


def ensure_schema():
    # Schema is migrated once at application startup, not eight remote DDL calls per read.
    return get_db()


# ---------------------------------------------------------------------------
# Rate limiting (in-process, no PII)
# ---------------------------------------------------------------------------

def _rate_key(address):
    return hashlib.sha256(f"{_RATE_SALT}:{address or ''}".encode("utf-8")).hexdigest()[:16]


def allow_submission(address, now_ts=None):
    """True when this address may submit again; records the hit when it may."""
    import time

    moment = time.time() if now_ts is None else float(now_ts)
    key = _rate_key(address)
    window_start = moment - RATE_WINDOW_SECONDS
    if key not in _rate_hits and len(_rate_hits) >= 4096:
        for old_key, old_hits in list(_rate_hits.items()):
            if not old_hits or old_hits[-1] < window_start:
                _rate_hits.pop(old_key, None)
        while len(_rate_hits) >= 4096:
            _rate_hits.pop(next(iter(_rate_hits)))
    hits = [hit for hit in _rate_hits.get(key, []) if hit >= window_start]
    if len(hits) >= RATE_LIMIT:
        _rate_hits[key] = hits
        return False
    hits.append(moment)
    _rate_hits[key] = hits
    return True


def reset_rate_limits():
    """Clear the limiter. Used by tests and by a deliberate release."""
    _rate_hits.clear()


# ---------------------------------------------------------------------------
# Public submission parsing / validation
# ---------------------------------------------------------------------------

def _text(value, limit=200):
    return str(value or "").strip()[:limit]


def _digits(value):
    return re.sub(r"\D", "", str(value or ""))


def _branch_row(branch_id):
    try:
        branch_id = int(branch_id)
    except (TypeError, ValueError):
        return None
    row = get_db().execute(
        "SELECT * FROM branches WHERE id = ? AND active = 1", (branch_id,)
    ).fetchone()
    return row_dict(row) if row else None


def _product_row(product_id):
    try:
        product_id = int(product_id)
    except (TypeError, ValueError):
        return None
    row = get_db().execute(
        """SELECT p.*, g.active AS group_active, g.becomes_store_visible AS group_visible
        FROM products p
        LEFT JOIN product_groups g ON g.id = p.product_group_id
        WHERE p.id = ?""",
        (product_id,),
    ).fetchone()
    return row_dict(row) if row else None


def _product_is_public(product):
    """Active, published, not under maintenance, and in a published category."""
    if not product:
        return False
    if not int(product.get("active") or 0):
        return False
    if not int(product.get("public_visible") or 0):
        return False
    if int(product.get("under_maintenance") or 0):
        return False
    if product.get("product_group_id") is not None:
        if not int(product.get("group_active") or 0):
            return False
        if not int(product.get("group_visible") or 0):
            return False
    return True


def _product_at_branch(product, branch_id):
    """True when the product belongs to the branch or is stocked there."""
    owner = product.get("branch_id")
    if owner is None:
        return True
    try:
        if int(owner) == int(branch_id):
            return True
    except (TypeError, ValueError):
        return False
    row = get_db().execute(
        "SELECT id FROM product_branch_stock WHERE product_id = ? AND branch_id = ?",
        (product["id"], branch_id),
    ).fetchone()
    return row is not None


def _parse_items(form):
    """The requested lines: ``items_json`` when given, else product_id+quantity."""
    if form.get('enquiry_type') == 'repair' and not form.get('product_id') and not form.get('items_json'):
        if len(str(form.get('notes') or '').strip()) < 5:
            raise ValueError('Please describe the repair or service you need.')
        return []
    raw = form.get("items_json")
    entries = None
    if raw is not None and str(raw).strip():
        if len(str(raw)) > 5000:
            raise ValueError('The product list is too long for one enquiry.')
        try:
            parsed = json.loads(str(raw))
        except (TypeError, ValueError):
            raise ValueError("The product list could not be read. Refresh the page and try again.")
        if not isinstance(parsed, list) or not parsed:
            raise ValueError("Add at least one product to this enquiry.")
        entries = parsed
    else:
        product_id = form.get("product_id")
        quantity = form.get("quantity") or 1
        if product_id is None or str(product_id).strip() == "":
            raise ValueError("Choose a product for this enquiry.")
        entries = [{"product_id": product_id, "quantity": quantity}]

    if len(entries) > MAX_LINES:
        raise ValueError(f"An enquiry may list at most {MAX_LINES} products.")

    merged = []
    seen = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("The product list could not be read. Refresh the page and try again.")
        try:
            product_id = int(str(entry.get("product_id")).strip())
        except (TypeError, ValueError):
            raise ValueError("Choose a valid product for every line.")
        raw_quantity = entry.get("quantity", 1)
        try:
            quantity = int(str(raw_quantity).strip())
        except (TypeError, ValueError):
            raise ValueError("Quantities must be whole numbers.")
        if not 1 <= quantity <= MAX_LINE_QUANTITY:
            raise ValueError(f"Quantities must be between 1 and {MAX_LINE_QUANTITY}.")
        if product_id in seen:
            merged[seen[product_id]]["quantity"] += quantity
            if merged[seen[product_id]]["quantity"] > MAX_LINE_QUANTITY:
                raise ValueError(f"Quantities must be between 1 and {MAX_LINE_QUANTITY}.")
        else:
            seen[product_id] = len(merged)
            merged.append({"product_id": product_id, "quantity": quantity})
    return merged


def _parse_datetime_pair(form, enquiry_type, today=None):
    """Return ``(start_at, end_at)`` ISO strings or ``(None, None)``.

    Rentals **must** carry a pickup and return moment, and the pair has to be a
    real forward interval inside the allowed horizon. Purchase and repair
    enquiries do not need dates; if they send a partial pair the request is
    refused rather than silently ignored.
    """
    start_date = _text(form.get("start_date"), 10)
    end_date = _text(form.get("end_date"), 10)
    start_time = _text(form.get("start_time"), 5)
    end_time = _text(form.get("end_time"), 5)

    if not (start_date or end_date or start_time or end_time):
        if enquiry_type == "rental":
            raise ValueError("Choose a pickup date and a return date for a rental enquiry.")
        return None, None

    if not (start_date and end_date):
        raise ValueError("A start date and an end date are both required.")
    if not (_DATE_RE.match(start_date) and _DATE_RE.match(end_date)):
        raise ValueError("Dates must be entered as YYYY-MM-DD.")
    start_time = start_time or "09:00"
    end_time = end_time or "15:00"
    if not (_TIME_RE.match(start_time) and _TIME_RE.match(end_time)):
        raise ValueError("Times must be entered as HH:MM.")

    try:
        start = datetime.strptime(f"{start_date} {start_time}", "%Y-%m-%d %H:%M")
        end = datetime.strptime(f"{end_date} {end_time}", "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError("That date and time combination does not exist.")

    business_today = today or local_now_iso()[:10]
    try:
        today_date = datetime.strptime(business_today, "%Y-%m-%d").date()
    except ValueError:
        today_date = datetime.now().date()
    if start.date() < today_date:
        raise ValueError("The pickup date cannot be in the past.")
    if end <= start:
        raise ValueError("The return date and time must be after the pickup date and time.")
    if end.date() - start.date() > timedelta(days=MAX_RENTAL_DAYS):
        raise ValueError(f"A rental enquiry may cover at most {MAX_RENTAL_DAYS} days.")
    return start.isoformat(), end.isoformat()


def parse_submission(form):
    """Validate a public enquiry and return the normalised payload.

    Raises ``ValueError`` with a customer-facing sentence for anything invalid,
    so the route can show the reason and write nothing at all.
    """
    enquiry_type = _text(form.get("enquiry_type"), 20).lower()
    if enquiry_type not in ENQUIRY_TYPES:
        raise ValueError("Choose whether this is a rental, a purchase or a repair.")

    branch = _branch_row(form.get("branch_id"))
    if branch is None:
        raise ValueError("Choose a branch to send this enquiry to.")

    name = " ".join(_text(form.get("customer_name"), MAX_NAME_LENGTH).split())
    email = _text(form.get("customer_email"), 200).lower()
    phone = _text(form.get("customer_phone"), 32)
    if not name:
        raise ValueError("Please enter your full name.")
    if not _EMAIL_RE.match(email):
        raise ValueError("Please enter a valid email address.")
    if not _PHONE_RE.match(phone) or len(_digits(phone)) < 8:
        raise ValueError("Please enter a valid phone number.")

    items = _parse_items(form)
    for item in items:
        product = _product_row(item["product_id"])
        if not _product_is_public(product):
            raise ValueError("One of the products is no longer available. Please choose another.")
        expected_type = {'rental': 'rental', 'purchase': 'sale', 'repair': 'service'}[enquiry_type]
        if product.get('product_type') != expected_type:
            raise ValueError('Hire, purchase and repair requests must use their own product types. Please use the matching shop page.')
        if not _product_at_branch(product, branch["id"]):
            from app.services.shop_catalogue import compatible_public_unit_ids
            alternative = next((candidate for candidate in (_product_row(pid) for pid in compatible_public_unit_ids(item['product_id'])) if _product_at_branch(candidate, branch['id'])), None)
            if alternative is None:
                raise ValueError("One of the products is not available at that branch.")
            product = alternative
            item['product_id'] = int(product['id'])
        item["product_name"] = product.get("name") or ""
        item["sku"] = product.get("sku") or ""
        item["unit_price"] = float(product.get("price_amount") or 0)

    start_at, end_at = _parse_datetime_pair(form, enquiry_type)
    if start_at:
        from app.services.branches import pickup_hours_error
        from datetime import datetime
        hours_error = pickup_hours_error(int(branch['id']), datetime.fromisoformat(start_at))
        if hours_error:
            raise ValueError(hours_error)
    notes = _text(form.get("notes"), MAX_NOTE_LENGTH)

    return {
        "enquiry_type": enquiry_type,
        "branch_id": int(branch["id"]),
        "customer_name": name,
        "email": email,
        "phone": phone,
        "notes": notes,
        "start_at": start_at,
        "end_at": end_at,
        "items": items,
    }


# ---------------------------------------------------------------------------
# Privacy consent
# ---------------------------------------------------------------------------

def published_notice_version():
    """The notice version in force, or ``None`` when nothing is published.

    An enquiry may not be accepted without a published notice: the acceptance
    would point at wording the customer never saw. Parents render this value
    into the form so the posted ``notice_version`` can be checked for equality.
    """
    try:
        from app.services import consent

        notice = consent.published_notice()
    except Exception:
        return None
    if not notice:
        return None
    row = row_dict(notice)
    return (row.get("notice_version") or "").strip() or None


def consent_error(form):
    """The refusal message when the privacy acceptance is missing, else ``None``."""
    from app.services import consent

    posted_version = _text(form.get("notice_version"), 40)
    current = published_notice_version()
    if current is None:
        return "This form is not open yet. Please contact the branch."
    if posted_version and posted_version != current:
        return "The privacy notice was updated. Reload the page and accept it again."
    if not consent.acceptance_given(form.get("privacy_accepted")):
        return consent.consent_required_error()
    return None


# ---------------------------------------------------------------------------
# Idempotent submit
# ---------------------------------------------------------------------------

def _canonical_digest(payload):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _request_key(form, digest):
    supplied = _text(form.get("request_key"), 128)
    return supplied or f"auto:{digest}"


def _existing_receipt(request_key, digest):
    row = get_db().execute(
        "SELECT id, receipt_token, status FROM shop_enquiries WHERE request_key = ? AND payload_digest = ?",
        (request_key, digest),
    ).fetchone()
    return row_dict(row) if row else None


def _new_receipt_token():
    return secrets.token_urlsafe(24)


def submit_enquiry(form, address="", notify=True):
    """Store one public enquiry and return ``{'receipt_token', 'enquiry_id', 'created'}``.

    Re-submitting the **same** request (same ``request_key`` and same payload)
    returns the original opaque receipt token and writes nothing new. The token
    is what the customer's receipt page shows, so a refresh cannot create a
    second enquiry or a second notification.
    """
    if str(form.get(HONEYPOT_FIELD) or "").strip():
        raise ValueError("We could not accept this request. Please contact the branch.")

    if not allow_submission(address):
        raise ValueError("Too many enquiries from this connection. Please try again later.")

    payload = parse_submission(form)
    # A blocked client must not be able to open a fresh online enquiry either.
    # Same conservative check the public booking form uses (strong email/phone
    # match only), and the same deliberately generic sentence: the private
    # blocking reason is never shown to a public visitor.
    try:
        from app.services import portal_intake

        if portal_intake.blocked_match(
            payload["customer_name"], payload["phone"], payload["email"]
        ):
            raise ValueError(
                "We cannot accept this enquiry online. Please contact the branch directly."
            )
    except ValueError:
        raise
    except Exception:
        # If the block lookup itself fails, fail closed rather than quietly
        # accepting a submission that should have been refused.
        raise ValueError("We cannot accept this enquiry online. Please contact the branch directly.")

    consent_message = consent_error(form)
    if consent_message:
        raise ValueError(consent_message)

    payload["notice_version"] = published_notice_version()
    digest = _canonical_digest(payload)
    request_key = _request_key(form, digest)
    ensure_schema()

    existing = _existing_receipt(request_key, digest)
    collision = get_db().execute('SELECT payload_digest FROM shop_enquiries WHERE request_key = ?', (request_key,)).fetchone()
    if collision and collision['payload_digest'] != digest:
        raise ValueError('This request has already been submitted with different details. Open a fresh enquiry form to send another request.')
    if existing:
        return {
            "receipt_token": existing["receipt_token"],
            "enquiry_id": int(existing["id"]),
            "created": False,
        }

    timestamp = now()
    token = _new_receipt_token()
    created_id = None
    try:
        with write_transaction():
            db = get_db()
            cursor = db.execute(
                """INSERT INTO shop_enquiries
                (receipt_token, request_key, payload_digest, enquiry_type, branch_id,
                 customer_name, email, phone, notes, start_at, end_at, status,
                 notice_version, consent_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?)""",
                (
                    token, request_key, digest, payload["enquiry_type"], payload["branch_id"],
                    payload["customer_name"], payload["email"], payload["phone"],
                    payload["notes"], payload["start_at"], payload["end_at"],
                    payload["notice_version"] or "", timestamp, timestamp, timestamp,
                ),
            )
            created_id = cursor.lastrowid
            for item in payload["items"]:
                quantity = int(item["quantity"])
                unit_price = float(item["unit_price"] or 0)
                db.execute(
                    """INSERT INTO shop_enquiry_items
                    (enquiry_id, product_id, product_name, sku, unit_price, quantity, line_total)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        created_id, item["product_id"], item["product_name"], item["sku"],
                        unit_price, quantity, round(unit_price * quantity, 2),
                    ),
                )
    except Exception:
        # A concurrent identical submission may have won the unique race. The
        # replay is still the same request, so return that receipt instead of
        # an error; anything else is a real failure and must surface.
        replayed = _existing_receipt(request_key, digest)
        if replayed:
            return {
                "receipt_token": replayed["receipt_token"],
                "enquiry_id": int(replayed["id"]),
                "created": False,
            }
        raise

    result = {"receipt_token": token, "enquiry_id": int(created_id), "created": True}
    if notify:
        notify_new_enquiry(created_id)
    return result


def get_receipt(token):
    """The minimal public receipt, or ``None``.

    Deliberately carries **no** name, email, phone or notes: the token is a
    bearer reference that may be reopened on a shared device, so it can only
    ever confirm that the branch received an enquiry.
    """
    token = _text(token, 200)
    if not token:
        return None
    row = get_db().execute(
        """SELECT e.id, e.receipt_token, e.enquiry_type, e.status, e.created_at,
                  e.start_at, e.end_at, b.name AS branch_name
        FROM shop_enquiries e
        LEFT JOIN branches b ON b.id = e.branch_id
        WHERE e.receipt_token = ?""",
        (token,),
    ).fetchone()
    if not row:
        return None
    data = row_dict(row)
    data["type_label"] = TYPE_LABELS.get(data.get("enquiry_type"), "Enquiry")
    data["status_label"] = STATUS_LABELS.get(data.get("status"), "Received")
    return data


# ---------------------------------------------------------------------------
# Staff notifications
# ---------------------------------------------------------------------------

def recipient_user_ids(branch_id):
    """Active staff who explicitly belong to this branch.

    Exactly two grants count: the account's primary ``users.branch_id`` and an
    explicit ``user_branch_access`` row. ``can_view_all_branches`` is **not** a
    grant here — head office sees every branch and would otherwise be pinged for
    every depot's enquiries. The main profile (role ``owner``) is excluded too.
    """
    try:
        branch_id = int(branch_id)
    except (TypeError, ValueError):
        return []
    rows = get_db().execute(
        """SELECT DISTINCT u.id FROM users u
        WHERE u.active = 1
          AND u.role <> 'owner'
          AND (
            u.branch_id = ?
            OR EXISTS (
                SELECT 1 FROM user_branch_access a
                WHERE a.user_id = u.id AND a.branch_id = ?
            )
          )
        ORDER BY u.id""",
        (branch_id, branch_id),
    ).fetchall()
    ids = []
    for row in rows:
        try:
            ids.append(int(row_dict(row)["id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return ids


def notify_new_enquiry(enquiry_id):
    """Push a minimal alert to the selected branch's staff, never raising.

    Payload: a title, the enquiry type and branch label, and the staff URL
    ``/online-store/enquiries/<id>``. No customer name, phone, email or notes —
    the same minimisation rule the portal notifications follow.
    """
    try:
        enquiry = get_enquiry(enquiry_id)
        if not enquiry:
            return None
        recipients = recipient_user_ids(enquiry["branch_id"])
        if not recipients:
            return None
        from app.services import notification_preferences, push_notifications

        # The shop enquiry is the same kind of customer alert as a portal submission,
        # so it honours the one 'Customer notifications' preference. Absence of a row
        # means that type's default (ON), preserving the alerts staff already receive.
        recipients = notification_preferences.enabled_user_ids(
            recipients, notification_preferences.CUSTOMER_NOTIFICATIONS
        )
        if not recipients:
            return None
        payload = {
            "title": "New store enquiry",
            "body": (
                f"A new {TYPE_LABELS.get(enquiry['enquiry_type'], 'store').lower()} enquiry "
                f"for {enquiry.get('branch_name') or 'your branch'}."
            ),
            "url": f"/online-store/enquiries/{int(enquiry_id)}",
        }
        return push_notifications.send_to_users(recipients, payload)
    except Exception:
        # A notification problem must never undo an enquiry the customer sent.
        return None


# ---------------------------------------------------------------------------
# Reading enquiries (staff)
# ---------------------------------------------------------------------------

def _item_rows(enquiry_ids):
    if not enquiry_ids:
        return {}
    marks = ",".join("?" for _ in enquiry_ids)
    rows = get_db().execute(
        f"""SELECT id, enquiry_id, product_id, product_name, sku, unit_price, quantity, line_total
        FROM shop_enquiry_items WHERE enquiry_id IN ({marks}) ORDER BY id""",
        list(enquiry_ids),
    ).fetchall()
    grouped = {}
    for row in rows:
        item = row_dict(row)
        grouped.setdefault(int(item["enquiry_id"]), []).append(item)
    return grouped


def get_enquiry(enquiry_id):
    """One enquiry with its branch name, assignee, lines and audit trail."""
    try:
        enquiry_id = int(enquiry_id)
    except (TypeError, ValueError):
        return None
    ensure_schema()
    row = get_db().execute(
        """SELECT e.*, b.name AS branch_name, b.phone AS branch_phone,
                  u.name AS assigned_name
        FROM shop_enquiries e
        LEFT JOIN branches b ON b.id = e.branch_id
        LEFT JOIN users u ON u.id = e.assigned_user_id
        WHERE e.id = ?""",
        (enquiry_id,),
    ).fetchone()
    if not row:
        return None
    data = row_dict(row)
    data["items"] = _item_rows([enquiry_id]).get(enquiry_id, [])
    data["type_label"] = TYPE_LABELS.get(data.get("enquiry_type"), "Enquiry")
    data["status_label"] = STATUS_LABELS.get(data.get("status"), data.get("status") or "")
    events = get_db().execute(
        """SELECT ev.*, u.name AS user_name FROM shop_enquiry_events ev
        LEFT JOIN users u ON u.id = ev.user_id
        WHERE ev.enquiry_id = ? ORDER BY ev.created_at DESC, ev.id DESC""",
        (enquiry_id,),
    ).fetchall()
    data["events"] = [row_dict(event) for event in events]
    data["units_total"] = sum(int(item.get("quantity") or 0) for item in data["items"])
    return data


def _status_counts(where_sql, params):
    rows = get_db().execute(
        f"SELECT e.status, COUNT(*) AS c FROM shop_enquiries e WHERE {where_sql} GROUP BY e.status",
        params,
    ).fetchall()
    counts = {status: 0 for status in STATUSES}
    for row in rows:
        entry = row_dict(row)
        counts[str(entry.get("status"))] = int(entry.get("c") or 0)
    return counts


def list_enquiries(branch_ids=None, branch_id=None, status="", enquiry_type="", query="", limit=200):
    """Filtered enquiry list.

    ``branch_ids=None`` means every branch; a list means exactly those (an empty
    list is a real restriction to nothing). ``branch_id`` narrows further and is
    only honoured when it is inside ``branch_ids``.
    """
    ensure_schema()
    clauses = []
    params = []
    ids = None
    if branch_id:
        try:
            branch_id = int(branch_id)
        except (TypeError, ValueError):
            branch_id = None
    if branch_ids is not None:
        allowed = [int(value) for value in branch_ids]
        if branch_id and branch_id in allowed:
            ids = [branch_id]
        else:
            ids = allowed
    elif branch_id:
        ids = [branch_id]

    if ids is not None:
        if not ids:
            return [], {status_key: 0 for status_key in STATUSES}, 0
        clauses.append("e.branch_id IN (" + ",".join("?" for _ in ids) + ")")
        params.extend(ids)

    if status in STATUSES:
        clauses.append("e.status = ?")
        params.append(status)
    if enquiry_type in ENQUIRY_TYPES:
        clauses.append("e.enquiry_type = ?")
        params.append(enquiry_type)

    query = _text(query, 100)
    if query:
        like = f"%{query.lower()}%"
        clauses.append(
            "(LOWER(e.customer_name) LIKE ? OR LOWER(e.email) LIKE ? "
            "OR REPLACE(REPLACE(REPLACE(e.phone, ' ', ''), '-', ''), '+', '') LIKE ? "
            "OR LOWER(e.receipt_token) LIKE ?)"
        )
        params.extend([like, like, f"%{_digits(query)}%", like])

    where_sql = " AND ".join(clauses) if clauses else "1=1"
    rows = get_db().execute(
        f"""SELECT e.*, b.name AS branch_name, u.name AS assigned_name,
                   (SELECT COUNT(*) FROM shop_enquiry_items i WHERE i.enquiry_id = e.id) AS line_count,
                   (SELECT COALESCE(SUM(i.quantity), 0) FROM shop_enquiry_items i WHERE i.enquiry_id = e.id) AS units_total
        FROM shop_enquiries e
        LEFT JOIN branches b ON b.id = e.branch_id
        LEFT JOIN users u ON u.id = e.assigned_user_id
        WHERE {where_sql}
        ORDER BY CASE WHEN e.status = 'new' THEN 0 ELSE 1 END, e.created_at DESC, e.id DESC
        LIMIT ?""",
        [*params, max(1, min(int(limit or 200), 500))],
    ).fetchall()
    enquiries = [row_dict(row) for row in rows]
    for enquiry in enquiries:
        enquiry["type_label"] = TYPE_LABELS.get(enquiry.get("enquiry_type"), "Enquiry")
        enquiry["status_label"] = STATUS_LABELS.get(enquiry.get("status"), enquiry.get("status") or "")
    counts = _status_counts(where_sql, params)
    total = sum(counts.values())
    return enquiries, counts, total


def count_new_enquiries(branch_ids=None):
    """How many unopened enquiries the nav badge should show.

    Public contract for the parent's navigation: pass the branch ids the
    signed-in account may see (``None`` = every branch). ``new_enquiry_count``
    below is kept as a readable alias for callers already using that name.
    """
    ensure_schema()
    sql = "SELECT COUNT(*) AS c FROM shop_enquiries WHERE status = 'new'"
    params = []
    if branch_ids is not None:
        ids = [int(value) for value in branch_ids]
        if not ids:
            return 0
        sql += " AND branch_id IN (" + ",".join("?" for _ in ids) + ")"
        params.extend(ids)
    row = get_db().execute(sql, params).fetchone()
    return int(row_dict(row).get("c") or 0) if row else 0


def new_enquiry_count(branch_ids=None):
    """Alias for :func:`count_new_enquiries`."""
    return count_new_enquiries(branch_ids)


def assignable_staff(branch_id):
    """Active staff who may be assigned this branch's enquiries."""
    try:
        branch_id = int(branch_id)
    except (TypeError, ValueError):
        return []
    rows = get_db().execute(
        """SELECT DISTINCT u.id, u.name, u.branch_id FROM users u
        WHERE u.active = 1 AND u.role <> 'owner'
          AND (
            u.branch_id = ?
            OR EXISTS (SELECT 1 FROM user_branch_access a WHERE a.user_id = u.id AND a.branch_id = ?)
          )
        ORDER BY LOWER(u.name), u.id""",
        (branch_id, branch_id),
    ).fetchall()
    return [row_dict(row) for row in rows]


def _event(db, enquiry_id, user_id, event_type, detail=""):
    db.execute(
        """INSERT INTO shop_enquiry_events (enquiry_id, user_id, event_type, detail, created_at)
        VALUES (?, ?, ?, ?, ?)""",
        (enquiry_id, user_id, event_type, _text(detail, MAX_NOTE_LENGTH), now()),
    )


def set_status(enquiry_id, status, user_id):
    """Move an enquiry through the staff workflow. Returns the enquiry or ``None``."""
    if status not in STATUSES:
        raise ValueError("Unknown enquiry status")
    enquiry = get_enquiry(enquiry_id)
    if not enquiry:
        return None
    if enquiry["status"] == status:
        return enquiry
    ensure_schema()
    with write_transaction():
        db = get_db()
        db.execute(
            "UPDATE shop_enquiries SET status = ?, updated_at = ? WHERE id = ?",
            (status, now(), enquiry["id"]),
        )
        _event(
            db, enquiry["id"], user_id, "status_changed",
            f"{STATUS_LABELS.get(enquiry['status'], enquiry['status'])} → {STATUS_LABELS.get(status, status)}",
        )
    return get_enquiry(enquiry["id"])


def assign(enquiry_id, user_id, staff_user_id):
    """Assign (or, with ``user_id=None``, unassign) an enquiry.

    Only an active staff account that actually belongs to the enquiry's branch
    may be assigned; the main profile and all-branch viewers are refused.
    """
    enquiry = get_enquiry(enquiry_id)
    if not enquiry:
        return None
    target = None
    if user_id not in (None, "", "0", 0):
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            raise ValueError("Choose a staff member from this branch")
        allowed = {int(row["id"]) for row in assignable_staff(enquiry["branch_id"])}
        if user_id not in allowed:
            raise ValueError("That account cannot be assigned this branch's enquiries")
        target = user_id
    ensure_schema()
    with write_transaction():
        db = get_db()
        db.execute(
            "UPDATE shop_enquiries SET assigned_user_id = ?, updated_at = ? WHERE id = ?",
            (target, now(), enquiry["id"]),
        )
        detail = "Unassigned" if target is None else f"Assigned to user #{target}"
        _event(db, enquiry["id"], staff_user_id, "assignment", detail)
    return get_enquiry(enquiry["id"])


def add_note(enquiry_id, note, user_id):
    """Append an audited internal note. Never touches customer-facing text."""
    note = _text(note, MAX_NOTE_LENGTH)
    if not note:
        raise ValueError("Type a note before saving")
    enquiry = get_enquiry(enquiry_id)
    if not enquiry:
        return None
    stamp = now()
    existing = (enquiry.get("internal_notes") or "").strip()
    entry = f"[{stamp[:16].replace('T', ' ')}] {note}"
    combined = f"{existing}\n{entry}".strip() if existing else entry
    ensure_schema()
    with write_transaction():
        db = get_db()
        db.execute(
            "UPDATE shop_enquiries SET internal_notes = ?, updated_at = ? WHERE id = ?",
            (combined, stamp, enquiry["id"]),
        )
        _event(db, enquiry["id"], user_id, "note", note)
    return get_enquiry(enquiry["id"])


# ---------------------------------------------------------------------------
# Conversion to the normal order flow
# ---------------------------------------------------------------------------

def _normalise_name(value):
    return " ".join(str(value or "").lower().split())


def resolve_customer(enquiry):
    """Conservatively find the existing client behind an enquiry.

    Returns ``{'customer_id', 'ambiguous'}``. A match is only accepted when
    **exactly one** stored client matches by email (preferred), or by
    digit-normalised phone. Two candidates means ``ambiguous`` — staff choose,
    the app never guesses which person to attach.
    """
    if not enquiry:
        return {"customer_id": None, "ambiguous": False}
    db = get_db()
    email = (enquiry.get("email") or "").strip().lower()
    phone = _digits(enquiry.get("phone"))
    if email:
        rows = db.execute(
            "SELECT id FROM customers WHERE LOWER(email) = ? ORDER BY id LIMIT 5", (email,)
        ).fetchall()
        if len(rows) == 1:
            return {"customer_id": int(row_dict(rows[0])["id"]), "ambiguous": False}
        if len(rows) > 1:
            return {"customer_id": None, "ambiguous": True}
    if phone and len(phone) >= 8:
        rows = db.execute(
            """SELECT id, phone FROM customers
            WHERE REPLACE(REPLACE(REPLACE(phone, ' ', ''), '-', ''), '+', '') LIKE ?
            ORDER BY id LIMIT 5""",
            (f"%{phone[-9:]}",),
        ).fetchall()
        matches = [row_dict(row) for row in rows]
        if len(matches) == 1:
            return {"customer_id": int(matches[0]["id"]), "ambiguous": False}
        if len(matches) > 1:
            return {"customer_id": None, "ambiguous": True}
    return {"customer_id": None, "ambiguous": False}


def create_customer_for_enquiry(enquiry_id, user_id=None):
    """Explicit staff action: create a client from the enquiry's own details.

    This is the **normal** customer capture path — ``customers.create_customer``
    — with nothing approved on the client's behalf: ``client_verified`` stays
    NULL (the existing verification workflow still has to run) and no pending /
    blocked / POPIA check is skipped. The enquiry is *not* marked converted;
    only a saved order does that.
    """
    enquiry = get_enquiry(enquiry_id)
    if not enquiry:
        raise ValueError("Enquiry not found")
    existing = resolve_customer(enquiry)
    if existing["customer_id"]:
        return existing["customer_id"]
    if existing['ambiguous']:
        raise ValueError('More than one customer matches. Select the correct existing customer on the order screen; do not create a duplicate.')
    from app.services.customers import create_customer

    form = {
        "customer_type": "individual",
        "name": enquiry["customer_name"],
        "email": enquiry["email"],
        "phone": enquiry["phone"],
    }
    customer_id = create_customer(form, branch_id=enquiry["branch_id"])
    db = get_db()
    _event(db, enquiry["id"], user_id, "customer_created", f"Customer #{customer_id} created from enquiry")
    db.commit()
    return customer_id


def order_prefill(enquiry_id, customer_id=None):
    """Everything the New order screen needs, without retyping.

    Returns ``None`` for an unknown enquiry. Dates come back as separate
    ``YYYY-MM-DD`` / ``HH:MM`` values because that is what the order form's
    ``datetime-local``-adjacent inputs post.
    """
    enquiry = get_enquiry(enquiry_id)
    if not enquiry:
        return None
    resolved = resolve_customer(enquiry)
    chosen_customer = customer_id or resolved["customer_id"]
    start_at = enquiry.get("start_at") or ""
    end_at = enquiry.get("end_at") or ""
    booking_type = "rental" if enquiry["enquiry_type"] == "rental" else "sale"
    lines = []
    for item in enquiry.get("items") or []:
        lines.append({
            "product_id": item.get("product_id"),
            "product_display": " · ".join(
                part for part in [item.get("product_name") or "", item.get("sku") or ""] if part
            ),
            "custom_name": "",
            "description": "",
            "custom_billing_mode": "rental_day" if enquiry["enquiry_type"] == "rental" else "fixed",
            "custom_unit_price": item.get("unit_price") or "",
            "quantity": item.get("quantity") or 1,
        })
    note = f"Store enquiry #{enquiry['id']} ({enquiry['type_label']})."
    if enquiry.get("notes"):
        note += f" Customer note: {enquiry['notes']}"
    return {
        "enquiry_id": int(enquiry["id"]),
        "customer_id": chosen_customer,
        "customer_display": enquiry.get("customer_name") or "",
        "customer_name": enquiry.get("customer_name") or "",
        "customer_email": enquiry.get("email") or "",
        "customer_phone": enquiry.get("phone") or "",
        "customer_ambiguous": resolved["ambiguous"] and not chosen_customer,
        "collect_branch_id": enquiry["branch_id"],
        "return_branch_id": enquiry["branch_id"],
        "booking_type": booking_type,
        "start_date": start_at[:10] if start_at else "",
        "start_time": start_at[11:16] if start_at else "",
        "end_date": end_at[:10] if end_at else "",
        "end_time": end_at[11:16] if end_at else "",
        "notes": note,
        "lines": lines,
    }


def _conversion_permission(user_id):
    """The signed-in staff account allowed to confirm a conversion.

    Deliberately conservative: there must be a real request session, the DB row
    must still be active (a disabled account cannot convert), and the account
    must hold the online-store module. The order/branch checks happen next to
    the rows themselves.
    """
    if user_id is None and has_request_context():
        try:
            user_id = int(session.get("user_id") or 0) or None
        except (TypeError, ValueError):
            user_id = None
    if not user_id:
        raise PermissionError("A signed-in staff member must confirm this conversion")
    row = get_db().execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
    if not row:
        raise PermissionError("Your account could not be found")
    account = row_dict(row)
    if not int(account.get("active") or 0):
        raise PermissionError("Your account is no longer active")
    if account.get("role") != "owner":
        modules = access.user_module_keys_from_row(account)
        if modules is None:
            settings_row = get_db().execute(
                "SELECT staff_permissions_json FROM company_settings WHERE id = 1"
            ).fetchone()
            modules = access.staff_modules_from_settings(row_dict(settings_row) if settings_row else None)
        if not any(module in (modules or []) for module in ('orders', 'online_store')):
            raise PermissionError("Your account cannot access online-store enquiries")
    return account


def _order_for_conversion(order_id):
    row = get_db().execute(
        """SELECT o.id, o.order_number, o.customer_id, o.collect_branch_id, o.return_branch_id,
                  o.status, c.name AS customer_name, c.email AS customer_email, c.phone AS customer_phone
        FROM orders o LEFT JOIN customers c ON c.id = o.customer_id
        WHERE o.id = ?""",
        (int(order_id),),
    ).fetchone()
    return row_dict(row) if row else None


def link_order(enquiry_id, order_id, user_id=None):
    """Link the saved order back to the enquiry and mark it converted.

    Permission-safe and idempotent:

    * a signed-in, active staff account with the online-store module must
      confirm it, and must be able to see both the enquiry's branch and the
      order (``access.user_can_access_order``);
    * the order must belong to the enquiry's branch and to the same customer
      (matching email or phone — a name alone is not proof);
    * one order may satisfy one enquiry, and one enquiry may point at one order.

    Repeated calls with the same pair are a no-op that returns ``already``.
    Raises ``ValueError`` for a mismatch and ``PermissionError`` for an account
    that may not do this.
    """
    enquiry = get_enquiry(enquiry_id)
    if not enquiry:
        raise ValueError("Enquiry not found")
    try:
        order_id = int(order_id)
    except (TypeError, ValueError):
        raise ValueError("Order not found")
    order = _order_for_conversion(order_id)
    if not order:
        raise ValueError("Order not found")

    account = _conversion_permission(user_id)

    scope = access.session_branch_scope_ids() if has_request_context() else None
    enquiry_branch = int(enquiry["branch_id"])
    if scope is not None and enquiry_branch not in scope:
        raise PermissionError("That enquiry belongs to another branch")
    if has_request_context() and not access.user_can_access_order({
        "collect_branch_id": order.get("collect_branch_id"),
        "return_branch_id": order.get("return_branch_id"),
    }):
        raise PermissionError("That order belongs to another branch")

    order_branches = {
        value for value in (order.get("collect_branch_id"), order.get("return_branch_id"))
        if value is not None
    }
    if enquiry_branch not in {int(value) for value in order_branches}:
        raise ValueError("The order is not for the enquiry's branch")

    enquiry_email = (enquiry.get("email") or "").strip().lower()
    order_email = (order.get("customer_email") or "").strip().lower()
    enquiry_phone = _digits(enquiry.get("phone"))
    order_phone = _digits(order.get("customer_phone"))
    if not order.get("customer_id"):
        raise ValueError("Attach the customer to the order before converting the enquiry")
    customer_row = get_db().execute(
        "SELECT is_blocked FROM customers WHERE id = ?", (order["customer_id"],)
    ).fetchone()
    if customer_row and int(row_dict(customer_row).get("is_blocked") or 0):
        raise ValueError("This client is blocked, so the enquiry cannot be converted")
    if not (
        (enquiry_email and order_email and enquiry_email == order_email)
        or (enquiry_phone and order_phone and enquiry_phone[-9:] == order_phone[-9:])
    ):
        raise ValueError("The order's customer does not match this enquiry")

    if enquiry.get("linked_order_id"):
        if int(enquiry["linked_order_id"]) == order_id:
            return {"enquiry_id": enquiry["id"], "order_id": order_id, "already": True}
        raise ValueError("This enquiry is already linked to another order")
    other = get_db().execute(
        "SELECT id FROM shop_enquiries WHERE linked_order_id = ? AND id <> ?",
        (order_id, enquiry["id"]),
    ).fetchone()
    if other:
        raise ValueError("That order is already linked to another enquiry")

    stamp = now()
    with write_transaction():
        db = get_db()
        db.execute(
            """UPDATE shop_enquiries
            SET linked_order_id = ?, status = 'converted', converted_at = ?, updated_at = ?
            WHERE id = ?""",
            (order_id, stamp, stamp, enquiry["id"]),
        )
        _event(
            db, enquiry["id"], account["id"], "converted",
            f"Linked to order {order.get('order_number') or ('#' + str(order_id))}",
        )
    return {"enquiry_id": enquiry["id"], "order_id": order_id, "already": False}


# ---------------------------------------------------------------------------
# Public form support (the storefront template renders these)
# ---------------------------------------------------------------------------

def public_branch_options():
    rows = get_db().execute(
        "SELECT id, name, city, phone FROM branches WHERE active = 1 ORDER BY name"
    ).fetchall()
    return [row_dict(row) for row in rows]


def public_product_options():
    rows = get_db().execute(
        """SELECT p.id, p.name, p.sku, p.product_type, p.price_amount, p.product_group_id
        FROM products p
        LEFT JOIN product_groups g ON g.id = p.product_group_id
        WHERE p.active = 1 AND p.public_visible = 1
          AND COALESCE(p.under_maintenance, 0) = 0
          AND (p.product_group_id IS NULL OR (g.active = 1 AND g.becomes_store_visible = 1))
        ORDER BY p.name"""
    ).fetchall()
    return [row_dict(row) for row in rows]


def whatsapp_link(phone, text=""):
    """A user-clicked WhatsApp deep link, or ``""`` when there is no number."""
    number = _digits(phone)
    if len(number) < 8:
        return ""
    if number.startswith("0"):
        number = "27" + number[1:]
    link = f"https://wa.me/{number}"
    if text:
        from urllib.parse import quote

        link += "?text=" + quote(text, safe="")
    return link


def call_link(phone):
    number = _text(phone, 32)
    cleaned = re.sub(r"[^0-9+]", "", number)
    return f"tel:{cleaned}" if len(_digits(cleaned)) >= 8 else ""
