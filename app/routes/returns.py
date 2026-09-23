"""Staff scan-to-return screen — programme phase 6 (feature D / D2).

Don's ask (2026-09-23): staff scan a licence disc — the **trailer's** or the
**towing car's** — and the matching rental is marked returned on the admin side.

This is the screen over :mod:`app.services.returns` (phase 5's matcher). It is
deliberately shaped like the scan-a-vehicle screen next to it
(``app/routes/vehicles.py``), so staff learn one screen and get the other free:

* **Capture** — photograph the disc, paste the decoded barcode text, or type the
  plate. All three reach the same matcher; a wet, damaged or missing disc never
  blocks a return. The photo is read in memory and never stored (Render's disk is
  ephemeral and there is nothing worth keeping but the order's audit line).
* **Review** — the matcher's candidates are shown with the evidence that produced
  them. **One** candidate gets one "Mark returned" button; **two or more** get one
  each, and nothing happens until staff pick one; **none** says so and links to
  the started-orders list.
* **Confirm** — guards through ``returns.returnable_order()`` and then calls
  ``returns.mark_returned_via_scan()``, which writes the audit trail and calls the
  existing ``transition_order(order_id, "return")``. No transition logic is
  duplicated and the checklist / deposit / charging flow on the order page is left
  exactly where it is; the redirect hands staff straight to it.

Module gating is the app-wide one (``app/__init__.py::enforce_module_access`` →
``app/services/access.py``): a sign-in without ``scan_return`` gets a 403, not a
hidden button.
"""

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from app.routes.auth import login_required
from app.routes.vehicles import ALLOWED_IMAGE_TYPES, DECODE_ERROR_MESSAGES, MAX_DISK_UPLOAD_BYTES
from app.services import returns
from app.services import vehicle_disk as disk
from app.services.settings import get_company_settings
from app.services.vehicles import registration_key

bp = Blueprint("returns", __name__)

NO_INPUT_MESSAGE = "Photograph the disc, paste the barcode text, or type the plate on the disc."

UNREADABLE_TEXT_MESSAGE = (
    "That text holds no vehicle fields. Type the plate (or the NaTIS number) in the box below."
)

NO_CHOICE_MESSAGE = "Choose which rental is coming back — press Mark returned on the right order."

#: Why the rental was offered, in words staff can act on. The keys are the
#: matcher's ``matched_on`` values; anything unexpected falls back to a readable
#: version of the key itself rather than inventing an explanation.
MATCH_LABELS = {
    returns.MATCH_TRAILER_PLATE: "the trailer's number plate",
    returns.MATCH_CUSTOMER_VEHICLE_PLATE: "the towing car's number plate",
    returns.MATCH_VEHICLE_REGISTRATION_NUMBER: "the towing car's NaTIS registration number",
    returns.MATCH_VEHICLE_VIN: "the towing car's VIN",
    returns.MATCH_VEHICLE_ENGINE: "the towing car's engine number",
}

#: The identity fields the review step carries into the confirm form, so the
#: audit line can say what was actually on the disc that was scanned.
IDENTIFIER_FIELDS = ("scan_plate", "scan_natis", "scan_disc_licence", "scan_vin", "scan_engine")

#: A trailer can be found by any of its three recorded identifiers, but the matcher
#: reports all three as ``trailer_plate``. Naming the right one in the review keeps
#: the evidence honest: staff see *which* value on the disk matched the trailer
#: (its number plate, its NaTIS number or its disk licence number), not a generic
#: "matched on the plate" that would be wrong for two of the three.
TRAILER_EVIDENCE_LABELS = (
    ("scan_plate", "the trailer's number plate"),
    ("scan_natis", "the trailer's NaTIS registration number"),
    ("scan_disc_licence", "the trailer's disk licence number"),
)


def _to_int(value):
    try:
        return int(str(value).strip()) or None
    except (TypeError, ValueError):
        return None


def _match_label(matched_on):
    return MATCH_LABELS.get(matched_on, (matched_on or "").replace("_", " "))


def _evidence_label(matched_on, evidence, identifiers):
    """Why this rental was offered, named for the value that actually matched."""
    if matched_on == returns.MATCH_TRAILER_PLATE:
        key = registration_key(evidence)
        for field, label in TRAILER_EVIDENCE_LABELS:
            if key and key == registration_key(identifiers.get(field) or ""):
                return label
    return _match_label(matched_on)


def _parsed_from_identifiers(identifiers):
    """The **parser's** field names for a set of identifiers (never a second mapping).

    ``disk.parse_disc_text()`` returns the number plate under ``licence_number``
    (it mirrors the reference TypeScript's ``licenceNumber``) and the disc's own
    licence number under ``disc_licence_number``; ``returns.scanned_identifiers()``
    is the one place that mapping is written down. A typed-in plate is therefore
    handed over in exactly the same shape as a scanned one.
    """
    return {
        "licence_number": identifiers.get("scan_plate", ""),
        "registration_number": identifiers.get("scan_natis", ""),
        "disc_licence_number": identifiers.get("scan_disc_licence", ""),
        "vin": identifiers.get("scan_vin", ""),
        "engine_number": identifiers.get("scan_engine", ""),
    }


def _parsed_from_form(form):
    return _parsed_from_identifiers({field: (form.get(field) or "").strip() for field in IDENTIFIER_FIELDS})


def _identifiers_from_parsed(parsed):
    ident = returns.scanned_identifiers(parsed or {})
    return {
        "scan_plate": ident["registration"],
        "scan_natis": ident["registration_number"],
        "scan_disc_licence": ident["licence_number"],
        "scan_vin": ident["vin"],
        "scan_engine": ident["engine_number"],
    }


