"""Online-store listing editor, home artwork and public media (shop admin).

This blueprint is the *presentation* editor for existing ``products`` rows. It
exists so the public shop can carry marketing copy and photos without turning
the Inventory screen into a CMS — and, more importantly, without giving anyone
who edits the shop the ability to change a price, a deposit, a tax profile or a
stock count.

Boundaries this module keeps
----------------------------
* **One catalogue.** No product is created here. Every listing is an existing
  product row; the editor writes only ``shop_listing_meta`` and, when the form
  carries the control, ``products.public_visible``.
* **Permission gate.** A signed-in, *active* database user needs the
  ``online_store`` module (the owner always passes).
* **Branch scope.** Writes go through ``_scoped_product``, which applies the
  same ``session_branch_scope_ids`` rule as the Inventory page: a depot-limited
  account can only reach products assigned to its own depots (plus unassigned
  stock).
* **CSRF.** Every write validates the session-held ``shop_admin`` token before
  any service call, so a refused post cannot half-save a listing.
* **Public media.** ``/store/media/<id>`` and ``/store/home-image/<slot>``
  serve the sanitised, re-encoded bytes from the database. A listing photo is
  public only while its product is publicly eligible; otherwise it needs an
  authorised editor (preview) and is a 404 for everyone else, so a hidden
  listing's photos cannot be hotlinked.
"""

from functools import wraps

