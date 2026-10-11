"""Self-registered customers awaiting verification (client portal changes).

A public customer-portal submission that creates a **new** client does not, by itself,
make that client an approved customer of the branch. The submission is recorded here as a
**pending registration**; the client keeps behaving like a new sign-up — hidden from the
default customer list and from order / allocation selection — until staff have done both
of the verifications the owner asked for and then approved it explicitly:

* the **ID verification flag** the counter already uses (``customers.client_verified = 1``),
  set from the ordinary customer form; and
* a **recorded main vehicle** (the client's chosen ``main_vehicle_id``, captured by the
  existing scan screen).

Either can be done first — the approval is a separate, deliberate action that only checks
the *final* state, so nothing is ever auto-approved by a submission or a scan.

Two rules this module keeps deliberately narrow:

* **No ID image, ID number or OCR is stored.** A "pending registration" row is the
  reference, the branch and the customer's own captured fields, full stop. Verification is
  the existing yes/no flag, exactly as it is at the counter.
* **Additive only, never backfilled.** Existing and imported clients get no row here, so
  they continue to behave exactly as approved clients always have. Only a brand-new portal
  sign-up is ever pending.

Branch scoping reuses the app-wide ``customer_branch_clause``, so a depot's pending queue
shows that depot's submissions and an all-branch account sees them all — the same session
scope that governs every other customer view (a crafted request can only ever narrow it).
"""

from app.db import get_db, now
from app.services.access import (
    current_session_user_id,
    customer_branch_clause,
    session_branch_scope_ids,
)
from app.services.vehicles import get_main_vehicle_id

#: The states a registration can be in. ``pending`` hides a client as an unverified
#: self-registration; ``approved`` turns them into a normal client; ``deleted`` is the
#: verification-phase **tombstone** — the sign-up is removed from the queue and the client
#: lists, but the customer row (and any order/invoice/credit that references it) is kept, so
#: no financial record is ever erased or orphaned. ``pending`` and ``deleted`` both hide.
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DELETED = "deleted"

#: The registration states that make a client behave as a non-approved, hidden one — a
#: still-pending sign-up and its deleted (tombstoned) follow-on. Shared by the customer-list
#: exclusion clause and by ``is_pending`` so a removed sign-up is never shown *and* never
#: bookable.
HIDDEN_STATUSES = (STATUS_PENDING, STATUS_DELETED)

#: The refusal sentences the approve route shows. One per unmet check, so a staff member
#: is told exactly what is missing instead of a generic "cannot approve".
ID_NOT_VERIFIED_MESSAGE = (
    "Record the client's ID verification (Client verified = Yes) on the customer form "
    "before approving this registration."
)
NO_MAIN_VEHICLE_MESSAGE = (
    "Capture and select the client's main vehicle (scan the licence disk and make it the "
    "main vehicle) before approving this registration."
)

#: Refusal shown when staff — or a crafted order payload — try to book a client whose portal
#: registration is still pending. A self-registered client cannot take an order before staff
#: have verified the ID, recorded the main vehicle and approved the registration.
PENDING_BLOCKED_MESSAGE = (
    "This client self-registered through the portal and is still awaiting verification. "
    "Verify the ID and record the main vehicle, then approve the registration before "
    "booking an order."
)

#: The column list the pending queue and the approval panel must carry, in the contract
#: order the frontend was given: id / reference / customer_id / name / email / phone /
#: branch_name / created_at (plus branch_id, which the branch-scope check needs).
CONTRACT_FIELDS = (
    "id",
    "reference",
    "customer_id",
    "name",
    "email",
    "phone",
    "branch_name",
    "created_at",
)


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _row_value(row, key, default=None):
    """Read a column from a sqlite3 row, a dict or a libSQL Row, tolerating absence.

    Handles ``KeyError``/``IndexError``/``TypeError`` and a libSQL Row (which exposes
    ``asdict()``/``_fields`` rather than ``keys()``); it never assumes a raw row shape.
    """
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        pass
    try:
        data = row.asdict()
    except (AttributeError, TypeError):
        data = None
    if isinstance(data, dict):
        return data.get(key, default)
    try:
        data = dict(zip(row._fields, row))
    except (AttributeError, TypeError):
        return default
    return data.get(key, default)