def _scan_summary(identifiers):
    """The identifiers the scan carried, in the order staff would read them."""
    labels = (
        ("scan_plate", "Number plate"),
        ("scan_natis", "NaTIS registration number"),
        ("scan_disc_licence", "Disc licence number"),
        ("scan_vin", "VIN"),
        ("scan_engine", "Engine number"),
    )
    return [
        {"field": field, "label": label, "value": (identifiers.get(field) or "").strip()}
        for field, label in labels
        if (identifiers.get(field) or "").strip()
    ]


def _review(parsed, *, raw_text="", message=None, category=None):
    """The review state: what was scanned, what it could be, and what to say about it."""
    identifiers = _identifiers_from_parsed(parsed)
    candidates = returns.match_open_rentals(parsed)
    for candidate in candidates:
        candidate["match_label"] = _evidence_label(
            candidate["matched_on"], candidate["evidence"], identifiers
        )
        candidate["also_labels"] = [
            {
                "label": _evidence_label(other["matched_on"], other["evidence"], identifiers),
                "evidence": other["evidence"],
            }
            for other in candidate.get("also_matched_on") or []
        ]
    return {
        "identifiers": identifiers,
        "summary": _scan_summary(identifiers),
        "candidates": candidates,
        "returnable": [candidate for candidate in candidates if candidate["returnable"]],
        "returned": returns.recently_returned(parsed),
        "raw_text": raw_text or "",
        "message": message,
        "category": category,
    }


def _render(review=None):
    return render_template(
        "admin/scan_return.html",
        settings=get_company_settings(),
        review=review,
        match_labels=MATCH_LABELS,
    )


def _read_scan(upload, pasted, typed_plate):
    """Read one submission and return ``(parsed, raw_text, message, category)``.

    Never raises beyond the decode guard: every failure ends in a message for
    staff plus the capture form they can use again, because a disc the camera
    could not read must not stop a trailer coming back.
    """
    if upload is not None and (getattr(upload, "filename", "") or "").strip():
        data = upload.read(MAX_DISK_UPLOAD_BYTES + 1)
        if not data:
            return {}, "", "That image file was empty — take the photo again.", "error"
        if len(data) > MAX_DISK_UPLOAD_BYTES:
            return {}, "", "That photo is larger than 8 MB — take it again at a smaller size.", "error"
        content_type = (getattr(upload, "mimetype", "") or "").lower()
        if content_type and content_type not in ALLOWED_IMAGE_TYPES and not content_type.startswith("image/"):
            return {}, "", f"That file is a {content_type} file, not an image.", "error"
        try:
            payloads = disk.decode_disc_image(data)
        except disk.DiscDecodeError as exc:
            return {}, "", DECODE_ERROR_MESSAGES.get(exc.kind, str(exc)), "error"
        try:
            parsed = disk.decode_payloads(payloads)
        except disk.DiscDecodeError:
            # The barcode was read but holds no vehicle fields: keep the payload's
            # text for display (the A3 screen does the same) and let staff type the
            # plate instead of showing them an error page.
            text = payloads[0] if payloads else ""
            return {}, text, UNREADABLE_TEXT_MESSAGE, "error"
        return parsed, str(parsed.get("raw_text") or ""), None, None

    if (pasted or "").strip():
        parsed = disk.parse_disc_text(pasted)
        if not disk.is_disc_parseable(parsed):
            return {}, str(parsed.get("raw_text") or pasted), UNREADABLE_TEXT_MESSAGE, "error"
        return parsed, str(parsed.get("raw_text") or pasted), None, None

    if (typed_plate or "").strip():
        # The plan's fallback: no disc at all. Handed over in the parser's shape,
        # never in a shape of its own.
        return {"licence_number": typed_plate.strip()}, "", None, None

    return {}, "", NO_INPUT_MESSAGE, "error"


@bp.route("/scan-return", methods=["GET", "POST"])
@login_required
def scan_return():
    """Capture screen: read a disc (or a typed plate) and show what it matches."""
    if request.method == "GET":
        return _render()

    parsed, raw_text, message, category = _read_scan(
        request.files.get("disk_image"),
        request.form.get("disc_text") or "",
        request.form.get("plate") or "",
    )
    if message:
        flash(message, category or "error")
        return _render()

    return _render(_review(parsed, raw_text=raw_text))


@bp.post("/scan-return/confirm")
@login_required
def confirm_return():
    """Mark the chosen rental returned, through the existing return flow."""
    parsed = _parsed_from_form(request.form)
    order_id = _to_int(request.form.get("order_id"))
    if order_id is None:
        # Ambiguity is a question, not an action: re-show the candidates.
        flash(NO_CHOICE_MESSAGE, "error")
        return _render(_review(parsed))

    try:
        result = returns.mark_returned_via_scan(
            order_id, user_id=session.get("user_id"), parsed=parsed
        )
    except ValueError as exc:
        # The existing flow's refusal (or the matcher's scope/state guard),
        # surfaced verbatim — never rewritten, never swallowed.
        flash(str(exc), "error")
        return _render(_review(parsed))

    what = "Trailer" if result["source"] == returns.SOURCE_TRAILER_DISC else "Towing vehicle"
    identity = result["registration"] or result["order_number"]
    flash(
        f"{what} {identity} returned via disc scan — finish the return checklist and the "
        "deposit on the order below.",
        "success",
    )
    # The checklist / deposit / charging work stays on the order page: hand staff
    # straight to it rather than rebuilding any of it here.
    return redirect(url_for("orders.detail", order_id=order_id))
