"""The POPIA document pack as it exists on disk — the read-only reader plus the
phase 14 (Feature Q, §Q1) acceptance service.

Don's ask for Feature P was a real privacy notice a customer can read and a consent
tick they must give. The notice itself is not written by software: it is the reviewed
document in ``docs/popia/`` (Sano Trailers is the responsible party; we are the
operator). What software *does* own is this rule, decision **D11**:

    nothing unfinished is ever published or presented for acceptance.

So this module reads the pack and reports, honestly, what is still open in it:

* :func:`document_paths` — the manifest merged with what is actually on disk. A file
  that is missing is reported as missing; nothing is invented.
* :func:`document_text` — the Markdown source, verbatim.
* :func:`document_hash` — the ``sha256`` of that source (phase 14).
* :func:`outstanding_fields` — every bracketed placeholder token in a document
  (``[TO CONFIRM — …]``, ``[WEBSITE URL]``, ``[Confirm per branch …]``, ``[TODAY]``).
  The rule is **general on purpose** — any ``[...]`` containing a capitalised word —
  so a new placeholder cannot slip past by being worded differently. That is what
  gates ``/privacy`` today and what gates :func:`accept_document` in phase 14.
* :func:`is_complete` / :func:`notice_metadata` — the gate and the few facts the
  public page renders from the document rather than hard-coding.
* :func:`acceptance_for` / :func:`is_stale` / :func:`accept_document` /
  :func:`pack_status` — phase 14's adoption record, backed by the
  ``document_acceptances`` table in ``app/db.py``. :func:`accept_document` refuses
  while :func:`outstanding_fields` is non-empty, and stores the minimum: document
  identity/version/hash, who, when — no IP address, no user agent.
"""

import hashlib
import re
from pathlib import Path

from app.db import get_db
from app.services.timezone import local_now_iso

#: The pack manifest — ``docs/popia/PACK-MANIFEST.md`` is the single source of
#: truth for each document's key, human title, file name and the signature-required
#: flag, and the pack order lives there (not in templates).
MANIFEST_PATH = Path(__file__).resolve().parents[2] / "docs" / "popia" / "PACK-MANIFEST.md"

#: Values the manifest's "Signature required" column reads as True.
_SIGNATURE_TRUE = {"yes", "true", "1", "y", "required"}


def _parse_manifest():
    """Read the pack manifest into an ordered list of entries.

    Each Markdown-table row becomes ``{"key", "title", "file", "signature_required"}``.
    An unparseable manifest raises rather than silently inventing an empty pack.
    """
    entries = []
    for line in MANIFEST_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 4:
            continue
        key, title, filename, signature = cells[0], cells[1], cells[2], cells[3].strip("`").lower()
        if key.strip("`").lower() == "key" or set(signature) <= {"-", ":", " "}:
            continue  # the header row and the ``---`` separator row
        entries.append(
            {
                "key": key.strip("`"),
                "title": title,
                "file": filename.strip("`"),
                "signature_required": signature in _SIGNATURE_TRUE,
            }
        )
    if not entries:
        raise RuntimeError(f"The POPIA pack manifest parsed no documents: {MANIFEST_PATH}")
    return entries


#: The pack in manifest order — the order templates must render it.
PACK = _parse_manifest()
#: Lookup by key, used by :func:`document_paths` and friends.
MANIFEST = {entry["key"]: entry for entry in PACK}

#: The key the public privacy page and the consent records are pinned to.
PRIVACY_NOTICE_KEY = "privacy_notice"

#: Any ``[...]`` at all …
_BRACKET_RE = re.compile(r"\[[^\[\]]*\]")
#: … that contains a capitalised word (TO CONFIRM, WEBSITE URL, CCTV, TODAY, but also
#: "To be supplied" or "Confirm per branch"). The gate errs towards flagging: a false
#: positive keeps the page interim (safe), while a false negative would publish a
#: placeholder to a customer (not safe).
_CAPITALISED_RE = re.compile(r"\b[A-Z]")


def documents_dir():
    """``docs/popia/`` — the pack lives beside the code, not in the database."""
    return Path(__file__).resolve().parents[2] / "docs" / "popia"


