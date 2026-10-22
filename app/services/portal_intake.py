"""Public portal intake: dedupe, the safe lookup, and creating/ linking a client (§B2).

Feature B of the ABI programme. §B1 gave every branch a link and a QR; this module is what
happens when a customer opens it and types their details in.

Four decisions shape it:

* **D6 — dedupe is a *decision*, not a block.** The matching *logic* is Bubblebounce's
  (``docs/plans/reference-notes-trailerpro-bubblebounce.md`` §3a: exact normalised phone →
  email → name, in that order); the "Is this you?" screen is ABI's own, because Bubblebounce
  resolves duplicates fully automatically and has no such screen (§3c). What this module owns
  is :func:`find_possible_matches`, which reports candidates **masked**, and
  :func:`create_or_link_customer`, which acts only on the decision the customer posted.
* **D8 — the lookup never leaks.** :func:`lookup_public` answers with first name + surname
  initial + the last four digits of the phone, and nothing else: no full surname, no email, no
  address, no balance, no history. A blocked client answers as "not found", because "come to
  the counter" is a conversation, not a web response.
* **D7 — public submissions are low-trust.** A honeypot swallows a bot post and a hard rate
  limit (10 lookups per address per 5 minutes) throttles the lookup. The limiter keys on a
  **salted digest** of the address, generated per process: the address itself is never stored,
  in the database or in memory (no IP address, no user agent — data minimisation).
* **D10/D11 — consent and the notice.** :func:`registration_is_open` is ``False`` while the
  published notice still carries an open placeholder, so the public write path stays shut until
  Sano's facts land — no code change is needed when they do.

Nothing here writes to a record the submission did not match, and a ``link`` decision is only
honoured for a candidate this submission actually matched (otherwise a crafted POST could write
to any customer id in the book).
"""

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import unicodedata

from app.db import get_db, now
from app.services import consent
from app.services.customers import create_customer, get_customer

#: The ``customers.source_system`` value every portal submission carries.
PORTAL_SOURCE_SYSTEM = "portal"

#: Confidence labels, in the order they outrank each other. A phone or email match is the same
#: person with near-certainty; a name match is a hint for staff to check, never a fact.
CONFIDENCE_HIGH = "high"
CONFIDENCE_LOW = "low"

#: The honeypot the public form carries. Hidden from people, irresistible to a form-filler bot:
#: a submission that arrives with it filled is swallowed without writing anything.
HONEYPOT_FIELD = "company_website"

#: The hidden field the public form posts so one *submission* writes at most one customer, however
#: many times the button is tapped (client portal changes: a double-click while the first request
#: was still in flight created two company rows). The browser is the only client, and the field is
#: rendered server-side, so it is present even with JavaScript off; a request that omits it (an old
#: fixture, or a hand-written POST) falls back to a digest of the submission itself, below.
REQUEST_KEY_FIELD = "request_key"

#: Bounds for a key the caller supplied. A browser key is ~43 URL-safe characters; the ceiling is
#: generous, the floor stops an empty or one-character value being used to replay another claim.
REQUEST_KEY_MIN_LEN = 16
REQUEST_KEY_MAX_LEN = 200

#: The claim table's two states. 'in_flight' until the customer row is written, then 'done'.
CLAIM_IN_FLIGHT = "in_flight"
CLAIM_DONE = "done"

#: What ``claim_submission`` reports back. Public so a caller (and the tests) can name the state.
SUBMISSION_CLAIMED = "claimed"    # this request owns the claim and may write
SUBMISSION_REPLAY = "replay"      # this exact submission already succeeded: answer, do not write
SUBMISSION_IN_FLIGHT = "in_flight"  # another request holds the claim: refuse, do not write

#: The refusal a concurrent second submit sees. Plain words, and it says what to do.
IN_FLIGHT_MESSAGE = (
    "We're already saving this registration — please wait a moment rather than sending it again."
)


class SubmissionInFlight(ValueError):
    """The same submission is already being written by another request.

    Its own type so the route can answer 409 (a conflict with an in-flight write) rather than the
    generic 400 every other refusal uses, while still being a ``ValueError`` so any existing caller
    that only catches ``ValueError`` keeps behaving.
    """

#: Lookup throttle — 10 per address per 5 minutes (D7).
LOOKUP_LIMIT = 10
LOOKUP_WINDOW_SECONDS = 300

#: In-process only. Keyed by a salted digest of the address, never the address itself.
_LOOKUP_HITS = {}
_LOOKUP_SALT = os.urandom(16).hex()

#: The columns a portal submission may fill in on an existing record. Deliberately not the
#: blocking columns and not the balance: a public page cannot block or unblock anybody.
#: Client Verified and Standard discount are missing for the same reason — those are counter
#: decisions. Custom details (VAT, company reg, vehicle make, alternative contact) are filled on
#: the **create** path only: a link to an existing record never rewrites what the counter captured.
FILLABLE_FIELDS = (
    "name",
    "email",
    "phone",
    "address_line1",
    "address_line2",
    "suburb",
    "city",
    "province",
    "postal_code",
    "country",
)

