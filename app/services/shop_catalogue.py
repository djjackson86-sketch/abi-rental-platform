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
import threading

_IMAGE_DECODE_LOCK = threading.BoundedSemaphore(1)

from flask import current_app, g, has_app_context, has_request_context, url_for
from PIL import Image, ImageOps

from app.db import get_db, now

#: Hard cap on a listing's gallery: a cover plus detail shots.
MAX_IMAGES = 6

#: Generous for a phone photo, small enough that a stray video is refused early.
#: Staff browsers downscale before upload (see the listing/settings templates);
#: this cap is the server's backstop for scripts and older browsers.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

#: Longest edge of a stored photo. ``thumbnail`` scales only down, never up.
MAX_IMAGE_EDGE = 1280

#: Longest edge of the small card thumbnail generated at upload time. Cards ask
#: for this instead of the full photo, so a catalogue page is a few hundred KB.
THUMB_EDGE = 480

#: Refuse an absurd pixel count *before* Pillow decodes it, so a tiny compressed
#: "decompression bomb" cannot tie up a worker's memory.
MAX_IMAGE_PIXELS = 24_000_000
MAX_SOURCE_EDGE = 12_000

#: Checked before Pillow is asked to decode anything.
ALLOWED_MIME = frozenset({"image/png", "image/jpeg", "image/jpg", "image/webp"})

JPEG_QUALITY = 78
WEBP_QUALITY = 78
THUMB_JPEG_QUALITY = 72
THUMB_WEBP_QUALITY = 72

