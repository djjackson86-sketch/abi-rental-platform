"""Staff scan-to-return screen — programme phase 6 (feature D / D2).

Don's ask (2026-09-23): staff scan a licence disc — the **trailer's** or the
**towing car's** — and the matching rental is marked returned on the admin side.

This is the screen over :mod:`app.services.returns` (phase 5's matcher). It is
deliberately shaped like the scan-a-vehicle screen next to it
(``app/routes/vehicles.py``), so staff learn one screen and get the other free:

* **Capture** — the camera reads the disc's PDF417 barcode **in the browser** and posts the decoded
  text, or staff type the plate. Both reach the same matcher; a wet, damaged or missing disc never
  blocks a return. The server never sees an image: the retired photo-upload path is refused from
  the request headers before its body is parsed (`app/routes/vehicles.py::scan_request_rejection`),
  so the image ladder that expanded a phone photo to hundreds of megabytes can no longer run.
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
from werkzeug.datastructures import MultiDict

from app.routes.auth import login_required
from app.db import get_db
from app.routes.vehicles import MAX_DISC_TEXT_CHARS, SCAN_TEXT_TOO_LARGE_MESSAGE, scan_request_rejection
from app.services import returns
from app.services.orders import add_return_charges, update_return_checklist
from app.services import vehicle_disk as disk
from app.services.settings import get_company_settings
from app.services.vehicles import registration_key

bp = Blueprint("returns", __name__)

NO_INPUT_MESSAGE = "Scan the licence disk, or type the trailer/client vehicle plate manually."

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


def _truthy(value):
    return str(value or "").strip().lower() in {"1", "on", "yes", "true"}


def _damage_form_for_return(form):
    """The one-step return card's damage report, mapped to the existing order settlement flow."""
    damage_charge = str(form.get("damage_charge") or "").strip()
    damage_description = str(form.get("damage_description") or "").strip()
    damage_severity = str(form.get("damage_severity") or "").strip().upper()
    # Browser posts carry the checked No damage box by default. Crafted/test posts may omit it;
    # when they also carry no damage charge, treat that as the same clean-return default.
    no_damage = (not damage_charge) or _truthy(form.get("no_damages"))
    payload = MultiDict()
    if no_damage:
        payload.add("no_damages", "1")
    else:
        payload.add("damage_charge", damage_charge or "0")
    # ABI does not yet have TrailerPro's photo damage-log table. Preserve the written report text
    # where staff already see return/deposit notes, and put the charge through the existing
    # Damage charge line-item path so totals/deposits stay correct.
    note_parts = []
    if damage_description:
        note_parts.append(damage_description)
    if damage_severity:
        note_parts.append(f"Severity: {damage_severity.title()}")
    if note_parts:
        payload.add("deposit_note", "Damage report — " + "; ".join(note_parts))
    return payload


def _save_damage_note(order_id, payload):
    note = str(payload.get("deposit_note") or "").strip()
    if not note:
        return
    get_db().execute("UPDATE orders SET deposit_note = ? WHERE id = ?", (note, order_id))
    get_db().commit()


def _read_scan(pasted, typed_plate):
    """Read one submission and return ``(parsed, raw_text, message, category)``.

    The camera decodes the barcode in the browser and posts the text; there is no server-side image
    path any more. Never raises beyond the decode guard: every failure ends in a message for staff
    plus the capture form they can use again, because a disc the camera could not read must not stop
    a trailer coming back.
    """
    text = (pasted or "").strip()
    if text:
        if len(text) > MAX_DISC_TEXT_CHARS:
            return {}, "", SCAN_TEXT_TOO_LARGE_MESSAGE, "error"
        parsed = disk.parse_disc_text(text)
        if not disk.is_disc_parseable(parsed):
            return {}, str(parsed.get("raw_text") or text), UNREADABLE_TEXT_MESSAGE, "error"
        return parsed, str(parsed.get("raw_text") or text), None, None

    if (typed_plate or "").strip():
        # The manual fallback: no disc at all. Handed over in the parser's shape,
        # never in a shape of its own.
        return {"licence_number": typed_plate.strip()}, "", None, None

    return {}, "", NO_INPUT_MESSAGE, "error"


@bp.route("/scan-return", methods=["GET", "POST"])
@login_required
def scan_return():
    """Capture screen: read a disc in the browser (or type a plate) and show what it matches."""
    if request.method == "GET":
        return _render()

    rejection = scan_request_rejection(request)
    if rejection:
        # Refused from the request headers alone: no multipart body is parsed, no image decoded.
        flash(rejection, "error")
        return _render()

    if (request.form.get("action") or "") == "manual":
        # The button only opens the manual-entry panel client-side. If a browser posts it directly,
        # render the same page with the manual panel visible rather than doing any work.
        return _render({"manual_open": True})

    parsed, raw_text, message, category = _read_scan(
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
        returns.returnable_order(order_id)
        # One-card flow: staff either leave the default "No damage" ticked or file the damage
        # report/charge here, and the actual-return-date is accepted as not revised. Those are the
        # exact gates the order page requires before a return transition is allowed.
        update_return_checklist(order_id, MultiDict([("no_revision_required", "1")]))
        damage_form = _damage_form_for_return(request.form)
        add_return_charges(order_id, damage_form)
        _save_damage_note(order_id, damage_form)
        result = returns.mark_returned_via_scan(
            order_id, user_id=session.get("user_id"), parsed=parsed
        )
    except ValueError as exc:
        # The existing flow's refusal (or the matcher's scope/state guard),
        # surfaced verbatim — never rewritten, never swallowed.
        flash(str(exc), "error")
        return _render(_review(parsed, message=str(exc), category="error"))

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
