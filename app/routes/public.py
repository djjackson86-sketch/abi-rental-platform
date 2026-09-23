from flask import Blueprint, abort, flash, make_response, redirect, render_template, request, url_for
from urllib.parse import urlparse

import secrets

from app.db import get_db
from app.services import branches as branches_service
from app.services import consent, group_images, pdf_documents, popia_pack, portal, portal_intake
from app.services.orders import (
    PUBLIC_BOOKING_NOTE,
    PUBLIC_SOURCE_SYSTEM,
    build_multi_item_order_payload,
    create_order,
    get_order,
    order_items,
)
from app.services.settings import get_company_settings, global_vat_rate

bp = Blueprint("public", __name__)

# How long a browser may cache a branch's QR image. A day: long enough that a counter screen or a
# cashier's browser does not re-render it on every page view, short enough that a corrected
# ``public_base_url`` stops being served within a shift (the QR's *content* is the link, so a
# stale image is a stale link).
PORTAL_QR_MAX_AGE_SECONDS = 86400

#: How long a browser may cache a category photo. Shorter than the QR: staff replace these from
#: the group form, and a stale header image is visible while a stale QR only hurts when scanned.
CATEGORY_IMAGE_MAX_AGE_SECONDS = 3600


def _public_products():
    # Ticket ABI-341953038(3): a trailer flagged "Trailer under maintenance" is
    # not offered in the online store until the flag is released. It stays fully
    # visible in the back office inventory.
    # The effective tax rate travels with the product so the booking page's live estimate can
    # mirror calculate_line() exactly instead of guessing with the global VAT rate (T3b).
    return get_db().execute(
        "SELECT p.*, COALESCE(t.rate, 0) AS tax_rate FROM products p "
        "LEFT JOIN tax_profiles t ON t.id = p.tax_profile_id "
        "WHERE p.active = 1 AND p.public_visible = 1 "
        "AND COALESCE(p.under_maintenance, 0) = 0 ORDER BY p.name"
    ).fetchall()


def _public_product(product_id):
    return get_db().execute(
        "SELECT p.*, COALESCE(t.rate, 0) AS tax_rate FROM products p "
        "LEFT JOIN tax_profiles t ON t.id = p.tax_profile_id "
        "WHERE p.id = ? AND p.active = 1 AND p.public_visible = 1 "
        "AND COALESCE(p.under_maintenance, 0) = 0",
        (product_id,),
    ).fetchone()


def _store_sections():
    """The public store grouped by category (programme phase 11 / §C1).

    One section per active, store-visible ``product_groups`` row (sorted by ``sort_order``), each
    carrying the products that belong to it; every product with no group lands in a trailing
    "Other" section. A category with no visible products is skipped rather than rendered empty,
    and ``has_image`` tells the template whether to draw the category photo or the name fallback —
    the blob bytes themselves are never handed to a template.
    """
    db = get_db()
    groups = db.execute(
        "SELECT * FROM product_groups WHERE active = 1 AND becomes_store_visible = 1 "
        "ORDER BY sort_order ASC, name ASC"
    ).fetchall()
    visible_group_ids = {group["id"] for group in groups}
    by_group = {}
    other = []
    for product in _public_products():
        # A product whose group is not a store-visible section (no group at all, an inactive
        # category, or one hidden from the store) lands in "Other" rather than disappearing: the
        # product is still active and public, and the store must never silently drop it because its
        # category was switched off.
        if product["product_group_id"] is not None and product["product_group_id"] in visible_group_ids:
            by_group.setdefault(product["product_group_id"], []).append(product)
        else:
            other.append(product)
    sections = []
    for group in groups:
        products = by_group.get(group["id"], [])
        if not products:
            continue
        sections.append({
            "id": group["id"],
            "name": group["name"],
            "description": group["description"],
            "has_image": group["image_blob"] is not None,
            "products": products,
        })
    if other:
        sections.append({"id": None, "name": "Other", "description": "", "has_image": False, "products": other})
    return sections


@bp.route("/store")
def store():
    settings = get_company_settings()
    if not settings["store_enabled"]:
        return render_template("public/store_unavailable.html", settings=settings)
    return render_template("public/store.html", settings=settings, sections=_store_sections())


