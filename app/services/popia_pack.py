"""The POPIA document pack as it exists on disk — read-only (feature P, §P1).

Don's ask for Feature P was a real privacy notice a customer can read and a consent
tick they must give. The notice itself is not written by software: it is the reviewed
document in ``docs/popia/`` (Sano Trailers is the responsible party; we are the
operator). What software *does* own is this rule, decision **D11**:

    nothing unfinished is ever published or presented for acceptance.

So this module reads the pack and reports, honestly, what is still open in it:

* :func:`document_paths` — the manifest merged with what is actually on disk. A file
  that is missing is reported as missing; nothing is invented.
* :func:`document_text` — the Markdown source, verbatim.
* :func:`outstanding_fields` — every bracketed placeholder token in a document
  (``[TO CONFIRM — …]``, ``[WEBSITE URL]``, ``[Confirm per branch …]``, ``[TODAY]``).
  The rule is **general on purpose** — any ``[...]`` containing a capitalised word —
  so a new placeholder cannot slip past by being worded differently. That is what
  gates ``/privacy`` today and what will gate ``accept_document()`` in phase 14.
* :func:`is_complete` / :func:`notice_metadata` — the gate and the few facts the
  public page renders from the document rather than hard-coding.

**Phase 14 (Feature Q, §Q1) extends this module** — it adds ``document_hash``,
``acceptance_for``, ``is_stale`` and ``accept_document`` (which refuses while
:func:`outstanding_fields` is non-empty) plus the ``document_acceptances`` table.
This module deliberately contains no writing and no database access, so that phase
can build on it without reworking anything here.
"""

import re
from pathlib import Path

#: The document keys the app refers to, mapped to their filenames in ``docs/popia/``.
#: Keys are stable; the wording of the documents is Sano's.
DOCUMENTS = {
    "privacy_notice": "PRIVACY-NOTICE.md",
    "retention_policy": "RETENTION-POLICY-AND-SCHEDULE.md",
    "action_plan": "POPIA-COMPLIANCE-ACTION-PLAN.md",
    "operator_agreement": "OPERATOR-AGREEMENT-ABI-SANO.md",
    "privacy_notice_review": "PRIVACY-NOTICE-REVIEW.md",
    "blockers_checklist": "BLOCKERS-CHECKLIST.md",
}

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
    """Every known document with the path it should be at (whether or not it exists)."""
    directory = documents_dir()
    return {key: directory / filename for key, filename in DOCUMENTS.items()}


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
