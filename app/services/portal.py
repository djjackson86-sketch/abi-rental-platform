"""The per-branch public portal: the shareable link, the QR image, and the slug rules.

Feature B (§B1) of the ABI programme. A branch hands a customer a link on site
(WhatsApp, or a printed sheet) and the customer lands on that branch's portal page:

    https://<public_base_url>/portal/<branch-slug>

Three decisions shape this module:

* **D5 — the URL shape is ``/portal/<slug>`` and the QR encodes an ABSOLUTE url.** The base comes
  from the ``public_base_url`` setting first and the asking request's own host second, so a slug is
  stable across environments while the printed QR points at the right deployment.
* **The slug is stored, not derived.** ``ensure_slug()`` only ever fills a *blank* slug: renaming a
  branch never silently repoints a QR code that is already stuck to a counter. That is also why the
  one-off backfill lives in ``db._backfill_branch_slugs`` rather than being recomputed per request.
* **D4 — nothing is written to disk.** ``qr_png_bytes()`` renders the PNG in memory from the URL
  (pure-Python ``qrcode`` over the already-shipped Pillow), because Render's filesystem is
  ephemeral. There is no QR file on disk to lose on a redeploy, and no third-party QR service, so
  the link never leaves the app.
"""

import io
import sqlite3
import unicodedata

import qrcode
from qrcode.constants import ERROR_CORRECT_M

from app.db import get_db, now
from app.services.settings import get_company_settings

#: The public URL shape (decision D5). One place, so the route, the QR and the admin page (B3)
#: cannot disagree about it.
PORTAL_PATH_TEMPLATE = "/portal/{slug}"

#: Quiet zone, in modules. A QR with no border scans badly off a printed page, and the house
#: standard for a printed sheet is at least 4.
QR_BORDER_MODULES = 4
QR_BOX_SIZE_PX = 10

#: The print sheet (B3) asks for a heavier code than a screen preview: a 410 px PNG spread over
#: 80 mm of paper is barely 130 dpi, which is thin for a tired office printer. 12 modules at 4 px
#: of quiet zone gives 492 px for the real links, ~156 dpi on the sheet.
QR_PRINT_BOX_SIZE_PX = 12

#: The range the ``?box=`` knob is clamped to. A printed code staff rely on must never 500 because
#: of a query string, and an unbounded box size is a memory hole on a public route.
QR_BOX_SIZE_MIN = 4
QR_BOX_SIZE_MAX = 20

#: How much room a branch gets for its welcome line on the customer's form. Long enough for a
#: useful sentence, short enough that the form does not turn into a wall of text.
MAX_PORTAL_INTRO_CHARS = 300


class DuplicateSlugError(ValueError):
    """That link already belongs to another branch.

    A ``ValueError`` on purpose, so existing callers that catch ``ValueError`` keep working; the
    admin route catches this one *first* and answers **409**, because "someone else owns that URL"
    is a conflict rather than a typo. ``idx_branches_slug`` is the database-level backstop.
    """