#: The custom-detail keys the public form carries — the counter-only extras beside the
#: address block. The vehicle-make/colour/reg keys the staff Edit customer page also posts
#: were **removed from the public form** (client instruction): a customer never types a
#: vehicle, staff capture and scan it. Country was removed for the same reason (a South
#: African depot's customers are South African; the record defaults to SA internally), and
#: ``website`` is handled separately because it applies to a company only.
PORTAL_CUSTOM_FIELD_KEYS = (
    "alternative_contact_name",
    "alternative_contact_number",
    "alternative_contact_relationship",
    "vat_number",
    "company_reg_no",
)

#: Every field the public form must carry a value for, with the sentence shown when it is
#: blank. The owner asked for these to be mandatory (the red asterisk on the form) and the
#: server enforces the same set, so a crafted POST that omits one is refused exactly like a
#: customer who left the box empty.
PORTAL_REQUIRED_FIELDS = (
    ("name", "Please enter your name."),
    ("email", "Please enter your email address."),
    ("phone", "Please enter your phone number."),
    ("address_line1", "Please enter your address line 1."),
    ("suburb", "Please enter your suburb."),
    ("city", "Please enter your city."),
    ("province", "Please enter your province."),
    ("postal_code", "Please enter your postal code."),
    ("alternative_contact_name", "Please enter the alternative contact's name."),
    ("alternative_contact_number", "Please enter the alternative contact's number."),
    ("alternative_contact_relationship", "Please enter the alternative contact's relationship."),
)

#: The two customer types the staff form offers; anything else falls back to individual.
PORTAL_CUSTOMER_TYPES = ("individual", "company")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: An SA number is 10 digits with the trunk 0 (9 without); the window is generous on purpose so a
#: landline, a border number or a "+27 82…" typed four ways all pass, while "123" does not.
_PHONE_DIGITS_MIN = 9
_PHONE_DIGITS_MAX = 13

INTAKE_CLOSED_MESSAGE = (
    "Online registration is currently unavailable. Please contact the counter staff for assistance."
)

BLOCKED_MESSAGE = (
    "We already hold a record in this name at the branch. Please speak to the counter staff "
    "before registering again."
)

#: What a customer sees when their phone or email is already on file (client portal changes).
#: Registering is not a way around the counter: an exact contact match is refused outright — no
#: "Is this you?" decision, no second customer — and the person is sent to the front desk.
DUPLICATE_MESSAGE = "Client is already registered. Proceed to the front desk."


class DuplicateRegistration(ValueError):
    """The submitted phone/email already belongs to a client: refuse and write nothing.

    Its own type so the public route can render the duplicate warning page, while staying a
    ``ValueError`` so any caller that only catches ``ValueError`` keeps behaving as it did.
    """


def _clean_ascii(value):
    """Fold accents so ``Müller`` and ``Muller`` compare (and mask) the same."""
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def normalise_phone(value):
    """Digits only, in local ``0xx`` shape — ``+27 82 123 4567`` and ``082 123 4567`` agree.

    Three ways of writing one number (``+27…``, ``0027…``, ``0…``) all land on the same key, which
    is the whole point: the phone is the primary dedupe key (D6), so a spacing or country-code
    difference must not create a second client.
    """
    digits = re.sub(r"\D", "", str(value if value is not None else ""))
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("27") and len(digits) > 10:
        digits = "0" + digits[2:]
    return digits


def normalise_email(value):
    """Trimmed, lower-cased — ``Pieter@Example.CO.ZA`` and ``pieter@example.co.za`` agree."""
    return str(value if value is not None else "").strip().lower()


def _name_key(name):
    """The comparison key for a name: accent-folded, lower-case, tokens sorted.

    Sorting the tokens means "Mokoena Charmaine" matches "Charmaine Mokoena" — people do type
    their surname first — while still requiring the same set of names.
    """
    tokens = re.findall(r"[a-z0-9]+", _clean_ascii(name).lower())
    return " ".join(sorted(tokens))


def _row_value(row, key, default=""):
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def masked_display(customer):
    """First name + surname initial + the last four digits of the phone (D8) — never more.

    ``Charmaine Mokoena`` on ``0821234567`` reads ``Charmaine M. · …4567``. A one-word name stays
    as it is; a client with no phone gets no digits. This is the *only* shape any public page
    renders about an existing client.
    """
    parts = [part for part in re.split(r"\s+", str(_row_value(customer, "name", "")).strip()) if part]
    if parts:
        display = parts[0]
        if len(parts) > 1:
            display = f"{display} {parts[-1][:1].upper()}."
    else:
        display = "A customer"
    phone = normalise_phone(_row_value(customer, "phone", ""))
    if len(phone) >= 4:
        return f"{display} · …{phone[-4:]}"
    return display