#: Server-side page sizes. Both listings page in SQL — the old code loaded every
#: product (and every gallery row) and sliced in Python, which is what made one
#: request expensive on a remote database.
PUBLIC_PAGE_SIZE = 12
ADMIN_PAGE_SIZE = 20
MAX_PAGE = 10_000

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
THUMB_ROUTE = "/store/media/{media_id}/thumb"
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
    # Thumbnails live in their own additive table rather than new columns on the
    # gallery table: creating a table is safe on every boot and on Turso, while
    # ALTER TABLE would need a migration pass. One row per gallery photo; the
    # row disappears with its photo (``ON DELETE CASCADE``).
    """
    CREATE TABLE IF NOT EXISTS shop_listing_thumbnails (
        image_id INTEGER PRIMARY KEY REFERENCES shop_listing_images(id) ON DELETE CASCADE,
        image_blob BLOB,
        image_mime TEXT NOT NULL DEFAULT '',
        width INTEGER NOT NULL DEFAULT 0,
        height INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT ''
    )
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
    # One-time seed provenance for listings imported from an external catalogue
    # (the original Sano sale-trailer price list). A seed row is the *only*
    # durable identity a rerun may trust: it is never shown in an editor and is
    # never rewritten, so staff copy, photos, prices and publish state stay
    # theirs. ``ON DELETE CASCADE`` means deleting the product also forgets the
    # seed, and a deliberate re-import can then create it again.
    """
    CREATE TABLE IF NOT EXISTS shop_listing_seeds (
        product_id INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
        source_key TEXT NOT NULL UNIQUE,
        source_ref TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT ''
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

#: Every column the shop reads, named explicitly. ``products`` has no binary
#: column today, but ``p.*`` also silently drags any column a future migration
#: adds — including a BLOB — into every listing query. Keeping the list explicit
#: is what bounds a listing response to text.
_PRODUCT_COLUMNS = (
    "p.id, p.name, p.product_type, p.description, p.sku, p.active, p.public_visible,"
    " p.price_amount, p.price_unit, p.security_deposit, p.hourly_extra_rate,"
    " p.registration, p.licence_number, p.registration_number, p.tax_profile_id,"
    " p.product_group_id, p.quantity, p.tracking_method, p.wheel_size,"
    " p.under_maintenance, p.branch_id, p.created_at"
)

_SELECT = f"""
SELECT {_PRODUCT_COLUMNS},
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
       m.updated_at AS meta_updated_at,
       s.source_key AS meta_source_key
FROM products p
LEFT JOIN product_groups g ON g.id = p.product_group_id
LEFT JOIN branches b ON b.id = p.branch_id
LEFT JOIN shop_listing_meta m ON m.product_id = p.id
LEFT JOIN shop_listing_seeds s ON s.product_id = p.id
WHERE 1=1
"""

#: The FROM/JOIN tail alone, for counting and paginating queries built around a
#: window function (which needs the joins without the leading ``SELECT``).
_FROM_SQL = _SELECT[_SELECT.index("FROM products p"):]

#: The ``SELECT`` list without its FROM tail (product columns + meta aliases),
#: reused by the paginated query so both paths expose identical names.
_LIST_COLUMNS = _SELECT[_SELECT.index("SELECT ") + len("SELECT "):_SELECT.index("FROM products p")].strip().rstrip(",")

#: One SQL predicate, used for public reads and for the shop editor's "Live"
#: filter, so a listing cannot be live on one screen and hidden on the other.
_PUBLIC_CONDITION = (
    "p.active = 1"
    " AND p.public_visible = 1"
    " AND COALESCE(p.under_maintenance, 0) = 0"
    " AND (p.product_group_id IS NULL"
    "      OR (COALESCE(g.active, 0) = 1 AND COALESCE(g.becomes_store_visible, 0) = 1))"
)

#: Public eligibility: the same meaning as the public store routes (active,
#: published, not under maintenance) plus a visible category. A product whose
#: category is hidden is excluded outright — it is never re-labelled into an
#: "Other" bucket, because that would keep selling something the shop hid.
_PUBLIC_SQL = f" AND {_PUBLIC_CONDITION}"

#: The provenance prefix of a one-time import seed, ``sale/<model>/<code>`` →
#: ``sale/<model>``. This is the *sale presentation* model family: the original
#: price list sold one model line in several sizes, and those sizes belong on one
#: card. It is deliberately narrow — the literal ``sale/`` namespace of the
#: durable ``shop_listing_seeds`` table, written once by the importer and never by
#: the storefront — so no same-name guess can fold two trailers together, and a
#: rental unit (which never carries a sale seed) can never be pulled in. The
#: source manifest is never read at runtime; only the saved seed row is.
_SALE_FAMILY_SQL = (
    "CASE WHEN p.product_type = 'sale'"
    " AND COALESCE(s.source_key, '') LIKE 'sale/%'"
    " AND INSTR(SUBSTR(s.source_key, 6), '/') > 0"
    " THEN SUBSTR(s.source_key, 1, INSTR(SUBSTR(s.source_key, 6), '/') + 4)"
    " ELSE '' END"
)

#: Public cards are per *model group*, not per row. Sizes of one imported sale
#: model line collapse through :data:`_SALE_FAMILY_SQL`; otherwise units the
#: operator declared compatible collapse as before. The same expression is used
#: to COUNT, page and de-duplicate, so a page boundary can never drop or
#: duplicate a card.
#:
#: The ``model_key`` signature below is the rental *unit allocation* contract and
#: is deliberately unchanged: it still demands the same type, category, price,
#: price unit, deposit and tax profile. Sale size variants are grouped by seed
#: family instead, precisely so a different price never blocks a size.
_GROUP_KEY_SQL = (
    f"CASE WHEN ({_SALE_FAMILY_SQL}) <> '' THEN 'm:' || ({_SALE_FAMILY_SQL})"
    " WHEN COALESCE(m.model_key, '') <> '' THEN"
    " 'k:' || m.model_key || '|' || p.product_type || '|' || COALESCE(p.product_group_id, -1)"
    " || '|' || printf('%.2f', p.price_amount) || '|' || p.price_unit"
    " || '|' || printf('%.2f', p.security_deposit) || '|' || COALESCE(p.tax_profile_id, -1)"
    " ELSE 'p:' || p.id END"
)

#: Cheapest size first, so a model card opens the variant whose price it shows.
_GROUP_ROW_ORDER_SQL = (
    "p.price_amount ASC, COALESCE(m.store_sort_order, 0) ASC,"
    " COALESCE(m.featured, 0) DESC, p.id ASC"
)

#: A size/dimension tail on a saved title: ``Model — 1.8m x 1.2m (CODE)``. The
#: separator needs surrounding whitespace, so ``un-braked`` is never split.
_VARIANT_RE = re.compile(
    r"^(?P<model>.+?)\s+[\u2014\u2013-]\s+(?P<size>.+?)(?:\s*\((?P<code>[^()]+)\))?\s*$"
)


def _clamp_page(page, per_page):
    """A safe ``(page, per_page, offset)`` from untrusted query-string values."""
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(per_page)
    except (TypeError, ValueError):
        per_page = PUBLIC_PAGE_SIZE
    page = max(1, min(page, MAX_PAGE))
    per_page = max(1, min(per_page, 100))
    return page, per_page, (page - 1) * per_page


def _paginated(items, total, page, per_page):
    total = max(0, _int(total))
    pages = (total + per_page - 1) // per_page if total else 0
    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
        "has_next": page < pages,
        "has_prev": page > 1,
        "next_page": page + 1 if page < pages else 0,
        "prev_page": page - 1 if page > 1 else 0,
    }


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
    # Paginated queries add internal ordering/partition helpers; they are never
    # part of a product dict a template or caller can see.
    product.pop("_group_key", None)
    product.pop("_rn", None)
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
    source_key = str(product.pop("meta_source_key", "") or "").strip()
    sale_family = _sale_family_of(source_key, product.get("product_type"))
    variant_model, variant_size = _variant_parts(title or product.get("name") or "")
    variant_code = str(product.get("sku") or "").strip() or _seed_code(source_key)

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
            # Sale model-family presentation. Empty on a product with no import
            # seed, which is every rental, service line and parts row.
            "sale_family": sale_family,
            "variant_model": variant_model,
            "variant_size": variant_size,
            "variant_code": variant_code,
            "model_family": "",
            "model_variants": [],
            "variant_count": 1,
            "price_from": _float(product.get("price_amount")),
            "price_varies": False,
            "size_range": "",
        }
    )
    return product


