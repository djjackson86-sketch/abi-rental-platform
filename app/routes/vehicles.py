"""Staff licence-disc scan screen — programme phase 3 (feature A / A3).

Mirrors the house scan-screen pattern (``app/routes/admin.py::scan_barcode``): a GET form, a POST
that reads something, a flash and a redirect. Three deliberate differences, all from the plan:

* the subject is a NaTIS **licence disc**, so the POST takes the barcode text the browser decoded
  (or a typed-in fallback) and then shows a **review form** instead of jumping straight to a record
  — staff eyeball every parsed field, correct what is wrong, and choose the client;
* the camera decodes the PDF417 barcode **in the browser** and posts the text, so the server never
  sees an image at all. The old in-memory photo path — an eleven-variant image ladder that expanded
  a 12 MP phone photo to hundreds of megabytes — is retired along with the Render OOM it caused, and
  it is actively refused from the request headers (see :func:`scan_request_rejection`);
* a registration already recorded for another client is **refused with the owner named**, with an
  explicit "Transfer to this client" action — never a silent re-own (decision D3, rule from A2).

Module gating is the app-wide one: ``app/__init__.py::enforce_module_access`` maps these endpoints
through ``app/services/access.py``, so a sign-in without the ``scan_vehicle`` module gets a 403
rather than a hidden button.
"""

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, session, url_for
from app.services import csrf
from app.services.access import user_can_module

from app.routes.auth import login_required
from app.services import portal_registrations
from app.services import vehicle_disk as disk
from app.services import vehicles
from app.services.customers import find_customer_id_by_text, get_customer, search_customers
from app.services.settings import get_company_settings

bp = Blueprint("vehicles", __name__)

#: A licence-disc barcode payload is a few hundred bytes — the modern positional record is 148
#: characters and the longest legacy label-value disc stays well under ~2 KB. This cap is generous
#: by an order of magnitude, so a genuine disk is never refused, yet a crafted POST can never make
#: the parser chew on a megabyte of "barcode text".
MAX_DISC_TEXT_CHARS = 4000

#: The largest scan-screen POST body these routes will parse at all. A real submission (one hidden
#: text field carrying the browser-decoded barcode, plus a couple of ids) is well under a kilobyte;
#: anything approaching this is not a scan. Checked against the declared ``Content-Length`` header so
#: nothing larger is ever read into memory.
MAX_SCAN_REQUEST_BYTES = 64 * 1024

#: The photo-upload path is retired: the camera now decodes the PDF417 **in the browser** and posts
#: the decoded text, so any multipart scan POST is a stale client or a hand-crafted upload. It is
#: refused from the headers alone, *before* Werkzeug reads a byte of the body — the old server path
#: fed a 12 MP phone photo through an eleven-variant image ladder (including a 3x upscale, ~325 MB
#: for one buffer), which is what exhausted the 512 Mi Render instance. There is deliberately **no**
#: photo fallback: manual entry is the only fallback.
IMAGE_UPLOAD_REJECTED_MESSAGE = (
    "Photo uploads are not accepted on this screen any more — the camera reads the barcode in the "
    "browser. Scan the disk again, or type the details below."
)

#: A "barcode text" that is absurdly long is refused rather than parsed (see ``MAX_DISC_TEXT_CHARS``).
SCAN_TEXT_TOO_LARGE_MESSAGE = (
    "That scan text is far too long to be a licence disk. Scan again, or type the details below."
)

#: The one *parse* failure kind still reachable from the text path. A payload that decoded but holds
#: no vehicle fields must read differently from an empty submission, and staff are never blocked:
#: the sentence always comes with a form to type the fields.
DECODE_ERROR_MESSAGES = {
    disk.DiscErrorKind.UNPARSEABLE: (
        "The barcode was read but it holds no vehicle fields. Type the details below."
    ),
}

NO_INPUT_MESSAGE = "Scan the licence disk with the camera, or type the details below."