def _mapping(row):
    """A plain dict for a registration row (libSQL-safe), or ``None`` for no row."""
    if row is None:
        return None
    from app.services.reports import row_dict

    return row_dict(row)


def pending_exclusion_clause(alias="c"):
    """SQL fragment that excludes a client while they are a pending self-registration.

    Appended to customer list / count / selection queries so a self-registered client does
    not behave as an approved one until staff approve them, and a **deleted** (tombstoned)
    sign-up stays hidden even after it is removed from the pending queue. A client with
    **no** pending/deleted row (every existing, imported or already-approved client) is
    untouched.
    """
    hidden = ", ".join(f"'{status}'" for status in HIDDEN_STATUSES)
    return (
        f" AND COALESCE({alias}.source_system, '') <> 'portal_pending'"
        f" AND NOT EXISTS (SELECT 1 FROM portal_registrations pr "
        f"WHERE pr.customer_id = {alias}.id AND pr.status IN ({hidden}))"
    )


# --- recording ----------------------------------------------------------------------------------

def record_pending_registration(customer_id, branch_id, reference, name="", email="", phone="", commit=True):
    """Record one pending registration for a freshly created portal client.

    Called by ``portal_intake`` on the **create** path only, in the same request that wrote
    the customer. Returns the new row id. A submission that links to an existing client does
    not call this — an existing (possibly already verified) client is never downgraded.

    ``commit`` lets the caller that must write the customer row and this registration as one
    submission hold the commit back and own it itself (``commit=False``), so a failure here
    rolls the whole submission back. Default ``True`` keeps every other caller committing
    immediately.
    """
    resolved = _as_int(customer_id)
    if resolved is None:
        raise ValueError("A pending registration needs a customer.")
    db = get_db()
    cur = db.execute(
        "INSERT INTO portal_registrations "
        "(reference, customer_id, branch_id, name, email, phone, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(reference or ""),
            resolved,
            _as_int(branch_id),
            str(name or ""),
            str(email or ""),
            str(phone or ""),
            STATUS_PENDING,
            now(),
        ),
    )
    if commit:
        db.commit()
    return cur.lastrowid


# --- reads --------------------------------------------------------------------------------------

def list_pending_registrations():
    """The branch-scoped pending queue, newest first, in the frontend contract shape.

    Each item is a dict with the contract keys (``id``, ``reference``, ``customer_id``,
    ``name``, ``email``, ``phone``, ``branch_name``, ``created_at``) plus ``branch_id``.
    The session's branch scope is applied through ``customer_branch_clause`` — a depot sees
    its own submissions, an all-branch account sees every pending sign-up.
    """
    alias = "r"
    clause, params = customer_branch_clause(alias)
    sql = (
        "SELECT r.id, r.reference, r.customer_id, r.branch_id, r.name, r.email, r.phone, "
        "r.created_at, b.name AS branch_name "
        "FROM portal_registrations r "
        "LEFT JOIN branches b ON b.id = r.branch_id "
        "WHERE r.status = ?" + clause +
        " ORDER BY r.created_at DESC, r.id DESC"
    )
    rows = get_db().execute(sql, [STATUS_PENDING, *params]).fetchall()
    queue = []
    for row in rows:
        mapping = _mapping(row) or {}
        queue.append(
            {
                "id": mapping.get("id"),
                "reference": mapping.get("reference") or "",
                "customer_id": mapping.get("customer_id"),
                "branch_id": mapping.get("branch_id"),
                "name": mapping.get("name") or "",
                "email": mapping.get("email") or "",
                "phone": mapping.get("phone") or "",
                "branch_name": mapping.get("branch_name") or "",
                "created_at": mapping.get("created_at") or "",
            }
        )
    return queue


def registration_by_id(registration_id):
    """One registration (any status) as a dict, or ``None`` — the approve route's lookup."""
    resolved = _as_int(registration_id)
    if resolved is None:
        return None
    row = get_db().execute(
        "SELECT r.*, b.name AS branch_name FROM portal_registrations r "
        "LEFT JOIN branches b ON b.id = r.branch_id WHERE r.id = ?",
        (resolved,),
    ).fetchone()
    return _mapping(row)