def _sale_family_of(source_key, product_type):
    """``sale/<model>`` for an imported sale size, else ``''``.

    Only the ``sale/`` namespace of a durable seed is a family. Anything else —
    a rental with a ``model_key``, a spare part with no seed, a seed written for
    a different catalogue — is left alone, so this can never fold inventory the
    operator did not import as one sale price list.
    """
    key = str(source_key or "").strip()
    if (product_type or "") != "sale" or not key.startswith("sale/"):
        return ""
    head, sep, _tail = key[len("sale/"):].partition("/")
    if not sep or not head:
        return ""
    return "sale/" + head


def _seed_code(source_key):
    """The trailing ``<code>`` of ``sale/<model>/<code>``, or ``''``."""
    key = str(source_key or "").strip()
    if "/" not in key:
        return ""
    return key.rsplit("/", 1)[-1].strip()


def _variant_parts(title):
    """Split a saved title into ``(model line, size)`` without inventing either.

    ``"Single axle un-braked utility trailers — 1.8m x 1.2m (ST1.8-900UBU)"``
    becomes ``("Single axle un-braked utility trailers", "1.8m x 1.2m")``. A
    title with no such tail keeps its whole text as the model line and an empty
    size, so a staff-written title is never mangled into a fake dimension.
    """
    text = " ".join(str(title or "").split())
    if not text:
        return "", ""
    match = _VARIANT_RE.match(text)
    if not match:
        return text, ""
    model = (match.group("model") or "").strip()
    size = (match.group("size") or "").strip()
    if not model or not size:
        return text, ""
    return model, size


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