def scan_request_rejection(req):
    """Why a scan-screen POST must be refused before its body is parsed, or ``None`` to continue.

    Header-only by design: ``req.mimetype`` and ``req.content_length`` come from the request line
    and the request headers, so a multipart body (the retired photo upload) is never buffered,
    parsed or handed to an image library. This is the guard that removes the decode-OOM surface
    entirely; the caller must invoke it *before* touching ``req.form`` / ``req.files``.
    """
    if (req.mimetype or "").lower() == "multipart/form-data":
        return IMAGE_UPLOAD_REJECTED_MESSAGE
    length = req.content_length
    if length is None or length <= 0 or length > MAX_SCAN_REQUEST_BYTES:
        # Unknown-length/chunked submissions cannot be bounded from headers.
        # Browser form submissions declare their length; reject everything else
        # before Werkzeug is allowed to read an unbounded request stream.
        return SCAN_TEXT_TOO_LARGE_MESSAGE
    return None


def _to_int(value):
    try:
        return int(str(value).strip()) or None
    except (TypeError, ValueError):
        return None


def _truthy(value):
    """A query-string / hidden-field opt-in flag: ``1``/``on``/``yes``/``true`` (case-blind)."""
    return str(value or "").strip().lower() in {"1", "on", "yes", "true"}


def _customer_edit_redirect(customer_id, return_to_verification):
    """Land back on the customer edit screen, at the verification checklist when asked.

    The scanner is opened from the self-registration verification panel (which lives on
    ``customers.edit`` behind the ``#pending-registration`` anchor). When that panel sent staff
    here with ``return_to_verification=1`` they came to record the main vehicle and expect to be
    returned to the checklist they left — not dropped at the top of the page. Without the opt-in
    the redirect is exactly the historical one, so the ordinary scanner is unchanged.
    """
    url = url_for("customers.edit", customer_id=customer_id)
    if return_to_verification:
        url += "#pending-registration"
    return redirect(url)


def _blank_values():
    """An empty review form: every text field blank, both masses blank (never 0 — D3)."""
    values = {field: "" for field in vehicles.TEXT_FIELDS}
    values.update({field: "" for field in vehicles.MASS_FIELDS})
    values["source"] = vehicles.SOURCE_SCAN
    return values


def _posted_values(form):
    """The vehicle fields a submitted review form carries, echoed back for a re-render."""
    values = {field: (form.get(field) or "") for field in (*vehicles.TEXT_FIELDS, *vehicles.MASS_FIELDS)}
    values["source"] = (form.get("source") or "").strip() or vehicles.SOURCE_SCAN
    return values


def _review(values, customer_id=None, *, raw_text="", unparsed=None, needs_main_choice=False):
    """The context for the review panel, including the "already somebody else's" warning.

    The warning is computed *before* the save as well as after a refusal, so staff see it while
    they pick the client instead of only when the save fails. ``needs_main_choice`` is set when a
    save was refused because the client already has a main vehicle and staff did not say whether
    the vehicle being saved should replace it — the form highlights the choice.
    """
    existing = None
    if customer_id and (values.get("registration") or "").strip():
        existing = vehicles.get_customer_vehicle_by_registration(customer_id, values.get("registration"))
    return {
        "fields": values,
        "customer": get_customer(customer_id) if customer_id else None,
        "owner": vehicles.customer_for_vehicle_registration(values.get("registration") or ""),
        "raw_text": raw_text or values.get("raw_scan_text") or "",
        "unparsed": unparsed or {},
        "needs_main_choice": needs_main_choice,
        # The plate's own saved approval state when it is already recorded for this client, so
        # the review panel can show the unapproved red state instead of silently looking normal.
        "existing_vehicle_status": vehicles.vehicle_status(existing) if existing is not None else "",
        # "Licence disk expired on …" when the scanned/typed expiry is in the past — the review form
        # (and the customer form) can show the warning without recomputing the date rules.
        "expiry_state": vehicles.licence_disk_expiry_state(values.get("licence_disk_expiry")),
    }


