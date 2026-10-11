import html
import re
from urllib.parse import urlparse

from flask import Blueprint, abort, current_app, flash, make_response, redirect, render_template, request, url_for

from app.db import get_db
from app.services import branches as branches_service
from app.services import consent, group_images, pdf_documents, popia_pack, portal, portal_intake
from app.services.customers import create_customer
from app.services.orders import _build_order_payload, create_order, get_order, order_items
from app.services.settings import get_company_settings

bp = Blueprint("public", __name__)
CATEGORY_IMAGE_MAX_AGE_SECONDS = 3600

#: How long a browser may cache a branch's QR image. A day: long enough that a counter
#: screen does not re-render it on every page view, short enough that a corrected
#: ``public_base_url`` stops being served within a shift (the QR's *content* is the link,
#: so a stale image is a stale link). Shared by the branch and the universal endpoints.
PORTAL_QR_MAX_AGE_SECONDS = 86400


def _public_products():
    # Ticket ABI-341953038(3): a trailer flagged "Trailer under maintenance" is
    # not offered in the online store until the flag is released. It stays fully
    # visible in the back office inventory.
    return get_db().execute(
        "SELECT * FROM products WHERE active = 1 AND public_visible = 1 "
        "AND COALESCE(under_maintenance, 0) = 0 ORDER BY name"
    ).fetchall()


def _public_product(product_id):
    return get_db().execute(
        "SELECT * FROM products WHERE id = ? AND active = 1 AND public_visible = 1 "
        "AND COALESCE(under_maintenance, 0) = 0",
        (product_id,),
    ).fetchone()


def _store_sections():
    db = get_db()
    groups = db.execute(
        "SELECT * FROM product_groups WHERE active = 1 AND becomes_store_visible = 1 "
        "ORDER BY sort_order ASC, name ASC"
    ).fetchall()
    visible_group_ids = {group["id"] for group in groups}
    by_group = {}
    other = []
    for product in _public_products():
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
    return render_template(
        "public/store.html",
        settings=settings,
        sections=_store_sections(),
        # POPIA s18 wants the notice at the point of collection: the storefront footer
        # offers it whenever the privacy-notice route is actually registered.
        privacy_url=(
            url_for("public.privacy_notice")
            if "public.privacy_notice" in current_app.view_functions
            else None
        ),
    )


@bp.route("/store/category-image/<int:group_id>")
def category_image(group_id):
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


@bp.post("/store/products/<int:product_id>/book")
def book_product(product_id):
    settings = get_company_settings()
    if not settings["store_enabled"]:
        return render_template("public/store_unavailable.html", settings=settings), 403
    product = _public_product(product_id)
    if not product:
        flash("Product not found", "error")
        return redirect(url_for("public.store"))
    customer_name = request.form.get("customer_name", "").strip()
    customer_email = request.form.get("customer_email", "").strip().lower()
    if not customer_name or not customer_email:
        flash("Name and email are required", "error")
        return render_template("public/product.html", settings=get_company_settings(), product=product), 400
    order_form = {
        "product_id": str(product_id),
        "quantity": request.form.get("quantity", "1"),
        "start_date": request.form.get("start_date", ""),
        "start_time": request.form.get("start_time", ""),
        "end_date": request.form.get("end_date", ""),
        "end_time": request.form.get("end_time", ""),
        "notes": f"Public booking request. {request.form.get('notes', '').strip()}".strip(),
    }
    try:
        # Validate the booking before the customer row is created, so a refused
        # request (e.g. a pickup outside the branch's trading hours) leaves no
        # stray customer behind.
        _build_order_payload(order_form)
        customer_id = create_customer({
            "customer_type": "individual",
            "name": customer_name,
            "email": customer_email,
            "phone": request.form.get("customer_phone", ""),
            "marketing_opt_in": request.form.get("marketing_opt_in", ""),
        })
        order_form["customer_id"] = str(customer_id)
        order_id = create_order(order_form)
    except ValueError as exc:
        flash(str(exc), "error")
        return render_template("public/product.html", settings=get_company_settings(), product=product), 400
    return redirect(url_for("public.booking_confirmation", order_id=order_id))


@bp.route("/store/booking/<int:order_id>")
def booking_confirmation(order_id):
    order = get_order(order_id)
    if not order:
        flash("Booking request not found", "error")
        return redirect(url_for("public.store"))
    return render_template("public/confirmation.html", settings=get_company_settings(), order=order, items=order_items(order_id))


# ---------------------------------------------------------------------------
# Privacy notice (feature P §P1)
# ---------------------------------------------------------------------------
#
# Markdown -> safe HTML for the published notice. Kept here (rather than pulling in the
# whole POPIA document-pack blueprint) so the public page renders the wizard's notice
# with no dependency on the internal compliance screens.