def _candidate_rows():
    """Only what the match and the mask need — never the address, the balance or the history.

    A client whose sign-up was **removed** (``portal_registrations.status = 'deleted'``) is
    excluded: removing a rejected sign-up is exactly what lets that person register again, so a
    tombstoned record must never keep matching them as an existing client (client portal changes).
    """
    from app.services import portal_registrations

    deleted = getattr(portal_registrations, "STATUS_DELETED", "deleted")
    return get_db().execute(
        "SELECT id, name, phone, email, is_blocked FROM customers AS c "
        "WHERE NOT EXISTS (SELECT 1 FROM portal_registrations pr "
        "WHERE pr.customer_id = c.id AND pr.status = ?)",
        (deleted,),
    ).fetchall()


def find_possible_matches(name, phone, email):
    """Possible existing clients for a submission, strongest match first (D6).

    Each candidate is ``{customer_id, confidence, matched_on, masked_display, is_blocked}``.
    ``confidence`` is ``high`` for an exact phone or email hit and ``low`` for a name-only hit;
    ``matched_on`` names which keys agreed, so the screen can say *why* it is asking. Ordered by
    confidence then id, so the same submission always presents the same list.

    This never returns a full name, email, address or balance — only :func:`masked_display`.
    """
    phone_key = normalise_phone(phone)
    email_key = normalise_email(email)
    name_key = _name_key(name)
    matches = []
    for row in _candidate_rows():
        reasons = []
        confidence = None
        if phone_key and normalise_phone(_row_value(row, "phone")) == phone_key:
            reasons.append("phone")
            confidence = CONFIDENCE_HIGH
        if email_key and normalise_email(_row_value(row, "email")) == email_key:
            reasons.append("email")
            confidence = CONFIDENCE_HIGH
        if name_key and _name_key(_row_value(row, "name")) == name_key:
            reasons.append("name")
            confidence = confidence or CONFIDENCE_LOW
        if reasons:
            matches.append(
                {
                    "customer_id": row["id"],
                    "confidence": confidence,
                    "matched_on": reasons,
                    "masked_display": masked_display(row),
                    "is_blocked": bool(_row_value(row, "is_blocked", 0)),
                }
            )
    matches.sort(key=lambda match: (0 if match["confidence"] == CONFIDENCE_HIGH else 1, match["customer_id"]))
    return matches


def blocked_match(name, phone, email):
    """The first blocked client this submission *strongly* matches (phone or email), or ``None``.

    A blocked client must not be resurrected by registering again (D6/§B2), so a strong match on
    a blocked record stops the public write path. A name-only match is not enough: two people can
    share a name, and refusing on that would block honest sign-ups.
    """
    for match in find_possible_matches(name, phone, email):
        strong = "phone" in match["matched_on"] or "email" in match["matched_on"]
        if match["is_blocked"] and strong:
            return match
    return None


def lookup_public(name, phone):
    """The "am I already a customer?" answer — masked, and only for a name **and** number (D8).

    Both are required: a number alone would let anyone walk the book one address at a time. A
    blocked client answers exactly like an unknown one, because "we have a record on this number"
    is a sentence for the counter, not for a web page.
    """
    phone_key = normalise_phone(phone)
    name_key = _name_key(name)
    if not phone_key or not name_key:
        return {"found": False, "display": "", "reason": "missing"}
    for row in _candidate_rows():
        if normalise_phone(_row_value(row, "phone")) != phone_key:
            continue
        if _name_key(_row_value(row, "name")) != name_key:
            continue
        if _row_value(row, "is_blocked", 0):
            return {"found": False, "display": "", "reason": "blocked"}
        return {"found": True, "display": masked_display(row), "reason": "match"}
    return {"found": False, "display": "", "reason": "none"}


# --- rate limit (D7) ----------------------------------------------------------------------------

def _rate_key(address):
    """A salted digest of the address. The address itself is never kept, anywhere."""
    return hashlib.sha256(f"{_LOOKUP_SALT}{address or ''}".encode("utf-8")).hexdigest()[:16]


def allow_lookup(address, now_ts=None):
    """True when this address may look up again; records the hit when it may.

    In-process (a dict, cleared on restart) and deliberately not a database table: a log of who
    searched for whom would be exactly the kind of record POPIA's minimisation rule is about.
    """
    moment = time.time() if now_ts is None else float(now_ts)
    key = _rate_key(address)
    hits = [hit for hit in _LOOKUP_HITS.get(key, []) if moment - hit < LOOKUP_WINDOW_SECONDS]
    if len(hits) >= LOOKUP_LIMIT:
        _LOOKUP_HITS[key] = hits
        return False
    hits.append(moment)
    _LOOKUP_HITS[key] = hits
    return True