def _render(review=None, *, customer_id=None, vehicle_role=None, start_scan=False, return_to_verification=False):
    preselected = get_customer(customer_id) if customer_id else None
    if review is not None and review.get("customer") is None and preselected is not None:
        review["customer"] = preselected
    # The review form's "Approve client?" dialog is armed from the *saved* state of the client
    # actually on the form (their verification flag and a still-pending portal registration), so
    # both doors into the scanner — the verification checklist's pre-selected client and a plain
    # scan where the client is picked on this page — show the same thing for the same client.
    review_customer = (review or {}).get("customer")
    pending_registration_id = ""
    if review_customer is not None:
        pending = portal_registrations.pending_registration_for_customer(review_customer["id"])
        if pending is not None:
            pending_registration_id = pending["id"]
    return render_template(
        "admin/scan_vehicle.html",
        settings=get_company_settings(),
        review=review,
        preselected_customer=preselected,
        field_labels=disk.FIELD_LABELS,
        # The customer form's "Scan main vehicle disk" link passes vehicle_role=main; the
        # panel's "Add another vehicle" link passes vehicle_role=additional. The review form uses
        # it to default the "make this the main vehicle?" choice (yes for a main-vehicle scan).
        vehicle_role=vehicle_role,
        # ``start_scan`` is the verification checklist's opt-in: open the camera on load. The
        # template marks the capture form so the shared scanner module is driven straight into its
        # own start routine — the server cannot grant camera permission, so the module's existing
        # blocked/permission fallback (and the always-visible scan + manual buttons) is the
        # degradation, never a blank screen.
        start_scan=start_scan,
        # ``return_to_verification`` rides the capture and review forms' hidden fields so the save
        # can hand control back to ``customers.edit#pending-registration``.
        return_to_verification=return_to_verification,
        # Saved client state the "Approve client?" dialog keys off (ABI-341953212): the client's
        # persisted ID verification and the id of their still-pending portal registration, if any.
        client_id_verified=portal_registrations.id_verified(review_customer) if review_customer else False,
        pending_portal_registration_id=pending_registration_id,
        scan_approval_csrf_token=csrf.issue_token("portal_approval"),
    )


def _read_scan(pasted):
    """Read one scan submission: ``(values, raw_text, unparsed, message, category)``.

    The camera decodes the barcode in the browser and posts the text (``disc_text``); there is no
    server-side image path any more. Never raises. Every failure ends in a message for staff **and**
    a review form they can fill in by hand: a disc the camera could not read must not stop a
    trailer going out.
    """
    blank, no_unparsed = _blank_values(), {}
    text = (pasted or "").strip()
    if not text:
        return blank, "", no_unparsed, NO_INPUT_MESSAGE, "error"
    if len(text) > MAX_DISC_TEXT_CHARS:
        return blank, "", no_unparsed, SCAN_TEXT_TOO_LARGE_MESSAGE, "error"
    return _scan_result(disk.parse_disc_text(text))


def _scan_result(parsed):
    """Turn a parse into review-form values plus the sentence (if any) that goes with it."""
    values = vehicles.fields_from_disc(parsed)
    unparsed = parsed.get("unparsed_fields") or {}
    raw_text = str(parsed.get("raw_text") or "")
    if not disk.is_disc_parseable(parsed):
        return values, raw_text, unparsed, DECODE_ERROR_MESSAGES[disk.DiscErrorKind.UNPARSEABLE], "error"
    return values, raw_text, unparsed, None, None