def approved_registration_for_customer(customer_id):
    """Saved approval evidence for the verification panel after refresh."""
    resolved = _as_int(customer_id)
    if resolved is None:
        return None
    row = get_db().execute(
        "SELECT r.*, b.name AS branch_name FROM portal_registrations r "
        "LEFT JOIN branches b ON b.id = r.branch_id "
        "WHERE r.customer_id = ? AND r.status = ? ORDER BY r.id DESC LIMIT 1",
        (resolved, STATUS_APPROVED),
    ).fetchone()
    return _mapping(row)


def pending_registration_for_customer(customer_id):
    """The client's pending registration as a dict, or ``None``.

    This is the mapping the customer edit/detail route hands the template; ``None`` means
    the client is not a pending self-registration (an ordinary, imported or approved
    client), so the verification panel is not shown for them.
    """
    resolved = _as_int(customer_id)
    if resolved is None:
        return None
    row = get_db().execute(
        "SELECT r.*, b.name AS branch_name FROM portal_registrations r "
        "LEFT JOIN branches b ON b.id = r.branch_id "
        "WHERE r.customer_id = ? AND r.status = ? ORDER BY r.id DESC LIMIT 1",
        (resolved, STATUS_PENDING),
    ).fetchone()
    return _mapping(row)


def is_pending(customer_id):
    """True when the client is a **pending or deleted** portal registration (not approved).

    The order create/update path uses this to refuse booking a self-registered client before
    staff approve them, and (after the tombstone) to also refuse one whose sign-up was
    removed — a deleted sign-up is *not* a real client, so no order may ever be raised
    against it, even though its customer row is kept for the ledger. A client with no
    pending/deleted row — every ordinary, imported or already-approved client — is never
    pending, which keeps the rule additive and never backfilled.
    """
    resolved = _as_int(customer_id)
    if resolved is None:
        return False
    row = get_db().execute(
        "SELECT 1 FROM customers c WHERE c.id = ? AND "
        "(c.source_system = 'portal_pending' OR EXISTS "
        "(SELECT 1 FROM portal_registrations r WHERE r.customer_id = c.id "
        "AND r.status IN (?, ?))) LIMIT 1",
        (resolved, STATUS_PENDING, STATUS_DELETED),
    ).fetchone()
    return row is not None


# --- the verification flags ---------------------------------------------------------------------

def id_verified(customer):
    """True when the counter's ID verification flag is set (``client_verified = 1``).

    ``None`` means "not answered yet" and is deliberately NOT verified — the same
    distinction the customer list and the order pages already make.
    """
    value = _row_value(customer, "client_verified", None)
    if value is None or value == "":
        return False
    try:
        return int(value) == 1
    except (TypeError, ValueError):
        return False


def main_vehicle_recorded(customer_id):
    """True when the client has a recorded **main** vehicle (the one invoices print)."""
    resolved = _as_int(customer_id)
    if resolved is None:
        return False
    return get_main_vehicle_id(resolved) is not None


def approval_blockers(customer):
    """The reasons this client may not be approved yet, in a stable order.

    Empty list means every check is met. Only ever computed from the existing
    verification semantics — there is no ID number, image or scan text involved.
    """
    blockers = []
    if not id_verified(customer):
        blockers.append(ID_NOT_VERIFIED_MESSAGE)
    if not main_vehicle_recorded(_row_value(customer, "id", None)):
        blockers.append(NO_MAIN_VEHICLE_MESSAGE)
    return blockers


def is_approvable(customer):
    """True when the client is ID-verified **and** has a recorded main vehicle."""
    return not approval_blockers(customer)


def branch_in_session_scope(registration):
    """True when the session may act on this registration's branch.

    An all-branch/owner session (scope ``None``) may act on any registration. A
    branch-limited session may act only on its own depots — the same boundary every other
    customer view uses, checked server-side so a crafted id cannot cross it.
    """
    if registration is None:
        return False
    scope = session_branch_scope_ids()
    if scope is None:
        return True
    branch_id = _row_value(registration, "branch_id", None)
    resolved = _as_int(branch_id)
    if resolved is None:
        return False
    return resolved in scope


# --- the explicit approval ----------------------------------------------------------------------