@bp.route("/store/category-image/<int:group_id>")
def category_image(group_id):
    """Serve a category's stored photo (public, no auth; 404 when the group has none)."""
    data, mime = group_images.group_image_bytes(group_id)
    if data is None:
        abort(404)
    response = make_response(data)
    response.headers["Content-Type"] = mime
    response.headers["Cache-Control"] = f"public, max-age={CATEGORY_IMAGE_MAX_AGE_SECONDS}"
    return response


@bp.route("/store/products/<int:product_id>")
def product_detail(product_id):
    settings = get_company_settings()
    if not settings["store_enabled"]:
        return render_template("public/store_unavailable.html", settings=settings)
    product = _public_product(product_id)
    if not product:
        flash("Product not found", "error")
        return redirect(url_for("public.store"))
    return render_template("public/product.html", settings=settings, product=product)


def _booking_branch_id(form):
    """The collection branch for a booking: the chosen one when it is a real active branch, else
    the default active branch (the same rule ``_build_order_payload`` applies)."""
    raw = str(form.get("collect_branch_id") or "").strip()
    if raw:
        try:
            branch_id = int(raw)
        except ValueError:
            raise ValueError("Please choose one of the collection branches listed.")
        row = get_db().execute("SELECT id FROM branches WHERE id = ? AND active = 1", (branch_id,)).fetchone()
        if row:
            return branch_id
        raise ValueError("Please choose one of the collection branches listed.")
    return branches_service.default_branch_id()


def _booking_reference():
    """A short, unique reference for one booking, stored as the order's ``source_id``."""
    return f"BOOK-{secrets.token_hex(4).upper()}"


def _booking_scalar_keys():
    return (
        "name", "phone", "email", "address_line1", "suburb", "city", "province",
        "postal_code", "marketing_opt_in", "collect_branch_id", "start_date", "start_time",
        "end_date", "end_time", "notes",
    )


def _booking_posted(form):
    """Echo back the scalar values and the de-duplicated trailer selection for a re-render."""
    scalars = {}
    for key in _booking_scalar_keys():
        value = form.get(key)
        if value not in (None, ""):
            scalars[key] = value
    product_ids = form.getlist("product_id") if hasattr(form, "getlist") else []
    quantities = form.getlist("quantity") if hasattr(form, "getlist") else []
    selected = {}
    for index, raw_id in enumerate(product_ids):
        raw_id = str(raw_id).strip()
        if not raw_id:
            continue
        raw_qty = str(quantities[index]).strip() if index < len(quantities) else ""
        try:
            qty = int(raw_qty) if raw_qty else 1
        except (TypeError, ValueError):
            qty = 1
        if qty <= 0:
            continue
        selected[raw_id] = selected.get(raw_id, 0) + qty
    return scalars, selected


def _prefill_from_args(args):
    """Values the booking page should start with, read from a redirect's query string."""
    scalars = {}
    for key in _booking_scalar_keys():
        value = args.get(key)
        if value:
            scalars[key] = value
    selected = {}
    for pid in args.getlist("product_id"):
        pid = str(pid).strip()
        if pid:
            selected[pid] = selected.get(pid, 0) + 1
    return scalars, selected


def _booking_context(settings, status=200, error=None, scalars=None, selected=None, candidates=None, intake_closed=None):
    context = {
        "settings": settings,
        "sections": _store_sections(),
        "branches": branches_service.branch_options(),
        "vat_rate": global_vat_rate(),
        "tax_mode": settings["tax_mode"],
        "error": error,
        "candidates": candidates or [],
        "intake_closed": (not portal_intake.registration_is_open()) if intake_closed is None else intake_closed,
        "intake_closed_message": portal_intake.INTAKE_CLOSED_MESSAGE,
    }
    html = render_template(
        "public/book.html",
        **context,
        form_values=scalars or {},
        selected=selected or {},
    )
    return (html, status) if status != 200 else html