@bp.route("/scan-vehicle", methods=["GET", "POST"])
@login_required
def scan_vehicle():
    """Capture screen: read a disc in the browser, then review and allocate it to a client.

    The photo-upload path is retired. A multipart POST (or an oversized body) is refused from the
    request headers *before* the body is parsed, so no image is ever decoded server-side; staff get
    the manual review form instead. The camera posts the decoded barcode text urlencoded.

    Two opt-ins, both from the verification checklist on ``customers.edit`` (they are the only
    difference from the ordinary scanner, which they must never alter):

    * ``start_scan=1`` — drive the shared camera module straight into its start routine on load, so
      the checklist's "Scan main vehicle disk" button opens the camera itself rather than the
      scanner's landing screen. Honoured on a fresh GET only: the decode POST re-renders the same
      capture form, and re-arming the camera there would loop after every scan.
    * ``return_to_verification=1`` — carried on the query string and echoed through the capture and
      review forms so the finished save returns to ``customers.edit#pending-registration``.
    """
    if request.method == "POST":
        rejection = scan_request_rejection(request)
        if rejection:
            # Refused from the headers alone: the body is deliberately left unparsed, so only the
            # query string is read for the redirect context. Staff still get the manual review form.
            customer_id = _to_int(request.args.get("customer_id"))
            flash(rejection, "error")
            return _render(
                _review(_blank_values(), customer_id),
                customer_id=customer_id,
                vehicle_role=(request.args.get("vehicle_role") or "").strip() or None,
                return_to_verification=_truthy(request.args.get("return_to_verification")),
            )

    customer_id = _to_int(request.values.get("customer_id"))
    # ``main`` (the customer form's main-vehicle link) or ``additional`` (the vehicles panel's
    # "Add another vehicle"). It decides what the "make this the main vehicle?" choice defaults to.
    vehicle_role = (request.values.get("vehicle_role") or "").strip() or None
    # Auto-start is a GET-only opt-in (see the docstring): the capture form re-renders after every
    # decode POST, so honouring it there would reopen the camera in a loop.
    start_scan = request.method == "GET" and _truthy(request.args.get("start_scan"))
    # The return intent survives the decode POST through the capture form's hidden field, so it is
    # read from the merged query string + form, not the query string alone.
    return_to_verification = _truthy(request.values.get("return_to_verification"))

    if request.method == "GET":
        return _render(
            None,
            customer_id=customer_id,
            vehicle_role=vehicle_role,
            start_scan=start_scan,
            return_to_verification=return_to_verification,
        )

    if (request.form.get("action") or "").strip() == "manual":
        # The manual fallback: no scan at all, just type the fields.
        flash("Type the vehicle details and choose the client.", "info")
        return _render(
            _review(_blank_values(), customer_id),
            customer_id=customer_id,
            vehicle_role=vehicle_role,
            return_to_verification=return_to_verification,
        )

    values, raw_text, unparsed, message, category = _read_scan(request.form.get("disc_text"))
    if message:
        flash(message, category or "error")
    else:
        flash("Disc read — check every field before saving.", "success")
    return _render(
        _review(values, customer_id, raw_text=raw_text, unparsed=unparsed),
        customer_id=customer_id,
        vehicle_role=vehicle_role,
        return_to_verification=return_to_verification,
    )


