from flask import Blueprint, flash, redirect, render_template, request, url_for
from urllib.parse import urlparse

from app.db import get_db
from app.services import popia_pack
from app.services.customers import create_customer
from app.services.orders import _build_order_payload, create_order, get_order, order_items
from app.services.settings import get_company_settings

bp = Blueprint("public", __name__)


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


@bp.route("/store")
def store():
    settings = get_company_settings()
    if not settings["store_enabled"]:
        return render_template("public/store_unavailable.html", settings=settings)
    products = _public_products()
    return render_template("public/store.html", settings=settings, products=products)


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
    if not popia_pack.is_complete(popia_pack.PRIVACY_NOTICE_KEY):
        return render_template(
            "public/privacy_notice_interim.html",
            settings=settings,
            back_url=_privacy_back_url(),
        )
    return render_template(
        "public/privacy_notice.html",
        settings=settings,
        notice=popia_pack.notice_metadata(popia_pack.PRIVACY_NOTICE_KEY),
        back_url=_privacy_back_url(),
    )
