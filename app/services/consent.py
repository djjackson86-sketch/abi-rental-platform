"""POPIA consent records for the client-facing capture points (feature P, §P1).

Don's ask: a privacy-terms tick box the client must accept, "similar to trailerpro
app". Decision **D10** answers how far we take it:

* the client-facing capture points — the per-branch portal (§B2) and the public
  booking form (§C2) — each show the privacy-notice link plus a **required,
  unticked-by-default** acceptance;
* the submission is **refused server-side** when it is unticked (:func:`consent_required_error`
  is the one refusal message both forms return, so the wording cannot drift between them);
* every acceptance is **recorded** — the reference app keeps consent in component
  state only, which evidences nothing.

What is stored is deliberately the minimum: **who** (the customer), **which notice
version**, **which channel**, **when**. There is no IP address, no user agent and no
device fingerprint, and ``tests/test_programme_20260923_popia_consent.py`` asserts the
schema so that decision cannot be quietly undone later.

The notice version is pinned to the reviewed document (``docs/popia/PRIVACY-NOTICE.md``)
by a test — the published page and the acceptance record have to name the same version,
or an acceptance proves nothing.
"""

from app.db import get_db
from app.services import popia_pack
from app.services.timezone import local_now_iso

#: The wording in force, taken from the ``**Version:**`` line of the notice document.
#: ``tests/test_programme_20260923_popia_consent.py::test_privacy_notice_version_matches_the_document``
#: pins the two together, so bumping the document without bumping this fails the suite.
PRIVACY_NOTICE_VERSION = "1.1"

#: The document's ``**Last reviewed:**`` date, pinned the same way.
PRIVACY_NOTICE_REVIEWED = "2026-09-23"

#: The one consent type this phase records. Marketing consent (POPIA s69) stays the
#: separate ``marketing_opt_in`` field on the customer record — not this table.
CONSENT_TYPE_POPIA_PRIVACY = "popia_privacy"

#: The channels the capture points name, so an audit line reads like a sentence and
#: the same words are used everywhere. A caller may pass any label; these are the
#: canonical ones the portal/booking forms must use.
CHANNEL_PORTAL = "branch portal"
CHANNEL_PUBLIC_BOOKING = "public booking page"
CHANNEL_COUNTER = "counter"


def notice_is_publishable():
    """False while the notice still carries an open placeholder (decision D11)."""
    return popia_pack.is_complete(popia_pack.PRIVACY_NOTICE_KEY)


def consent_required_error():
    """The single refusal both public capture points return when the box is unticked."""
    return "Please tick the box to accept the privacy notice before sending this in."


def _customer_exists(db, customer_id):
    return db.execute("SELECT id FROM customers WHERE id = ?", (customer_id,)).fetchone() is not None


#: What a ticked box posts. The check is done on the *value*, not on truthiness:
#: ``"0"`` is a truthy string in Python but it is not consent, so a crafted post of
#: ``popia_consent=0`` is refused like an unticked box (decision D10's server-side rule).
_ACCEPTED_VALUES = {"1", "true", "yes", "on", "y", "t", "checked"}


def acceptance_given(posted):
    """Whether the posted consent field is a real acceptance."""
    if posted is True:
        return True
    if posted is None or posted is False:
        return False
    if isinstance(posted, int):
        return posted == 1
    return str(posted).strip().lower() in _ACCEPTED_VALUES


def record_consent(customer_id, channel, accepted, notice_version=None, consent_type=CONSENT_TYPE_POPIA_PRIVACY):
    """Record one acceptance and return its row id.

    ``accepted`` is the posted box: anything that is not a real acceptance is refused
    with ``ValueError`` and **nothing is written**, because a consent record that
    exists when the box was not ticked is worse than no record at all. An unknown
    customer is refused too (the foreign key would fail anyway; this refuses it with a
    message rather than a database error out of a route).
    """
    if not acceptance_given(accepted):
        raise ValueError(consent_required_error())
    db = get_db()
    if not _customer_exists(db, customer_id):
        raise ValueError(f"Unknown customer: {customer_id}")
    cur = db.execute(
        "INSERT INTO consent_records (customer_id, consent_type, notice_version, channel, accepted_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            customer_id,
            consent_type or CONSENT_TYPE_POPIA_PRIVACY,
            (notice_version or PRIVACY_NOTICE_VERSION),
            (channel or "").strip(),
            local_now_iso(),
        ),
    )
    db.commit()
    return cur.lastrowid


def consent_for(customer_id, consent_type=CONSENT_TYPE_POPIA_PRIVACY):
    """The latest acceptance for that customer (or ``None``) — newest first."""
    return get_db().execute(
        "SELECT * FROM consent_records WHERE customer_id = ? AND consent_type = ? "
        "ORDER BY accepted_at DESC, id DESC LIMIT 1",
        (customer_id, consent_type),
    ).fetchone()


def consent_summary(customer_id, consent_type=CONSENT_TYPE_POPIA_PRIVACY):
    """The plain-language evidence line the admin customer page shows, or ``None``.

    Shape: ``POPIA consent — accepted 2026-09-23 (notice v1.1, via branch portal)``.
    """
    row = consent_for(customer_id, consent_type)
    if not row:
        return None
    accepted_on = (row["accepted_at"] or "")[:10] or "an unrecorded date"
    version = row["notice_version"] or "unversioned"
    channel = (row["channel"] or "").strip() or "an unrecorded channel"
    return f"POPIA consent — accepted {accepted_on} (notice v{version}, via {channel})"


def delete_consents_for_customer(customer_id):
    """Clear a customer's consent rows and report how many went.

    The foreign key declares ``ON DELETE CASCADE``, but a Turso connection does not
    guarantee ``PRAGMA foreign_keys=ON``, so ``customers.delete_customer()`` clears
    these explicitly — the same belt-and-braces cleanup it does for a client's
    vehicles. No orphans, on either path.
    """
    db = get_db()
    cur = db.execute("DELETE FROM consent_records WHERE customer_id = ?", (customer_id,))
    db.commit()
    return cur.rowcount
