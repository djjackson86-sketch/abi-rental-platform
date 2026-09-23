"""The POPIA setup wizard's engine (programme phase W1 / feature P, service half).

This module owns the *state and the generated notice* — no routes, no UI. It is
the thing that finally replaces the placeholder-gated ``/privacy`` with a real,
answer-driven notice:

* :func:`prefill` — every fact the app already knows arrives filled (from
  ``company_settings`` and ``branches``); anything it does *not* know arrives
  blank, never guessed. There is deliberately no ``trading_name`` in
  ``company_settings``, so prefill returns it blank.
* :func:`save_step` / :func:`state` — each step saves to a single-row table
  (``popia_wizard_state``), so an abort or a closed tab loses nothing.
* :func:`build_notice` — renders ``templates/popia/notice.j2`` with the sections
  switched on by the nine Step-3 answers. The renderer **refuses to output any
  text containing a ``[...]`` token** — a placeholder is impossible by
  construction (decision D11, applied to generated text).
* :func:`left_out` — the "what we left out and why" list: each skipped paragraph
  and the answer that skipped it.
* :func:`publish` — refuses while the Information Officer is unregistered or a
  required question is unanswered (naming the exact field), then writes a
  date-stamped version (``YYYY-MM-DD.N``) and the ``sha256`` of the rendered
  text. The notice text lives in the database, never on disk (decision D4).

The one non-negotiable (the plan's own words): the generated notice makes the
data subject aware of **all eight** s18(1) items — (a) what is collected and its
source, (b) the responsible party's name and address, (c) the purposes,
(d) whether supply is voluntary, (e) the law requiring it, (f) any cross-border
transfer and its protection level, (g) the recipients, (h) their rights plus the
Information Regulator's contact details. Those eight live in the *fixed* part of
the template, so they are present whatever the answers are; the nine Step-3
answers only add or drop optional paragraphs. ``tests/test_programme_20260923_popia_wizard.py``
asserts all eight across a grid of answer combinations.
"""

import hashlib
import json
import re

from flask import current_app

from app.db import get_db
from app.services import branches as branches_service
from app.services import settings as settings_service
from app.services.timezone import local_now, local_now_iso

#: The three answer states. ``''`` means "not answered / not sure" and keeps the
#: paragraph out of the notice (and blocks publication for a required question).
YES = "yes"
NO = "no"

#: Step-1 facts that must be filled before the notice can be published. The
#: trading name is *not* here: a business may trade under its own name.
STEP1_REQUIRED_FIELDS = [
    "business_name",
    "registration_number",
    "vat_number",
    "address",
    "telephone",
    "contact_email",
]

#: The Information Officer details that must be filled before publication.
OFFICER_REQUIRED_FIELDS = [
    "officer_name",
    "officer_position",
    "officer_email",
    "officer_telephone",
]

#: The nine Step-3 questions, in the order the wizard asks them. ``paragraph`` is
#: the short name shown in the "what we left out" list.
QUESTIONS = [
    {"key": "cctv", "label": "Do you have CCTV at any branch?", "paragraph": "CCTV at branches"},
    {"key": "marketing", "label": "Do you send marketing to customers (SMS, email or WhatsApp)?", "paragraph": "Direct marketing"},
    {"key": "id_documents", "label": "Do you keep copies of ID documents, driver's licences or vehicle licence discs?", "paragraph": "Copies of identity documents and licences"},
    {"key": "share_info", "label": "Do you share customer information with anyone else (debt collectors, insurers, attorneys, tracing agents, assessors)?", "paragraph": "Sharing with debt collectors, insurers, attorneys and tracing agents"},
    {"key": "service_providers", "label": "Do you use an accountant, IT support or other providers who can see customer information?", "paragraph": "Accountant, IT support and other providers"},
    {"key": "card_payments", "label": "Do you take card or other electronic payments?", "paragraph": "Card and electronic payments"},
    {"key": "under_18", "label": "Do you rent to anyone under 18, or hold information about children?", "paragraph": "Information about children"},
    {"key": "vehicle_registration", "label": "Do you keep the vehicle registration of the person hiring?", "paragraph": "The hiring customer's vehicle registration"},
    {"key": "credit_checks", "label": "Do you need credit checks or reference checks?", "paragraph": "Credit and reference checks"},
]
QUESTION_KEYS = [question["key"] for question in QUESTIONS]
QUESTION_BY_KEY = {question["key"]: question for question in QUESTIONS}