def reset_rate_limits():
    """Clear the in-process limiter (a fresh start for a new test, or a deliberate release)."""
    _LOOKUP_HITS.clear()


# --- the submission -----------------------------------------------------------------------------

def honeypot_triggered(form):
    """True when the invisible field arrived filled — a bot, not a customer."""
    return bool(str(form.get(HONEYPOT_FIELD) or "").strip())


def parse_decision(value):
    """``(None, None)`` / ``('create', None)`` / ``('link', <id>)`` — anything else is refused."""
    raw = str(value or "").strip()
    if not raw:
        return None, None
    if raw == "create":
        return "create", None
    if raw.startswith("link:"):
        target = raw.split(":", 1)[1].strip()
        if not target.isdigit() or int(target) <= 0:
            raise ValueError("Please choose one of the options we showed you.")
        return "link", int(target)
    raise ValueError("Please choose one of the options we showed you.")


def submission_values(form):
    """The public form's values, cleaned and validated — the shape the intake service writes.

    The customer's own capture: the customer type, the contact block, the (required) SA
    address block and the alternative-contact block. Two staff-only things are gone by
    design — **country** (a South African depot's clients are South African, so the record
    defaults to SA internally and a posted country is ignored) and the **vehicle** fields
    (make/colour/reg), which staff capture and scan, never the customer. ``website`` is a
    company-only detail: it is accepted for a company and dropped for an individual, so a
    crafted post cannot put one on a person.

    Every field the owner marked mandatory is required here too (``PORTAL_REQUIRED_FIELDS``),
    with a real email/phone shape check on top. The rule is server-side on purpose: the red
    asterisk and the browser's ``required`` are a courtesy, this is the boundary.
    """
    customer_type = str(form.get("customer_type") or "individual").strip().lower()
    if customer_type not in PORTAL_CUSTOMER_TYPES:
        customer_type = "individual"
    # Individuals must supply both parts explicitly: never guess a surname from a
    # combined name (including a crafted/old POST). Storage still uses the full name.
    name = str(form.get("name") or "").strip()
    first_names = str(form.get("first_names") or "").strip()
    surname = str(form.get("surname") or "").strip()
    if customer_type == "individual":
        if not first_names:
            raise ValueError("Please enter your name(s).")
        if not surname:
            raise ValueError("Please enter your surname.")
        name = f"{first_names} {surname}"
    values = {
        "customer_type": customer_type,
        "name": name,
        "email": normalise_email(form.get("email")),
        "phone": str(form.get("phone") or "").strip(),
        "address_line1": str(form.get("address_line1") or "").strip(),
        "address_line2": str(form.get("address_line2") or "").strip(),
        "suburb": str(form.get("suburb") or "").strip(),
        "city": str(form.get("city") or "").strip(),
        "province": str(form.get("province") or "").strip(),
        "postal_code": str(form.get("postal_code") or "").strip(),
        "marketing_opt_in": 1 if form.get("marketing_opt_in") else 0,
    }
    if customer_type == "individual":
        # Keep the explicit parts when validated values are passed through intake again.
        values.update(first_names=first_names, surname=surname)
    for key in PORTAL_CUSTOM_FIELD_KEYS:
        values[key] = str(form.get(key) or "").strip()
    if customer_type == "company":
        from app.services.customer_emails import additional_emails
        values["other_email_addresses"] = "; ".join(additional_emails(form.get("other_email_addresses"), values["email"], max_addresses=3))
    # Company-only: website. A crafted ``website`` on an individual is dropped here rather
    # than stored, so the field really is "visible only for Company" server-side too.
    values["website"] = (
        str(form.get("website") or "").strip() if customer_type == "company" else ""
    )
    for key, message in PORTAL_REQUIRED_FIELDS:
        if not str(values.get(key) or "").strip():
            raise ValueError(message)
    if not _EMAIL_RE.match(values["email"]):
        raise ValueError("Please enter a valid email address.")
    digits = normalise_phone(values["phone"])
    if not (_PHONE_DIGITS_MIN <= len(digits) <= _PHONE_DIGITS_MAX):
        raise ValueError("Please enter a valid phone number, e.g. 082 123 4567.")
    alt_digits = normalise_phone(values["alternative_contact_number"])
    if not (_PHONE_DIGITS_MIN <= len(alt_digits) <= _PHONE_DIGITS_MAX):
        raise ValueError("Please enter a valid alternative contact number, e.g. 082 123 4567.")
    return values


def new_reference(slug):
    """A short, human-readable reference for one submission — unique per submission, by design.

    The customers table carries a **unique** index on ``(source_system, source_id)`` (the Booqable
    importer's, ``app/db.py``), so the slug alone cannot be the ``source_id``: a branch's *second*
    sign-up would be refused by the database. The slug is kept as the prefix — so a branch's
    submissions are still selectable — and this reference makes the key unique.
    """
    stem = re.sub(r"[^A-Za-z0-9]", "", str(slug or ""))[:3].upper() or "PRT"
    return f"{stem}-{secrets.token_hex(3).upper()}"


