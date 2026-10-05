"""Category photos for product groups (programme phase 11 / feature C §C1).

Each product group (a store category) can carry one photo used as the section header on the
public store. Decision D4 fixes where the bytes live: **in the database**, never on disk —
Render's filesystem is ephemeral, so a photo written to ``static/`` or ``instance/`` would vanish
on the next deploy. The bytes are stored on ``product_groups.image_blob`` and served back by the
public ``/store/category-image/<id>`` route.

The upload guardrail mirrors ``app/routes/vehicles.py`` (the licence-disc scan): a hard size cap,
a whitelist of image MIME types, and a Pillow ``verify()`` before any byte is stored. On top of
that, ``set_group_image`` **downscales to at most 1600px wide and re-encodes** (PNG/JPEG to JPEG,
WebP to WebP), so a full-resolution phone photo cannot bloat a row into the megabytes.

``image_source`` is how a staff photo and a shipped Sano default are told apart, which is what lets
``scripts/seed_default_group_images.py`` refuse to clobber an image a person actually chose.
"""

import io

from PIL import Image

from app.db import get_db, now

#: A phone photo is a few hundred KB; 4 MB is generous and keeps a row from ballooning.
MAX_UPLOAD_BYTES = 4 * 1024 * 1024

#: The spec (§C1) accepts png / jpeg / webp only. The Pillow ``verify()`` below is the
#: authoritative check — a renamed text file that claims ``image/png`` still fails it — but this
#: whitelist gives a clear refusal for an obvious ``application/pdf`` before any decoding is tried.
ALLOWED_MIME = frozenset({"image/png", "image/jpeg", "image/jpg", "image/webp"})

#: Longest edge a stored photo may have. ``thumbnail`` scales down only, never up.
MAX_WIDTH = 1600

#: Re-encode quality for the stored bytes (JPEG and WebP). Keeps rows small while staying crisp
#: enough for a category header.
JPEG_QUALITY = 85
WEBP_QUALITY = 85

#: ``image_source`` values.
SOURCE_UPLOAD = "upload"
SOURCE_DEFAULT_SANO = "default:sano"


class GroupImageError(ValueError):
    """A refused image save: wrong type, too large, or not a readable image.

    Subclasses ``ValueError`` so an existing caller that catches ``ValueError`` (the house pattern
    for a refused save) keeps working unchanged, while the route can still tell an image refusal
    from a name/sort refusal if it needs to.
    """


def _assert_group_exists(group_id):
    row = get_db().execute("SELECT id FROM product_groups WHERE id = ?", (group_id,)).fetchone()
    if row is None:
        raise GroupImageError("Product group not found")


def _decode(data, source_mime):
    """Verify + open one uploaded image, returning a Pillow ``Image``.

    Raises ``GroupImageError`` with a distinct sentence per failure kind, so the route can flash
    "that is not an image" apart from "that image is too large" apart from "wrong file type".
    """
    if not data:
        raise GroupImageError("That image file was empty — choose the photo again.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise GroupImageError(
            "That photo is larger than 4 MB — choose a smaller one."
        )
    mime = (source_mime or "").lower()
    if mime and mime not in ALLOWED_MIME:
        raise GroupImageError(
            f"That file is a {mime} file, not a PNG, JPEG or WebP image."
        )
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
    except GroupImageError:
        raise
    except Exception:
        raise GroupImageError("That file is not a readable image. Choose a PNG, JPEG or WebP photo.")
    image = Image.open(io.BytesIO(data))
    if image.format not in {"PNG", "JPEG", "WEBP"}:
        raise GroupImageError("That file is not a PNG, JPEG or WebP image.")
    return image


def _reencode(image, source_mime):
    """Downscale to <=1600px and re-encode, returning ``(bytes, mime)``.

    PNG and JPEG become JPEG (alpha is flattened onto white); WebP stays WebP so a transparent
    photo keeps its transparency.
    """
    image.thumbnail((MAX_WIDTH, MAX_WIDTH), Image.Resampling.LANCZOS)
    output_mime = "image/webp" if (source_mime or "").lower() == "image/webp" else "image/jpeg"
    buffer = io.BytesIO()
    if output_mime == "image/webp":
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA")
        image.save(buffer, format="WEBP", quality=WEBP_QUALITY)
    else:
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue(), output_mime


def set_group_image(group_id, file_storage):
    """Store a staff-chosen photo for a group, replacing any existing image.

    ``file_storage`` is a Werkzeug ``FileStorage`` (``request.files['...']``). Returns a dict with
    ``filename`` (display name), ``mime`` and ``size`` (byte count of the *stored* re-encode) for
    the flash message. Raises ``GroupImageError`` on a refused image; nothing is written.
    """
    _assert_group_exists(group_id)
    filename = (getattr(file_storage, "filename", "") or "").strip().split("/")[-1].split("\\")[-1]
    data = file_storage.read(MAX_UPLOAD_BYTES + 1)
    image = _decode(data, getattr(file_storage, "mimetype", ""))
    stored, mime = _reencode(image, getattr(file_storage, "mimetype", ""))
    db = get_db()
    db.execute(
        "UPDATE product_groups SET image_blob = ?, image_mime = ?, image_filename = ?, "
        "image_source = ?, updated_at = ? WHERE id = ?",
        (stored, mime, filename, SOURCE_UPLOAD, now(), group_id),
    )
    db.commit()
    return {"filename": filename, "mime": mime, "size": len(stored)}


def clear_group_image(group_id):
    """Remove a group's photo (back to the store's text fallback). Returns True if one was there."""
    _assert_group_exists(group_id)
    db = get_db()
    row = db.execute("SELECT image_blob FROM product_groups WHERE id = ?", (group_id,)).fetchone()
    had = row is not None and row["image_blob"] is not None
    db.execute(
        "UPDATE product_groups SET image_blob = NULL, image_mime = '', image_filename = '', "
        "image_source = '', updated_at = ? WHERE id = ?",
        (now(), group_id),
    )
    db.commit()
    return had


def group_image_bytes(group_id):
    """``(bytes, mime)`` for a group's photo, or ``(None, None)`` when it has none."""
    row = get_db().execute(
        "SELECT image_blob, image_mime FROM product_groups WHERE id = ?", (group_id,)
    ).fetchone()
    if row is None or row["image_blob"] is None:
        return None, None
    return row["image_blob"], (row["image_mime"] or "image/jpeg")


def group_image_info(group_id):
    """Display metadata for the group form: does it have a photo, and where did it come from."""
    row = get_db().execute(
        "SELECT image_blob, image_mime, image_filename, image_source FROM product_groups WHERE id = ?",
        (group_id,),
    ).fetchone()
    if row is None:
        return {"has_image": False, "mime": "", "filename": "", "source": ""}
    return {
        "has_image": row["image_blob"] is not None,
        "mime": row["image_mime"] or "",
        "filename": row["image_filename"] or "",
        "source": row["image_source"] or "",
    }