@bp.route("/store/book", methods=["GET", "POST"])
def book():
    """The public multi-trailer booking page (phase 12, §C2).

    GET renders the form: branch chooser, pickup/return period, the trailer list grouped by
    category (with the category photo), a per-trailer quantity control, a live estimate and a
    single customer block. POST validates the selection and per-item availability **first**, then
    reuses §B2's dedupe and §P1's consent to create/link the customer and write **one** order with
    one line per selected trailer. The D11 gate shuts the whole path while the notice is
    unfinished, exactly like the portal form.
    """
    settings = get_company_settings()
    if not settings["store_enabled"]:
        return render_template("public/store_unavailable.html", settings=settings)
    if request.method == "GET":
        scalars, selected = _prefill_from_args(request.args)
        return _booking_context(settings, scalars=scalars, selected=selected)

    form = request.form
    if not portal_intake.registration_is_open():
        scalars, selected = _booking_posted(form)
        return _booking_context(settings, intake_closed=True, scalars=scalars, selected=selected)
    # A filled honeypot is a bot: it gets the store back and leaves no record.
    if portal_intake.honeypot_triggered(form):
        return redirect(url_for("public.store"))
    scalars, selected = _booking_posted(form)
    # D10: consent is required server-side and must precede any write.
    if not consent.acceptance_given(form.get("popia_consent")):
        return _booking_context(settings, status=400, error=consent.consent_required_error(), scalars=scalars, selected=selected)
    try:
        values = portal_intake.submission_values(form)
    except ValueError as exc:
        return _booking_context(settings, status=400, error=str(exc), scalars=scalars, selected=selected)
    # D6: a possible duplicate is a question for the customer, never a silent merge.
    decision = str(form.get("decision") or "").strip()
    if not decision:
        candidates = portal_intake.find_possible_matches(values["name"], values["phone"], values["email"])
        if candidates:
            return _booking_context(settings, candidates=candidates, scalars=scalars, selected=selected)
    # Resolve the branch, then validate the trailer selection + availability BEFORE any customer is
    # created, so a refused booking leaves no stray client behind.
    try:
        branch_id = _booking_branch_id(form)
    except ValueError as exc:
        return _booking_context(settings, status=400, error=str(exc), scalars=scalars, selected=selected)
    try:
        _payload, order_form = build_multi_item_order_payload(form)
    except ValueError as exc:
        return _booking_context(settings, status=400, error=str(exc), scalars=scalars, selected=selected)
    # §B2 dedupe: create or link the client (a blocked client is refused here).
    try:
        result = portal_intake.create_or_link_customer(form, branch_id, decision, slug="store")
    except ValueError as exc:
        return _booking_context(settings, status=400, error=str(exc), scalars=scalars, selected=selected)
    # §P1: record the acceptance on the customer it belongs to, on the public-booking channel.
    consent.record_consent(result["customer_id"], consent.CHANNEL_PUBLIC_BOOKING, form.get("popia_consent"))
    note = PUBLIC_BOOKING_NOTE
    customer_note = str(form.get("notes") or "").strip()
    if customer_note:
        note = f"{note}. {customer_note}"
    order_form.set("customer_id", str(result["customer_id"]))
    order_form.set("booking_type", "return")
    order_form.set("collect_branch_id", str(branch_id) if branch_id else "")
    order_form.set("return_branch_id", str(branch_id) if branch_id else "")
    order_form.set("notes", note)
    order_id = create_order(order_form)
    # Public-source marker so staff can tell a web request apart from a counter or import order.
    reference = _booking_reference()
    db = get_db()
    db.execute(
        "UPDATE orders SET source_system = ?, source_id = ? WHERE id = ?",
        (PUBLIC_SOURCE_SYSTEM, reference, order_id),
    )
    db.commit()
    return redirect(url_for("public.booking_confirmation", order_id=order_id))


