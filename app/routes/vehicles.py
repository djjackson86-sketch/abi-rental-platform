"""Staff licence-disc scan screen — programme phase 3 (feature A / A3).

Mirrors the house scan-screen pattern (``app/routes/admin.py::scan_barcode``): a GET form, a POST
that reads something, a flash and a redirect. Three deliberate differences, all from the plan:

* the subject is a NaTIS **licence disc**, so the POST decodes a photo (or accepts pasted barcode
  text) and then shows a **review form** instead of jumping straight to a record — staff eyeball
  every parsed field, correct what is wrong, and choose the client;
* the upload never touches the filesystem (Render's disk is ephemeral and the only artefact worth
  keeping is the vehicle row), so the image is read in memory with a size and type guard;
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
from app.services.customers import get_customer, search_customers
from app.services.settings import get_company_settings

bp = Blueprint("vehicles", __name__)

#: A phone photo is a few hundred KB; 8 MB is generous for the modern 12 MP camera at full size.
MAX_DISK_UPLOAD_BYTES = 8 * 1024 * 1024

#: Phones are inconsistent about the MIME type they send for a photo, so anything that announces
#: itself as ``image/…`` is accepted, plus these when the type arrives stripped.
ALLOWED_IMAGE_TYPES = frozenset(
    {
        "application/octet-stream",
        "",
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
        "image/heic",
        "image/heif",
        "image/bmp",
        "image/tiff",
    }
)

#: One distinct sentence per failure kind — A1 flagged that the UI must tell "no barcode on the
#: photo" and "barcode read but nothing in it" apart, and "that file is not an image at all" apart
#: from both. Staff are never blocked: every one of these still renders a form to type the fields.
DECODE_ERROR_MESSAGES = {
    disk.DiscErrorKind.IMAGE_UNREADABLE: (
        "That file is not a readable image. Upload a JPEG or PNG photo of the disc, or type the "
        "details below."
    ),
    disk.DiscErrorKind.NO_BARCODE: (
        "No barcode found on that photo. Get the whole disc face in frame, flat and in focus, or "
        "type the details below."
    ),
    disk.DiscErrorKind.UNPARSEABLE: (
        "The barcode was read but it holds no vehicle fields. Type the details below."
    ),
}

NO_INPUT_MESSAGE = "Photograph the disc, or paste the barcode text, or type the details below."


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


def _review(values, customer_id=None, *, raw_text="", unparsed=None):
    """The context for the review panel, including the "already somebody else's" warning.

    The warning is computed *before* the save as well as after a refusal, so staff see it while
    they pick the client instead of only when the save fails.
    """
    return {
        "fields": values,
        "customer": get_customer(customer_id) if customer_id else None,
        "owner": vehicles.customer_for_vehicle_registration(values.get("registration") or ""),
        "raw_text": raw_text or values.get("raw_scan_text") or "",
        "unparsed": unparsed or {},
    }


def _render(review=None, *, customer_id=None):
    preselected = get_customer(customer_id) if customer_id else None
    if review is not None and review.get("customer") is None and preselected is not None:
        review["customer"] = preselected
    return render_template(
        "admin/scan_vehicle.html",
        settings=get_company_settings(),
        review=review,
        preselected_customer=preselected,
        field_labels=disk.FIELD_LABELS,
    )


def _read_scan(upload, pasted):
    """Read one scan submission: ``(values, raw_text, unparsed, message, category)``.

    Never raises. Every failure ends in a message for staff **and** a review form they can fill in
    by hand: a disc the camera could not read must not stop a trailer going out.
    """
    blank, no_unparsed = _blank_values(), {}

    if upload is not None and (getattr(upload, "filename", "") or "").strip():
        data = upload.read(MAX_DISK_UPLOAD_BYTES + 1)
        if not data:
            return blank, "", no_unparsed, "That image file was empty — take the photo again.", "error"
        if len(data) > MAX_DISK_UPLOAD_BYTES:
            return (
                blank,
                "",
                no_unparsed,
                "That photo is larger than 8 MB — take it again at a smaller size.",
                "error",
            )
        content_type = (getattr(upload, "mimetype", "") or "").lower()
        if content_type and content_type not in ALLOWED_IMAGE_TYPES and not content_type.startswith("image/"):
            return (
                blank,
                "",
                no_unparsed,
                f"That file is a {content_type} file, not an image. Upload a JPEG or PNG photo of the disc.",
                "error",
            )
        try:
            payloads = disk.decode_disc_image(data)
        except disk.DiscDecodeError as exc:
            message = DECODE_ERROR_MESSAGES.get(exc.kind, str(exc))
            return blank, "", no_unparsed, message, "error"
        parsed = _parse_payloads(payloads)
        return _scan_result(parsed)

    if pasted:
        return _scan_result(disk.parse_disc_text(pasted))

    return blank, "", no_unparsed, NO_INPUT_MESSAGE, "error"


def _parse_payloads(payloads):
    """The best parse of the decoded payloads, without raising when none of them holds fields.

    ``decode_payloads`` prefers the high-confidence payload and raises ``UNPARSEABLE`` when nothing
    is parseable; A3 wants that payload's text kept for display, so the raise is caught and the
    payload still parsed (to ``confidence: low``, empty fields) here.
    """
    try:
        return disk.decode_payloads(payloads)
    except disk.DiscDecodeError:
        return disk.parse_disc_text(payloads[0] if payloads else "")


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
    """Capture screen: photograph/paste a disc, then review and allocate it to a client."""
    customer_id = _to_int(request.values.get("customer_id"))

    if request.method == "GET":
        return _render(None, customer_id=customer_id)

    if (request.form.get("action") or "").strip() == "manual":
        # The plan's second fallback: no scan at all, just type the fields.
        flash("Type the vehicle details and choose the client.", "info")
        return _render(_review(_blank_values(), customer_id), customer_id=customer_id)

    values, raw_text, unparsed, message, category = _read_scan(
        request.files.get("disk_image"), (request.form.get("disc_text") or "").strip()
    )
    if message:
        flash(message, category or "error")
    else:
        flash("Disc read — check every field before saving.", "success")
    return _render(_review(values, customer_id, raw_text=raw_text, unparsed=unparsed), customer_id=customer_id)


@bp.post("/scan-vehicle/save")
@login_required
def save_scan_vehicle():
    """Save the reviewed scan against the chosen client (or transfer it, explicitly)."""
    values = _posted_values(request.form)
    customer_id = _to_int(request.form.get("customer_id"))
    transfer = str(request.form.get("transfer") or "").strip().lower() in {"1", "on", "yes", "true"}

    if not customer_id:
        flash("Choose the client this vehicle belongs to.", "error")
        return _render(_review(values, None, raw_text=values["raw_scan_text"]))
    customer = get_customer(customer_id)
    if customer is None:
        flash("That client is no longer on file — search for them again.", "error")
        return _render(_review(values, None, raw_text=values["raw_scan_text"]))

    if transfer:
        owner = vehicles.customer_for_vehicle_registration(values["registration"])
        if owner is None:
            flash(
                "That registration is not recorded for another client, so there is nothing to transfer.",
                "error",
            )
            return _render(_review(values, customer_id, raw_text=values["raw_scan_text"]))
        # Explicit action: MOVE the existing row (never copy), then apply the freshly scanned
        # fields to it, so the record keeps one owner and gains the new disc details.
        vehicles.transfer_vehicle(owner["vehicle_id"], customer_id)
        try:
            vehicles.update_vehicle(owner["vehicle_id"], values)
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash(f"Vehicle {values['registration']} transferred to {customer['name']}.", "success")
        return redirect(url_for("customers.detail", customer_id=customer_id))

    try:
        vehicles.create_vehicle(request.form, customer_id=customer_id)
    except ValueError as exc:
        # A2's message names the current owner; surface it verbatim (never a silent second row).
        flash(str(exc), "error")
        return _render(_review(values, customer_id, raw_text=values["raw_scan_text"]))

    flash(f"Vehicle {values['registration'] or 'saved'} allocated to {customer['name']}.", "success")
    return redirect(url_for("customers.detail", customer_id=customer_id))


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
        flash("Vehicle saved.", "success")
    return redirect(url_for("customers.detail", customer_id=vehicle["customer_id"]))


@bp.post("/vehicles/<int:vehicle_id>/delete")
@login_required
def delete_vehicle(vehicle_id):
    vehicle = vehicles.get_vehicle(vehicle_id)
    if vehicle is None:
        flash("That vehicle is no longer on file.", "error")
        return redirect(url_for("customers.index"))
    vehicles.delete_vehicle(vehicle_id)
    flash("Vehicle removed — the client record is untouched.", "success")
    return redirect(url_for("customers.detail", customer_id=vehicle["customer_id"]))


@bp.get("/customers/<int:customer_id>/vehicles")
@login_required
def customer_vehicles(customer_id):
    """The client page's vehicles panel feed (A4 renders it; A3 proves the data is there)."""
    rows = [dict(row) for row in vehicles.list_vehicles(customer_id)]
    return jsonify({"customer_id": customer_id, "count": len(rows), "vehicles": rows})


@bp.get("/api/customers/search")
@login_required
def customer_search():
    """Typeahead for the allocate step: name and phone only, never email/balance/history."""
    return jsonify({"customers": search_customers(request.args.get("q", ""))})
