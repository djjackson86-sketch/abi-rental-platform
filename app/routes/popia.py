"""POPIA document pack (feature Q / phase 14, §Q1) — the main-profile UI half.

The service half (``app/services/popia_pack.py``) reads the pack off disk and owns
the acceptance record. This blueprint is the surface Sano sees:

* ``GET /popia`` — the pack, one card per document with its adoption status and
  the outstanding fields that still block it;
* ``GET /popia/<key>`` — one document rendered in-app with a status banner;
* ``POST /popia/<key>/accept`` — adopt on the record, refused with the reason
  while the document still carries ``[TO CONFIRM]``-style placeholders;
* ``GET /popia/pack.pdf`` / ``GET /popia/<key>.pdf`` — the printable A4 pack and
  the single-document sheet.

Everything here is **main profile only**: ``@main_required`` returns 403 for a
staff account, and the templates never render the nav entry or the notification
bar for anyone but the owner. Nothing in this half edits a document's wording,
emails anything, or marks a document adopted on Sano's behalf.
"""

import html
import re

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    session,
    url_for,
)

from app.routes.auth import login_required
from app.services import popia_pack
from app.services.access import main_required
from app.services.pdf_documents import popia_document_pdf_bytes, popia_pack_pdf_bytes
from app.services.timezone import display_local_date

bp = Blueprint("popia", __name__, url_prefix="/popia")


# ---------------------------------------------------------------------------
# Markdown -> safe HTML for the in-app document view
# ---------------------------------------------------------------------------


def _md_inline(text):
    """Escape a fragment, then apply the inline Markdown the pack actually uses.

    Every fragment is HTML-escaped *before* any Markdown is applied, so a stray
    ``<script>`` in a document can never reach the page. Links become plain
    ``target="_blank"`` anchors; images degrade to their alt text.
    """
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
    return text


_BLOCK_START_RE = re.compile(r"^(\s{0,3})(#{1,6})\s|^(\s{0,3})>\s|^\s*\|")