@bp.post("/store/products/<int:product_id>/book")
def book_product(product_id):
    """One-click booking from a product page: fold into the multi-trailer page pre-filled (§C2).

    The old single-item implementation created its own order with no consent and no POPIA gate; the
    spec replaces it with a redirect into :func:`book` carrying the chosen trailer, so every public
    booking goes through the one gated, consent-carrying, de-duplicating flow instead of a second
    implementation.
    """
    settings = get_company_settings()
    if not settings["store_enabled"]:
        return render_template("public/store_unavailable.html", settings=settings), 403
    product = _public_product(product_id)
    if not product:
        flash("Product not found", "error")
        return redirect(url_for("public.store"))
    params = {
        "product_id": product_id,
        "quantity": request.form.get("quantity", "1"),
        "name": request.form.get("customer_name", "").strip(),
        "email": request.form.get("customer_email", "").strip(),
        "phone": request.form.get("customer_phone", "").strip(),
        "start_date": request.form.get("start_date", ""),
        "start_time": request.form.get("start_time", ""),
        "end_date": request.form.get("end_date", ""),
        "end_time": request.form.get("end_time", ""),
        "notes": request.form.get("notes", "").strip(),
    }
    return redirect(url_for("public.book", **params))


@bp.route("/store/booking/<int:order_id>")
def booking_confirmation(order_id):
    order = get_order(order_id)
    if not order:
        flash("Booking request not found", "error")
        return redirect(url_for("public.store"))
    return render_template("public/confirmation.html", settings=get_company_settings(), order=order, items=order_items(order_id))


def _privacy_back_url():
    """Where "back" goes: the page you came from if it is ours, else the store.

    A referrer is attacker-controlled, so it is only used when its host matches this
    request's host — otherwise a link on the notice page could be turned into an open
    redirect. Nothing else on the page depends on the referrer.
    """
    store_url = url_for("public.store")
    referrer = request.referrer or ""
    if not referrer:
        return store_url
    parsed = urlparse(referrer)
    if parsed.scheme in ("http", "https") and parsed.netloc == request.host:
        return referrer
    return store_url


@bp.route("/privacy")
def privacy_notice():
    """The customer-facing privacy notice (feature P §P1).

    Deliberately **not** gated by ``store_enabled``: a customer has to be able to read
    the notice — and find out how to complain — even while the online store is switched
    off (POPIA s18 wants the notice at the point of collection; "our online shop is
    closed" is not a reason to withhold it).

    The page is driven by the reviewed document in ``docs/popia/``, not by a second copy
    of it in code (decision D11): while ``PRIVACY-NOTICE.md`` still carries any
    placeholder token the route serves the short interim page, and the moment the open
    facts are filled in the same route serves the full notice — no code change, and no
    chance of a bracketed token reaching a customer in either state.
    """
    settings = get_company_settings()
    back_url = _privacy_back_url()
    published = consent.published_notice()
    if published:
        # The wizard's notice is the live one. It is generated from Sano's own answers and cannot
        # contain a placeholder, so a customer only ever reads wording that has been published.
        from app.routes.popia import render_markdown_html

        return render_template(
            "public/privacy_notice_published.html",
            settings=settings,
            back_url=back_url,
            notice=published,
            notice_html=render_markdown_html(published["notice_text"]),
        )
    if not popia_pack.is_complete(popia_pack.PRIVACY_NOTICE_KEY):
        return render_template(
            "public/privacy_notice_interim.html",
            settings=settings,
            back_url=back_url,
        )
    return render_template(
        "public/privacy_notice.html",
        settings=settings,
        notice=popia_pack.notice_metadata(popia_pack.PRIVACY_NOTICE_KEY),
        back_url=_privacy_back_url(),
    )


def _portal_context(branch, **extra):
    """Everything the portal pages need, so the three routes cannot disagree about the branch."""
    context = {
        "settings": get_company_settings(),
        "branch": branch,
        "portal_url": portal.portal_url(branch, request.url_root),
        "intake_closed_message": portal_intake.INTAKE_CLOSED_MESSAGE,
    }
    context.update(extra)
    return context