def document_paths():
    """The manifest merged with ``docs/popia/``: ``{key: resolved Path}`` in pack order."""
    directory = documents_dir()
    return {entry["key"]: directory / entry["file"] for entry in PACK}


def missing_documents():
    """The known keys whose file is not on disk (reported, never faked)."""
    return sorted(key for key, path in document_paths().items() if not path.exists())


def document_text(key):
    """The Markdown source of one document, verbatim."""
    path = document_paths().get(key)
    if path is None:
        raise KeyError(f"Unknown POPIA document: {key!r}")
    if not path.exists():
        raise FileNotFoundError(f"POPIA document not found on disk: {path}")
    return path.read_text(encoding="utf-8")


def _outstanding_in_text(text):
    """Every bracketed placeholder token in ``text``, in document order.

    The rule is deliberately general: any ``[...]`` whose contents include a
    capitalised word. ``[TO CONFIRM — …]`` and ``[TODAY]`` are caught, and so is a
    placeholder worded as a sentence (``[Confirm per branch before publication …]``).
    A bracket with no capital in it (``[see section 5]``) is left alone.
    """
    return [token for token in _BRACKET_RE.findall(text or "") if _CAPITALISED_RE.search(token[1:-1])]


def outstanding_fields(key):
    """The placeholder tokens still open in that document (empty list = finished)."""
    return _outstanding_in_text(document_text(key))


def is_complete(key):
    """True when the document carries no open placeholder — the D11 publish gate."""
    return not outstanding_fields(key)


def _clean_fact(value):
    """A confirmed fact, or ``None`` — a value that is still a placeholder is never returned."""
    if value is None:
        return None
    value = value.strip().strip("·").strip()
    if not value or "[" in value or "]" in value or "____" in value:
        return None
    return value


def notice_metadata(key=PRIVACY_NOTICE_KEY):
    """The few facts the public page renders *from the document*.

    Every one of them is ``None`` while it is still a placeholder, so filling the
    document in is enough for the published page to pick it up — no code change, and
    no chance of a token reaching a customer. ``cctv`` is tri-state: ``True`` once the
    per-branch CCTV question is answered yes, ``False`` when the answer is no (the
    paragraph is then left out entirely), ``None`` while it is unanswered.
    """
    text = document_text(key)
    meta: dict = {
        "version": None,
        "effective_date": None,
        "last_reviewed": None,
        "registered_name": None,
        "notice_url": None,
        "cctv": None,
    }
    if key != PRIVACY_NOTICE_KEY:
        return meta

    version = re.search(r"\*\*Version:\*\*\s*([^·\n]+)", text)
    if version:
        meta["version"] = _clean_fact(version.group(1))
    effective = re.search(r"\*\*Effective date:\*\*\s*([^·\n]+)", text)
    if effective:
        meta["effective_date"] = _clean_fact(effective.group(1))
    reviewed = re.search(r"\*\*Last reviewed:\*\*\s*([^\n·]+)", text)
    if reviewed:
        meta["last_reviewed"] = _clean_fact(reviewed.group(1))
    registered = re.search(r"^\*\*Sano Trailers\*\*\s*—\s*(.+)$", text, re.MULTILINE)
    if registered:
        meta["registered_name"] = _clean_fact(registered.group(1))
    url_match = re.search(r"always available at\s*\*\*(.+?)\*\*", text, re.DOTALL)
    if url_match:
        meta["notice_url"] = _clean_fact(url_match.group(1))

    # CCTV: answered per branch, so the paragraph is either confirmed, deleted, or
    # still open. "no cctv"/"no cameras" is read as a confirmed no.
    paragraph = None
    for block in text.split("\n\n"):
        if "Closed-circuit television" in block:
            paragraph = block
            break
    if paragraph is not None:
        lowered = paragraph.lower()
        if "[" in paragraph or "____" in paragraph:
            meta["cctv"] = None
        elif "no cctv" in lowered or "no cameras" in lowered or "not covered by cctv" in lowered:
            meta["cctv"] = False
        else:
            meta["cctv"] = True
    return meta


# ---------------------------------------------------------------------------
# Phase 14 (Feature Q, §Q1): acceptance, staleness and pack status
# ---------------------------------------------------------------------------