#: The questions whose answer must be a real yes/no before publication. These are
#: the ones where a "not sure" would let the notice silently omit a collection or
#: sharing that is in fact happening (the CCTV blocker, copies of identity
#: documents, sharing with third parties, card payments, and the vehicle
#: registration the disc scanner already records — which the plan pre-sets to yes).
REQUIRED_QUESTIONS = ["cctv", "id_documents", "share_info", "card_payments", "vehicle_registration"]

#: The fields each wizard step saves.
_STEP_FIELDS = {
    "1": STEP1_REQUIRED_FIELDS + ["trading_name"],
    "2": OFFICER_REQUIRED_FIELDS + ["officer_registered", "officer_registration_date", "officer_registration_ref"],
    "3": QUESTION_KEYS + ["cctv_branches_json", "cctv_signage"],
}

#: Any ``[...]`` token at all — stricter than popia_pack's capitalised-word rule,
#: because this text is generated by software and a bracket has no business being
#: in it. Also ``____`` (the blank-underscore placeholder the pack uses).
_PLACEHOLDER_RE = re.compile(r"\[[^\[\]]*\]")


def _clean(value):
    """A value stripped to ``''`` — never ``None``."""
    if value is None:
        return ""
    return str(value).strip()


def _today():
    return local_now().date().isoformat()


def _sval(row, key):
    """A safe read of one column off a ``sqlite3.Row`` or dict, stripped."""
    if row is None:
        return ""
    if hasattr(row, "keys"):
        return _clean(row[key]) if key in row.keys() else ""
    if isinstance(row, dict):
        return _clean(row.get(key))
    return ""


# ---------------------------------------------------------------------------
# Reading what the app already knows
# ---------------------------------------------------------------------------


def _join_address(row):
    if row is None:
        return ""
    parts = [
        _sval(row, "address_line1"),
        _sval(row, "address_line2"),
        _sval(row, "city"),
        _sval(row, "province"),
        _sval(row, "postcode"),
        _sval(row, "country"),
    ]
    return ", ".join(part for part in parts if part)


def prefill():
    """The Step-1 facts the app already knows, plus the branch list.

    Every value comes from a real record in ``company_settings`` / ``branches``;
    a fact the app does not hold (the trading name, the Information Officer, the
    Step-3 answers) is **blank, never guessed**. This is a pure read — it writes
    nothing; :func:`save_step` is what persists a confirmed value.
    """
    settings_row = settings_service.get_company_settings()
    return {
        "business_name": _sval(settings_row, "company_name"),
        "registration_number": _sval(settings_row, "company_reg_no"),
        "vat_number": _sval(settings_row, "vat_number"),
        "trading_name": "",  # no such field exists in the app — blank, never invented
        "address": _join_address(settings_row),
        "telephone": _sval(settings_row, "phone"),
        "contact_email": _sval(settings_row, "email"),
        "branches": [
            {"id": row["id"], "name": row["name"]}
            for row in branches_service.list_branches(active_only=True)
        ],
    }


# ---------------------------------------------------------------------------
# Saving and reading back the wizard state
# ---------------------------------------------------------------------------


def _blank_state():
    state = {
        "business_name": "",
        "registration_number": "",
        "vat_number": "",
        "trading_name": "",
        "address": "",
        "telephone": "",
        "contact_email": "",
        "officer_name": "",
        "officer_position": "",
        "officer_email": "",
        "officer_telephone": "",
        "officer_registered": "",
        "officer_registration_date": "",
        "officer_registration_ref": "",
        "cctv_branches_json": "[]",
        "cctv_signage": "",
        "published": False,
    }
    for question in QUESTIONS:
        state[question["key"]] = ""
    state["vehicle_registration"] = "yes"  # the disc scanner already fills this
    return state


def _get_row():
    return get_db().execute("SELECT * FROM popia_wizard_state WHERE id = 1").fetchone()


def _ensure_row(db):
    db.execute(
        "INSERT OR IGNORE INTO popia_wizard_state (id, vehicle_registration, updated_at) "
        "VALUES (1, 'yes', ?)",
        (local_now_iso(),),
    )


def _row_to_state(row):
    if row is None:
        return _blank_state()
    state = {key: row[key] for key in row.keys()}
    state["published"] = bool(state.get("published"))
    return state


def state():
    """The full saved wizard state. Blank where nothing has been confirmed yet,
    except ``vehicle_registration`` which is pre-set to ``yes`` (the disc scanner
    already records it — the plan's item 8)."""
    return _row_to_state(_get_row())