# --- the double-submit claim -------------------------------------------------------------------


def new_request_key():
    """A fresh, unguessable token for one rendered form — the browser's idempotency key.

    ``secrets`` (not ``random``): the key decides whether a submission writes, so it must be
    unpredictable, and it is per render, so two submissions of the same form share one key and a
    second render gets a new one. ~256 bits, URL-safe.
    """
    return secrets.token_urlsafe(32)


def _request_digest(values, decision, scope):
    """A stable digest of a submission that carried no ``request_key`` — the compatibility path.

    Old fixtures (and any hand-written POST) omit the field. Rather than lose the guard entirely,
    the submission is digested from its own canonical values, the posted ``decision`` and the portal
    scope, so two identical submissions of one form agree and two *different* submissions never do.
    The ``values`` here are already cleaned by :func:`submission_values`, so ``Pieter@X`` and
    ``pieter@x`` digest alike. Prefixed ``d:`` so a derived key can never equal a posted one.
    """
    payload = {"scope": str(scope or ""), "decision": str(decision or "")}
    if isinstance(values, dict):
        for key in sorted(values):
            payload[key] = values[key]
    serialised = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return "d:" + hashlib.sha256(serialised.encode("utf-8")).hexdigest()


def resolve_request_key(form, scope, values=None, decision=None):
    """The key this submission is claimed under: the posted one when it is usable, else a digest.

    A posted key that is too short (or absurdly long) is ignored rather than trusted — the floor
    keeps an empty or one-character value from being used to replay another submission, and the
    ceiling keeps a hostile POST from bloating the key column.
    """
    posted = str(form.get(REQUEST_KEY_FIELD) or "").strip() if form is not None else ""
    if REQUEST_KEY_MIN_LEN <= len(posted) <= REQUEST_KEY_MAX_LEN:
        return posted
    return _request_digest(values, decision, scope)


def contact_digest(values):
    """A stable digest of a submission's exact contact keys (normalised phone + email).

    The key a *shared contact claim* is taken under (client portal changes): two submissions that
    carry the same phone or email cannot both hold it at once, so on the remote libSQL backend —
    where a write only crosses to a concurrent request once it commits — two **different**
    submission keys with identical contact still cannot race into two customers. Prefixed ``c:`` so
    a contact claim can never collide with a posted or derived submission key.
    """
    values = values or {}
    payload = f"{normalise_phone(values.get('phone', ''))}|{normalise_email(values.get('email', ''))}"
    if not payload.strip("|"):
        return ""
    return "c:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_unique_violation(exc):
    """True for a local ``sqlite3`` or remote libSQL unique-index refusal.

    The two backends raise different types (``sqlite3.IntegrityError`` locally, ``LibsqlError``
    from the Turso adapter), so the check is by type where possible and by the error text
    otherwise. Only a *unique* refusal is swallowed here; anything else is re-raised.
    """
    if isinstance(exc, sqlite3.IntegrityError):
        return True
    text = f"{getattr(exc, 'code', '')} {getattr(exc, 'message', '')} {exc}".upper()
    return "UNIQUE" in text or "CONSTRAINT" in text


def claim_submission(scope, request_key):
    """Try to claim ``(scope, request_key)``; report whether to write, replay or refuse.

    Returns ``(state, row)`` where ``state`` is ``SUBMISSION_CLAIMED`` (this request may write),
    ``SUBMISSION_REPLAY`` (a previous request already wrote — ``row`` carries its result) or
    ``SUBMISSION_IN_FLIGHT`` (another request holds the claim right now).

    The claim is a single ``INSERT`` that only fires when no row exists yet, so on the two backends
    this app uses it is atomic exactly where it must be: the libSQL adapter autocommits every
    statement and its ``commit()``/``rollback()`` are no-ops (``app/turso_db.py``), so a claim that
    rode on a transaction would be invisible to a concurrent second request. The unique index is
    the backstop for the narrow race where two statements both pass the ``NOT EXISTS`` check; the
    loser sees the constraint refusal and reads the winner's row.
    """
    db = get_db()
    try:
        cursor = db.execute(
            "INSERT INTO portal_submission_claims (scope, request_key, status, created_at) "
            "SELECT ?, ?, ?, ? "
            "WHERE NOT EXISTS (SELECT 1 FROM portal_submission_claims "
            "WHERE scope = ? AND request_key = ?)",
            (scope, request_key, CLAIM_IN_FLIGHT, now(), scope, request_key),
        )
        won = cursor.rowcount == 1
    except Exception as exc:  # the unique index winning a race — expected, not an error
        if not _is_unique_violation(exc):
            raise
        won = False
    if won:
        return SUBMISSION_CLAIMED, None
    row = db.execute(
        "SELECT status, customer_id, reference, linked, created, registration_id "
        "FROM portal_submission_claims WHERE scope = ? AND request_key = ?",
        (scope, request_key),
    ).fetchone()
    if row is not None and str(_row_value(row, "status", "")) == CLAIM_DONE:
        return SUBMISSION_REPLAY, row
    return SUBMISSION_IN_FLIGHT, None