def _safe_notice_link(match):
    # Notice text is owner-editable: escaping text alone does not make href safe.
    from urllib.parse import urlsplit
    label, escaped_url = match.group(1), match.group(2)
    url = html.unescape(escaped_url)
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        return label
    try:
        parsed = urlsplit(url)
    except ValueError:
        return label
    allowed = (parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)) or parsed.scheme.lower() in ("mailto", "tel")
    allowed = allowed or (not parsed.scheme and (url.startswith("#") or (url.startswith("/") and not url.startswith("//"))))
    if not allowed:
        return label
    return f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{label}</a>'


def _md_inline(text):
    """Escape a fragment, then apply the inline Markdown the notice actually uses.

    Every fragment is HTML-escaped *before* any Markdown is applied, so a stray
    ``<script>`` in a document can never reach the page.
    """
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", _safe_notice_link, text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
    return text


def render_markdown_html(text):
    """Render the notice's Markdown to safe HTML for the published page."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    blocks = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].rstrip("\n")
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        heading = re.match(r"^#{1,6}\s+(.*)$", stripped)
        if heading:
            blocks.append(("h", len(heading.group(0).split()[0]), _md_inline(heading.group(1))))
            i += 1
            continue
        if re.fullmatch(r"([-*_])\1{2,}", stripped):
            blocks.append(("hr",))
            i += 1
            continue
        if stripped.startswith(">"):
            quote = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            blocks.append(("blockquote", _md_inline(" ".join(quote))))
            continue
        if stripped.startswith("|"):
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            blocks.append(("table", rows))
            continue
        item = re.match(r"^([-*+]|\d+[.)])\s+(.*)$", stripped)
        if item:
            ordered = item.group(1)[0].isdigit()
            entries = [(item.group(1), item.group(2))]
            i += 1
            while i < n:
                more = re.match(r"^([-*+]|\d+[.)])\s+(.*)$", lines[i].strip())
                if not more:
                    break
                entries.append((more.group(1), more.group(2)))
                i += 1
            blocks.append(("list", ordered, entries))
            continue
        para = [line]
        i += 1
        while i < n:
            nxt = lines[i].rstrip("\n")
            if not nxt.strip():
                break
            if re.match(r"^#{1,6}\s+", nxt.strip()) or re.fullmatch(r"([-*_])\1{2,}", nxt.strip()):
                break
            if nxt.strip().startswith(">") or nxt.strip().startswith("|"):
                break
            if re.match(r"^([-*+]|\d+[.)])\s+", nxt.strip()):
                break
            para.append(nxt)
            i += 1
        blocks.append(("para", para))

    parts = []
    for block in blocks:
        kind = block[0]
        if kind == "h":
            parts.append(f"<h{block[1]}>{block[2]}</h{block[1]}>")
        elif kind == "hr":
            parts.append("<hr>")
        elif kind == "blockquote":
            parts.append(f"<blockquote><p>{block[1]}</p></blockquote>")
        elif kind == "table":
            header = None
            body = []
            for raw in block[1]:
                cells = [c.strip() for c in raw.strip().strip("|").split("|")]
                if all(re.fullmatch(r"[-: ]+", c or "-") for c in cells):
                    continue
                if header is None:
                    header = cells
                else:
                    body.append(cells)
            rows = []
            if header is not None:
                rows.append("<tr>" + "".join(f"<th>{_md_inline(c)}</th>" for c in header) + "</tr>")
            for cells in body:
                rows.append("<tr>" + "".join(f"<td>{_md_inline(c)}</td>" for c in cells) + "</tr>")
            parts.append("<table>" + "".join(rows) + "</table>")
        elif kind == "list":
            tag = "ol" if block[1] else "ul"
            items = "".join(f"<li>{_md_inline(c)}</li>" for _m, c in block[2])
            parts.append(f"<{tag}>{items}</{tag}>")
        elif kind == "para":
            spans = []
            for raw in block[1]:
                spans.append(_md_inline(raw.rstrip()))
                spans.append("<br>" if raw.endswith("  ") else " ")
            parts.append(f"<p>{''.join(spans).strip()}</p>")
    return "\n".join(parts)


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
    off (POPIA s18 wants the notice at the point of collection).

    The page is driven by the reviewed document in ``docs/popia/``, not by a second copy
    of it in code (decision D11): while ``PRIVACY-NOTICE.md`` still carries any
    placeholder token the route serves the short interim page, and the moment the open
    facts are filled in (or the wizard publishes) the same route serves the full
    notice — no code change, and no chance of a bracketed token reaching a customer.
    """
    settings = get_company_settings()
    back_url = _privacy_back_url()
    published = consent.published_notice()
    if published:
        # The wizard's notice is the live one. It is generated from Sano's own answers and
        # cannot contain a placeholder, so a customer only ever reads wording that has been
        # published. Its own title is the first line; rendered as-is it would produce two
        # competing h1s, so the title becomes the page heading and the body starts beneath it.
        lines = published["notice_text"].strip().splitlines()
        title = "Our privacy notice"
        if lines and lines[0].startswith("# "):
            title = lines[0][2:].strip()
            lines = lines[1:]
        return render_template(
            "public/privacy_notice_published.html",
            settings=settings,
            back_url=back_url,
            notice=published,
            notice_title=title,
            notice_html=render_markdown_html("\n".join(lines).strip()),
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
        back_url=back_url,
    )


