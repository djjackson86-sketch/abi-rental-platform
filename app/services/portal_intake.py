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
import os
import re
import secrets
import time
import unicodedata

from app.db import get_db
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

#: Lookup throttle — 10 per address per 5 minutes (D7).
LOOKUP_LIMIT = 10
LOOKUP_WINDOW_SECONDS = 300

#: In-process only. Keyed by a salted digest of the address, never the address itself.
_LOOKUP_HITS = {}
_LOOKUP_SALT = os.urandom(16).hex()

#: The columns a portal submission may fill in on an existing record. Deliberately not the
#: blocking columns and not the balance: a public page cannot block or unblock anybody.
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
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: An SA number is 10 digits with the trunk 0 (9 without); the window is generous on purpose so a
#: landline, a border number or a "+27 82…" typed four ways all pass, while "123" does not.
_PHONE_DIGITS_MIN = 9
_PHONE_DIGITS_MAX = 13

INTAKE_CLOSED_MESSAGE = (
    "Online registration for this branch is not open yet — our privacy notice is still being "
    "finalised. Please give your details to the counter staff at this branch and they will load "
    "you onto the system for you."
)

BLOCKED_MESSAGE = (
    "We already hold a record in this name at the branch. Please speak to the counter staff "
    "before registering again."
)


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
    """Only what the match and the mask need — never the address, the balance or the history."""
    return get_db().execute(
        "SELECT id, name, phone, email, is_blocked FROM customers"
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
    """The public form's values, cleaned and validated — the same shape the staff form posts.

    Validation is deliberately small and honest: a name, at least one way to reach the customer,
    and a plausible email/phone when one was given. Everything else is optional, because a
    customer filling this in at a counter should never be blocked by a field they do not know.
    """
    values = {
        "customer_type": "individual",
        "name": str(form.get("name") or "").strip(),
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
    if not values["name"]:
        raise ValueError("Please enter your name.")
    if not values["phone"] and not values["email"]:
        raise ValueError("Please give a phone number or an email address so the branch can reach you.")
    if values["email"] and not _EMAIL_RE.match(values["email"]):
        raise ValueError("Please enter a valid email address, or leave the email box empty.")
    if values["phone"]:
        digits = normalise_phone(values["phone"])
        if not (_PHONE_DIGITS_MIN <= len(digits) <= _PHONE_DIGITS_MAX):
            raise ValueError("Please enter a valid phone number, e.g. 082 123 4567.")
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


def registration_is_open():
    """False while the published notice still carries an open placeholder (D11).

    The one switch, read from the document itself: a customer is never asked to accept a notice
    whose full text is not published yet, and the day Sano's five facts land the form opens with
    no code change.
    """
    return consent.notice_is_publishable()


def _source_keys_taken(system, reference, exclude_customer_id=None):
    row = get_db().execute(
        "SELECT id FROM customers WHERE source_system = ? AND source_id = ? AND id <> ?",
        (system, reference, exclude_customer_id if exclude_customer_id is not None else -1),
    ).fetchone()
    return row is not None


def create_or_link_customer(form, branch_id, decision, slug=None):
    """Create a client, or link this submission to the one it matched — the customer's choice.

    ``decision`` is the posted value: ``''`` (first pass — create when nothing matched),
    ``'create'`` ("none of these, I'm new") or ``'link:<id>'`` ("this is me"). ``slug`` is the branch
    slug from the **URL** (the route passes it); a posted ``portal_slug`` field is only a fallback,
    because the record's provenance should not be dictated by a hidden input a stranger can edit.

    Returns ``{customer_id, created, linked, reference}``. Raises ``ValueError`` with the message
    the page shows, and writes **nothing**, when the submission is invalid, when the link target
    was not one of this submission's own candidates (a crafted POST must not be able to write to
    an arbitrary client), or when it would resurrect a blocked client.
    """
    values = submission_values(form)
    action, target_id = parse_decision(decision)
    matches = find_possible_matches(values["name"], values["phone"], values["email"])
    blocked = blocked_match(values["name"], values["phone"], values["email"])
    slug = str(slug or form.get("portal_slug") or "").strip()
    reference = new_reference(slug)
    source_id = f"{slug}:{reference}" if slug else reference
    db = get_db()

    if action == "link":
        allowed = {match["customer_id"] for match in matches}
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
        # written below, because that is the branch this client belongs to.
        customer_id = create_customer(values)
        if _source_keys_taken(PORTAL_SOURCE_SYSTEM, source_id):
            reference = new_reference(slug)
            source_id = f"{slug}:{reference}" if slug else reference
        db.execute(
            "UPDATE customers SET branch_id = ?, source_system = ?, source_id = ? WHERE id = ?",
            (branch_id, PORTAL_SOURCE_SYSTEM, source_id, customer_id),
        )
        db.commit()
        return {"customer_id": customer_id, "created": True, "linked": False, "reference": reference}

    raise ValueError("Please choose one of the options we showed you.")  # pragma: no cover