def complete_submission(scope, request_key, result):
    """Mark a claim done and store the result a replay must return, once the customer is written.

    Called only after the write itself has committed, so a claim left ``in_flight`` is always a
    write that did not finish — the safe direction (a stuck claim refuses, never duplicates).
    """
    result = result or {}
    get_db().execute(
        "UPDATE portal_submission_claims "
        "SET status = ?, customer_id = ?, reference = ?, linked = ?, created = ?, "
        "registration_id = ?, completed_at = ? "
        "WHERE scope = ? AND request_key = ?",
        (
            CLAIM_DONE,
            result.get("customer_id"),
            str(result.get("reference") or ""),
            1 if result.get("linked") else 0,
            1 if result.get("created") else 0,
            result.get("registration_id"),
            now(),
            scope,
            request_key,
        ),
    )
    get_db().commit()


def release_submission(scope, request_key):
    """Drop an ``in_flight`` claim after a failed write, so the customer may try again.

    Only an ``in_flight`` row is ever deleted: a ``done`` claim stays so a late replay still gets
    the original success instead of writing a second customer.
    """
    try:
        get_db().execute(
            "DELETE FROM portal_submission_claims "
            "WHERE scope = ? AND request_key = ? AND status = ?",
            (scope, request_key, CLAIM_IN_FLIGHT),
        )
        get_db().commit()
    except Exception:
        # Releasing is best-effort: the operator's cleanup is the customer row, and a stuck claim
        # only refuses the *same* key — never a different submission.
        pass


def submission_result_from_claim(row):
    """The same ``create_or_link_customer`` result, rebuilt from a completed claim row."""
    return {
        "customer_id": _row_value(row, "customer_id", None),
        "created": bool(_row_value(row, "created", 0)),
        "linked": bool(_row_value(row, "linked", 0)),
        "reference": str(_row_value(row, "reference", "") or ""),
        "registration_id": _row_value(row, "registration_id", None),
        "replayed": True,
    }


def registration_is_open():
    """False while the published notice still carries an open placeholder (D11).

    The one switch, read from the document itself: a customer is never asked to accept a
    notice whose full text is not published yet, and the day Sano's facts land the form
    opens with no code change.

    A *read failure* (the notice document cannot be read, its pack manifest is missing,
    or the published-notice table cannot be queried) is treated as **closed**, never as
    open: the safe direction for a gate that decides whether a stranger may be asked to
    accept a notice is to stay shut and show the closed message, rather than raise a 500
    in the customer's face.
    """
    try:
        return bool(consent.notice_is_publishable())
    except (OSError, KeyError, RuntimeError, ValueError):
        return False


def _source_keys_taken(system, reference, exclude_customer_id=None):
    row = get_db().execute(
        "SELECT id FROM customers WHERE source_system = ? AND source_id = ? AND id <> ?",
        (system, reference, exclude_customer_id if exclude_customer_id is not None else -1),
    ).fetchone()
    return row is not None