def _ascii_fold(value):
    """Fold accents to their plain letters so ``Müller`` and ``Muller`` slug the same."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def slugify(name):
    """The slug shape: lower case, runs of anything non-alphanumeric become one ``-``.

    Returns ``''`` when nothing usable is left, and the caller decides the fallback — the
    backfill uses ``branch-<id>`` so a branch named ``!!!`` still gets a working link instead of
    an empty (and therefore un-reachable) slug.
    """
    folded = _ascii_fold(str(name or "")).lower()
    slug = "".join(char if char.isalnum() else "-" for char in folded)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")


def branch_by_slug(slug):
    """The branch a slug belongs to, or ``None``. Includes branches whose portal is switched off.

    The routes use :func:`portal_branch` instead; this function is deliberately total because the
    admin side (B3) has to show a switched-off branch's link in order to switch it back on.
    """
    candidate = slugify(slug)
    if not candidate:
        return None
    return get_db().execute("SELECT * FROM branches WHERE public_slug = ?", (candidate,)).fetchone()


def portal_branch(slug):
    """The branch a slug belongs to **and whose portal is live** — otherwise ``None``.

    One call, so the page and the QR endpoint cannot answer differently about a disabled branch:
    today both 404, which is what a customer who kept old sheet should see.
    """
    branch = branch_by_slug(slug)
    if branch is None or not branch["portal_enabled"]:
        return None
    return branch


def _slug_is_taken(candidate, exclude_branch_id=None):
    row = get_db().execute(
        "SELECT id FROM branches WHERE public_slug = ? AND id <> ?",
        (candidate, exclude_branch_id if exclude_branch_id is not None else -1),
    ).fetchone()
    return row is not None


def slug_owner(slug, exclude_branch_id=None):
    """The branch that already holds ``slug``, or ``None`` — so a refusal can name it."""
    candidate = slugify(slug)
    if not candidate:
        return None
    row = get_db().execute(
        "SELECT * FROM branches WHERE public_slug = ? AND id <> ?",
        (candidate, exclude_branch_id if exclude_branch_id is not None else -1),
    ).fetchone()
    return row


def unique_slug(base, exclude_branch_id=None):
    """``base``, or the first free ``base-2`` / ``base-3`` … — deterministic, in this order."""
    stem = slugify(base) or "branch"
    candidate, suffix = stem, 2
    while _slug_is_taken(candidate, exclude_branch_id):
        candidate = f"{stem}-{suffix}"
        suffix += 1
    return candidate


def ensure_slug(branch_id):
    """Return the branch's slug, giving it one on first use. Never renames an existing slug.

    Called when a branch is created (so its link works immediately, without waiting for the next
    app start) and by anything that needs a slug it cannot assume exists.
    """
    db = get_db()
    row = db.execute("SELECT id, name, public_slug FROM branches WHERE id = ?", (branch_id,)).fetchone()
    if row is None:
        raise ValueError(f"No branch with id {branch_id}")
    existing = (row["public_slug"] or "").strip()
    if existing:
        return existing
    slug = unique_slug(row["name"], exclude_branch_id=branch_id)
    db.execute("UPDATE branches SET public_slug = ?, updated_at = updated_at WHERE id = ?", (slug, branch_id))
    db.commit()
    return slug


def portal_path(slug):
    """The route path for a slug, e.g. ``/portal/roodepoort``."""
    return PORTAL_PATH_TEMPLATE.format(slug=slug)


def portal_url(branch, base_url=None):
    """The absolute link a customer gets: ``public_base_url`` wins, else the request's host.

    ``base_url`` is the caller's ``request.url_root`` (which is why this module never imports
    ``request``). With neither, the result is the bare path, which is still a working link on the
    page it is rendered on.
    """
    settings = get_company_settings()
    base = ""
    if settings is not None and "public_base_url" in settings.keys():
        base = (settings["public_base_url"] or "").strip()
    if not base:
        base = (base_url or "").strip()
    path = portal_path(branch["public_slug"])
    return f"{base.rstrip('/')}{path}" if base else path


def qr_png_bytes(url, box_size=QR_BOX_SIZE_PX):
    """A PNG of ``url``, rendered in process (decision D4) — no file, no external service."""
    code = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=box_size,
        border=QR_BORDER_MODULES,
    )
    code.add_data(url)
    code.make(fit=True)
    image = code.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def clamp_box_size(value, default=QR_BOX_SIZE_PX):
    """Whatever the query string carried, turned into a box size inside the safe range.

    Junk (``box=abc``, ``box=2.5``, a list from a repeated parameter) falls back to the house
    default rather than failing: the QR is a printed artefact and a 500 there is a dead sheet.
    """
    try:
        size = int(value)
    except (TypeError, ValueError):
        return default
    return max(QR_BOX_SIZE_MIN, min(QR_BOX_SIZE_MAX, size))


def branch_address(branch, separator=", "):
    """The branch's postal address as one line — what a printed sheet has to show.

    Blank parts are dropped rather than leaving a stray comma, and a branch with no address at all
    returns ``''`` so the sheet can leave the line out entirely.
    """
    parts = []
    for key in ("address_line1", "address_line2", "city", "province", "postal_code"):
        try:
            value = (branch[key] or "").strip()
        except (KeyError, IndexError, TypeError):
            continue
        if value:
            parts.append(value)
    return separator.join(parts)


def all_portal_links(base_url=None):
    """Every branch with its slug, path, link and the details the admin page (B3) renders.

    One query and one shape for the links page *and* the print sheet, so the page cannot show a
    link the sheet would print differently.
    """
    rows = get_db().execute("SELECT * FROM branches ORDER BY active DESC, name").fetchall()
    links = []
    for branch in rows:
        slug = (branch["public_slug"] or "").strip()
        links.append(
            {
                "branch_id": branch["id"],
                "name": branch["name"],
                "slug": slug,
                "portal_path": portal_path(slug) if slug else "",
                "portal_url": portal_url(branch, base_url) if slug else "",
                # ``enabled`` keeps its §B1 meaning (the portal is live *and* the branch is
                # active); the two raw flags travel beside it so the admin page can say which
                # of the two is actually off instead of showing one vague "off".
                "enabled": bool(branch["portal_enabled"]) and bool(branch["active"]),
                "portal_enabled": bool(branch["portal_enabled"]),
                "active": bool(branch["active"]),
                "address": branch_address(branch),
                "phone": (branch["phone"] or "").strip(),
                "intro": (branch["portal_intro"] or "").strip(),
                "qr_path": f"{portal_path(slug)}/qr.png" if slug else "",
            }
        )
    return links


def update_portal_settings(branch_id, form):
    """Save the portal half of a branch: slug, on/off and the welcome line. (**B3**)

    This is the write the DB↔UI gap needed: §B1 added ``public_slug`` / ``portal_enabled`` /
    ``portal_intro`` with no admin screen at all, so a slug could only be changed with a database
    client. It writes **exactly those three columns** (plus ``updated_at``) and nothing else, which
    is what lets the ordinary branch form and this one coexist without clobbering each other.

    A slug is normalised with the same :func:`slugify` the backfill uses, so what staff type and
    what the URL is cannot drift. Raises:

    * ``ValueError`` — no such branch, a slug that would leave the link empty, an over-long
      welcome line;
    * :class:`DuplicateSlugError` — the link belongs to another branch (the route answers 409);
      the partial unique index is re-checked on the write, so a save that loses a race is refused
      with the same message instead of a 500.
    """
    db = get_db()
    branch = db.execute("SELECT * FROM branches WHERE id = ?", (branch_id,)).fetchone()
    if branch is None:
        raise ValueError(f"No branch with id {branch_id}")

    typed_slug = (form.get("public_slug") or "").strip()
    slug = slugify(typed_slug)
    if not slug:
        raise ValueError(
            "A portal link needs a slug — use letters, numbers and dashes, e.g. roodepoort-west."
        )
    owner = slug_owner(slug, exclude_branch_id=branch_id)
    if owner is not None:
        raise DuplicateSlugError(
            f"/portal/{slug} is already {owner['name']}'s link — pick a different word for "
            f"{branch['name']}."
        )

    intro = (form.get("portal_intro") or "").strip()
    if len(intro) > MAX_PORTAL_INTRO_CHARS:
        raise ValueError(
            f"The welcome line is too long — {len(intro)} characters, the limit is "
            f"{MAX_PORTAL_INTRO_CHARS}."
        )
    enabled = 1 if form.get("portal_enabled") else 0

    try:
        db.execute(
            "UPDATE branches SET public_slug = ?, portal_enabled = ?, portal_intro = ?, "
            "updated_at = ? WHERE id = ?",
            (slug, enabled, intro, now(), branch_id),
        )
        db.commit()
    except sqlite3.IntegrityError as exc:  # the partial unique index winning a race
        raise DuplicateSlugError(
            f"/portal/{slug} was taken while you were saving — pick another link for "
            f"{branch['name']}."
        ) from exc

    return {
        "branch_id": branch_id,
        "name": branch["name"],
        "slug": slug,
        "typed_slug": typed_slug,
        "slug_normalised": slug != typed_slug,
        "slug_changed": slug != (branch["public_slug"] or "").strip(),
        "enabled": bool(enabled),
        "enabled_changed": bool(enabled) != bool(branch["portal_enabled"]),
        "intro_changed": intro != (branch["portal_intro"] or "").strip(),
    }
