"""Shop catalogue presentation layer for the ABI online store.

The public shop is **not** a second catalogue. Every item a customer can see is
an existing ``products`` row: the same id, the same stock, the same price and
the same deposit that a counter sale, a hire or a repair uses. This module only
adds the marketing copy, the photos and the home-page artwork that a public
listing needs. It never duplicates a product, a price, a tax profile or a stock
count.

Why separate tables
-------------------
``shop_listing_meta`` and ``shop_listing_images`` are keyed by ``products.id``
and hold *presentation only*:

* editing a public title can never rename the product on an invoice;
* a shop editor has no column here that could reprice or re-stock anything —
  price, deposit, quantity, tax and stock simply are not written by this module;
* deleting a product cascades its shop rows away, so nothing is orphaned.

``products.public_visible`` stays the single publishing switch, and
``active`` / ``under_maintenance`` / category visibility decide eligibility
exactly as the public store routes already do. A product nobody has edited
still appears: the fallback for a missing store title is the operational name,
and the fallback for missing copy is the operational description.

Photos live in the database — the same convention as category photos in
``app/services/group_images.py``. Render's filesystem is ephemeral, so a photo
written to ``static/`` would vanish on the next deploy. Bytes are validated,
downscaled and re-encoded before they are stored, and served back through
``/store/media/<id>`` (listing galleries) and ``/store/home-image/<slot>`` (the
three home-page slots).

The three home slots (hire / buy / repairs) are rows in ``shop_home_images``,
keyed by slot name, so replacing art is a data change, not a deploy.

Every read returns plain ``dict`` objects, never driver rows: production runs on
libSQL where a row is a tuple subclass (``row.count`` is a bound method), so a
caller must never read columns off the raw row.
"""

import io
import re

from flask import current_app, has_app_context, has_request_context, url_for
from PIL import Image, ImageOps

from app.db import get_db, now

#: Hard cap on a listing's gallery: a cover plus detail shots.
MAX_IMAGES = 6

#: Generous for a phone photo, small enough that a stray video is refused early.
MAX_UPLOAD_BYTES = 6 * 1024 * 1024

#: Longest edge of a stored photo. ``thumbnail`` scales only down, never up.
MAX_IMAGE_EDGE = 1600

#: Checked before Pillow is asked to decode anything.
ALLOWED_MIME = frozenset({"image/png", "image/jpeg", "image/jpg", "image/webp"})

JPEG_QUALITY = 84
WEBP_QUALITY = 84

MAX_ALT_TEXT = 200
MAX_STORE_TITLE = 120
MAX_STORE_DESCRIPTION = 2000

#: Product types the shop can present. A service line is as much a product row as
#: a trailer, so the shop never invents a type: it filters by the real one.
PRODUCT_TYPES = ("rental", "sale", "service")

#: The three home-page artwork slots, in the order the storefront shows them.
HOME_SLOTS = (
    ("hire", "Trailer hire"),
    ("buy", "Buy a trailer"),
    ("repairs", "Repairs & service"),
)
HOME_SLOT_KEYS = tuple(key for key, _label in HOME_SLOTS)

_MODEL_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")

#: Route literals (not ``url_for``) so a decorated dict can be produced outside a
#: request context — scripts, tests and the storefront preview all need this.
MEDIA_ROUTE = "/store/media/{media_id}"
HOME_IMAGE_ROUTE = "/store/home-image/{slot}"
CATEGORY_IMAGE_ROUTE = "/store/category-image/{group_id}"