def _normalize_step(step):
    step = str(step or "").strip().lower().lstrip("step")
    if step not in _STEP_FIELDS:
        raise ValueError(f"Unknown wizard step: {step!r}")
    return step


def save_step(step, form):
    """Persist one step's fields from a submitted form and return the new state.

    ``form`` is any ``.get()`` mapping (a Flask ``request.form`` or a plain
    dict). Values are stripped and stored verbatim; the answer fields are kept as
    ``yes`` / ``no`` / ``''`` (blank = not answered / not sure). ``cctv_branches``
    may arrive as a JSON string or a list and is stored as a JSON array.
    """
    step_key = _normalize_step(step)
    db = get_db()
    _ensure_row(db)

    values = {}
    for field in _STEP_FIELDS[step_key]:
        if field == "cctv_branches_json":
            raw = form.get("cctv_branches_json") if hasattr(form, "get") else None
            if raw is None and hasattr(form, "get"):
                raw = form.get("cctv_branches")
            values[field] = _normalise_branch_list(raw)
        else:
            raw = form.get(field) if hasattr(form, "get") else None
            values[field] = _clean(raw)

    values["updated_at"] = local_now_iso()
    assignments = ", ".join(f"{field} = :{field}" for field in values)
    db.execute(f"UPDATE popia_wizard_state SET {assignments} WHERE id = 1", values)
    db.commit()
    return state()


def _normalise_branch_list(raw):
    """A JSON array of branch names (or ``[]``) from a list or a JSON string."""
    if raw is None:
        return "[]"
    if isinstance(raw, (list, tuple)):
        items = [str(item).strip() for item in raw if str(item).strip()]
        return json.dumps(items)
    text = str(raw).strip()
    if not text:
        return "[]"
    try:
        parsed = json.loads(text)
    except ValueError:
        return "[]"
    if not isinstance(parsed, list):
        return "[]"
    return json.dumps([str(item).strip() for item in parsed if str(item).strip()])


def progress(st=None):
    """How many of the five wizard steps are done, as ``{"completed": n, "total": 5}``.

    Step 1/2/3 count once their fields are filled (step 2 counts on the officer's
    details, independent of the registration state — that state gates *publish*,
    not the step). Step 4 (check) is reachable once 1–3 are done; step 5 counts
    once a notice has been published.
    """
    if st is None:
        st = state()
    completed = 0
    if all(_clean(st.get(field)) for field in STEP1_REQUIRED_FIELDS):
        completed += 1
    if all(_clean(st.get(field)) for field in OFFICER_REQUIRED_FIELDS):
        completed += 1
    if all((st.get(question["key"]) or "").strip().lower() in (YES, NO) for question in QUESTIONS):
        completed += 1
    if completed >= 3:
        completed += 1  # step 4: the notice can be previewed
    if st.get("published"):
        completed += 1  # step 5: published
    return {"completed": completed, "total": 5}


# ---------------------------------------------------------------------------
# The generated notice
# ---------------------------------------------------------------------------


def _render_context(st):
    answers = {
        question["key"]: (_clean(st.get(question["key"])).lower() == YES)
        for question in QUESTIONS
    }
    try:
        cctv_branches = json.loads(st.get("cctv_branches_json") or "[]")
    except ValueError:
        cctv_branches = []
    if not isinstance(cctv_branches, list):
        cctv_branches = []
    cctv_branches = [str(branch).strip() for branch in cctv_branches if str(branch).strip()]

    return {
        "business_name": _clean(st.get("business_name")),
        "registration_number": _clean(st.get("registration_number")),
        "vat_number": _clean(st.get("vat_number")),
        "trading_name": _clean(st.get("trading_name")),
        "address": _clean(st.get("address")),
        "telephone": _clean(st.get("telephone")),
        "contact_email": _clean(st.get("contact_email")),
        "officer_name": _clean(st.get("officer_name")),
        "officer_position": _clean(st.get("officer_position")),
        "officer_email": _clean(st.get("officer_email")),
        "officer_telephone": _clean(st.get("officer_telephone")),
        "officer_registration_date": _clean(st.get("officer_registration_date")),
        "effective_date": _today(),
        "answers": answers,
        "cctv_branches": cctv_branches,
        "cctv_signage": _clean(st.get("cctv_signage")).lower() == YES,
    }


