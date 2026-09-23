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
import unicodedata

import qrcode
from qrcode.constants import ERROR_CORRECT_M

from app.db import get_db
from app.services.settings import get_company_settings

#: The public URL shape (decision D5). One place, so the route, the QR and the admin page (B3)
#: cannot disagree about it.
PORTAL_PATH_TEMPLATE = "/portal/{slug}"

#: Quiet zone, in modules. A QR with no border scans badly off a printed page, and the house
#: standard for a printed sheet is at least 4.
QR_BORDER_MODULES = 4
QR_BOX_SIZE_PX = 10


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


def all_portal_links(base_url=None):
    """Every branch with its slug, path and link — the list the admin page (B3) will render."""
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
                "enabled": bool(branch["portal_enabled"]) and bool(branch["active"]),
            }
        )
    return links