def _public_where(product_type=None, query="", category=""):
    """Shared WHERE tail for public reads, so list and page cannot drift apart."""
    sql = _PUBLIC_SQL
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
    return sql, params


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

    Unbounded by design for callers that need the whole live set (tests,
    validation helpers). Request handlers use :func:`page_public_products`,
    which bounds the query and the de-duplication in SQL.
    """
    where, params = _public_where(product_type, query, category)
    sql = _SELECT + where + _ORDER_SQL
    rows = get_db().execute(sql, params).fetchall()
    products = [decorate_product(row) for row in rows]
    _attach_images(products)
    _attach_model_units(products)
    return [_public_card(product) for product in products]


def page_public_products(
    product_type=None, query="", category="", page=1, per_page=PUBLIC_PAGE_SIZE
):
    """One bounded page of public cards, with an exact total.

    De-duplication happens in SQL: exactly one row is chosen per model group
    (``ROW_NUMBER()`` over the same key the COUNT uses), so walking every page
    yields each model exactly once and the totals agree with the rows. The only
    images read are gallery *metadata* for the page's products — never a BLOB.
    """
    where, params = _public_where(product_type, query, category)
    page, per_page, offset = _clamp_page(page, per_page)
    db = get_db()
    count_row = db.execute(
        f"SELECT COUNT(*) AS c FROM (SELECT {_GROUP_KEY_SQL} AS k"
        f" {_FROM_SQL}{where} GROUP BY k)",
        params,
    ).fetchone()
    total = _int(_first(count_row, "c"))
    if not total:
        return _paginated([], 0, page, per_page)
    rows = db.execute(
        "SELECT * FROM ("
        f"SELECT {_LIST_COLUMNS}, {_GROUP_KEY_SQL} AS _group_key,"
        f" ROW_NUMBER() OVER (PARTITION BY {_GROUP_KEY_SQL} ORDER BY {_GROUP_ROW_ORDER_SQL}) AS _rn"
        f" {_FROM_SQL}{where}"
        ") q WHERE q._rn = 1"
        " ORDER BY CASE WHEN LOWER(COALESCE(q.meta_group_name, '')) LIKE '%trailer%' THEN 0 ELSE 1 END ASC, COALESCE(q.meta_featured, 0) DESC,"
        " COALESCE(q.meta_sort_order, 0) ASC,"
        " COALESCE(q.meta_group_sort_order, 999999) ASC,"
        " COALESCE(q.meta_group_name, '') ASC, q.name ASC, q.id ASC"
        " LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()
    products = [decorate_product(row) for row in rows]
    _attach_images(products)
    # All units of the page's model groups, so a card's "other sizes" list is
    # complete even when its siblings sit on another page. Costs one bounded
    # metadata query and only runs when a model key is actually in use.
    keys = sorted({(p.get("model_key") or "").strip() for p in products} - {""})
    extra_units = []
    if keys:
        marks = ",".join("?" for _ in keys)
        extra_rows = get_db().execute(
            _SELECT + _PUBLIC_SQL + f" AND m.model_key IN ({marks}) ORDER BY p.id ASC",
            keys,
        ).fetchall()
        extra_units = [decorate_product(row) for row in extra_rows]
    _attach_model_units(products, extra_units)
    # One bounded, text-only query for the sizes of the sale model families on
    # this page (never the catalogue, never a BLOB), so a card can say how many
    # sizes it covers and show the lowest current price.
    _attach_sale_families(products)
    return _paginated([_page_card(product) for product in products], total, page, per_page)


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


def _page_card(product):
    """A catalogue-page card: the documented contract plus presentation extras.

    ``list_public_products`` keeps returning exactly the documented keys; the
    page path adds the thumbnail URL, the sale model family name, its size count,
    its size range and the lowest current price, so one card can stand for a
    model line whose sizes are priced differently.
    """
    card = _public_card(product)
    thumbs = [
        image.get("thumb_url") or ""
        for image in product.get("store_images") or []
        if image.get("thumb_url")
    ]
    card["thumb_url"] = thumbs[0] if thumbs else ""
    card["model_family"] = product.get("model_family") or ""
    card["variant_count"] = max(1, _int(product.get("variant_count"), 1))
    card["price_from"] = _float(product.get("price_from"), card["price_amount"])
    card["price_varies"] = bool(product.get("price_varies"))
    card["size_range"] = product.get("size_range") or ""
    return card


def _sale_family_where(families):
    marks = ",".join("?" for _ in families)
    return f" AND ({_SALE_FAMILY_SQL}) IN ({marks})", list(families)


def _attach_sale_families(products):
    """Fill each sale card's size family from the saved rows, in one query.

    A family is the ``sale/<model>`` prefix of the durable import seed, so the
    membership rule is: live (active, published, not under maintenance, visible
    category) **and** the same seed family. Hidden sizes therefore disappear from
    the selector and from the "from" price, and a crafted ``/store/products/<id>``
    URL for one still 404s through :func:`get_public_product`.

    Only text columns are read; the selector needs a code, a dimension and a
    saved price, never a photo blob. Nothing here reads the source manifest.
    """
    if not products:
        return
    families = sorted(
        {
            str(product.get("sale_family") or "")
            for product in products
            if product.get("sale_family")
        }
    )
    if not families:
        return
    where, params = _sale_family_where(families)
    rows = get_db().execute(
        _SELECT + _PUBLIC_SQL + where + " ORDER BY " + _GROUP_ROW_ORDER_SQL,
        params,
    ).fetchall()
    by_family = {}
    for row in rows:
        sibling = decorate_product(row)
        family = sibling.get("sale_family") or ""
        if family:
            by_family.setdefault(family, []).append(sibling)

    for product in products:
        family = product.get("sale_family") or ""
        variants = by_family.get(family) or []
        if not family or not variants:
            continue
        prices = [_float(variant.get("price_amount")) for variant in variants]
        product["model_variants"] = [_variant_view(variant) for variant in variants]
        product["variant_count"] = len(variants)
        product["price_from"] = min(prices)
        product["price_varies"] = len({round(price, 2) for price in prices}) > 1
        product["model_family"] = _family_title(variants)
        product["size_range"] = _size_range(variants)


def _variant_view(product):
    """One selectable size: real product id, saved code, saved price, saved size."""
    size = product.get("variant_size") or ""
    title = product.get("store_title") or product.get("name") or ""
    return {
        "id": _int(product.get("id")),
        "code": product.get("variant_code") or "",
        "size": size,
        "label": size or title,
        "title": title,
        "price_amount": _float(product.get("price_amount")),
        "price_unit": product.get("price_unit") or "",
    }


def _family_title(variants):
    """The shared model line of a family, taken from the saved titles only.

    When every size saved the same model line, that is the card title. When they
    differ (staff renamed one size), the longest shared prefix — trimmed to a
    whole word — is used, and if there is no shared prefix at all the cheapest
    size's own title is shown rather than an invented name.
    """
    titles = [variant.get("store_title") or variant.get("name") or "" for variant in variants]
    models = [variant.get("variant_model") or "" for variant in variants]
    models = [model for model in models if model]
    if models and len(set(models)) == 1:
        return models[0]
    if models:
        prefix = models[0]
        for model in models[1:]:
            limit = min(len(prefix), len(model))
            index = 0
            while index < limit and prefix[index] == model[index]:
                index += 1
            prefix = prefix[:index]
        prefix = prefix.rstrip()
        cut = prefix.rfind(" ")
        if cut > 0:
            prefix = prefix[:cut]
        if len(prefix) >= 12:
            return prefix
    return titles[0] if titles else ""


def _size_range(variants):
    """``"1.8m x 1.2m – 3.0m x 1.5m"`` for the saved sizes, cheapest first."""
    sizes = []
    for variant in variants:
        size = variant.get("variant_size") or ""
        if size and size not in sizes:
            sizes.append(size)
    if len(sizes) > 1:
        return f"{sizes[0]} – {sizes[-1]}"
    return sizes[0] if sizes else ""


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
    # The size selector on the detail page: every live sibling of the same saved
    # sale model family, with its own id, code, dimension and saved price.
    _attach_sale_families([product])
    return product


def _editable_where(branch_ids=None):
    """Scope + optional explicit depot list for an editor query."""
    from app.services.access import product_branch_clause

    branch_sql, branch_params = product_branch_clause("p", include_unassigned=True)
    sql, params = branch_sql, list(branch_params)
    if branch_ids is not None:
        try:
            ids = [int(value) for value in branch_ids]
        except (TypeError, ValueError):
            ids = []
        if not ids:
            sql += " AND 0=1"
        else:
            marks = ",".join("?" for _ in ids)
            sql += f" AND (p.branch_id IS NULL OR p.branch_id IN ({marks}))"
            params.extend(ids)
    return sql, params


#: State filters are SQL predicates, not a Python pass over every product.
_ADMIN_STATE_SQL = {
    "live": f" AND ({_PUBLIC_CONDITION})",
    "hidden": f" AND NOT ({_PUBLIC_CONDITION})",
    "missing_image": " AND NOT EXISTS (SELECT 1 FROM shop_listing_images i WHERE i.product_id = p.id)",
    "missing_description": (
        " AND TRIM(COALESCE(NULLIF(m.store_description, ''), p.description, '')) = ''"
    ),
    "featured": " AND COALESCE(m.featured, 0) = 1",
}


def editable_products():
    """Every product the session's depot scope allows, publish state ignored.

    Deliberately the same row set the Inventory page shows: the restriction comes
    from ``product_branch_clause``, the helper that page already uses, so nobody
    can reach a product in the shop editor that they cannot see in Inventory.
    Authorisation itself (the ``online_store`` module) is the route's job.

    Kept unbounded for callers that need the whole editor set; the listings
    screen uses :func:`page_editable_products`.
    """
    where, params = _editable_where()
    sql = _SELECT + where + _ORDER_SQL
    rows = get_db().execute(sql, params).fetchall()
    products = [decorate_product(row) for row in rows]
    _attach_images(products)
    _attach_model_units(products)
    return products


def page_editable_products(
    product_type="", state="", page=1, per_page=ADMIN_PAGE_SIZE, branch_ids=None
):
    """One bounded page of editor rows; filters and counts run in SQL."""
    where, params = _editable_where(branch_ids)
    if (product_type or "").strip() in PRODUCT_TYPES:
        where += " AND p.product_type = ?"
        params.append(product_type.strip())
    state_sql = _ADMIN_STATE_SQL.get((state or "").strip())
    if state_sql:
        where += state_sql
    page, per_page, offset = _clamp_page(page, per_page)
    db = get_db()
    count_row = db.execute(
        f"SELECT COUNT(*) AS c {_FROM_SQL}{where}", params
    ).fetchone()
    total = _int(_first(count_row, "c"))
    rows = db.execute(
        _SELECT + where + _ORDER_SQL + " LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()
    products = [decorate_product(row) for row in rows]
    _attach_images(products)
    _attach_model_units(products)
    return _paginated(products, total, page, per_page)


def editable_listing_counts(branch_ids=None):
    """Chip counts for the listings screen in one aggregate query."""
    where, params = _editable_where(branch_ids)
    rows = get_db().execute(
        "SELECT COALESCE(p.product_type, 'rental') AS ptype, COUNT(*) AS all_count,"
        f" SUM(CASE WHEN {_PUBLIC_CONDITION} THEN 1 ELSE 0 END) AS live_count,"
        f" SUM(CASE WHEN NOT ({_PUBLIC_CONDITION}) THEN 1 ELSE 0 END) AS hidden_count,"
        " SUM(CASE WHEN EXISTS (SELECT 1 FROM shop_listing_images i"
        "     WHERE i.product_id = p.id) THEN 1 ELSE 0 END) AS with_image_count,"
        " SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM shop_listing_images i"
        "     WHERE i.product_id = p.id) THEN 1 ELSE 0 END) AS missing_image_count"
        f" {_FROM_SQL}{where} GROUP BY ptype",
        params,
    ).fetchall()
    counts = {"all": 0, "live": 0, "hidden": 0, "with_image": 0, "missing_image": 0}
    type_counts = {}
    for row in rows:
        data = row_dict(row) or {}
        total = _int(data.get("all_count"))
        counts["all"] += total
        counts["live"] += _int(data.get("live_count"))
        counts["hidden"] += _int(data.get("hidden_count"))
        counts["with_image"] += _int(data.get("with_image_count"))
        counts["missing_image"] += _int(data.get("missing_image_count"))
        type_counts[data.get("ptype") or "rental"] = total
    return counts, type_counts


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
        "SELECT 1 AS ok FROM products p"
        " LEFT JOIN product_groups g ON g.id = p.product_group_id"
        f" WHERE p.id = ? AND {_PUBLIC_CONDITION}",
        [product_id],
    ).fetchone()
    return row is not None


def _attach_images(products):
    """Fill gallery metadata for a page of products in one query, cover first.

    Only text columns and a thumbnail *presence* flag are selected: a listing
    never transfers an image BLOB. Cards use ``thumb_url`` when a thumbnail
    exists and fall back to the full photo for old rows.
    """
    ids = [_int(product.get("id")) for product in products if product.get("id") is not None]
    if not ids:
        return
    marks = ",".join("?" for _ in ids)
    rows = get_db().execute(
        "SELECT i.id, i.product_id, i.alt_text, i.is_cover, i.sort_order, i.version,"
        " CASE WHEN t.image_id IS NULL THEN 0 ELSE 1 END AS has_thumb "
        "FROM shop_listing_images i "
        "LEFT JOIN shop_listing_thumbnails t ON t.image_id = i.id "
        f"WHERE i.product_id IN ({marks}) "
        "ORDER BY i.is_cover DESC, i.sort_order ASC, i.id ASC",
        ids,
    ).fetchall()
    by_product = {}
    for row in rows:
        data = row_dict(row) or {}
        media_id = _int(data.get("id"))
        version = _int(data.get("version"), 1) or 1
        by_product.setdefault(_int(data.get("product_id")), []).append(
            {
                "id": media_id,
                "url": media_url(media_id, version),
                "thumb_url": thumb_url(media_id, version) if _int(data.get("has_thumb")) else "",
                "alt": data.get("alt_text") or "",
                "is_cover": bool(_int(data.get("is_cover"))),
            }
        )
    for product in products:
        images = by_product.get(_int(product.get("id")), [])
        product["store_images"] = images
        if images:
            product["image_url"] = images[0]["url"]


def _attach_model_units(products, extra_units=None):
    """Group units only where the operator declared compatibility explicitly.

    A unit joins a model group when it carries the same non-empty ``model_key``
    **and** the same operational identity: product type, category, price, price
    unit and deposit. Two physical trailers with the same name but no declared
    key are never merged, and every unit keeps its own product id.

    ``extra_units`` are additional decorated rows that belong to the same
    groups but are not on this page (a paginated read); they contribute to the
    unit list without becoming cards.
    """
    pool = list({int(p['id']): p for p in list(extra_units or []) + list(products)}.values())
    buckets = {}
    for product in pool:
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


def thumb_url(media_id, version=1):
    """Versioned URL for the small card thumbnail of a gallery photo."""
    return f"{THUMB_ROUTE.format(media_id=_int(media_id))}?v={_int(version, 1) or 1}"


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
# one-time seed provenance
# ---------------------------------------------------------------------------


def seed_source_key(product_id):
    """The immutable import key bound to a product, or ``''`` when it has none."""
    row = get_db().execute(
        "SELECT source_key FROM shop_listing_seeds WHERE product_id = ?",
        [int(product_id)],
    ).fetchone()
    data = row_dict(row) or {}
    return str(data.get("source_key") or "")


def seed_product_id(source_key):
    """The product a seed key was created for, or ``None``."""
    key = str(source_key or "").strip()
    if not key:
        return None
    row = get_db().execute(
        "SELECT product_id FROM shop_listing_seeds WHERE source_key = ?", [key]
    ).fetchone()
    data = row_dict(row) or {}
    value = data.get("product_id")
    return int(value) if value is not None else None


def record_seed(product_id, source_key, source_ref=""):
    """Bind a product to its import key exactly once.

    Returns ``True`` when the row was written. A rerun of the importer calls this
    only for a product it has just created or adopted, so staff edits are never
    revisited: the seed row stores provenance, not a copy of the catalogue.
    """
    product_id = int(product_id)
    key = str(source_key or "").strip()
    if not key:
        raise ShopListingError("A seed needs a source key.")
    if not _product_exists(product_id):
        raise ShopListingError("Product not found")
    existing = seed_product_id(key)
    if existing is not None:
        if existing == product_id:
            return False
        raise ShopListingError("That source key is already bound to another product.")
    if seed_source_key(product_id):
        raise ShopListingError("That product is already bound to another source key.")
    db = get_db()
    db.execute(
        "INSERT INTO shop_listing_seeds (product_id, source_key, source_ref, created_at) "
        "VALUES (?, ?, ?, ?)",
        [product_id, key, str(source_ref or "")[:200], now()],
    )
    db.commit()
    return True


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
        limit_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
        raise ShopImageError(f"That photo is larger than {limit_mb} MB — choose a smaller one.")
    return data


def _encode_image(image, mime):
    """Encode one already-oriented image, choosing a mode each format supports."""
    buffer = io.BytesIO()
    if mime == "image/webp":
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA")
        image.save(buffer, format="WEBP", quality=WEBP_QUALITY, method=4)
    else:
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue()


def _encode_thumb(image, mime):
    """Encode the small card thumbnail from the full-size stored image."""
    thumb = image.copy()
    thumb.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    if mime == "image/webp":
        if thumb.mode not in ("RGB", "RGBA"):
            thumb = thumb.convert("RGBA")
        thumb.save(buffer, format="WEBP", quality=THUMB_WEBP_QUALITY, method=4)
    else:
        if thumb.mode not in ("RGB", "L"):
            thumb = thumb.convert("RGB")
        thumb.save(buffer, format="JPEG", quality=THUMB_JPEG_QUALITY, optimize=True)
    return buffer.getvalue(), thumb.width, thumb.height


def _prepare_image(data, source_mime):
    # Large phone inputs can briefly need several decoded copies (orientation,
    # resize, thumbnail). Keep concurrent uploads from multiplying that peak.
    with _IMAGE_DECODE_LOCK:
        return _prepare_image_unlocked(data, source_mime)


def _prepare_image_unlocked(data, source_mime):
    """Validate, orient, downscale and re-encode one photo, plus its thumbnail.

    Returns ``(bytes, mime, width, height, thumb_bytes, thumb_width, thumb_height)``.
    Nothing is written until this returns, so a refused file leaves the existing
    gallery (or home slot) exactly as it was.

    The pixel count is checked from the header *before* ``load()`` decodes the
    image, so a small file that expands to an enormous bitmap is refused without
    allocating the bitmap — the decompression-bomb guard.
    """
    mime = (source_mime or "").lower()
    if mime and mime not in ALLOWED_MIME:
        raise ShopImageError(f"That file is a {mime} file, not a PNG, JPEG or WebP image.")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            width, height = probe.size
            if width < 1 or height < 1:
                raise ShopImageError("That image has no usable pixels.")
            if max(width, height) > MAX_SOURCE_EDGE:
                raise ShopImageError(
                    f"That photo is {width}×{height} pixels — the longest side must be "
                    f"{MAX_SOURCE_EDGE} pixels or less."
                )
            if width * height > MAX_IMAGE_PIXELS:
                raise ShopImageError(
                    f"That photo is {width}×{height} pixels, which is too large to process. "
                    "Choose a smaller copy."
                )
            probe.verify()
    except ShopImageError:
        raise
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
    stored = _encode_image(image, output_mime)
    thumb, thumb_width, thumb_height = _encode_thumb(image, output_mime)
    return stored, output_mime, image.width, image.height, thumb, thumb_width, thumb_height


def _save_thumbnail(image_id, thumb, mime, width, height):
    """Upsert the card thumbnail for one gallery photo."""
    get_db().execute(
        "INSERT INTO shop_listing_thumbnails "
        "(image_id, image_blob, image_mime, width, height, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(image_id) DO UPDATE SET image_blob = excluded.image_blob, "
        "image_mime = excluded.image_mime, width = excluded.width, "
        "height = excluded.height, created_at = excluded.created_at",
        [int(image_id), thumb, mime, int(width), int(height), now()],
    )


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
    ids = [_int(row.get("id")) for row in _image_rows(product_id)]
    with_thumb = set()
    if ids:
        marks = ",".join("?" for _ in ids)
        with_thumb = {
            _int(_first(row, "image_id"))
            for row in get_db().execute(
                f"SELECT image_id FROM shop_listing_thumbnails WHERE image_id IN ({marks})",
                ids,
            ).fetchall()
        }
    return [
        {
            "id": _int(row.get("id")),
            "url": media_url(row.get("id"), row.get("version")),
            "thumb_url": thumb_url(row.get("id"), row.get("version"))
            if _int(row.get("id")) in with_thumb
            else "",
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
    stored, mime, width, height, thumb, thumb_width, thumb_height = _prepare_image(
        data, getattr(file_storage, "mimetype", "")
    )
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
    media_id = _int(getattr(cursor, "lastrowid", 0))
    if media_id:
        _save_thumbnail(media_id, thumb, mime, thumb_width, thumb_height)
    db.commit()
    return media_id


def replace_image(media_id, product_id, file_storage, alt_text=""):
    """Replace one photo's bytes in place, keeping its gallery position."""
    row = _owned_image(media_id, product_id)
    data = read_upload(file_storage)
    stored, mime, width, height, thumb, thumb_width, thumb_height = _prepare_image(
        data, getattr(file_storage, "mimetype", "")
    )
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
    _save_thumbnail(media_id, thumb, mime, thumb_width, thumb_height)
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