SHOP_PATH = "/store"

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS shop_listing_meta (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL UNIQUE REFERENCES products(id) ON DELETE CASCADE,
        store_title TEXT NOT NULL DEFAULT '',
        store_description TEXT NOT NULL DEFAULT '',
        model_key TEXT NOT NULL DEFAULT '',
        featured INTEGER NOT NULL DEFAULT 0,
        store_sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shop_listing_images (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        image_blob BLOB,
        image_mime TEXT NOT NULL DEFAULT '',
        image_filename TEXT NOT NULL DEFAULT '',
        alt_text TEXT NOT NULL DEFAULT '',
        width INTEGER NOT NULL DEFAULT 0,
        height INTEGER NOT NULL DEFAULT 0,
        is_cover INTEGER NOT NULL DEFAULT 0,
        sort_order INTEGER NOT NULL DEFAULT 0,
        version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_shop_listing_images_product
        ON shop_listing_images(product_id, is_cover DESC, sort_order, id)
    """,
    """
    CREATE TABLE IF NOT EXISTS shop_home_images (
        slot TEXT PRIMARY KEY,
        image_blob BLOB,
        image_mime TEXT NOT NULL DEFAULT '',
        image_filename TEXT NOT NULL DEFAULT '',
        width INTEGER NOT NULL DEFAULT 0,
        height INTEGER NOT NULL DEFAULT 0,
        version INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL DEFAULT ''
    )
    """,
)


class ShopImageError(ValueError):
    """A refused photo: wrong type, too large, unreadable, or over the cap."""


class ShopListingError(ValueError):
    """A refused listing save (copy too long, bad model key, unknown product)."""


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


def init_schema(db):
    """Create the shop tables. Idempotent and additive — safe on every boot.

    Called by the app factory inside an app context *after* the core schema, so
    ``REFERENCES products(id)`` always has a target. ``db`` is whatever
    ``get_db()`` returns (sqlite3 or libSQL). Nothing existing is altered, so a
    database that has never seen the shop simply gains three tables.
    """
    for statement in SCHEMA:
        db.execute(statement)
    db.commit()
    return True


# ---------------------------------------------------------------------------
# row helpers (sqlite3.Row and libSQL Row both end up as plain dicts)
# ---------------------------------------------------------------------------


def row_dict(row):
    """Normalise a driver row into a plain dict, or ``None``.

    libSQL rows are tuple subclasses whose ``count``/``index`` names collide with
    ordinary attribute access, so they are never read directly.
    """
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    asdict = getattr(row, "asdict", None)
    if callable(asdict):
        return dict(asdict())
    fields = getattr(row, "_fields", None)
    if fields:
        return {name: row[name] for name in fields}
    keys = getattr(row, "keys", None)
    if callable(keys):
        return dict(row)
    try:
        return dict(row)
    except (TypeError, ValueError):
        return {}


def _first(row, key, default=None):
    """Read one column off a sqlite3/libSQL row without assuming an interface."""
    data = row_dict(row)
    if not data:
        return default
    return data.get(key, default)


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

_SELECT = """
SELECT p.*,
       g.name AS meta_group_name,
       g.active AS meta_group_active,
       g.becomes_store_visible AS meta_group_store_visible,
       g.sort_order AS meta_group_sort_order,
       g.description AS meta_group_description,
       CASE WHEN g.image_blob IS NULL THEN 0 ELSE 1 END AS meta_group_has_image,
       b.name AS meta_branch_name,
       m.id AS meta_id,
       m.store_title AS meta_title,
       m.store_description AS meta_description,
       m.model_key AS meta_model_key,
       m.featured AS meta_featured,
       m.store_sort_order AS meta_sort_order,
       m.updated_at AS meta_updated_at
FROM products p
LEFT JOIN product_groups g ON g.id = p.product_group_id
LEFT JOIN branches b ON b.id = p.branch_id
LEFT JOIN shop_listing_meta m ON m.product_id = p.id
WHERE 1=1
"""

#: Public eligibility: the same meaning as the public store routes (active,
#: published, not under maintenance) plus a visible category. A product whose
#: category is hidden is excluded outright — it is never re-labelled into an
#: "Other" bucket, because that would keep selling something the shop hid.
_PUBLIC_SQL = (
    " AND p.active = 1"
    " AND p.public_visible = 1"
    " AND COALESCE(p.under_maintenance, 0) = 0"
    " AND (p.product_group_id IS NULL"
    "      OR (COALESCE(g.active, 0) = 1 AND COALESCE(g.becomes_store_visible, 0) = 1))"
)

_ORDER_SQL = (
    " ORDER BY COALESCE(m.featured, 0) DESC,"
    " COALESCE(m.store_sort_order, 0) ASC,"
    " COALESCE(g.sort_order, 999999) ASC,"
    " COALESCE(g.name, '') ASC, p.name ASC, p.id ASC"
)


def decorate_product(row):
    """One product plus its shop presentation fields, as a plain dict.

    Every ``products`` column survives untouched — the storefront and the
    checkout both read the same row shape — and only the internal ``meta_*``
    join aliases are consumed. Fallbacks: a listing nobody has edited shows the
    operational name, the operational description and a category photo (if the
    category has one).
    """
    product = row_dict(row) or {}
    group_name = product.pop("meta_group_name", None)
    group_description = product.pop("meta_group_description", "") or ""
    group_has_image = bool(product.pop("meta_group_has_image", 0))
    group_active = product.pop("meta_group_active", None)
    group_store_visible = product.pop("meta_group_store_visible", None)
    group_sort_order = product.pop("meta_group_sort_order", None)
    branch_name = product.pop("meta_branch_name", None)
    title = (product.pop("meta_title", "") or "").strip()
    description = (product.pop("meta_description", "") or "").strip()
    model_key = (product.pop("meta_model_key", "") or "").strip()
    featured = _int(product.pop("meta_featured", 0))
    sort_order = _int(product.pop("meta_sort_order", 0))
    updated_at = product.pop("meta_updated_at", "") or ""

    if not product.get("product_type"):
        # A legacy or imported row can carry a blank type. The storefront picks a
        # card layout from this field, so it always reports a known type; the
        # operational row itself is never rewritten.
        product["product_type"] = "rental"

    category_hidden = bool(
        product.get("product_group_id") is not None
        and not (_int(group_active) and _int(group_store_visible))
    )

    category_image = (
        CATEGORY_IMAGE_ROUTE.format(group_id=int(product["product_group_id"]))
        if product.get("product_group_id") is not None and group_has_image
        else ""
    )

    product.update(
        {
            "store_title": title or product.get("name") or "",
            "store_description": description or product.get("description") or "",
            "store_title_override": title,
            "store_description_override": description,
            "model_key": model_key,
            "featured": featured,
            "store_sort_order": sort_order,
            "category_name": group_name or "",
            "category_description": group_description,
            "category_sort_order": group_sort_order,
            "category_image_url": category_image,
            "image_url": category_image,
            "store_images": [],
            "store_has_meta": product.get("meta_id") is not None,
            "store_updated_at": updated_at,
            "store_category_hidden": category_hidden,
            "branch_name": branch_name or "",
            "store_public": _is_public_row(product, group_active, group_store_visible),
            "store_visibility_issues": _visibility_issues(product, group_active, group_store_visible),
        }
    )
    return product


def _visibility_issues(product, group_active=None, group_store_visible=None):
    issues = []
    if not _int(product.get("active")):
        issues.append("Product is switched off in Inventory")
    if not _int(product.get("public_visible")):
        issues.append("Not published to the shop")
    if _int(product.get("under_maintenance")):
        issues.append("Under maintenance")
    if product.get("product_group_id") is not None and not (
        _int(group_active) and _int(group_store_visible)
    ):
        issues.append("Category is hidden from the shop")
    return issues


def _is_public_row(product, group_active=None, group_store_visible=None):
    if not _int(product.get("active")) or not _int(product.get("public_visible")):
        return False
    if _int(product.get("under_maintenance")):
        return False
    if product.get("product_group_id") is not None and not (
        _int(group_active) and _int(group_store_visible)
    ):
        return False
    return True


def _category_clause(category):
    """Turn a category filter into SQL. Accepts a group id or a group name.

    A name is compared case-insensitively against the real category name, so an
    unknown value simply matches nothing rather than falling back to "Other".
    """
    value = (category or "").strip()
    if not value:
        return "", []
    if value.isdigit():
        return " AND p.product_group_id = ?", [int(value)]
    return " AND LOWER(COALESCE(g.name, '')) = LOWER(?)", [value]


def _query_clause(query):
    value = (query or "").strip()
    if not value:
        return "", []
    needle = f"%{value.lower()}%"
    return (
        " AND (LOWER(COALESCE(NULLIF(m.store_title, ''), p.name)) LIKE ?"
        " OR LOWER(COALESCE(NULLIF(m.store_description, ''), p.description)) LIKE ?)",
        [needle, needle],
    )


def list_public_products(product_type=None, query="", category=""):
    """Public shop items, as plain dicts — the storefront's one read path.

    Only items that are active, published (``products.public_visible``), not
    under maintenance and in a visible category are returned. ``product_type``
    filters on the *real* product type; ``query`` searches the public title and
    copy; ``category`` accepts a category id or name.

    Each dict carries exactly the documented contract keys:
    ``id, name, store_title, store_description, product_type, price_amount,
    price_unit, security_deposit, image_url, category_name, model_units``.
    ``model_units`` lists the separately-stocked units that share an explicitly
    declared ``model_key`` *and* identical compatible metadata. Units are never
    merged: every entry keeps its own product id and name.
    """
    sql = _SELECT + _PUBLIC_SQL
    params = []
    ptype = (product_type or "").strip()
    if ptype in PRODUCT_TYPES:
        sql += " AND p.product_type = ?"
        params.append(ptype)
    clause, clause_params = _query_clause(query)
    sql += clause
    params.extend(clause_params)
    clause, clause_params = _category_clause(category)
    sql += clause
    params.extend(clause_params)
    sql += _ORDER_SQL
    rows = get_db().execute(sql, params).fetchall()
    products = [decorate_product(row) for row in rows]
    _attach_images(products)
    _attach_model_units(products)
    return [_public_card(product) for product in products]


def compatible_public_unit_ids(product_id):
    """Explicit same-model units with compatible commercial settings; no name guessing."""
    row = get_db().execute(_SELECT + _PUBLIC_SQL + ' AND p.id = ?', (int(product_id),)).fetchone()
    if row is None:
        return []
    product = decorate_product(row)
    key = product.get('model_key') or ''
    if not key:
        return [int(product_id)]
    candidates = [decorate_product(r) for r in get_db().execute(_SELECT + _PUBLIC_SQL + ' AND m.model_key = ? ORDER BY p.id', (key,)).fetchall()]
    def signature(p):
        return (p.get('product_type'),p.get('product_group_id'),p.get('price_amount'),p.get('price_unit'),p.get('security_deposit'),p.get('tax_profile_id'))
    return [int(p['id']) for p in candidates if signature(p) == signature(product)]


def _public_card(product):
    return {
        "id": _int(product.get("id")),
        "name": product.get("name") or "",
        "store_title": product.get("store_title") or "",
        "store_description": product.get("store_description") or "",
        "product_type": product.get("product_type") or "rental",
        "price_amount": _float(product.get("price_amount")),
        "price_unit": product.get("price_unit") or "",
        "security_deposit": _float(product.get("security_deposit")),
        "image_url": product.get("image_url") or "",
        "category_name": product.get("category_name") or "",
        "model_units": product.get("model_units") or [],
    }


def get_public_product(product_id):
    """One public item with its full gallery, or ``None`` when it is not live.

    Returns a plain dict of every ``products`` column plus the shop fields
    (``store_title``, ``store_description``, ``image_url``, ``store_images``,
    ``category_name``, ``model_key``, ``model_units``).
    """
    try:
        product_id = int(product_id)
    except (TypeError, ValueError, OverflowError):
        return None
    if product_id < 1:
        return None
    row = get_db().execute(
        _SELECT + _PUBLIC_SQL + " AND p.id = ?", [product_id]
    ).fetchone()
    if row is None:
        return None
    product = decorate_product(row)
    _attach_images([product])
    _attach_model_units([product])
    return product


def editable_products():
    """Every product the session's depot scope allows, publish state ignored.

    Deliberately the same row set the Inventory page shows: the restriction comes
    from ``product_branch_clause``, the helper that page already uses, so nobody
    can reach a product in the shop editor that they cannot see in Inventory.
    Authorisation itself (the ``online_store`` module) is the route's job.
    """
    from app.services.access import product_branch_clause

    branch_sql, branch_params = product_branch_clause("p", include_unassigned=True)
    sql = _SELECT + branch_sql + _ORDER_SQL
    rows = get_db().execute(sql, branch_params).fetchall()
    products = [decorate_product(row) for row in rows]
    _attach_images(products)
    _attach_model_units(products)
    return products


def get_product(product_id):
    """A product's row decorated with its shop fields, publish state ignored."""
    try:
        product_id = int(product_id)
    except (TypeError, ValueError, OverflowError):
        return None
    if product_id < 1:
        return None
    row = get_db().execute(_SELECT + " AND p.id = ?", [product_id]).fetchone()
    if row is None:
        return None
    product = decorate_product(row)
    _attach_images([product])
    _attach_model_units([product])
    return product


def product_is_public(product_id):
    """True when this product is currently eligible to be shown publicly."""
    try:
        product_id = int(product_id)
    except (TypeError, ValueError, OverflowError):
        return False
    row = get_db().execute(
        _SELECT + _PUBLIC_SQL + " AND p.id = ?", [product_id]
    ).fetchone()
    return row is not None


def _attach_images(products):
    """Fill gallery metadata for a page of products in one query, cover first."""
    ids = [_int(product.get("id")) for product in products if product.get("id") is not None]
    if not ids:
        return
    marks = ",".join("?" for _ in ids)
    rows = get_db().execute(
        "SELECT id, product_id, alt_text, is_cover, sort_order, version "
        f"FROM shop_listing_images WHERE product_id IN ({marks}) "
        "ORDER BY is_cover DESC, sort_order ASC, id ASC",
        ids,
    ).fetchall()
    by_product = {}
    for row in rows:
        data = row_dict(row) or {}
        by_product.setdefault(_int(data.get("product_id")), []).append(
            {
                "id": _int(data.get("id")),
                "url": media_url(data.get("id"), data.get("version")),
                "alt": data.get("alt_text") or "",
                "is_cover": bool(_int(data.get("is_cover"))),
            }
        )
    for product in products:
        images = by_product.get(_int(product.get("id")), [])
        product["store_images"] = images
        if images:
            product["image_url"] = images[0]["url"]


def _attach_model_units(products):
    """Group units only where the operator declared compatibility explicitly.

    A unit joins a model group when it carries the same non-empty ``model_key``
    **and** the same operational identity: product type, category, price, price
    unit and deposit. Two physical trailers with the same name but no declared
    key are never merged, and every unit keeps its own product id.
    """
    buckets = {}
    for product in products:
        key = (product.get("model_key") or "").strip()
        if not key:
            continue
        signature = (
            key,
            product.get("product_type") or "",
            product.get("product_group_id"),
            round(_float(product.get("price_amount")), 2),
            product.get("price_unit") or "",
            round(_float(product.get("security_deposit")), 2),
            product.get('tax_profile_id'),
        )
        buckets.setdefault(signature, []).append(product)

    signatures = {}
    for product in products:
        key = (product.get("model_key") or "").strip()
        if not key:
            product["model_units"] = []
            continue
        signature = (
            key,
            product.get("product_type") or "",
            product.get("product_group_id"),
            round(_float(product.get("price_amount")), 2),
            product.get("price_unit") or "",
            round(_float(product.get("security_deposit")), 2),
            product.get('tax_profile_id'),
        )
        signatures[_int(product.get("id"))] = signature

    for product in products:
        signature = signatures.get(_int(product.get("id")))
        if signature is None:
            continue
        units = buckets.get(signature, [])
        product["model_units"] = [
            {
                "id": _int(unit.get("id")),
                "name": unit.get("name") or "",
                "quantity": _int(unit.get("quantity")),
            }
            for unit in units
        ]


def media_url(media_id, version=1):
    """Versioned URL for a gallery photo, so a replacement busts browser caches."""
    return f"{MEDIA_ROUTE.format(media_id=_int(media_id))}?v={_int(version, 1) or 1}"


# ---------------------------------------------------------------------------
# writing: copy, publishing, grouping
# ---------------------------------------------------------------------------


def _product_exists(product_id):
    return get_db().execute("SELECT id FROM products WHERE id = ?", [int(product_id)]).fetchone() is not None


def clean_listing_fields(form):
    """Validate every marketing input *before* anything is written."""
    get = form.get
    title = (get("store_title") or "").strip()
    description = (get("store_description") or "").strip()
    model_key = (get("model_key") or "").strip()

    if len(title) > MAX_STORE_TITLE:
        raise ShopListingError(f"Keep the shop title under {MAX_STORE_TITLE} characters.")
    if len(description) > MAX_STORE_DESCRIPTION:
        raise ShopListingError(
            f"Keep the shop description under {MAX_STORE_DESCRIPTION} characters."
        )
    if model_key and not _MODEL_KEY_RE.match(model_key):
        raise ShopListingError(
            "Model key may only contain letters, numbers, dots, dashes, colons and underscores."
        )
    try:
        sort_order = int(str(get("store_sort_order") or "0").strip() or 0)
    except (TypeError, ValueError):
        raise ShopListingError("Shop order must be a whole number.")
    sort_order = max(-9999, min(9999, sort_order))
    return {
        "store_title": title,
        "store_description": description,
        "model_key": model_key,
        "featured": 1 if get("featured") else 0,
        "store_sort_order": sort_order,
    }


def save_listing(product_id, form):
    """Save a product's shop copy. Never touches price, stock or tax.

    Only ``shop_listing_meta`` is written, plus ``products.public_visible`` when
    the form actually carried the publish control (so a partial post or a script
    cannot hide a live listing by omission). Operational fields are simply not
    read here — that is what makes the editor safe to hand to staff.

    Returns the saved field dict. Raises ``ShopListingError`` before writing.
    """
    product_id = int(product_id)
    if not _product_exists(product_id):
        raise ShopListingError("Product not found")

    fields = clean_listing_fields(form)
    keys = set(form.keys()) if hasattr(form, "keys") else set()
    public_visible = None
    if "public_visible" in keys:
        public_visible = 1 if str(form.get("public_visible")).strip().lower() in {
            "1",
            "true",
            "on",
            "yes",
            "public",
            "published",
        } else 0

    timestamp = now()
    db = get_db()
    existing = db.execute(
        "SELECT id FROM shop_listing_meta WHERE product_id = ?", [product_id]
    ).fetchone()
    if existing is None:
        db.execute(
            "INSERT INTO shop_listing_meta "
            "(product_id, store_title, store_description, model_key, featured, "
            "store_sort_order, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                product_id,
                fields["store_title"],
                fields["store_description"],
                fields["model_key"],
                fields["featured"],
                fields["store_sort_order"],
                timestamp,
                timestamp,
            ],
        )
    else:
        db.execute(
            "UPDATE shop_listing_meta SET store_title = ?, store_description = ?, "
            "model_key = ?, featured = ?, store_sort_order = ?, updated_at = ? "
            "WHERE product_id = ?",
            [
                fields["store_title"],
                fields["store_description"],
                fields["model_key"],
                fields["featured"],
                fields["store_sort_order"],
                timestamp,
                product_id,
            ],
        )
    if public_visible is not None:
        db.execute(
            "UPDATE products SET public_visible = ? WHERE id = ?",
            [public_visible, product_id],
        )
    db.commit()
    fields["public_visible"] = public_visible
    return fields


def set_public(product_id, public):
    """Publish or hide a listing through the one switch the storefront reads."""
    product_id = int(product_id)
    if not _product_exists(product_id):
        raise ShopListingError("Product not found")
    db = get_db()
    db.execute(
        "UPDATE products SET public_visible = ? WHERE id = ?",
        [1 if public else 0, product_id],
    )
    db.commit()
    return bool(public)


# ---------------------------------------------------------------------------
# writing: gallery photos
# ---------------------------------------------------------------------------


def _clean_alt(alt_text, fallback=""):
    text = " ".join(str(alt_text or "").split())
    if not text:
        text = " ".join(str(fallback or "").split())
    return text[:MAX_ALT_TEXT]


def _safe_filename(file_storage):
    raw = (getattr(file_storage, "filename", "") or "").strip()
    return raw.split("/")[-1].split("\\")[-1][:120]


def read_upload(file_storage):
    """Read an upload once, refusing an oversize or empty file before decoding."""
    if file_storage is None or not (getattr(file_storage, "filename", "") or "").strip():
        raise ShopImageError("Choose a photo to upload.")
    data = file_storage.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise ShopImageError("That image file was empty — choose the photo again.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ShopImageError("That photo is larger than 6 MB — choose a smaller one.")
    return data


def _prepare_image(data, source_mime):
    """Validate, orient, downscale and re-encode one photo.

    Nothing is written until this returns, so a refused file leaves the existing
    gallery (or home slot) exactly as it was.
    """
    mime = (source_mime or "").lower()
    if mime and mime not in ALLOWED_MIME:
        raise ShopImageError(f"That file is a {mime} file, not a PNG, JPEG or WebP image.")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
    except Exception:
        raise ShopImageError(
            "That file is not a readable image. Choose a PNG, JPEG or WebP photo."
        )
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception:
        raise ShopImageError(
            "That file is not a readable image. Choose a PNG, JPEG or WebP photo."
        )
    if image.format not in {"PNG", "JPEG", "WEBP"}:
        raise ShopImageError("That file is not a PNG, JPEG or WebP image.")
    # Phone cameras record orientation in EXIF and leave the pixels sideways.
    try:
        image = ImageOps.exif_transpose(image) or image
    except Exception:
        pass
    image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
    output_mime = "image/webp" if mime == "image/webp" else "image/jpeg"
    buffer = io.BytesIO()
    if output_mime == "image/webp":
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA")
        image.save(buffer, format="WEBP", quality=WEBP_QUALITY)
    else:
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue(), output_mime, image.width, image.height


def _image_rows(product_id):
    rows = get_db().execute(
        "SELECT id, product_id, image_mime, alt_text, is_cover, sort_order, version "
        "FROM shop_listing_images WHERE product_id = ? "
        "ORDER BY is_cover DESC, sort_order ASC, id ASC",
        [int(product_id)],
    ).fetchall()
    return [row_dict(row) or {} for row in rows]


def product_images(product_id):
    """Gallery metadata for one listing (no bytes), cover first."""
    return [
        {
            "id": _int(row.get("id")),
            "url": media_url(row.get("id"), row.get("version")),
            "alt": row.get("alt_text") or "",
            "is_cover": bool(_int(row.get("is_cover"))),
            "mime": row.get("image_mime") or "",
            "version": _int(row.get("version"), 1) or 1,
        }
        for row in _image_rows(product_id)
    ]


def add_image(product_id, file_storage, alt_text=""):
    """Add one photo to a listing's gallery; the first photo becomes the cover."""
    product_id = int(product_id)
    product = get_db().execute(
        "SELECT id, name FROM products WHERE id = ?", [product_id]
    ).fetchone()
    if product is None:
        raise ShopImageError("Product not found")
    existing = _image_rows(product_id)
    if len(existing) >= MAX_IMAGES:
        raise ShopImageError(f"A listing can carry at most {MAX_IMAGES} photos. Remove one first.")
    data = read_upload(file_storage)
    stored, mime, width, height = _prepare_image(data, getattr(file_storage, "mimetype", ""))
    is_cover = 0 if existing else 1
    next_order = max([_int(row.get("sort_order")) for row in existing] + [0]) + 1
    timestamp = now()
    db = get_db()
    cursor = db.execute(
        "INSERT INTO shop_listing_images "
        "(product_id, image_blob, image_mime, image_filename, alt_text, width, height, "
        "is_cover, sort_order, version, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
        [
            product_id,
            stored,
            mime,
            _safe_filename(file_storage),
            _clean_alt(alt_text, _first(product, "name", "")),
            width,
            height,
            is_cover,
            next_order,
            timestamp,
            timestamp,
        ],
    )
    db.commit()
    return _int(getattr(cursor, "lastrowid", 0))


def replace_image(media_id, product_id, file_storage, alt_text=""):
    """Replace one photo's bytes in place, keeping its gallery position."""
    row = _owned_image(media_id, product_id)
    data = read_upload(file_storage)
    stored, mime, width, height = _prepare_image(data, getattr(file_storage, "mimetype", ""))
    db = get_db()
    db.execute(
        "UPDATE shop_listing_images SET image_blob = ?, image_mime = ?, image_filename = ?, "
        "alt_text = COALESCE(NULLIF(?, ''), alt_text), width = ?, height = ?, "
        "version = version + 1, updated_at = ? WHERE id = ? AND product_id = ?",
        [
            stored,
            mime,
            _safe_filename(file_storage),
            _clean_alt(alt_text),
            width,
            height,
            now(),
            int(media_id),
            int(product_id),
        ],
    )
    db.commit()
    return _int(row.get("version"), 1) + 1


def delete_image(media_id, product_id):
    """Remove one photo; if it was the cover, the next photo is promoted."""
    row = _owned_image(media_id, product_id)
    db = get_db()
    db.execute(
        "DELETE FROM shop_listing_images WHERE id = ? AND product_id = ?",
        [int(media_id), int(product_id)],
    )
    if _int(row.get("is_cover")):
        remaining = _image_rows(product_id)
        if remaining:
            db.execute(
                "UPDATE shop_listing_images SET is_cover = 1 WHERE id = ?",
                [_int(remaining[0].get("id"))],
            )
    db.commit()
    return True


def set_cover(product_id, media_id):
    """Make one photo the cover (the card image on the shop)."""
    _owned_image(media_id, product_id)
    db = get_db()
    db.execute(
        "UPDATE shop_listing_images SET is_cover = 0 WHERE product_id = ?", [int(product_id)]
    )
    db.execute(
        "UPDATE shop_listing_images SET is_cover = 1 WHERE id = ? AND product_id = ?",
        [int(media_id), int(product_id)],
    )
    db.commit()
    return True


def move_image(product_id, media_id, direction):
    """Shift one photo one place earlier/later. Returns the new id order."""
    rows = _image_rows(product_id)
    ids = [_int(row.get("id")) for row in rows]
    if int(media_id) not in ids:
        raise ShopImageError("Photo not found for this listing.")
    index = ids.index(int(media_id))
    target = index - 1 if direction == "up" else index + 1
    if target < 0 or target >= len(ids):
        return ids
    ids[index], ids[target] = ids[target], ids[index]
    db = get_db()
    for position, image_id in enumerate(ids, start=1):
        db.execute(
            "UPDATE shop_listing_images SET sort_order = ? WHERE id = ? AND product_id = ?",
            [position, image_id, int(product_id)],
        )
    db.commit()
    return ids


def _owned_image(media_id, product_id):
    row = row_dict(
        get_db().execute(
            "SELECT id, product_id, is_cover, version FROM shop_listing_images WHERE id = ?",
            [int(media_id)],
        ).fetchone()
    )
    if row is None or _int(row.get("product_id")) != int(product_id):
        raise ShopImageError("Photo not found for this listing.")
    return row


def image_row(media_id):
    """Raw lookup used by the media route before it serves bytes."""
    return row_dict(
        get_db().execute(
            "SELECT id, product_id, version, image_mime FROM shop_listing_images WHERE id = ?",
            [int(media_id)],
        ).fetchone()
    )


def image_bytes(media_id):
    """``(bytes, mime, version, product_id)`` for a photo, or ``(None, …)``."""
    row = row_dict(
        get_db().execute(
            "SELECT image_blob, image_mime, version, product_id "
            "FROM shop_listing_images WHERE id = ?",
            [int(media_id)],
        ).fetchone()
    )
    if row is None or row.get("image_blob") is None:
        return None, None, 0, None
    return (
        row["image_blob"],
        row.get("image_mime") or "image/jpeg",
        _int(row.get("version"), 1) or 1,
        _int(row.get("product_id")),
    )


# ---------------------------------------------------------------------------
# writing: the three home-page artwork slots
# ---------------------------------------------------------------------------


def home_image_url(slot):
    """Public URL for a home slot's artwork, or ``''`` when it has none.

    Safe to call from a template without an app context: a missing table, a
    missing row or an unknown slot all read as "no artwork", so the storefront
    simply renders its text card.
    """
    key = (slot or "").strip().lower()
    if key not in HOME_SLOT_KEYS or not has_app_context():
        return ""
    try:
        row = row_dict(
            get_db().execute(
                "SELECT image_blob, version FROM shop_home_images WHERE slot = ?", [key]
            ).fetchone()
        )
    except Exception:
        return ""
    if not row or row.get("image_blob") is None:
        return ""
    return f"{HOME_IMAGE_ROUTE.format(slot=key)}?v={_int(row.get('version'), 1) or 1}"


def home_slots():
    """The three slots with their current artwork URL, for the settings screen."""
    slots = []
    for key, label in HOME_SLOTS:
        slots.append({"slot": key, "label": label, "url": home_image_url(key)})
    return slots


def set_home_image(slot, file_storage):
    """Store a slot's artwork, replacing whatever was there."""
    key = (slot or "").strip().lower()
    if key not in HOME_SLOT_KEYS:
        raise ShopImageError("Unknown home-page slot.")
    data = read_upload(file_storage)
    stored, mime, width, height = _prepare_image(data, getattr(file_storage, "mimetype", ""))
    db = get_db()
    timestamp = now()
    existing = db.execute(
        "SELECT slot FROM shop_home_images WHERE slot = ?", [key]
    ).fetchone()
    if existing is None:
        db.execute(
            "INSERT INTO shop_home_images "
            "(slot, image_blob, image_mime, image_filename, width, height, version, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            [key, stored, mime, _safe_filename(file_storage), width, height, timestamp],
        )
    else:
        db.execute(
            "UPDATE shop_home_images SET image_blob = ?, image_mime = ?, image_filename = ?, "
            "width = ?, height = ?, version = version + 1, updated_at = ? WHERE slot = ?",
            [stored, mime, _safe_filename(file_storage), width, height, timestamp, key],
        )
    db.commit()
    return {"slot": key, "mime": mime, "width": width, "height": height}


def clear_home_image(slot):
    """Remove a slot's artwork. Returns True when there was one."""
    key = (slot or "").strip().lower()
    if key not in HOME_SLOT_KEYS:
        raise ShopImageError("Unknown home-page slot.")
    db = get_db()
    row = db.execute(
        "SELECT image_blob FROM shop_home_images WHERE slot = ?", [key]
    ).fetchone()
    had = bool(row) and _first(row, "image_blob") is not None
    db.execute(
        "DELETE FROM shop_home_images WHERE slot = ?", [key]
    )
    db.commit()
    return had


def home_image_bytes(slot):
    """``(bytes, mime, version)`` for a slot's artwork, or ``(None, None, 0)``."""
    key = (slot or "").strip().lower()
    if key not in HOME_SLOT_KEYS:
        return None, None, 0
    row = row_dict(
        get_db().execute(
            "SELECT image_blob, image_mime, version FROM shop_home_images WHERE slot = ?",
            [key],
        ).fetchone()
    )
    if row is None or row.get("image_blob") is None:
        return None, None, 0
    return row["image_blob"], row.get("image_mime") or "image/jpeg", _int(row.get("version"), 1) or 1


# ---------------------------------------------------------------------------
# the shareable shop link
# ---------------------------------------------------------------------------


def shop_public_url(external_url=""):
    """The absolute shop link staff copy and send to customers.

    ``PUBLIC_BASE_URL`` (env) wins, then the ``public_base_url`` setting, then
    the external URL Flask built for this request. The base is trimmed of a
    trailing slash so the result never doubles one.
    """
    base = ""
    if has_app_context():
        base = (current_app.config.get("PUBLIC_BASE_URL") or "").strip()
        if not base:
            try:
                from app.services.settings import get_company_settings

                settings = row_dict(get_company_settings()) or {}
                base = (settings.get("public_base_url") or "").strip()
            except Exception:
                base = ""
    if not base and external_url:
        return external_url
    if base:
        return f"{base.rstrip('/')}{SHOP_PATH}"
    if has_request_context():
        try:
            return url_for("public.store")
        except Exception:
            return SHOP_PATH
    return SHOP_PATH