def create_or_link_customer(form, branch_id, decision, slug=None, portal_duplicate_refuse=False):
    """Create a client, or link this submission to the one it matched — the customer's choice.

    ``decision`` is the posted value: ``''`` (first pass — create when nothing matched),
    ``'create'`` ("none of these, I'm new") or ``'link:<id>'`` ("this is me"). ``slug`` is the branch
    slug from the **URL** (the route passes it); a posted ``portal_slug`` field is only a fallback,
    because the record's provenance should not be dictated by a hidden input a stranger can edit.

    ``portal_duplicate_refuse`` (client portal changes) is the **public routes'** switch: when true,
    an **exact** phone or email match — the same person with near-certainty — is refused outright
    with :class:`DuplicateRegistration`, whatever ``decision`` was posted, so a public form can
    never write a second customer for a contact already on file. A name-only match is **not** a
    duplicate (two people share a name), so it is left to the create path. Left false (the staff
    callers), the historical create/link behaviour is unchanged.

    Returns ``{customer_id, created, linked, reference}``. Raises ``ValueError`` with the message
    the page shows, and writes **nothing**, when the submission is invalid, when it is a duplicate
    and refusal is on, when the link target was not one of this submission's own candidates (a
    crafted POST must not be able to write to an arbitrary client), or when it would resurrect a
    blocked client.

    **One submission, one customer.** Before any row is written this request claims the
    submission's key (:func:`claim_submission`), so a second tap of the same form — a double-click
    while the first request is still in flight, or a replay once it is done — cannot create a
    second client. A completed claim is answered with the *same* success (``replayed=True``) and a
    claim another request still holds is refused with :class:`SubmissionInFlight`. The duplicate
    check runs **after** the claim and before any write, so a replay returns the original receipt
    without re-checking. A failed or refused write releases the claim, so a corrected resubmission
    still works.
    """
    values = submission_values(form)
    action, target_id = parse_decision(decision)
    slug = str(slug or form.get("portal_slug") or "").strip()

    request_key = resolve_request_key(form, slug, values, decision)
    state, claim_row = claim_submission(slug, request_key)
    if state == SUBMISSION_REPLAY:
        return submission_result_from_claim(claim_row)
    if state == SUBMISSION_IN_FLIGHT:
        # The key is deliberately left in place: a *retry* once the first request finishes replays
        # (returns its success) rather than writing a second customer.
        raise SubmissionInFlight(IN_FLIGHT_MESSAGE)

    # Claim each exact identifier globally, not merely the combined phone/email pair at
    # one branch. Requests with the same email but different phones (or vice versa), even
    # from different branches, must not race past the duplicate check on autocommit Turso.
    contact_claims = []
    reference = new_reference(slug)
    source_id = f"{slug}:{reference}" if slug else reference
    db = get_db()

    try:
        if portal_duplicate_refuse:
            identifiers = [(slug, contact_digest(values))]
            for kind, value in (("phone", normalise_phone(values.get("phone"))),
                                ("email", normalise_email(values.get("email")))):
                if value:
                    key = "contact:" + kind + ":" + hashlib.sha256(value.encode()).hexdigest()
                    identifiers.append(("@portal-contact-locks", key))
            for scope, key in sorted(set(identifiers)):
                if not key:
                    continue
                contact_state, _ = claim_submission(scope, key)
                if contact_state != SUBMISSION_CLAIMED:
                    raise SubmissionInFlight(DUPLICATE_MESSAGE)
                contact_claims.append((scope, key))
        # Read matches only AFTER acquiring every contact lock: a previous writer may
        # have committed between our request claim and our contact claim.
        matches = find_possible_matches(values["name"], values["phone"], values["email"])
        blocked = blocked_match(values["name"], values["phone"], values["email"])
        if portal_duplicate_refuse:
            # An exact phone or email match is the same person with near-certainty: refuse, and
            # never create or link, whatever ``decision`` was posted (a crafted ``create`` or
            # ``link:<id>`` is refused the same way). A name-only match is NOT a duplicate — two
            # people share a name — so it is left to the create path below.
            if blocked is not None:
                raise ValueError(BLOCKED_MESSAGE)
            if any(match["confidence"] == CONFIDENCE_HIGH for match in matches):
                raise DuplicateRegistration(DUPLICATE_MESSAGE)
        result = _write_submission(
            db, values, action, target_id, matches, blocked, slug, reference, source_id, branch_id
        )
    except Exception:
        release_submission(slug, request_key)
        for scope, key in contact_claims:
            release_submission(scope, key)
        raise
    complete_submission(slug, request_key, result)
    # Release only claims this request actually won, and only after its receipt is durable.
    for scope, key in contact_claims:
        release_submission(scope, key)
    # Only the writer reaches here: completed-claim replays return above. Send
    # after both the customer write and its receipt commit, never on a refusal.
    try:
        from app.services.telegram import send_portal_submission_notification

        send_portal_submission_notification(result["customer_id"])
    except Exception:
        from flask import current_app

        current_app.logger.exception("Customer portal notification failed")
    # Web Push, beside the Telegram alert and *once*: this line is reached only by the
    # request that actually wrote the customer (a replay returned above), and the branch
    # is re-read from the saved row by the service, never taken from this request. The
    # call is best-effort — a push failure (or a host without pywebpush) must never undo
    # or refuse a client's submission that is already durable.
    try:
        from app.services.push_notifications import notify_portal_submission

        notify_portal_submission(result["customer_id"])
    except Exception:
        from flask import current_app

        current_app.logger.exception("Customer portal push notification failed")
    return result