@bp.route("/portal/<slug>")
def branch_portal(slug):
    """A branch's public portal page (feature B, §B1 link + QR; §B2 the form itself).

    This is the page a customer reaches from the branch's shared link or its printed QR, so it *is*
    the registration form — §B1 shipped a placeholder here and §B2 replaced it, because a page that
    says "coming soon" behind a printed QR is a dead end in a customer's hand.

    GET only: the submission posts to ``/portal/<slug>/register``. Deliberately not gated by
    ``store_enabled`` (the online store and a branch's own sign-up sheet are separate surfaces); the
    per-branch ``portal_enabled`` flag is the switch that matters, and an unknown slug and a
    switched-off portal are the same 404 to the customer.
    """
    branch = portal.portal_branch(slug)
    if branch is None:
        abort(404)
    return _render_portal_form(branch)


def _render_portal_form(branch, status=200, intake_closed=None, **extra):
    """Render the registration form, optionally with an HTTP status (400 for a refused post).

    ``intake_closed`` defaults to the real gate so a caller that does not care cannot accidentally
    render a form while the notice is unfinished.
    """
    html = render_template(
        "public/portal_form.html",
        **_portal_context(
            branch,
            form_values=extra.pop("form_values", {}),
            error=extra.pop("error", None),
            lookup_error=extra.pop("lookup_error", None),
            intake_closed=(
                not portal_intake.registration_is_open() if intake_closed is None else intake_closed
            ),
            **extra,
        ),
    )
    return (html, status) if status != 200 else html


@bp.route("/portal/<slug>/register", methods=["GET", "POST"])
def branch_portal_register(slug):
    """The branch's self-registration form: capture → dedupe decision → create or link (§B2).

    The order of the checks is the safety story:

    1. an unknown or switched-off branch 404s before anything is read;
    2. while the published privacy notice still carries an open placeholder the write path is
       **shut** — the page says so in plain words and a POST writes nothing (decision D11);
    3. a filled honeypot is swallowed: a bot gets a page that looks like success and no record;
    4. the submission is validated, then the consent is required *server-side* — an unticked box
       (or a crafted ``popia_consent=0``) is refused and nothing is written (decision D10);
    5. only then does dedupe run. A possible match is **presented to the customer, not resolved for
       them** (decision D6): nothing is created, and the screen asks "Is this you?";
    6. the acceptance is recorded in the same request that writes the customer, on the portal
       channel, against the notice version.
    """
    branch = portal.portal_branch(slug)
    if branch is None:
        abort(404)

    if not portal_intake.registration_is_open():
        return _render_portal_form(branch, intake_closed=True)

    if request.method == "GET":
        return _render_portal_form(branch, intake_closed=False)

    form = request.form
    posted = dict(form)

    # A bot fills in the field nobody can see; a person never does. It gets the success page and
    # leaves no trace in the database.
    if portal_intake.honeypot_triggered(form):
        return render_template(
            "public/portal_confirm.html",
            **_portal_context(branch, reference=None, linked=False, first_name=""),
        )

    try:
        values = portal_intake.submission_values(form)
    except ValueError as exc:
        return _render_portal_form(branch, status=400, error=str(exc), form_values=posted, intake_closed=False)

    if not consent.acceptance_given(form.get("popia_consent")):
        return _render_portal_form(
            branch,
            status=400,
            error=consent.consent_required_error(),
            form_values=posted,
            intake_closed=False,
        )

    # D6: a possible duplicate is a question for the customer, never a silent merge.
    if not str(form.get("decision") or "").strip():
        candidates = portal_intake.find_possible_matches(values["name"], values["phone"], values["email"])
        if candidates:
            return render_template(
                "public/portal_exists.html",
                **_portal_context(branch, candidates=candidates, form_values=posted),
            )

    try:
        result = portal_intake.create_or_link_customer(
            form, branch["id"], form.get("decision"), slug=branch["public_slug"]
        )
    except ValueError as exc:
        return _render_portal_form(branch, status=400, error=str(exc), form_values=posted, intake_closed=False)

    # The acceptance is recorded on the record it belongs to, in the same request that wrote it.
    # ``record_consent`` refuses anything that is not a real acceptance, so a created client can
    # never carry a consent row for an unticked box.
    consent.record_consent(result["customer_id"], consent.CHANNEL_PORTAL, form.get("popia_consent"))

    return render_template(
        "public/portal_confirm.html",
        **_portal_context(
            branch,
            reference=result["reference"],
            linked=result["linked"],
            first_name=(values["name"].split() or [""])[0],
        ),
    )