@bp.post("/scan-vehicle/save")
@login_required
def save_scan_vehicle():
    """Save the reviewed scan against the chosen client (or transfer it, explicitly).

    The review form also posts ``make_main`` (``yes``/``no``): the vehicle captured becomes the
    client's main (invoiced) vehicle only when staff choose so. A different vehicle may not
    silently replace the current main one — when the client already has a main vehicle and no
    choice is posted, the save is refused with the choice highlighted.
    """
    values = _posted_values(request.form)
    customer_id = _to_int(request.form.get("customer_id"))
    # Carried through from the capture screen so the review re-renders and the finished save keep
    # the checklist's context: ``main`` keeps the "make this the main vehicle?" default and
    # ``return_to_verification`` decides whether the success redirect lands on the checklist anchor.
    vehicle_role = (request.form.get("vehicle_role") or "").strip() or None
    return_to_verification = _truthy(request.form.get("return_to_verification"))
    transfer = str(request.form.get("transfer") or "").strip().lower() in {"1", "on", "yes", "true"}
    make_main = vehicles.parse_main_choice(request.form.get("make_main"))
    # The review form's two saving actions (ABI-341953212). "Approve & Save Vehicle" is the
    # default — an older caller that posts no action at all keeps it — and records the vehicle as
    # approved; "Disapprove Vehicle" records the very same vehicle as unapproved. Either way the
    # vehicle stays on the client. Completing both checks also approves a pending sign-up.
    disapprove_vehicle = (request.form.get("action") or "").strip().lower() == "disapprove"
    vehicle_status = vehicles.STATUS_UNAPPROVED if disapprove_vehicle else vehicles.STATUS_APPROVED
    # The explicit Yes/No the "Approve client?" dialog posts. Yes (``approve_client=1``) is the
    # only thing that can approve a pending portal client from this screen; No — or no answer at
    # all — leaves the client's approval exactly as it was (the vehicle still saves).
    approve_client = _truthy(request.form.get("approve_client"))

    # The picker's hidden id is a convenience, not the source of truth: staff routinely type the
    # name (or pick the typeahead label) without the id landing in the form, which used to refuse a
    # vehicle after the client had visibly been chosen. Resolve from the visible text when the id
    # is missing — an ambiguous or unknown name still refuses.
    if not customer_id:
        customer_id = find_customer_id_by_text(request.form.get("customer_pick"), allow_pending=True)

    def review_response(review):
        """Re-render the review panel, keeping this screen's context for a retry."""
        return _render(
            review,
            vehicle_role=vehicle_role,
            return_to_verification=return_to_verification,
        )

    if (request.form.get("action") or "").strip() == "waiting":
        csrf.validate("portal_approval")
        try:
            vehicles.save_waiting_vehicle(request.form)
        except ValueError as exc:
            flash(str(exc), "error")
            return review_response(_review(values, None))
        flash("Vehicle approved and placed on the waiting list. No customer allocated.", "success")
        return redirect(url_for("vehicles.scan_vehicle"))

    if not customer_id:
        flash(
            "Choose the client from the list: type at least two letters of the name or number, "
            "then pick the client from the suggestions.",
            "error",
        )
        return review_response(_review(values, None, raw_text=values["raw_scan_text"]))
    customer = get_customer(customer_id)
    if customer is None:
        flash("That client is no longer on file — search for them again.", "error")
        return review_response(_review(values, None, raw_text=values["raw_scan_text"]))

    registration = values["registration"]

    # "Approve client? → Yes" is validated HERE, before a single row is written, with the same
    # checks the standalone approve route makes (a pending registration, inside the session's
    # branch scope, an ID-verified client who is not blocked) plus the requirement that the client
    # is even a pending portal sign-up. A refusal therefore cannot leave a half-saved vehicle
    # behind: nothing has been saved when it is raised. The main-vehicle check is the one check
    # left to ``approve_registration`` after the save below, because *this* request is what records
    # that main vehicle — validating it here would always show "no main vehicle yet".
    approval_registration_id = None
    if approve_client:
        if not user_can_module(session, "customers"):
            abort(403)
        csrf.validate("portal_approval")
        if disapprove_vehicle:
            flash(
                "Approve the client, or disapprove the vehicle — save them one at a time.",
                "error",
            )
            return review_response(_review(values, customer_id, raw_text=values["raw_scan_text"]))
        pending_registration = portal_registrations.pending_registration_for_customer(customer_id)
        refusal = portal_registrations.approval_refusal(customer, pending_registration)
        if refusal or pending_registration is None:
            flash(refusal or "This client has no pending portal registration to approve.", "error")
            return review_response(_review(values, customer_id, raw_text=values["raw_scan_text"]))
        approval_registration_id = pending_registration["id"]

    # Services historically commit each individual write. The scoped connection suppresses
    # those commits and owns one real transaction (including on production Turso).
    from app.db import scan_write_transaction

    try:
        with scan_write_transaction():
            if transfer:
                if make_main is None:
                    raise ValueError("Choose whether this vehicle becomes the client's main vehicle before transferring it.")
                owner = vehicles.customer_for_vehicle_registration(registration)
                if owner is None:
                    raise ValueError("That registration is not recorded for another client, so there is nothing to transfer.")
                vehicle_id = owner["vehicle_id"]
                vehicles.transfer_vehicle(vehicle_id, customer_id)
                vehicles.update_vehicle(vehicle_id, values)
                message = f"Vehicle {registration} transferred to {customer['name']}."
            else:
                existing = vehicles.get_customer_vehicle_by_registration(customer_id, registration)
                if existing is not None:
                    vehicle_id = existing["id"]
                    vehicles.update_vehicle(vehicle_id, values)
                    message = (f"Registration {registration} is already recorded for {customer['name']} — "
                               "updated the existing vehicle instead of adding a second one.")
                else:
                    if make_main is None:
                        if vehicles.get_main_vehicle_id(customer_id) is not None or vehicles.customer_custom_vehicle_plate(customer_id):
                            raise ValueError("This client already has a main vehicle on file. Choose whether this vehicle becomes their main vehicle, then save again.")
                        make_main = True
                    vehicle_id = vehicles.create_vehicle(request.form, customer_id=customer_id)
                    message = f"Vehicle {registration or 'saved'} allocated to {customer['name']}."
            if make_main is True:
                vehicles.promote_main_vehicle(customer_id, vehicle_id)
            vehicles.set_vehicle_status(vehicle_id, vehicle_status)
            if approval_registration_id is not None:
                portal_registrations.approve_registration(approval_registration_id)
            elif not disapprove_vehicle:
                # The final saved ID + approved main vehicle state is authoritative.
                # Keep the scoped CSRF requirement for the automatic approval too.
                pending = portal_registrations.pending_registration_for_customer(customer_id)
                if (pending and user_can_module(session, "customers")
                        and portal_registrations.branch_in_session_scope(pending)
                        and portal_registrations.is_approvable(get_customer(customer_id))):
                    csrf.validate("portal_approval")
                    portal_registrations.approve_if_verification_complete(customer_id)
    except ValueError as exc:
        flash(str(exc), "error")
        return review_response(_review(values, customer_id, raw_text=values["raw_scan_text"], needs_main_choice=make_main is None))
    if approval_registration_id is not None:
        flash("Client approved — they now show as a normal customer for this branch.", "success")
    flash(message, "success")
    return _customer_edit_redirect(customer_id, return_to_verification)