def _assert_placeholder_free(text):
    """Refuse any rendered text that still contains a ``[...]`` token or ``____``.

    This is the "placeholders are impossible by construction" guard (decision
    D11 applied to generated text): a bracket has no business in a notice this
    software produced, so *any* bracket is treated as an unresolved placeholder.
    """
    tokens = _PLACEHOLDER_RE.findall(text or "")
    if tokens:
        raise ValueError(
            "The generated notice contains unresolved placeholders: " + "; ".join(tokens)
        )
    if "____" in (text or ""):
        raise ValueError("The generated notice contains unresolved blank fields (____).")
    return text


def _render_notice(st):
    context = _render_context(st)
    # Autoescape off: this is a plain-text/Markdown notice, not HTML.
    env = current_app.jinja_env.overlay(autoescape=False)
    return env.get_template("popia/notice.j2").render(**context).strip() + "\n"


def build_notice(st=None):
    """Render the notice from the saved state (or an explicit state dict).

    Sections are switched on by the Step-3 answers; unanswered questions simply
    leave their paragraph out. The renderer refuses to return text containing a
    placeholder token.
    """
    if st is None:
        st = state()
    text = _render_notice(st)
    _assert_placeholder_free(text)
    return text


def left_out(st=None):
    """The "what we left out and why" list — one entry per paragraph the notice
    skipped, with the answer that skipped it (``no`` or ``''`` = not answered)."""
    if st is None:
        st = state()
    skipped = []
    for question in QUESTIONS:
        answer = _clean(st.get(question["key"])).lower()
        if answer == YES:
            continue
        skipped.append(
            {
                "key": question["key"],
                "label": question["label"],
                "paragraph": question["paragraph"],
                "answer": answer if answer in (YES, NO) else "",
            }
        )
    return skipped


# ---------------------------------------------------------------------------
# The publish gate, version and hash
# ---------------------------------------------------------------------------


def _missing_required(st):
    missing = []
    for field in STEP1_REQUIRED_FIELDS:
        if not _clean(st.get(field)):
            missing.append(field)
    for field in OFFICER_REQUIRED_FIELDS:
        if not _clean(st.get(field)):
            missing.append(field)
    if (st.get("officer_registered") or "").strip().lower() != YES:
        missing.append("officer_registered")
    elif not _clean(st.get("officer_registration_date")):
        missing.append("officer_registration_date")
    for question_key in REQUIRED_QUESTIONS:
        if (st.get(question_key) or "").strip().lower() not in (YES, NO):
            missing.append(question_key)
    return missing


def publish_errors(st=None):
    """The list of fields that still block publication, in wizard order (``[]`` = ready)."""
    if st is None:
        st = state()
    return _missing_required(st)


def _next_version(db):
    """The next ``YYYY-MM-DD.N`` for today — re-publishing bumps ``N``."""
    prefix = _today() + "."
    rows = db.execute(
        "SELECT notice_version FROM popia_notice_versions WHERE notice_version LIKE ?",
        (prefix + "%",),
    ).fetchall()
    highest = 0
    for row in rows:
        version = row["notice_version"]
        if version.startswith(prefix):
            try:
                highest = max(highest, int(version[len(prefix):]))
            except ValueError:
                continue
    return f"{prefix}{highest + 1}"


def publish(user_id=None):
    """Publish the notice, or refuse — naming the exact field that blocks it.

    Refused (``ValueError``, nothing written) while the Information Officer is
    unregistered or a required question is unanswered. On success the rendered
    text (placeholder-free, by :func:`build_notice`) is stored with a fresh
    date-stamped version and its ``sha256`` content hash. The text lives in the
    database, never on disk (decision D4); old consent rows keep their old
    version, so a customer's consent stays traceable to the exact wording they saw.
    """
    db = get_db()
    st = state()
    missing = _missing_required(st)
    if missing:
        raise ValueError(
            "The notice cannot be published yet: " + ", ".join(missing) + " must be completed first."
        )
    text = build_notice(st=st)
    version = _next_version(db)
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    db.execute(
        "INSERT INTO popia_notice_versions "
        "(notice_version, notice_content_hash, notice_text, published_at, published_by_user_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (version, content_hash, text, local_now_iso(), user_id),
    )
    db.execute(
        "UPDATE popia_wizard_state SET published = 1, updated_at = ? WHERE id = 1",
        (local_now_iso(),),
    )
    db.commit()
    return {"version": version, "content_hash": content_hash, "text": text}


def published_notice():
    """The newest published notice row (version, hash, text, when, by whom), or ``None``."""
    return get_db().execute(
        "SELECT * FROM popia_notice_versions ORDER BY published_at DESC, id DESC LIMIT 1"
    ).fetchone()