@bp.route("/portal/<slug>/check", methods=["POST"])
def branch_portal_check(slug):
    """The standalone "am I already a customer?" lookup (feature B §B2, decision D8).

    POST only, deliberately: a lookup is a search of the client book, and a GET would put the name
    and number in the browser history, the referrer and every proxy log along the way. The answer is
    masked (first name, surname initial, last four digits of the number) and never echoes what was
    typed, and the in-process rate limiter caps it at 10 searches per address per 5 minutes
    (decision D7) — keyed on a salted digest, so the address itself is never stored.
    """
    branch = portal.portal_branch(slug)
    if branch is None:
        abort(404)

    if not portal_intake.registration_is_open():
        return render_template(
            "public/portal_check.html", **_portal_context(branch, closed=True, result=None, rate_limited=False)
        )

    if not portal_intake.allow_lookup(request.remote_addr):
        return (
            render_template(
                "public/portal_check.html", **_portal_context(branch, closed=False, result=None, rate_limited=True)
            ),
            429,
        )

    name = request.form.get("name", "")
    phone = request.form.get("phone", "")
    if not name.strip() or not phone.strip():
        return render_template(
            "public/portal_check.html",
            **_portal_context(branch, closed=False, rate_limited=False, result={"found": False, "reason": "missing"}),
        )
    result = portal_intake.lookup_public(name, phone)
    return render_template(
        "public/portal_check.html",
        **_portal_context(branch, closed=False, rate_limited=False, result=result),
    )


@bp.route("/portal/<slug>/qr.png")
def branch_portal_qr(slug):
    """The branch's QR as a PNG, rendered in process (decisions D4 and D5).

    The same gate as the page: an unknown slug or a disabled portal 404s, so a QR that was printed
    before a branch was switched off stops resolving rather than opening a dead form. Nothing is
    written to disk, and the link is never handed to a third-party QR service.

    ``?box=`` is the one knob the admin print sheet (**B3**) needs: the same code path, rendered
    heavier for paper than for a screen preview. It is clamped (:func:`portal.clamp_box_size`) and
    junk falls back to the house default, so a query string can neither 500 nor hand the app a
    memory hole. The default is unchanged, so every §B1 caller gets the same bytes as before.
    """
    branch = portal.portal_branch(slug)
    if branch is None:
        abort(404)
    box_size = portal.clamp_box_size(request.args.get("box"))
    png = portal.qr_png_bytes(portal.portal_url(branch, request.url_root), box_size=box_size)
    response = make_response(png)
    response.headers["Content-Type"] = "image/png"
    response.headers["Cache-Control"] = f"public, max-age={PORTAL_QR_MAX_AGE_SECONDS}"
    return response


@bp.route("/portal/<slug>/qr.pdf")
def branch_portal_qr_sheet(slug):
    """The printable A4 sheet for a branch's portal code (Don's decision on 23 Sept).

    Same gate as the page and the PNG, so a sheet can never be printed for a link that 404s:
    an unknown slug, or a portal that has been switched off, is a 404 here too.

    The sheet is a real PDF with the code drawn as vectors from the very matrix the PNG
    encodes (:func:`portal.qr_matrix`), so the printed code and the endpoint cannot disagree.
    It is served ``inline`` because staff open it to print it, and it is the counterpart of the
    on-screen print sheet at ``settings.portal_print``, not a replacement for it.
    """
    branch = portal.portal_branch(slug)
    if branch is None:
        abort(404)
    url = portal.portal_url(branch, request.url_root)
    settings = get_company_settings()
    sheet = pdf_documents.qr_sheet_pdf_bytes(
        branch["name"],
        url,
        portal.qr_matrix(url),
        company_name=(settings["company_name"] or "").strip(),
        address=portal.branch_address(branch),
    )
    response = make_response(sheet)
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f'inline; filename="{slug}-trailer-portal.pdf"'
    response.headers["Cache-Control"] = f"public, max-age={PORTAL_QR_MAX_AGE_SECONDS}"
    return response