@bp.post("/vehicles/<int:vehicle_id>/edit")
@login_required
def edit_vehicle(vehicle_id):
    vehicle = vehicles.get_vehicle(vehicle_id)
    if vehicle is None:
        flash("That vehicle is no longer on file.", "error")
        return redirect(url_for("customers.index"))
    try:
        vehicles.update_vehicle(vehicle_id, request.form)
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        # The edit form's "make this the main vehicle" box: ticking it promotes the vehicle so the
        # client's invoices print it. Leaving it unticked leaves the current main vehicle alone.
        if vehicles.parse_main_choice(request.form.get("make_main")) is True:
            vehicles.promote_main_vehicle(vehicle["customer_id"], vehicle_id)
        flash("Vehicle saved.", "success")
    # Vehicle details live on the customer edit screen now, so keep the user there.
    return redirect(url_for("customers.edit", customer_id=vehicle["customer_id"]))


@bp.post("/vehicles/<int:vehicle_id>/delete")
@login_required
def delete_vehicle(vehicle_id):
    vehicle = vehicles.get_vehicle(vehicle_id)
    if vehicle is None:
        flash("That vehicle is no longer on file.", "error")
        return redirect(url_for("customers.index"))
    vehicles.delete_vehicle(vehicle_id)
    flash("Vehicle removed. If it was the main vehicle, its main vehicle details were cleared.", "success")
    # Vehicle details live on the customer edit screen now, so keep the user there.
    return redirect(url_for("customers.edit", customer_id=vehicle["customer_id"]))


@bp.get("/customers/<int:customer_id>/vehicles")
@login_required
def customer_vehicles(customer_id):
    """The client page's vehicles panel feed (A4 renders it; A3 proves the data is there)."""
    from app.services.reports import row_dict
    rows = [row_dict(row) for row in vehicles.list_vehicles(customer_id)]
    return jsonify({"customer_id": customer_id, "count": len(rows), "vehicles": rows})


@bp.get("/scan-vehicle/client-approval-state")
@login_required
def client_approval_state():
    """Resolve the scanner's current selection from saved state, never from browser flags."""
    customer_id = _to_int(request.args.get("customer_id"))
    if not customer_id:
        customer_id = find_customer_id_by_text(request.args.get("customer_pick"), allow_pending=True)
    customer = get_customer(customer_id) if customer_id else None
    registration = portal_registrations.pending_registration_for_customer(customer_id) if customer else None
    ask = bool(customer and registration and portal_registrations.id_verified(customer)
               and portal_registrations.branch_in_session_scope(registration)
               and user_can_module(session, "customers"))
    return jsonify(customer_id=customer_id if customer else None, ask_approval=ask)


@bp.get("/api/customers/search")
@login_required
def customer_search():
    """Typeahead for the allocate step: name and phone only, never email/balance/history."""
    return jsonify({"customers": search_customers(request.args.get("q", ""), allow_pending=True)})