def media_record(media_id, thumbnail=False):
    """Everything the media route needs, in **one** database round trip.

    On the live app the database is remote (Turso over HTTPS), so each extra
    query was a full network round trip — the old route made four per image.
    This returns the bytes *and* the two facts used to authorise them:

    ``is_public``   the product is currently eligible for the public shop
    ``store_enabled`` the shop itself is switched on
    ``branch_id``   for the staff-preview depot check

    ``thumbnail=True`` reads the small card image instead of the full photo.
    Returns ``None`` when the photo (or its thumbnail) does not exist.
    """
    try:
        media_id = int(media_id)
    except (TypeError, ValueError, OverflowError):
        return None
    if media_id < 1:
        return None
    if thumbnail:
        sql = (
            "SELECT i.id, i.product_id, i.version, t.image_mime AS image_mime,"
            " t.image_blob AS image_blob, p.branch_id,"
            " (SELECT store_enabled FROM company_settings WHERE id = 1) AS store_enabled,"
            f" CASE WHEN {_PUBLIC_CONDITION} THEN 1 ELSE 0 END AS is_public "
            "FROM shop_listing_thumbnails t "
            "JOIN shop_listing_images i ON i.id = t.image_id "
            "JOIN products p ON p.id = i.product_id "
            "LEFT JOIN product_groups g ON g.id = p.product_group_id "
            "WHERE t.image_id = ? AND t.image_blob IS NOT NULL"
        )
    else:
        sql = (
            "SELECT i.id, i.product_id, i.version, i.image_mime, i.image_blob,"
            " p.branch_id,"
            " (SELECT store_enabled FROM company_settings WHERE id = 1) AS store_enabled,"
            f" CASE WHEN {_PUBLIC_CONDITION} THEN 1 ELSE 0 END AS is_public "
            "FROM shop_listing_images i "
            "JOIN products p ON p.id = i.product_id "
            "LEFT JOIN product_groups g ON g.id = p.product_group_id "
            "WHERE i.id = ? AND i.image_blob IS NOT NULL"
        )
    data = row_dict(get_db().execute(sql, [media_id]).fetchone())
    if not data or data.get("image_blob") is None:
        return None
    return {
        "id": _int(data.get("id")),
        "product_id": _int(data.get("product_id")),
        "version": _int(data.get("version"), 1) or 1,
        "mime": data.get("image_mime") or "image/jpeg",
        "data": data.get("image_blob"),
        "branch_id": data.get("branch_id"),
        "store_enabled": bool(_int(data.get("store_enabled"))),
        "is_public": bool(_int(data.get("is_public"))),
    }


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


