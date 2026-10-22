"""Routes for branch enquiries from the online store.

Public surface
--------------
``POST /store/enquiries/request``  ``shop_enquiries.submit``
``GET  /store/request/<token>``    ``shop_enquiries.receipt``

The public POST is CSRF-checked with the ``shop_public`` scope and refuses to
write anything unless the request is fully valid. It never creates a customer,
order, payment or stock movement — see ``app.services.shop_enquiries``.

Staff surface
-------------
``/online-store/enquiries`` and ``/<id>`` live behind a sign-in, the
``online_store`` module **re-checked against the live users row**, and the
session's branch scope. Status, assignment, internal notes and the hand-off to
the normal New order screen are all audited.
"""

from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for,
)

from app.db import get_db
from app.routes.auth import login_required
from app.services import access
from app.services import csrf
from app.services import shop_enquiries
from app.services.reports import row_dict
from app.services.settings import get_company_settings

bp = Blueprint("shop_enquiries", __name__)

PUBLIC_CSRF_SCOPE = "shop_public"
ADMIN_CSRF_SCOPE = "shop_enquiry_admin"


@bp.after_request
def private_enquiry_response(response):
    if response.mimetype == 'text/html':
        response.headers['Cache-Control'] = 'private, no-store, max-age=0'
        response.headers['Referrer-Policy'] = 'no-referrer'
    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wants_json():
    if request.is_json:
        return True
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    best = request.accept_mimetypes.best
    if not best:
        return False
    return best == "application/json" and request.accept_mimetypes[best] > request.accept_mimetypes["text/html"]


def _store_available():
    try:
        return bool(get_company_settings()["store_enabled"])
    except Exception:
        return False


def _back_url():
    """Where a refused public post returns to: our own store page, never elsewhere."""
    referrer = request.referrer or ""
    if referrer:
        from urllib.parse import urlparse

        parsed = urlparse(referrer)
        if parsed.netloc == request.host:
            path = parsed.path or "/"
            if path.startswith("/store") or path.startswith("/online-store"):
                return referrer
    return url_for("public.store")


def _public_refusal(message, status=400):
    """Refuse a public submission without writing: JSON for scripts, flash for people."""
    if _wants_json():
        return jsonify({"ok": False, "message": message}), status
    flash(message, "error")
    return redirect(_back_url(), code=303)


def _live_account(required_modules=("orders", "online_store")):
    """The signed-in account, re-read from the database.

    A session is not proof of anything: the account could have been deactivated
    or had the module removed since sign-in. This is the current-state check the
    enquiry screens rely on, not the stale ``session["staff_modules"]`` copy.

    **Either** ``orders`` or ``online_store`` is enough: the depot staff who
    receive the new-enquiry alert work from Orders and do not carry the
    online-store administration module. Editing products/store settings still
    requires ``online_store`` — that is a different screen and a different gate.
    """
    if isinstance(required_modules, str):
        required_modules = (required_modules,)
    try:
        user_id = int(session.get("user_id") or 0)
    except (TypeError, ValueError):
        user_id = 0
    if not user_id:
        abort(403)
    row = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        abort(403)
    account = row_dict(row)
    if not int(account.get("active") or 0):
        abort(403)
    if account.get("role") == "owner":
        return account
    modules = access.user_module_keys_from_row(account)
    if modules is None:
        settings_row = get_db().execute(
            "SELECT staff_permissions_json FROM company_settings WHERE id = 1"
        ).fetchone()
        modules = access.staff_modules_from_settings(row_dict(settings_row) if settings_row else None)
    if not any(module in (modules or []) for module in required_modules):
        abort(403)
    return account