def _write_submission(db, values, action, target_id, matches, blocked, slug, reference, source_id, branch_id):
    """The link / create writes themselves — reached only by a request that holds the claim."""
    if action == "link":
        # Only a **high-confidence** match may be linked (an exact normalised phone or
        # email). A name-only match is a hint for staff, never proof of identity: allowing
        # it to collect a link would let a stranger who merely guessed a name write the
        # blank contact fields of any client in the book (found in the release security
        # audit). The candidate is still shown on the "Is this you?" screen; it is the
        # *write* that is refused here.
        allowed = {
            match["customer_id"]
            for match in matches
            if match["confidence"] == CONFIDENCE_HIGH
        }
        if target_id not in allowed:
            raise ValueError("Please choose one of the options we showed you.")
        existing = get_customer(target_id)
        if existing is None:
            raise ValueError("Please choose one of the options we showed you.")
        if blocked is not None:
            raise ValueError(BLOCKED_MESSAGE)
        # Only blank fields are filled. A populated field on the existing record is never
        # overwritten by a public page: the counter's record wins, always.
        updates = {
            key: values[key]
            for key in FILLABLE_FIELDS
            if values.get(key) and not str(_row_value(existing, key, "")).strip()
        }
        if updates:
            assignments = ", ".join(f"{key} = :{key}" for key in sorted(updates))
            db.execute(
                f"UPDATE customers SET {assignments} WHERE id = :id",
                {**updates, "id": target_id},
            )
        # Trace the submission on the record it linked to, but never displace an existing source
        # key (an imported client keeps their Booqable key).
        if not str(_row_value(existing, "source_id", "")).strip():
            if not _source_keys_taken(PORTAL_SOURCE_SYSTEM, source_id, exclude_customer_id=target_id):
                db.execute(
                    "UPDATE customers SET source_system = ?, source_id = ? WHERE id = ?",
                    (PORTAL_SOURCE_SYSTEM, source_id, target_id),
                )
        db.commit()
        return {"customer_id": target_id, "created": False, "linked": True, "reference": reference}

    if action == "create" or action is None:
        if blocked is not None:
            raise ValueError(BLOCKED_MESSAGE)
        # create_customer() is the house path: name required, email lower-cased, the branch taken
        # from the session — which, on a public request, is nobody. The branch the QR belongs to is
        # passed in explicitly so the client — and the Telegram message — keep that branch
        # attribution (client instruction: a Midrand submission stays Midrand). The notification is
        # deferred (`notify=False`) so it goes out once, after the pending registration below.
        from app.services import portal_registrations

        # The whole create is ONE submission: the customer row, its portal source keys and the
        # pending registration are written together and committed once. A failure while recording
        # the pending registration rolls the customer row back with it, so the client never leaks
        # as an ordinary, approved-looking customer that carries no pending flag. Every other
        # caller keeps create_customer's and record_pending_registration's own commits; here each
        # takes the deferred form and this path owns the single commit.
        customer_id = None
        try:
            customer_id = create_customer(values, branch_id=branch_id, notify=False, defer_commit=True, pending_portal=True)
            if _source_keys_taken(PORTAL_SOURCE_SYSTEM, source_id):
                reference = new_reference(slug)
                source_id = f"{slug}:{reference}" if slug else reference
            db.execute(
                "UPDATE customers SET branch_id = ?, source_system = ?, source_id = ?, created_by_user_id = NULL WHERE id = ?",
                (branch_id, "portal_pending", source_id, customer_id),
            )
            # Company-only website: stored beside the other custom details. Never written for an
            # individual (submission_values already dropped it), never for the staff path.
            if values.get("website") and values.get("customer_type") == "company":
                row = db.execute(
                    "SELECT custom_fields_json FROM customers WHERE id = ?", (customer_id,)
                ).fetchone()
                try:
                    fields = json.loads((_row_value(row, "custom_fields_json", "") or "") or "{}")
                except ValueError:
                    fields = {}
                if not isinstance(fields, dict):
                    fields = {}
                fields["website"] = values["website"]
                db.execute(
                    "UPDATE customers SET custom_fields_json = ? WHERE id = ?",
                    (json.dumps(fields, ensure_ascii=False), customer_id),
                )
            # A brand-new portal client is a *pending* registration: staff must verify the ID and
            # record the main vehicle, then approve — the submission itself never approves anyone.
            # Additive only: a link to an existing client (above) records no registration, so an
            # already-verified client is never downgraded, and nothing is ever backfilled.
            registration_id = portal_registrations.record_pending_registration(
                customer_id,
                branch_id,
                reference,
                name=values["name"],
                email=values["email"],
                phone=values["phone"],
                commit=False,
            )
            # Fail closed on Turso too: its adapter autocommits each statement.
            # Only drop the initial pending marker once the queue row exists.
            db.execute("UPDATE customers SET source_system = ? WHERE id = ?",
                       (PORTAL_SOURCE_SYSTEM, customer_id))
            db.commit()
        except Exception:
            db.rollback()
            # Compensating cleanup: the remote libSQL adapter autocommits every statement and its
            # commit()/rollback() are deliberate no-ops (see app/turso_db.py), so a rollback alone
            # cannot undo the customer row there. Delete it explicitly so a failed pending insert
            # can never leave an approved-looking client on either backend. On SQLite the rollback
            # has already removed the row and these deletes are harmless no-ops.
            if customer_id is not None:
                try:
                    db.execute("DELETE FROM portal_registrations WHERE customer_id = ?", (customer_id,))
                    db.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
                    db.commit()
                except Exception:
                    pass
            raise
        return {
            "customer_id": customer_id,
            "created": True,
            "linked": False,
            "reference": reference,
            "registration_id": registration_id,
        }

    raise ValueError("Please choose one of the options we showed you.")  # pragma: no cover