def _home_image_versions():
    """Slot -> version for slots that have artwork, cached for this request.

    The old helper selected the whole ``image_blob`` three times per home page
    just to test for NULL. This reads versions only, once per request, so a home
    render costs one small query rather than three BLOB transfers.
    """
    if not has_app_context():
        return {}
    cached = getattr(g, "_shop_home_image_versions", None)
    if cached is None:
        cached = {}
        try:
            rows = get_db().execute(
                "SELECT slot, version FROM shop_home_images"
            ).fetchall()
        except Exception:
            rows = []
        for row in rows:
            data = row_dict(row) or {}
            key = str(data.get("slot") or "")
            if key in HOME_SLOT_KEYS:
                cached[key] = _int(data.get("version"), 1) or 1
        g._shop_home_image_versions = cached
    return cached


def home_image_url(slot):
    """Public URL for a home slot's artwork, or ``''`` when it has none.

    Safe to call from a template without an app context: a missing table, a
    missing row or an unknown slot all read as "no artwork", so the storefront
    simply renders its text card.
    """
    key = (slot or "").strip().lower()
    if key not in HOME_SLOT_KEYS or not has_app_context():
        return ""
    version = _home_image_versions().get(key)
    if not version:
        return ""
    return f"{HOME_IMAGE_ROUTE.format(slot=key)}?v={version}"


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
    stored, mime, width, height, _thumb, _tw, _th = _prepare_image(
        data, getattr(file_storage, "mimetype", "")
    )
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
    if has_app_context():
        g.pop("_shop_home_image_versions", None)
    return {"slot": key, "mime": mime, "width": width, "height": height}


