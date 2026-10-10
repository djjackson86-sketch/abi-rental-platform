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

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from app.routes.auth import login_required
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
    return {
        "fields": values,
        "customer": get_customer(customer_id) if customer_id else None,
        "owner": vehicles.customer_for_vehicle_registration(values.get("registration") or ""),
        "raw_text": raw_text or values.get("raw_scan_text") or "",
        "unparsed": unparsed or {},
        "needs_main_choice": needs_main_choice,
        # "Licence disk expired on …" when the scanned/typed expiry is in the past — the review form
        # (and the customer form) can show the warning without recomputing the date rules.
        "expiry_state": vehicles.licence_disk_expiry_state(values.get("licence_disk_expiry")),
    }


def _render(review=None, *, customer_id=None, vehicle_role=None):
    preselected = get_customer(customer_id) if customer_id else None
    if review is not None and review.get("customer") is None and preselected is not None:
        review["customer"] = preselected
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
            )

    customer_id = _to_int(request.values.get("customer_id"))
    # ``main`` (the customer form's main-vehicle link) or ``additional`` (the vehicles panel's
    # "Add another vehicle"). It decides what the "make this the main vehicle?" choice defaults to.
    vehicle_role = (request.values.get("vehicle_role") or "").strip() or None

    if request.method == "GET":
        return _render(None, customer_id=customer_id, vehicle_role=vehicle_role)

    if (request.form.get("action") or "").strip() == "manual":
        # The manual fallback: no scan at all, just type the fields.
        flash("Type the vehicle details and choose the client.", "info")
        return _render(_review(_blank_values(), customer_id), customer_id=customer_id, vehicle_role=vehicle_role)

    values, raw_text, unparsed, message, category = _read_scan(request.form.get("disc_text"))
    if message:
        flash(message, category or "error")
    else:
        flash("Disc read — check every field before saving.", "success")
    return _render(
        _review(values, customer_id, raw_text=raw_text, unparsed=unparsed),
        customer_id=customer_id,
        vehicle_role=vehicle_role,
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
    transfer = str(request.form.get("transfer") or "").strip().lower() in {"1", "on", "yes", "true"}
    make_main = vehicles.parse_main_choice(request.form.get("make_main"))

    # The picker's hidden id is a convenience, not the source of truth: staff routinely type the
    # name (or pick the typeahead label) without the id landing in the form, which used to refuse a
    # vehicle after the client had visibly been chosen. Resolve from the visible text when the id
    # is missing — an ambiguous or unknown name still refuses.
    if not customer_id:
        customer_id = find_customer_id_by_text(request.form.get("customer_pick"))

    if not customer_id:
        flash(
            "Choose the client from the list: type at least two letters of the name or number, "
            "then pick the client from the suggestions.",
            "error",
        )
        return _render(_review(values, None, raw_text=values["raw_scan_text"]))
    customer = get_customer(customer_id)
    if customer is None:
        flash("That client is no longer on file — search for them again.", "error")
        return _render(_review(values, None, raw_text=values["raw_scan_text"]))

    registration = values["registration"]

    if transfer:
        if make_main is None:
            flash("Choose whether this vehicle becomes the client's main vehicle before transferring it.", "error")
            return _render(_review(values, customer_id, needs_main_choice=True))
        owner = vehicles.customer_for_vehicle_registration(registration)
        if owner is None:
            flash(
                "That registration is not recorded for another client, so there is nothing to transfer.",
                "error",
            )
            return _render(_review(values, customer_id, raw_text=values["raw_scan_text"]))
        # Explicit action: MOVE the existing row (never copy), then apply the freshly scanned
        # fields to it, so the record keeps one owner and gains the new disc details. If staff
        # chose "yes", the moved vehicle also becomes the new owner's main vehicle; otherwise the
        # new owner's main vehicle is left exactly as it was.
        vehicles.transfer_vehicle(owner["vehicle_id"], customer_id)
        try:
            vehicles.update_vehicle(owner["vehicle_id"], values)
        except ValueError as exc:
            flash(str(exc), "error")
            return _render(_review(values, customer_id, raw_text=values["raw_scan_text"]))
        if make_main is True:
            vehicles.promote_main_vehicle(customer_id, owner["vehicle_id"])
        flash(f"Vehicle {registration} transferred to {customer['name']}.", "success")
        return redirect(url_for("customers.edit", customer_id=customer_id))

    # Re-scanning a plate this client already holds is a correction, not a second vehicle: update
    # that record in place (the duplicate guard in create_vehicle still stands for everything else).
    existing_same = vehicles.get_customer_vehicle_by_registration(customer_id, registration)
    if existing_same is not None:
        try:
            vehicles.update_vehicle(existing_same["id"], values)
        except ValueError as exc:
            flash(str(exc), "error")
            return _render(_review(values, customer_id, raw_text=values["raw_scan_text"]))
        if make_main is True:
            vehicles.promote_main_vehicle(customer_id, existing_same["id"])
        flash(
            f"Registration {registration} is already recorded for {customer['name']} — "
            "updated the existing vehicle instead of adding a second one.",
            "success",
        )
        return redirect(url_for("customers.edit", customer_id=customer_id))

    # A brand-new vehicle. The main vehicle is the one the client's invoices print, so a different
    # vehicle may only take that place when staff say so.
    if make_main is None:
        has_main = vehicles.get_main_vehicle_id(customer_id) is not None
        custom_plate = vehicles.customer_custom_vehicle_plate(customer_id)
        if has_main or custom_plate:
            flash(
                "This client already has a main vehicle on file. Choose whether this vehicle becomes "
                "their main vehicle, then save again.",
                "error",
            )
            return _render(
                _review(values, customer_id, raw_text=values["raw_scan_text"], needs_main_choice=True)
            )
        # First vehicle for the client, and no choice was posted (an older caller): keep the old
        # behaviour of making it the main one so nothing regresses.
        make_main = True

    try:
        vehicle_id = vehicles.create_vehicle(request.form, customer_id=customer_id)
    except ValueError as exc:
        # A2's message names the current owner; surface it verbatim (never a silent second row).
        flash(str(exc), "error")
        return _render(_review(values, customer_id, raw_text=values["raw_scan_text"]))

    if make_main:
        vehicles.promote_main_vehicle(customer_id, vehicle_id)
    flash(f"Vehicle {registration or 'saved'} allocated to {customer['name']}.", "success")
    return redirect(url_for("customers.edit", customer_id=customer_id))


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


@bp.get("/api/customers/search")
@login_required
def customer_search():
    """Typeahead for the allocate step: name and phone only, never email/balance/history."""
    return jsonify({"customers": search_customers(request.args.get("q", ""))})