def document_hash(key):
    """The ``sha256`` hex digest of the document's Markdown source, verbatim."""
    return hashlib.sha256(document_text(key).encode("utf-8")).hexdigest()


_VERSION_RE = re.compile(r"\*\*Version:\*\*\s*([^·\n]+)")


def document_version(key):
    """The document's own ``**Version:**`` value if it carries one, else ``''``.

    A version that is still a placeholder (brackets) is reported as ``''`` — the
    same "never publish a token" rule :func:`_clean_fact` already enforces.
    """
    match = _VERSION_RE.search(document_text(key))
    if match:
        value = _clean_fact(match.group(1))
        if value:
            return value
    return ""


def acceptance_for(key):
    """The newest acceptance row for ``key`` (or ``None``) — newest first.

    ``document_acceptances`` is an audit trail: accepting twice is allowed and the
    newest row wins, so this orders by ``accepted_at`` then ``id`` to break ties.
    """
    return get_db().execute(
        "SELECT * FROM document_acceptances WHERE document_key = ? "
        "ORDER BY accepted_at DESC, id DESC LIMIT 1",
        (key,),
    ).fetchone()


def is_stale(key):
    """True when the newest stored acceptance's hash differs from the current file.

    The document was edited after it was adopted, so the acceptance no longer
    evidences the text that is on disk now.
    """
    row = acceptance_for(key)
    if row is None:
        return False
    return (row["document_hash"] or "") != document_hash(key)


def accept_document(key, user_id, note=""):
    """Adopt one document on the record, or refuse.

    The hard gate (decision D11): while :func:`outstanding_fields` is non-empty the
    document is refused with ``ValueError`` and **nothing is written** — the app
    must never present a document containing ``[TO CONFIRM]``-style tokens as
    adoptable. Otherwise a row recording document identity/version/hash, who and
    when is written. No IP address, no user agent (data minimisation).
    """
    outstanding = outstanding_fields(key)
    if outstanding:
        raise ValueError(
            "This document cannot be adopted yet: "
            + "; ".join(outstanding)
            + " must be completed first."
        )
    db = get_db()
    cur = db.execute(
        "INSERT INTO document_acceptances "
        "(document_key, document_version, document_hash, accepted_by_user_id, accepted_at, note) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (key, document_version(key), document_hash(key), user_id, local_now_iso(), (note or "").strip()),
    )
    db.commit()
    return cur.lastrowid


def pack_status():
    """Per-document status in pack order: title, required?, accepted (by whom,
    when), stale?, and the outstanding fields that still block adoption."""
    result = []
    db = get_db()
    for entry in PACK:
        key = entry["key"]
        acceptance = acceptance_for(key)
        accepted_by = None
        accepted_by_user_id = acceptance["accepted_by_user_id"] if acceptance else None
        if accepted_by_user_id is not None:
            user = db.execute("SELECT name FROM users WHERE id = ?", (accepted_by_user_id,)).fetchone()
            accepted_by = user["name"] if user else None
        result.append(
            {
                "key": key,
                "title": entry["title"],
                "required": entry["signature_required"],
                "accepted_by": accepted_by,
                "accepted_by_user_id": accepted_by_user_id,
                "accepted_at": acceptance["accepted_at"] if acceptance else None,
                "stale": is_stale(key),
                "outstanding_fields": outstanding_fields(key),
            }
        )
    return result


def pack_notification():
    """A one-line summary for the main profile's compliance nag, or ``None``.

    A document needs attention when it has never been adopted or its latest
    acceptance is stale (the file changed after it was adopted). One acceptance
    query, then at most one hash per already-adopted document, so the bar stays
    cheap on every page render while nothing has been adopted yet.
    """
    rows = get_db().execute(
        "SELECT document_key, document_hash FROM document_acceptances "
        "ORDER BY accepted_at DESC, id DESC"
    ).fetchall()
    latest = {}
    for row in rows:
        latest.setdefault(row["document_key"], row["document_hash"])
    needs = []
    for entry in PACK:
        key = entry["key"]
        if key not in latest:
            needs.append(key)
        elif (latest[key] or "") != document_hash(key):
            needs.append(key)
    if not needs:
        return None
    return {"count": len(needs), "documents": needs}