# ---------------------------------------------------------------------------
# Customer portal (feature B §B1/§B2)
# ---------------------------------------------------------------------------

def _portal_context(branch, **extra):
    """Everything the portal pages need, so the routes cannot disagree about the branch."""
    universal = bool(extra.get("universal"))
    context = {
        "settings": get_company_settings(),
        "branch": branch,
        "portal_url": (
            portal.universal_portal_url(request.url_root) if universal
            else portal.portal_url(branch, request.url_root)
        ),
        "intake_closed_message": portal_intake.INTAKE_CLOSED_MESSAGE,
        "universal": universal,
    }
    context.update(extra)
    return context


def _universal_portal_branch():
    """Synthetic branch context for the universal portal.

    Customers captured from the universal link are assigned to the default active branch
    internally so existing branch-scoped admin screens still have a home for the record,
    but the public page does not ask the customer to choose or know a branch.
    """
    branch_id = branches_service.default_branch_id()
    settings = get_company_settings()
    company_name = (settings["company_name"] if settings else "") or "Customer portal"
    return {
        "id": branch_id,
        "name": company_name,
        "public_slug": portal.UNIVERSAL_PORTAL_SOURCE_SLUG,
        "phone": (settings["phone"] or settings["store_contact_phone"] or "") if settings else "",
        "portal_intro": "Register your customer details once. Staff can find your record from any branch.",
        "address_line1": "",
        "city": "",
    }


def _render_portal_form(branch, status=200, intake_closed=None, **extra):
    """Render the registration form, optionally with an HTTP status (400 for a refused post).

    ``intake_closed`` defaults to the real gate so a caller that does not care cannot
    accidentally render a form while the notice is unfinished (decision D11).
    """
    html_doc = render_template(
        "public/portal_form.html",
        **_portal_context(
            branch,
            form_values=extra.pop("form_values", {}),
            error=extra.pop("error", None),
            lookup_error=extra.pop("lookup_error", None),
            intake_closed=(
                not portal_intake.registration_is_open() if intake_closed is None else intake_closed
            ),
            universal=extra.pop("universal", False),
            **extra,
        ),
    )
    return (html_doc, status) if status != 200 else html_doc


@bp.route("/portal")
def customer_portal():
    """The universal customer portal: one public link / one QR code for any customer.

    The D11 gate applies here exactly as it does to a branch portal: while the published
    privacy notice still carries an open placeholder the page states that registration
    is not open yet and shows no form, so nothing is collected before the notice is
    finished.
    """
    return _render_portal_form(_universal_portal_branch(), universal=True)


@bp.route("/portal/<slug>")
def branch_portal(slug):
    """A branch's public portal page (feature B, §B1 link + QR; §B2 the form itself).

    GET only: the submission posts to ``/portal/<slug>/register``. Deliberately not gated
    by ``store_enabled``; the per-branch ``portal_enabled`` flag is the switch that
    matters, and an unknown slug and a switched-off portal are the same 404.
    """
    branch = portal.portal_branch(slug)
    if branch is None:
        abort(404)
    return _render_portal_form(branch)