def _account_branch_ids(account, respect_active=True):
    """Branch ids this account may open, read from the database (or ``None`` for all).

    Deliberately **not** the session's cached ``branch_ids``: a permission edit
    must take effect on the next request, not on the next sign-in. An account
    with no explicit grants falls back to its primary ``users.branch_id``, which
    is the historic single-branch rule. The active-branch choice can only
    narrow this set.
    """
    if not account:
        return []
    if account.get("role") == "owner" or int(account.get("can_view_all_branches") or 0):
        return None
    rows = get_db().execute(
        "SELECT branch_id FROM user_branch_access WHERE user_id = ? ORDER BY branch_id",
        (account["id"],),
    ).fetchall()
    ids = []
    for row in rows:
        try:
            value = int(row_dict(row)["branch_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if value and value not in ids:
            ids.append(value)
    if account.get("branch_id"):
        try:
            primary = int(account['branch_id'])
            if primary not in ids:
                ids.append(primary)
        except (TypeError, ValueError):
            pass
    try:
        active = int(session.get("active_branch_id") or 0) or None
    except (TypeError, ValueError):
        active = None
    if respect_active and active and active in ids:
        return [active]
    return ids


def _must_see(branch_id, account):
    # A valid alert may point at another explicitly granted depot while a list is filtered.
    allowed = _account_branch_ids(account, respect_active=False)
    if allowed is not None:
        try:
            if int(branch_id) not in allowed:
                abort(404)
        except (TypeError, ValueError):
            abort(404)


def _admin_enquiry(enquiry_id, account):
    enquiry = shop_enquiries.get_enquiry(enquiry_id)
    if not enquiry:
        abort(404)
    _must_see(enquiry["branch_id"], account)
    if account.get('role') != 'owner' and not int(account.get('can_view_all_branches') or 0):
        # Keep the ordinary order form's scope consistent with the authorised alert target.
        session['active_branch_id'] = int(enquiry['branch_id'])
    return enquiry


def _flash_and_back(message, category="success", enquiry_id=None):
    flash(message, category)
    if enquiry_id:
        return redirect(url_for("shop_enquiries.detail", enquiry_id=enquiry_id), code=303)
    return redirect(url_for("shop_enquiries.index"), code=303)


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

@bp.post("/store/enquiries/request")
def submit():
    """Accept one branch enquiry. Writes the enquiry, its items, nothing else."""
    if not _store_available():
        return _public_refusal("The online store is not taking requests right now.", 403)

    # CSRF first: a mismatched/missing token writes nothing (400 from csrf.validate).
    csrf.validate(PUBLIC_CSRF_SCOPE)

    try:
        result = shop_enquiries.submit_enquiry(request.form, address=request.remote_addr or "")
    except ValueError as exc:
        return _public_refusal(str(exc))

    return redirect(url_for("shop_enquiries.receipt", token=result["receipt_token"]), code=303)


@bp.get("/store/request/<token>")
def receipt(token):
    """Minimal public receipt: a reference, the branch, the type. No PII."""
    data = shop_enquiries.get_receipt(token)
    if not data:
        abort(404)
    return render_template(
        "public/shop_receipt.html",
        settings=get_company_settings(),
        receipt=data,
        store_url=url_for("public.store"),
        privacy_url=(
            url_for("public.privacy_notice")
            if "public.privacy_notice" in current_app.view_functions
            else None
        ),
    )


# ---------------------------------------------------------------------------
# Staff list
# ---------------------------------------------------------------------------

@bp.get("/online-store/enquiries")
@login_required
def index():
    account = _live_account()
    allowed = _account_branch_ids(account)
    all_branches = [row_dict(row) for row in access_branch_options()]
    if allowed is None:
        branches = all_branches
    else:
        branches = [branch for branch in all_branches if branch["id"] in allowed]

    requested = (request.args.get("branch") or "").strip()
    branch_id = None
    if requested:
        try:
            candidate = int(requested)
        except (TypeError, ValueError):
            candidate = None
        if candidate and (allowed is None or candidate in allowed):
            branch_id = candidate
    selected = str(branch_id) if branch_id else ""
    branch_label = next((b["name"] for b in branches if b["id"] == branch_id), "")

    status = (request.args.get("status") or "").strip()
    if status not in shop_enquiries.STATUSES:
        status = ""
    enquiry_type = (request.args.get("type") or "").strip()
    if enquiry_type not in shop_enquiries.ENQUIRY_TYPES:
        enquiry_type = ""
    query = (request.args.get("query") or "").strip()

    branch_ids = [branch_id] if branch_id else allowed
    enquiries, counts, total = shop_enquiries.list_enquiries(
        branch_ids=branch_ids,
        branch_id=branch_id,
        status=status,
        enquiry_type=enquiry_type,
        query=query,
    )
    return render_template(
        "admin/shop_enquiries.html",
        title="Store enquiries",
        enquiries=enquiries,
        counts=counts,
        total=total,
        branch_options=branches,
        selected_branch=selected,
        branch_label=branch_label,
        selected_status=status,
        selected_type=enquiry_type,
        query=query,
        statuses=shop_enquiries.STATUSES,
        status_labels=shop_enquiries.STATUS_LABELS,
        enquiry_types=shop_enquiries.ENQUIRY_TYPES,
        type_labels=shop_enquiries.TYPE_LABELS,
        new_badge=shop_enquiries.count_new_enquiries(allowed),
        csrf_token=csrf.issue_token(ADMIN_CSRF_SCOPE),
    )


def access_branch_options():
    """Active depots for the filter. Wrapped so tests can override it if needed."""
    from app.services.branches import branch_options

    return branch_options()


# ---------------------------------------------------------------------------
# Staff detail
# ---------------------------------------------------------------------------

@bp.get("/online-store/enquiries/<int:enquiry_id>")
@login_required
def detail(enquiry_id):
    account = _live_account()
    enquiry = _admin_enquiry(enquiry_id, account)
    staff = shop_enquiries.assignable_staff(enquiry["branch_id"])
    whatsapp_text = (
        f"Hello {enquiry['customer_name']}, thank you for your enquiry "
        f"with {enquiry.get('branch_name') or 'us'}."
    )
    return render_template(
        "admin/shop_enquiry.html",
        title="Store enquiry",
        enquiry=enquiry,
        staff=staff,
        statuses=shop_enquiries.STATUSES,
        status_labels=shop_enquiries.STATUS_LABELS,
        type_labels=shop_enquiries.TYPE_LABELS,
        whatsapp_url=shop_enquiries.whatsapp_link(enquiry.get("phone"), whatsapp_text),
        call_url=shop_enquiries.call_link(enquiry.get("phone")),
        customer_match=shop_enquiries.resolve_customer(enquiry),
        csrf_token=csrf.issue_token(ADMIN_CSRF_SCOPE),
    )


@bp.post("/online-store/enquiries/<int:enquiry_id>/status")
@login_required
def update_status(enquiry_id):
    account = _live_account()
    csrf.validate(ADMIN_CSRF_SCOPE)
    enquiry = _admin_enquiry(enquiry_id, account)
    status = (request.form.get("status") or "").strip()
    try:
        shop_enquiries.set_status(enquiry["id"], status, account["id"])
    except ValueError as exc:
        return _flash_and_back(str(exc), "error", enquiry["id"])
    return _flash_and_back(
        f"Enquiry marked {shop_enquiries.STATUS_LABELS.get(status, status).lower()}.",
        "success",
        enquiry["id"],
    )


@bp.post("/online-store/enquiries/<int:enquiry_id>/assignment")
@login_required
def update_assignment(enquiry_id):
    account = _live_account()
    csrf.validate(ADMIN_CSRF_SCOPE)
    enquiry = _admin_enquiry(enquiry_id, account)
    try:
        shop_enquiries.assign(enquiry["id"], request.form.get("assigned_user_id"), account["id"])
    except ValueError as exc:
        return _flash_and_back(str(exc), "error", enquiry["id"])
    return _flash_and_back("Assignment saved.", "success", enquiry["id"])


@bp.post("/online-store/enquiries/<int:enquiry_id>/note")
@login_required
def add_note(enquiry_id):
    account = _live_account()
    csrf.validate(ADMIN_CSRF_SCOPE)
    enquiry = _admin_enquiry(enquiry_id, account)
    try:
        shop_enquiries.add_note(enquiry["id"], request.form.get("note"), account["id"])
    except ValueError as exc:
        return _flash_and_back(str(exc), "error", enquiry["id"])
    return _flash_and_back("Internal note added.", "success", enquiry["id"])


@bp.post("/online-store/enquiries/<int:enquiry_id>/prepare-order")
@login_required
def prepare_order(enquiry_id):
    """Hand the enquiry to the normal New order screen.

    Two conservative options, both explicit staff choices:

    * attach an existing client when exactly one stored record matches the
      enquiry's email or phone;
    * otherwise create the client from the enquiry's details *now*, through the
      normal customer service. That client is unverified (``client_verified``
      stays NULL), so the existing pending/approval workflow still applies — this
      never auto-approves anybody.

    Nothing is marked converted here. The parent-owned orders route prefills the
    form, saves through the existing validation and approval rules, and then
    calls ``shop_enquiries.link_order()`` to close the loop.
    """
    account = _live_account()
    csrf.validate(ADMIN_CSRF_SCOPE)
    enquiry = _admin_enquiry(enquiry_id, account)

    action = (request.form.get("customer_action") or "prepare").strip()
    match = shop_enquiries.resolve_customer(enquiry)
    customer_id = match["customer_id"]

    if action == "create_customer":
        if customer_id:
            flash("That client already exists — the existing record will be used.", "info")
        else:
            try:
                customer_id = shop_enquiries.create_customer_for_enquiry(enquiry["id"], account["id"])
                flash(
                    "Client created from the enquiry. Verification is still required before pickup.",
                    "success",
                )
            except ValueError as exc:
                return _flash_and_back(str(exc), "error", enquiry["id"])

    target = url_for("orders.new", enquiry_id=enquiry["id"])
    if customer_id:
        target = url_for("orders.new", enquiry_id=enquiry["id"], customer_id=customer_id)
    elif match["ambiguous"]:
        flash(
            "More than one client matches this enquiry. Pick the right client on the order screen.",
            "info",
        )
    return redirect(target, code=303)