def approve_registration(registration_id):
    """Approve a pending registration. Raises ``ValueError`` when a check is unmet.

    The only path that turns a self-registered client into an approved one. It refuses
    unless the client is ID-verified **and** has a recorded main vehicle, and it never
    touches the customer row itself — it only flips this registration's status and records
    who approved it and when. Returns the (now approved) registration dict.
    """
    registration = registration_by_id(registration_id)
    if registration is None:
        raise ValueError("That portal registration is no longer on file.")
    if _row_value(registration, "status", "") != STATUS_PENDING:
        raise ValueError("That registration has already been approved.")
    # Defence in depth beside the route's 403: a registration outside the session's branch
    # scope can never be approved even if a caller reaches this service directly. A non-request
    # (all-branch) context resolves to the unrestricted scope, so nothing changes for it.
    if not branch_in_session_scope(registration):
        raise ValueError("That portal registration is outside the branches you can access.")
    customer_id = _row_value(registration, "customer_id", None)

    from app.services.customers import get_customer

    customer = get_customer(customer_id)
    if customer is None:
        raise ValueError("The client record for this registration is no longer on file.")

    blockers = approval_blockers(customer)
    if blockers:
        raise ValueError(" ".join(blockers))

    db = get_db()
    db.execute(
        "UPDATE portal_registrations SET status = ?, approved_at = ?, approved_by_user_id = ? "
        "WHERE id = ? AND status = ?",
        (
            STATUS_APPROVED,
            now(),
            current_session_user_id(),
            registration["id"],
            STATUS_PENDING,
        ),
    )
    db.commit()
    return registration_by_id(registration["id"])


# --- the "cannot proceed" removal (tombstone) ---------------------------------------------------

#: Refusal sentences the delete route can show, kept here so both the route and the service
#: speak with one voice about what may and may not be removed.
ONLY_PENDING_DELETABLE_MESSAGE = (
    "Only a pending registration can be removed. This client has already been approved — "
    "use the client's own Delete only if it truly has no history."
)
OUT_OF_SCOPE_DELETE_MESSAGE = "That portal registration is outside the branches you can access."


def delete_pending_registration(registration_id):
    """Remove a still-pending registration from the verification queue — **tombstone, never erase**.

    The counter's "we cannot proceed with this client" action. Corrected rule: the removal
    **always succeeds for a pending sign-up** — there is no financial/order-history
    eligibility gate, and the delete never erases an order, invoice, payment, credit or any
    other financial row. Instead the registration is marked ``status = 'deleted'``: the
    sign-up leaves the pending queue and the client leaves the default customer list, the
    counts and the order/allocation pickers, but the customer row and every row that
    references it stay behind, so no ledger link is ever broken or orphaned. A client
    tombstoned this way is still refused by ``is_pending`` (``HIDDEN_STATUSES``), so a removed
    sign-up can never be booked either.

    Deliberately narrow, in the same spirit as ``approve_registration``:

    * **Pending only.** A registration that is already ``approved`` is refused — an approved
      client is a real customer with a real ledger and is never touched through this path. An
      already-tombstoned (``deleted``) registration is idempotent: it returns ``False``.
    * **Branch-scoped.** A registration outside the session's branch scope is refused even
      if a caller reaches this service directly.
    * **The registration id is the one the route already resolved**, never a form field, so a
      crafted payload cannot redirect the action at another client.

    Returns ``True`` when the registration was tombstoned, ``False`` when it was already
    removed. Nothing external (no notification, no refund, no financial write) is ever sent.
    """
    registration = registration_by_id(registration_id)
    if registration is None:
        raise ValueError("That portal registration is no longer on file.")
    status = _row_value(registration, "status", "")
    if status == STATUS_DELETED:
        # Already tombstoned — nothing left to remove, and no second tombstone is written.
        return False
    if status != STATUS_PENDING:
        raise ValueError(ONLY_PENDING_DELETABLE_MESSAGE)
    if not branch_in_session_scope(registration):
        raise ValueError(OUT_OF_SCOPE_DELETE_MESSAGE)

    db = get_db()
    cur = db.execute(
        "UPDATE portal_registrations SET status = ? WHERE id = ? AND status = ?",
        (STATUS_DELETED, registration["id"], STATUS_PENDING),
    )
    db.commit()
    return cur.rowcount > 0