def render_markdown_html(text):
    """Render the pack's Markdown to safe HTML for the document view.

    Handles the constructs the pack documents actually use: ATX headings, bold /
    italic, inline code, links, block quotes, bullet and ordered lists, tables and
    paragraphs (a line ending in two spaces is a hard break).
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    blocks = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].rstrip("\n")
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        heading = re.match(r"^#{1,6}\s+(.*)$", stripped)
        if heading:
            blocks.append(("h", len(heading.group(0).split()[0]), _md_inline(heading.group(1))))
            i += 1
            continue
        if re.fullmatch(r"([-*_])\1{2,}", stripped):
            blocks.append(("hr",))
            i += 1
            continue
        if stripped.startswith(">"):
            quote = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            blocks.append(("blockquote", _md_inline(" ".join(quote))))
            continue
        if stripped.startswith("|"):
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            blocks.append(("table", rows))
            continue
        item = re.match(r"^([-*+]|\d+[.)])\s+(.*)$", stripped)
        if item:
            ordered = item.group(1)[0].isdigit()
            entries = [(item.group(1), item.group(2))]
            i += 1
            while i < n:
                more = re.match(r"^([-*+]|\d+[.)])\s+(.*)$", lines[i].strip())
                if not more:
                    break
                entries.append((more.group(1), more.group(2)))
                i += 1
            blocks.append(("list", ordered, entries))
            continue
        # Paragraph: gather until a blank line or the start of another block.
        para = [line]
        i += 1
        while i < n:
            nxt = lines[i].rstrip("\n")
            if not nxt.strip():
                break
            if re.match(r"^#{1,6}\s+", nxt.strip()) or re.fullmatch(r"([-*_])\1{2,}", nxt.strip()):
                break
            if nxt.strip().startswith(">") or nxt.strip().startswith("|"):
                break
            if re.match(r"^([-*+]|\d+[.)])\s+", nxt.strip()):
                break
            para.append(nxt)
            i += 1
        blocks.append(("para", para))

    parts = []
    for block in blocks:
        kind = block[0]
        if kind == "h":
            parts.append(f"<h{block[1]}>{block[2]}</h{block[1]}>")
        elif kind == "hr":
            parts.append("<hr>")
        elif kind == "blockquote":
            parts.append(f"<blockquote><p>{block[1]}</p></blockquote>")
        elif kind == "table":
            header = None
            body = []
            for raw in block[1]:
                cells = [c.strip() for c in raw.strip().strip("|").split("|")]
                if all(re.fullmatch(r"[-: ]+", c or "-") for c in cells):
                    continue
                if header is None:
                    header = cells
                else:
                    body.append(cells)
            rows = []
            if header is not None:
                rows.append("<tr>" + "".join(f"<th>{_md_inline(c)}</th>" for c in header) + "</tr>")
            for cells in body:
                rows.append("<tr>" + "".join(f"<td>{_md_inline(c)}</td>" for c in cells) + "</tr>")
            parts.append("<table>" + "".join(rows) + "</table>")
        elif kind == "list":
            tag = "ol" if block[1] else "ul"
            items = "".join(f"<li>{_md_inline(c)}</li>" for _m, c in block[2])
            parts.append(f"<{tag}>{items}</{tag}>")
        elif kind == "para":
            spans = []
            for raw in block[1]:
                spans.append(_md_inline(raw.rstrip()))
                spans.append("<br>" if raw.endswith("  ") else " ")
            parts.append(f"<p>{''.join(spans).strip()}</p>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------


def _status_for(entry):
    """The per-document status the cards and banner render, plus whether Accept
    is possible. Accept is possible exactly when no placeholder remains (D11)."""
    fields = entry["outstanding_fields"]
    if fields:
        count = len(fields)
        return {
            "kind": "pending",
            "label": "Pending details",
            "detail": f"{count} field{'s' if count != 1 else ''} must be completed before this can be adopted",
            "can_accept": False,
        }
    if entry["stale"]:
        return {"kind": "stale", "label": "Changed since it was adopted", "detail": None, "can_accept": True}
    if entry["accepted_at"]:
        detail = f"Adopted on {display_local_date(entry['accepted_at'])} by {entry['accepted_by'] or '—'}"
        return {"kind": "adopted", "label": "Adopted", "detail": detail, "can_accept": True}
    return {"kind": "needs", "label": "Needs adopting", "detail": None, "can_accept": True}


def _pack_views():
    """The pack in manifest order with everything the templates need."""
    statuses = {entry["key"]: entry for entry in popia_pack.pack_status()}
    views = []
    for manifest in popia_pack.PACK:
        key = manifest["key"]
        row = statuses[key]
        view = dict(row)
        view["title"] = manifest["title"]
        view["signature_required"] = manifest["signature_required"]
        view["version"] = popia_pack.document_version(key)
        view["hash_prefix"] = popia_pack.document_hash(key)[:12]
        view["status"] = _status_for(row)
        views.append(view)
    return views


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@bp.route("")
@login_required
@main_required
def index():
    return render_template("admin/popia_index.html", pack=_pack_views())


@bp.route("/<key>")
@login_required
@main_required
def document(key):
    if key not in popia_pack.MANIFEST:
        abort(404)
    views = {view["key"]: view for view in _pack_views()}
    view = views[key]
    view["text_html"] = render_markdown_html(popia_pack.document_text(key))
    return render_template("admin/popia_document.html", doc=view)


@bp.post("/<key>/accept")
@login_required
@main_required
def accept(key):
    if key not in popia_pack.MANIFEST:
        abort(404)
    entry = popia_pack.MANIFEST[key]
    user_id = None
    try:
        user_id = int(session.get("user_id") or 0) or None
    except (TypeError, ValueError):
        user_id = None
    try:
        popia_pack.accept_document(key, user_id)
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        version = popia_pack.document_version(key)
        flash(f"Adopted {entry['title']}" + (f" v{version}" if version else ""), "success")
    return redirect(url_for("popia.document", key=key))


@bp.get("/pack.pdf")
@login_required
@main_required
def pack_pdf():
    return Response(popia_pack_pdf_bytes(), mimetype="application/pdf")


@bp.get("/<key>.pdf")
@login_required
@main_required
def document_pdf(key):
    if key not in popia_pack.MANIFEST:
        abort(404)
    return Response(popia_document_pdf_bytes(key), mimetype="application/pdf")