@bp.route("/portal/register", methods=["GET", "POST"])
def customer_portal_register():
    """Universal customer registration: no branch in the URL or QR.

    The gate is checked *first* and on POST as well as GET, so the universal link can
    never collect a submission while the privacy notice is unfinished (decision D11).
    """
    branch = _universal_portal_branch()
    if not portal_intake.registration_is_open():
        return _render_portal_form(branch, intake_closed=True, universal=True)

    if request.method == "GET":
        return _render_portal_form(branch, universal=True)

    form = request.form
    posted = dict(form)
    # A bot fills in the field nobody can see; it gets the success page and leaves no trace.
    if portal_intake.honeypot_triggered(form):
        return render_template(
            "public/portal_confirm.html",
            **_portal_context(branch, reference=None, linked=False, first_name="", universal=True),
        )
    try:
        values = portal_intake.submission_values(form)
    except ValueError as exc:
        return _render_portal_form(branch, status=400, error=str(exc), form_values=posted, universal=True)
    if not consent.acceptance_given(form.get("popia_consent")):
        return _render_portal_form(
            branch, status=400, error=consent.consent_required_error(), form_values=posted, universal=True
        )
    # D7 (release security audit): the shared lookup budget is charged *before* any dedupe
    # query, so the client-book search behind registration — the candidate list **and** the
    # decision flow that re-runs the match inside create_or_link_customer — is throttled
    # exactly like the standalone lookup. Over the limit the answer is 429 and nothing is
    # read or written.
    if not portal_intake.allow_lookup(request.remote_addr):
        return _render_portal_form(
            branch, status=429, intake_closed=False, rate_limited=True,
            form_values=posted, universal=True,
        )
    # D6: a possible duplicate is a question for the customer, never a silent merge.
    if not str(form.get("decision") or "").strip():
        candidates = portal_intake.find_possible_matches(values["name"], values["phone"], values["email"])
        if candidates:
            return render_template(
                "public/portal_exists.html",
                **_portal_context(branch, candidates=candidates, form_values=posted, universal=True),
            )
    # Re-check the gate immediately before the write: the notice could have been
    # withdrawn between the GET and a slow POST, and the public write path stays shut.
    if not portal_intake.registration_is_open():
        return _render_portal_form(branch, intake_closed=True, universal=True)
    try:
        result = portal_intake.create_or_link_customer(
            form,
            branch["id"],
            form.get("decision"),
            slug=portal.UNIVERSAL_PORTAL_SOURCE_SLUG,
        )
    except ValueError as exc:
        return _render_portal_form(branch, status=400, error=str(exc), form_values=posted, universal=True)
    # The acceptance is recorded against the notice genuinely in force — never a hard-coded
    # version — in the same request that wrote the customer (decision D10).
    consent.record_consent(result["customer_id"], consent.CHANNEL_PORTAL, form.get("popia_consent"))
    return render_template(
        "public/portal_confirm.html",
        **_portal_context(
            branch,
            reference=result["reference"],
            linked=result["linked"],
            first_name=(values["name"].split() or [""])[0],
            universal=True,
        ),
    )


@bp.route("/portal/<slug>/register", methods=["GET", "POST"])
def branch_portal_register(slug):
    """The branch's self-registration form: capture → dedupe decision → create or link (§B2).

    The order of the checks is the safety story:

    1. an unknown or switched-off branch 404s before anything is read;
    2. while the published privacy notice still carries an open placeholder the write path
       is **shut** — the page says so in plain words and a POST writes nothing (decision D11);
    3. a filled honeypot is swallowed: a bot gets a page that looks like success and no record;
    4. the submission is validated, then the consent is required *server-side* — an unticked box
       (or a crafted ``popia_consent=0``) is refused and nothing is written (decision D10);
    5. only then does dedupe run. A possible match is **presented to the customer, not resolved
       for them** (decision D6): nothing is created, and the screen asks "Is this you?";
    6. the acceptance is recorded in the same request that writes the customer, on the portal
       channel, against the notice version.
    """
    branch = portal.portal_branch(slug)
    if branch is None:
        abort(404)

    if not portal_intake.registration_is_open():
        return _render_portal_form(branch, intake_closed=True)

    if request.method == "GET":
        return _render_portal_form(branch)

    form = request.form
    posted = dict(form)

    if portal_intake.honeypot_triggered(form):
        return render_template(
            "public/portal_confirm.html",
            **_portal_context(branch, reference=None, linked=False, first_name=""),
        )

    try:
        values = portal_intake.submission_values(form)
    except ValueError as exc:
        return _render_portal_form(branch, status=400, error=str(exc), form_values=posted)

    if not consent.acceptance_given(form.get("popia_consent")):
        return _render_portal_form(
            branch,
            status=400,
            error=consent.consent_required_error(),
            form_values=posted,
        )

    # D7 (release security audit): the shared lookup budget is charged *before* any dedupe
    # query, so the client-book search behind registration — the candidate list **and** the
    # decision flow — is throttled exactly like the standalone lookup. Over the limit → 429.
    if not portal_intake.allow_lookup(request.remote_addr):
        return _render_portal_form(
            branch, status=429, intake_closed=False, rate_limited=True, form_values=posted
        )

    if not str(form.get("decision") or "").strip():
        candidates = portal_intake.find_possible_matches(values["name"], values["phone"], values["email"])
        if candidates:
            return render_template(
                "public/portal_exists.html",
                **_portal_context(branch, candidates=candidates, form_values=posted),
            )

    if not portal_intake.registration_is_open():
        return _render_portal_form(branch, intake_closed=True)

    try:
        result = portal_intake.create_or_link_customer(
            form, branch["id"], form.get("decision"), slug=branch["public_slug"]
        )
    except ValueError as exc:
        return _render_portal_form(branch, status=400, error=str(exc), form_values=posted)

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