def clear_home_image(slot):
    """Remove a slot's artwork. Returns True when there was one."""
    key = (slot or "").strip().lower()
    if key not in HOME_SLOT_KEYS:
        raise ShopImageError("Unknown home-page slot.")
    db = get_db()
    row = db.execute(
        "SELECT 1 AS present FROM shop_home_images WHERE slot = ? AND image_blob IS NOT NULL",
        [key],
    ).fetchone()
    had = row is not None
    db.execute(
        "DELETE FROM shop_home_images WHERE slot = ?", [key]
    )
    db.commit()
    if has_app_context():
        g.pop("_shop_home_image_versions", None)
    return had


def home_image_record(slot):
    """A home slot's artwork and shop state in one round trip, or ``None``."""
    key = (slot or "").strip().lower()
    if key not in HOME_SLOT_KEYS:
        return None
    row = row_dict(
        get_db().execute(
            "SELECT image_blob, image_mime, version,"
            " (SELECT store_enabled FROM company_settings WHERE id = 1) AS store_enabled"
            " FROM shop_home_images WHERE slot = ? AND image_blob IS NOT NULL",
            [key],
        ).fetchone()
    )
    if not row or row.get("image_blob") is None:
        return None
    return {
        "data": row.get("image_blob"),
        "mime": row.get("image_mime") or "image/jpeg",
        "version": _int(row.get("version"), 1) or 1,
        "store_enabled": bool(_int(row.get("store_enabled"))),
    }


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
