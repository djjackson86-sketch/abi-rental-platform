"""Edit / re-publish the *current* published POPIA notice — no wizard gate.

This is the service half of the Settings -> POPIA "edit the published notice"
feature. It deliberately does **not** import or call
``app.services.popia_wizard``: the wizard's ``publish`` is gated on the
Information Officer being registered and on the nine Step-3 answers, and it
always *regenerates* the text from those answers. A client who wants to correct
the wording of an already-published notice must not have to re-answer the whole
wizard — but they must never be able to quietly rewrite history either.

Rules this module enforces (all non-negotiable):

* **Editing only.** There is no first-publication path here. If nothing is
  published yet, :func:`publish_notice` refuses with ``ValueError``; the wizard
  remains the only publisher of a *first* notice.
* **Immutable history.** A publish is a new ``INSERT``, never an ``UPDATE``. Every
  earlier row stays byte-for-byte, so a consent recorded against an older
  ``notice_version`` still points at the exact wording the data subject saw, and
  the wizard answers are never touched.
* **Stale-write safety.** The write is a single atomic
  ``INSERT ... SELECT ... WHERE`` that inserts only while the caller's
  ``expected_version`` is still the newest version. A concurrent or stale editor
  inserts zero rows; we read the version back, roll back and raise
  :class:`NoticeConflict`.
* **Canonical text.** Newlines are normalised to LF, C0 controls other than tab
  and LF are refused, empty text is refused, and the length is capped.
* **Exact hash.** The stored hash is the ``sha256`` of the exact normalised text
  that is stored.

The ``notice_version`` column is ``UNIQUE`` and the table is **not** migrated: a
new version is a date stamp plus a UUID fragment, e.g.
``2026-10-11.4f9c2ab1e07d``.
"""

import hashlib
import re
import uuid

from app.db import get_db
from app.services.timezone import local_now, local_now_iso

#: The columns read off a notice row. Read by *index*, never ``.keys()``: the
#: production libSQL SDK row object has no ``.keys()``.
NOTICE_FIELDS = (
    "id",
    "notice_version",
    "notice_content_hash",
    "notice_text",
    "published_at",
    "published_by_user_id",
)

#: The longest notice a client may publish (characters, after normalisation).
MAX_NOTICE_CHARS = 30000

#: C0 controls other than tab (0x09) and LF (0x0A). CR (0x0D) is handled by
#: normalisation before this runs; anything else in the block is unsafe.
_UNSAFE_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_SELECT_FIELDS = ", ".join(NOTICE_FIELDS)


class NoticeConflict(RuntimeError):
    """The published notice changed under the editor (or none is published).

    Re-read the current notice and retry with the fresh ``notice_version``.
    """


def _row_value(row, field):
    """One column off a row, by name (falling back to positional index)."""
    if row is None:
        return None
    try:
        return row[field]
    except (KeyError, IndexError, TypeError):
        pass
    try:
        return row[NOTICE_FIELDS.index(field)]
    except (IndexError, ValueError, TypeError):
        return None


def _notice_dict(row):
    """A plain ``dict`` built from indexed column reads off a real DB row."""
    if row is None:
        return None
    return {field: _row_value(row, field) for field in NOTICE_FIELDS}


def _latest_row(db):
    return db.execute(
        f"SELECT {_SELECT_FIELDS} FROM popia_notice_versions "
        "ORDER BY published_at DESC, id DESC LIMIT 1"
    ).fetchone()


def current_notice():
    """The newest published notice as a plain dict, or ``None`` when none exists.

    Built from indexed column reads, so it works with a production libSQL SDK row
    (which has no ``.keys()``) exactly as it does with a ``sqlite3.Row``.
    """
    return _notice_dict(_latest_row(get_db()))


def _clean_text(text):
    """Normalise and validate editor-supplied notice text, or raise ``ValueError``."""
    if text is None:
        raise ValueError("The notice text is required.")
    if not isinstance(text, str):
        raise ValueError("The notice text must be text.")
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    if _UNSAFE_CONTROL.search(normalised):
        raise ValueError("The notice text contains an unsupported control character.")
    if not normalised.strip():
        raise ValueError("The notice text cannot be empty.")
    if len(normalised) > MAX_NOTICE_CHARS:
        raise ValueError(
            f"The notice text cannot exceed {MAX_NOTICE_CHARS} characters."
        )
    return normalised


def _new_version():
    """A fresh, unique ``YYYY-MM-DD.<uuid>`` version for today (no migration)."""
    return f"{local_now().date().isoformat()}.{uuid.uuid4().hex[:12]}"


def publish_notice(text, expected_version, user_id=None):
    """Publish a new, immutable version of the current notice.

    ``expected_version`` is the ``notice_version`` the editor was looking at; if
    it is no longer the newest version the write is refused. Returns the notice
    dict (with ``"unchanged": False``) on success, or — when the text is exactly
    the current text — the existing notice dict with ``"unchanged": True`` and no
    new row. Raises ``ValueError`` on invalid input (including "nothing published
    yet") and :class:`NoticeConflict` on a stale/concurrent edit.
    """
    if not isinstance(expected_version, str) or not expected_version.strip():
        raise ValueError("An expected_version is required to edit the notice.")
    expected_version = expected_version.strip()

    db = get_db()
    existing = current_notice()
    if existing is None:
        # No first-publication bypass: the wizard publishes the first notice.
        raise ValueError("There is no published notice to edit yet.")
    if existing["notice_version"] != expected_version:
        raise NoticeConflict(
            "The published notice changed while you were editing it; "
            "reload and try again."
        )

    normalised = _clean_text(text)
    content_hash = hashlib.sha256(normalised.encode("utf-8")).hexdigest()

    if normalised == existing["notice_text"]:
        unchanged = dict(existing)
        unchanged["unchanged"] = True
        return unchanged

    version = _new_version()
    # One atomic statement: insert only while the newest version is still the
    # version the editor expected. A stale or concurrent editor inserts nothing.
    db.execute(
        "INSERT INTO popia_notice_versions "
        "(notice_version, notice_content_hash, notice_text, published_at, "
        "published_by_user_id) "
        "SELECT ?, ?, ?, ?, ? WHERE COALESCE((SELECT notice_version FROM "
        "popia_notice_versions ORDER BY published_at DESC, id DESC LIMIT 1), '') = ?",
        (version, content_hash, normalised, local_now_iso(), user_id, expected_version),
    )
    db.commit()

    row = db.execute(
        f"SELECT {_SELECT_FIELDS} FROM popia_notice_versions "
        "WHERE notice_version = ?",
        (version,),
    ).fetchone()
    if row is None:
        # Zero rows inserted: the guard saw a different newest version. Undo and
        # let the caller reload the fresh notice.
        db.rollback()
        raise NoticeConflict(
            "The published notice changed while you were editing it; "
            "reload and try again."
        )

    notice = _notice_dict(row)
    notice["unchanged"] = False
    return notice