from flask import (
    Blueprint,
    abort,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from app.db import get_db
from app.services.access import session_branch_scope_ids, user_can_module
from app.services.csrf import FORM_FIELD, issue_token, validate
from app.services.shop_catalogue import (
    HOME_SLOTS,
    MAX_IMAGES,
    MAX_STORE_DESCRIPTION,
    MAX_STORE_TITLE,
    PRODUCT_TYPES,
    ShopImageError,
    ShopListingError,
    add_image,
    clear_home_image,
    delete_image,
    editable_products,
    get_product,
    home_image_bytes,
    home_image_url,
    home_slots,
    image_bytes,
    image_row,
    move_image,
    product_is_public,
    replace_image,
    save_listing,
    set_cover,
    set_home_image,
    set_public,
    shop_public_url,
)

bp = Blueprint("shop_admin", __name__)

MODULE_KEY = "online_store"

#: One CSRF scope for every shop form, so a token from any shop page opens all of
#: them and none of the other protected surfaces.
CSRF_SCOPE = "shop_admin"


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------


def csrf_token():
    """The current session's shop CSRF token, created on first use."""
    return issue_token(CSRF_SCOPE)


def _active_user():
    """The signed-in user's row, or ``None`` when it is missing or deactivated.

    A session cookie outlives an account being switched off, so the database is
    the authority here: a deactivated account stops editing the shop immediately,
    on every device it is signed in on.
    """
    user_id = session.get("user_id")
    if not user_id:
        return None
    row = get_db().execute(
        "SELECT id, name, role, active FROM users WHERE id = ?", [user_id]
    ).fetchone()
    if row is None:
        return None
    data = dict(row) if hasattr(row, "keys") else (row.asdict() if hasattr(row, "asdict") else None)
    if data is None:
        try:
            data = {"id": row["id"], "name": row["name"], "role": row["role"], "active": row["active"]}
        except Exception:
            return None
    try:
        if not int(data.get("active") or 0):
            return None
    except (TypeError, ValueError):
        return None
    return data


def shop_editor_required(view):
    """Signed in, active in the database, AND allowed the Online store module."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        user = _active_user()
        if user is None:
            session.clear()
            return redirect(url_for("auth.login"))
        from app.routes.shop_enquiries import _live_account
        _live_account(required_modules='online_store')
        # Established on every shop page so a render can never post an empty token.
        csrf_token()
        return view(*args, **kwargs)

    return wrapped


def require_csrf(view):
    """Refuse a write that did not carry this session's shop token (400)."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        validate(CSRF_SCOPE)
        return view(*args, **kwargs)

    return wrapped


@bp.app_context_processor
def inject_shop_helpers():
    return {
        "shop_csrf_token": csrf_token,
        "shop_csrf_field": FORM_FIELD,
        "shop_max_images": MAX_IMAGES,
        "shop_max_title": MAX_STORE_TITLE,
        "shop_max_description": MAX_STORE_DESCRIPTION,
        "shop_home_slots": HOME_SLOTS,
        "shop_product_types": PRODUCT_TYPES,
    }


# ---------------------------------------------------------------------------
# scope
# ---------------------------------------------------------------------------


def _scoped_product(product_id):
    """The product, or 404 when missing or outside this session's depots."""
    product = get_product(product_id)
    if product is None:
        abort(404)
    from app.routes.shop_enquiries import _live_account, _account_branch_ids
    scope = _account_branch_ids(_live_account(required_modules='online_store'))
    if scope is not None:
        if len(scope) == 0:
            abort(404)
        branch_id = product.get("branch_id")
        if branch_id is not None:
            try:
                if int(branch_id) not in [int(value) for value in scope]:
                    abort(404)
            except (TypeError, ValueError):
                abort(404)
    return product


# ---------------------------------------------------------------------------
# listings
# ---------------------------------------------------------------------------


@bp.route("/online-store/listings")
@shop_editor_required
def listings():
    """Every product this session may see, with its shop state beside it."""
    products = editable_products()
    from app.routes.shop_enquiries import _live_account, _account_branch_ids
    current_scope = _account_branch_ids(_live_account(required_modules='online_store'))
    if current_scope is not None:
        products = [p for p in products if current_scope and (p.get('branch_id') is None or int(p['branch_id']) in current_scope)]
    type_filter = (request.args.get("type") or "").strip()
    state_filter = (request.args.get("state") or "").strip()

    def matches(product):
        ptype = product.get("product_type") or "rental"
        if type_filter and ptype != type_filter:
            return False
        if state_filter == "live":
            return bool(product.get("store_public"))
        if state_filter == "hidden":
            return not product.get("store_public")
        if state_filter == "missing_image":
            return not product.get("store_images")
        if state_filter == "missing_description":
            return not (product.get("store_description") or "").strip()
        if state_filter == "featured":
            return bool(product.get("featured"))
        return True

    visible = [product for product in products if matches(product)]
    counts = {
        "all": len(products),
        "live": sum(1 for p in products if p.get("store_public")),
        "hidden": sum(1 for p in products if not p.get("store_public")),
        "with_image": sum(1 for p in products if p.get("store_images")),
        "missing_image": sum(1 for p in products if not p.get("store_images")),
    }
    type_counts = {}
    for product in products:
        key = product.get("product_type") or "rental"
        type_counts[key] = type_counts.get(key, 0) + 1

    shop_url = shop_public_url(url_for("public.store", _external=True))
    return render_template(
        "admin/shop_listings.html",
        products=visible,
        counts=counts,
        type_counts=type_counts,
        type_filter=type_filter,
        state_filter=state_filter,
        shop_url=shop_url,
    )

@bp.route("/online-store/listings/<int:product_id>", methods=["GET", "POST"])
@shop_editor_required
def listing_detail(product_id):
    """Edit one product's public title, copy, grouping and gallery."""
    product = _scoped_product(product_id)
    if request.method == "POST":
        validate(CSRF_SCOPE)
        try:
            save_listing(product_id, request.form)
        except ShopListingError as exc:
            flash(str(exc), "error")
            listing = get_product(product_id)
            if listing is None:
                abort(404)
            listing["shop_unsaved"] = {
                "store_title": request.form.get("store_title", ""),
                "store_description": request.form.get("store_description", ""),
                "model_key": request.form.get("model_key", ""),
                "store_sort_order": request.form.get("store_sort_order", ""),
                "featured": bool(request.form.get("featured")),
                "public_visible": bool(request.form.get("public_visible")),
            }
            return render_template("admin/shop_listing.html", product=listing), 400
        flash("Shop listing saved.", "success")
        return redirect(url_for("shop_admin.listing_detail", product_id=int(product_id)))

    listing = get_product(product_id)
    return render_template("admin/shop_listing.html", product=listing)


@bp.post("/online-store/listings/<int:product_id>/publish")
@shop_editor_required
@require_csrf
def publish_listing(product_id):
    """Quick publish/hide from the list, through ``products.public_visible``."""
    _scoped_product(product_id)
    state = (request.form.get("public_visible") or "").strip().lower()
    public = state in {"1", "true", "on", "yes", "public", "published"}
    try:
        set_public(product_id, public)
    except ShopListingError as exc:
        flash(str(exc), "error")
    else:
        flash("Listing published to the shop." if public else "Listing hidden from the shop.", "success")
    return redirect(request.referrer or url_for("shop_admin.listings"))


# ---------------------------------------------------------------------------
# gallery
# ---------------------------------------------------------------------------


@bp.post("/online-store/listings/<int:product_id>/images")
@shop_editor_required
@require_csrf
def upload_image(product_id):
    _scoped_product(product_id)
    try:
        add_image(product_id, request.files.get("image"), request.form.get("alt_text", ""))
    except ShopImageError as exc:
        flash(str(exc), "error")
    else:
        flash("Photo added to the listing.", "success")
    return redirect(url_for("shop_admin.listing_detail", product_id=int(product_id)))


@bp.post("/online-store/listings/<int:product_id>/images/<int:media_id>/replace")
@shop_editor_required
@require_csrf
def replace_listing_image(product_id, media_id):
    _scoped_product(product_id)
    try:
        replace_image(media_id, product_id, request.files.get("image"), request.form.get("alt_text", ""))
    except ShopImageError as exc:
        flash(str(exc), "error")
    else:
        flash("Photo replaced.", "success")
    return redirect(url_for("shop_admin.listing_detail", product_id=int(product_id)))


@bp.post("/online-store/listings/<int:product_id>/images/<int:media_id>/delete")
@shop_editor_required
@require_csrf
def delete_listing_image(product_id, media_id):
    _scoped_product(product_id)
    try:
        delete_image(media_id, product_id)
    except ShopImageError as exc:
        flash(str(exc), "error")
    else:
        flash("Photo removed.", "success")
    return redirect(url_for("shop_admin.listing_detail", product_id=int(product_id)))


@bp.post("/online-store/listings/<int:product_id>/images/<int:media_id>/cover")
@shop_editor_required
@require_csrf
def cover_listing_image(product_id, media_id):
    _scoped_product(product_id)
    try:
        set_cover(product_id, media_id)
    except ShopImageError as exc:
        flash(str(exc), "error")
    else:
        flash("Cover photo updated.", "success")
    return redirect(url_for("shop_admin.listing_detail", product_id=int(product_id)))


@bp.post("/online-store/listings/<int:product_id>/images/<int:media_id>/move")
@shop_editor_required
@require_csrf
def move_listing_image(product_id, media_id):
    _scoped_product(product_id)
    direction = "up" if (request.form.get("direction") or "up") == "up" else "down"
    try:
        move_image(product_id, media_id, direction)
    except (ShopImageError, ValueError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("shop_admin.listing_detail", product_id=int(product_id)))


# ---------------------------------------------------------------------------
# shop settings: the three home-page artwork slots
# ---------------------------------------------------------------------------


@bp.route("/online-store/shop-settings", methods=["GET", "POST"])
@shop_editor_required
def shop_settings():
    """Manage the three home-page images (hire / buy / repairs)."""
    if request.method == "POST":
        validate(CSRF_SCOPE)
        changed = 0
        uploaded = set()
        for key, label in HOME_SLOTS:
            upload = request.files.get(f"image_{key}")
            if upload is not None and (getattr(upload, "filename", "") or "").strip():
                try:
                    set_home_image(key, upload)
                except ShopImageError as exc:
                    flash(f"{label}: {exc}", "error")
                else:
                    changed += 1
                    uploaded.add(key)
            # An upload replaces the slot; a stale "remove" tick in the same post
            # must not delete the photo that was just chosen.
            if request.form.get(f"remove_{key}") and key not in uploaded:
                try:
                    if clear_home_image(key):
                        changed += 1
                except ShopImageError as exc:
                    flash(f"{label}: {exc}", "error")
        if changed:
            flash("Home-page images updated.", "success")
        elif not any(
            (request.files.get(f"image_{key}") and (getattr(request.files.get(f"image_{key}"), "filename", "") or "").strip())
            or request.form.get(f"remove_{key}")
            for key, _label in HOME_SLOTS
        ):
            flash("Choose an image for a slot first.", "error")
        return redirect(url_for("shop_admin.shop_settings"))

    shop_url = shop_public_url(url_for("public.store", _external=True))
    return render_template(
        "admin/shop_settings.html",
        slots=home_slots(),
        shop_url=shop_url,
        listings_url=url_for("shop_admin.listings"),
    )


@bp.post("/online-store/shop-settings/home-image/<slot>")
@shop_editor_required
@require_csrf
def upload_home_image(slot):
    try:
        set_home_image(slot, request.files.get("image"))
    except ShopImageError as exc:
        flash(str(exc), "error")
    else:
        flash("Home-page image saved.", "success")
    return redirect(url_for("shop_admin.shop_settings"))


@bp.post("/online-store/shop-settings/home-image/<slot>/remove")
@shop_editor_required
@require_csrf
def remove_home_image(slot):
    try:
        clear_home_image(slot)
    except ShopImageError as exc:
        flash(str(exc), "error")
    else:
        flash("Home-page image removed.", "success")
    return redirect(url_for("shop_admin.shop_settings"))


# ---------------------------------------------------------------------------
# public media
# ---------------------------------------------------------------------------


@bp.route("/store/media/<int:media_id>")
def media(media_id):
    """Serve one listing photo's sanitised bytes from the database."""
    row = image_row(media_id)
    if row is None:
        abort(404)
    product_id = int(row.get("product_id") or 0)
    from app.services.settings import get_company_settings
    is_public = bool(get_company_settings()['store_enabled']) and product_is_public(product_id)
    if not is_public and not _preview_allowed(product_id):
        abort(404)
    data, mime, version, _owner = image_bytes(media_id)
    if data is None:
        abort(404)
    response = _image_response(data, mime, f"shop-media-{media_id}-{version}")
    if not is_public:
        response.headers['Cache-Control'] = 'private, no-store, max-age=0'
    return response


@bp.route("/store/home-image/<slot>")
def home_image(slot):
    """Serve one home-page slot's artwork from the database."""
    from app.services.settings import get_company_settings
    is_public = bool(get_company_settings()['store_enabled'])
    if not is_public:
        if _active_user() is None:
            abort(404)
        from app.routes.shop_enquiries import _live_account
        _live_account(required_modules='online_store')
    data, mime, version = home_image_bytes(slot)
    if data is None:
        abort(404)
    response = _image_response(data, mime, f"shop-home-{slot}-{version}")
    if not is_public:
        response.headers['Cache-Control'] = 'private, no-store, max-age=0'
    return response


def _image_response(data, mime, etag):
    response = make_response(data)
    response.headers["Content-Type"] = mime or "image/jpeg"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "public, max-age=3600"
    response.set_etag(etag)
    try:
        response = response.make_conditional(request)
    except Exception:
        pass
    return response


def _preview_allowed(product_id):
    """Signed in, active, allowed the shop module, and inside this product's depot."""
    if _active_user() is None:
        return False
    if not user_can_module(session, MODULE_KEY):
        return False
    from app.routes.shop_enquiries import _live_account, _account_branch_ids
    scope = _account_branch_ids(_live_account(required_modules='online_store'))
    product = get_product(product_id)
    if product is None:
        return False
    if scope is None:
        return True
    if len(scope) == 0:
        return False
    branch_id = product.get("branch_id")
    if branch_id is None:
        return True
    try:
        return int(branch_id) in [int(value) for value in scope]
    except (TypeError, ValueError):
        return False


@bp.route('/online-store/listings/<int:product_id>/preview', methods=['GET','POST'])
@shop_editor_required
def preview_listing(product_id):
    product = _scoped_product(product_id)
    if request.method == 'POST':
        validate(CSRF_SCOPE)
        from app.services.shop_catalogue import clean_listing_fields
        fields = clean_listing_fields(request.form)
        product['store_title'] = fields.get('store_title') or product['name']
        product['store_description'] = fields.get('store_description') or product.get('description','')
    from app.routes.shop_front import context
    response = make_response(render_template('public/shop_product.html', **context('shop'), product=product, staff_preview=True))
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response