@bp.route("/portal/check", methods=["POST"])
def customer_portal_check():
    """Universal lookup: masked result, no branch named to the customer.

    Respects the same gate as the form: while registration is not open the lookup answers
    with the closed message rather than searching the client book.
    """
    branch = _universal_portal_branch()
    closed = not portal_intake.registration_is_open()
    if not portal_intake.allow_lookup(request.remote_addr):
        return (
            render_template(
                "public/portal_check.html",
                **_portal_context(branch, closed=closed, result=None, rate_limited=True, universal=True),
            ),
            429,
        )
    name = request.form.get("name", "")
    phone = request.form.get("phone", "")
    if closed or not name.strip() or not phone.strip():
        result = {"found": False, "display": "", "reason": "missing"}
    else:
        result = portal_intake.lookup_public(name, phone)
    return render_template(
        "public/portal_check.html",
        **_portal_context(branch, closed=closed, rate_limited=False, result=result, universal=True),
    )


@bp.route("/portal/<slug>/check", methods=["POST"])
def branch_portal_check(slug):
    """The standalone "am I already a customer?" lookup (feature B §B2, decision D8).

    POST only, deliberately: a lookup is a search of the client book, and a GET would put
    the name and number in the browser history, the referrer and every proxy log along the
    way. The answer is masked (first name, surname initial, last four digits of the number)
    and never echoes what was typed, and the in-process rate limiter caps it at 10 searches
    per address per 5 minutes (decision D7) — keyed on a salted digest, so the address
    itself is never stored.
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


def _qr_response(png):
    response = make_response(png)
    response.headers["Content-Type"] = "image/png"
    response.headers["Cache-Control"] = f"public, max-age={PORTAL_QR_MAX_AGE_SECONDS}"
    return response


@bp.route("/portal/qr.png")
def customer_portal_qr():
    """The universal portal's QR as a PNG, rendered in process (no file, no external service)."""
    png = portal.qr_png_bytes(
        portal.universal_portal_url(request.url_root),
        box_size=portal.clamp_box_size(request.args.get("box")),
    )
    return _qr_response(png)


@bp.route("/portal/qr.pdf")
def customer_portal_qr_sheet():
    """The universal portal's A4 printable sheet (link + code as vectors)."""
    url = portal.universal_portal_url(request.url_root)
    settings = get_company_settings()
    company_name = (settings["company_name"] if settings else "").strip()
    sheet = pdf_documents.qr_sheet_pdf_bytes(
        "Customer portal",
        url,
        portal.qr_matrix(url),
        company_name=company_name,
        address="Universal customer registration link",
    )
    response = make_response(sheet)
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = 'inline; filename="customer-portal.pdf"'
    response.headers["Cache-Control"] = f"public, max-age={PORTAL_QR_MAX_AGE_SECONDS}"
    return response


@bp.route("/portal/<slug>/qr.png")
def branch_portal_qr(slug):
    """The branch's QR as a PNG, rendered in process (decisions D4 and D5).

    The same gate as the page: an unknown slug or a disabled portal 404s, so a QR that was
    printed before a branch was switched off stops resolving rather than opening a dead form.
    ``?box=`` is the one knob the admin print sheet needs; it is clamped, and junk falls back
    to the house default, so a query string can neither 500 nor hand the app a memory hole.
    """
    branch = portal.portal_branch(slug)
    if branch is None:
        abort(404)
    box_size = portal.clamp_box_size(request.args.get("box"))
    png = portal.qr_png_bytes(portal.portal_url(branch, request.url_root), box_size=box_size)
    return _qr_response(png)


@bp.route("/portal/<slug>/qr.pdf")
def branch_portal_qr_sheet(slug):
    """The printable A4 sheet for a branch's portal code.

    Same gate as the page and the PNG, so a sheet can never be printed for a link that 404s.
    The sheet draws the code as vectors from the very matrix the PNG encodes, so the printed
    code and the endpoint cannot disagree. Served ``inline`` because staff open it to print it.
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
