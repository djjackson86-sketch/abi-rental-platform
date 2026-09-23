# Feature Q — POPIA document pack in the app: notification, Sano's acceptance, print

**Ask (Don, 2026-09-23):** "Add a job last on list — that privacy notice that we came up with between us and
Sano: create notification on app that pulls it up, and that allows Sano to accept and print."

**CLARIFIED by Don (2026-09-23, second message):** the document he means is the agreement **between us and Sano**
— the one that says who is responsible for what — **not** the customer-facing privacy notice.

**CLARIFIED FURTHER by Don (2026-09-23, third message: "You came up with the simpler agreement").** The document
is the **simple form the assistant drafted on 15 Sept** (`@session:default/20260915_080422_c7a958cc`), now
committed as **`docs/popia/CLIENT-DATA-NOTICE-JACKAPP-SANO.md`** — *Jackapp and Sano Trailers — Client Information
Notice*, 11 short plain-English sections, no schedules and no section citations. **That is the headline item of
this phase**, not the nine-page agreement: the notification is headlined on it, its status reads *awaiting
acceptance*, and the printed copy carries its signature/acceptance table (§11).

The nine-page `OPERATOR-AGREEMENT-ABI-SANO.md` stays in the pack as the **full form**, adoptable if Sano ever wants
it — see `docs/popia/OPERATOR-AGREEMENT-REVIEW.md` for its six blockers (B1–B6: whose name is on it, the clause 9.3
cap, insurance 9.4, jurisdiction 11.3, the service-agreement reference 11.4, the two Schedule C rows) and the drafted
Schedule D. **Do not block the simple notice on those blockers** — it contains no placeholders, no cap and no
indemnity, so it can be adopted as-is. Blocking it on the heavy agreement's blanks would be exactly the
over-lawyering Don has now pushed back on twice.

**Read first:** `docs/popia/PRIVACY-NOTICE-REVIEW.md` (the assistant's review of the pack against POPIA). It
lists five blockers (B1–B5) and seven content gaps (G1–G7). **This phase builds the mechanism; it must not
paper over the blockers** — see the hard gate below.

**The pack to surface** (single source of truth = these files, which already exist and are reviewed):
`docs/popia/PRIVACY-NOTICE.md` · `RETENTION-POLICY-AND-SCHEDULE.md` · `POPIA-COMPLIANCE-ACTION-PLAN.md` ·
`OPERATOR-AGREEMENT-ABI-SANO.md`.

**What "accept" means legally** (review §6): the privacy notice, retention policy and action plan are
documents Sano **adopts** — an in-app "Adopted v1.0 by <name>, <date>" record is the evidence wanted. The
**operator agreement is a contract** and s21(1) requires it in writing; an in-app acceptance is a valid
electronic signature (ECTA 25 of 2002) **provided** it records the document identity/version/hash and the person
accepting, and the printed copy carries the signature block plus a statement of when and by whom it was accepted
electronically. Our side countersigns. Offer wet-ink as an option.

---

## Q1 (phase 14) — The notification, the accept action, and the printable pack

**Objective:** the POPIA pack lives inside the app, the main profile is told when something needs adopting or
has changed, Sano can adopt it on the record, and either party can print a complete, dated pack with an
acceptance certificate.

**Files**
- Create `docs/popia/PACK-MANIFEST.md` — a small manifest listing each document's `key`, human title, file path
  and its **signature-required** flag (`privacy_notice: false`, `retention_policy: false`,
  `action_plan: false`, `operator_agreement: true`). One place to add a fifth document later; keep the pack
  order here rather than in templates.
- Create `app/services/popia_pack.py`:
  - `document_paths()` → the manifest merged with `docs/popia/` (a missing file is reported, never invented).
  - `document_text(key)` / `document_hash(key)` — the Markdown source and its `sha256`.
  - `outstanding_fields(key)` — **every bracketed placeholder token** found in that document, as a list: scan for
    any `[...]` whose contents include an uppercase word, which covers the real tokens in the pack today
    (`[TO CONFIRM — …]`, `[WEBSITE URL]`, `[Confirm per branch …]`, `[TODAY]`). Drives the gate, the interim
    page and the "pending details" banner. Implement it as the general rule, not a fixed list, so a new
    placeholder cannot slip past by being worded differently.
  - `acceptance_for(key)` → latest row or `None`; `is_stale(key)` → `True` when the stored hash differs from the
    current file's hash (the document was edited after it was adopted); `accept_document(key, user_id)` →
    **refuses (`ValueError`) while `outstanding_fields(key)` is non-empty**, otherwise writes a row.
  - `pack_status()` → per-document: title, required?, accepted (by whom, when), stale?, outstanding fields.
- Modify `app/db.py` — additive `CREATE TABLE IF NOT EXISTS document_acceptances (id, document_key TEXT NOT NULL
  DEFAULT '', document_version TEXT NOT NULL DEFAULT '', document_hash TEXT NOT NULL DEFAULT '',
  accepted_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, accepted_at TEXT NOT NULL, note TEXT NOT
  NULL DEFAULT '')` in both `SCHEMA` and `run_migrations()`, indexed on `document_key`. Accepting twice is
  allowed (it is an audit trail) — the newest row wins. **No IP address, no user agent** (data minimisation, same
  rule as `consent_records`).
- Create `app/routes/popia.py` (blueprint `popia`) — **main profile only** (`@main_required`, matching the
  unarchive/revert-draft gate; a staff account must get 403, and it must not appear in their nav):
  - `GET /popia` — the pack: one card per document with status (Adopted on <date> by <user> / **Needs
    adopting** / **Changed since it was adopted** / **Pending details — cannot be adopted yet**), an
    "Open" link, an "Accept" button (rendered only when accept is possible), and a "Print pack" button.
  - `GET /popia/<key>` — the document rendered in-app, with a status banner, the outstanding-fields list when
    it cannot be adopted, and Print/Accept actions.
  - `POST /popia/<key>/accept` — refuses with the reason when outstanding fields remain (flash the field list);
    on success flashes "Adopted <title> v<version>" and re-renders. Records user + timestamp + hash.
  - `GET /popia/pack.pdf` — print-ready pack: a cover page (Sano Trailers logo, "POPIA compliance pack",
    generated date, and an **acceptance certificate** table: document, version, hash prefix, accepted by, date),
    then each document's full text, each page footed with document name + version + page number. Reuse the
    pattern of `app/services/pdf_documents.py` for layout/headers and the existing print CSS conventions.
  - `GET /popia/<key>.pdf` — a single document + its acceptance certificate and the **operator-agreement
    signature block** when `signature_required`.
- Create `templates/admin/popia_index.html`, `templates/admin/popia_document.html`,
  `templates/admin/_popia_notification.html` and a print stylesheet block (`@page { size: A4; margin: 15mm }`).
- Modify `templates/admin/layout.html` — include `_popia_notification.html` on every admin page **for the main
  profile only**: a dismissible-at-your-peril bar ("POPIA pack: 2 documents need adopting — open") that keeps
  reappearing while anything is unadopted or stale, plus a nav entry. It must not block the dashboard (this is a
  compliance nag, not a lockdown) and must never render for staff accounts.
- Modify `app/services/access.py` and `app/routes/admin.py` only if a top-level notification needs a count
  (keep it a simple service call from the layout context processor if one exists — **do not** add a query per
  page render if a cached count is easy; measure it and say what you chose in the ledger).
- Create `tests/test_programme_20260923_popia_pack.py` — every document in the manifest resolves to a real file;
  `outstanding_fields()` finds the placeholder tokens that actually exist in the pack today (assert against a
  synthetic fixture **and** against the real files, so the test keeps working as the blockers get filled);
  **accept is refused while placeholders remain** and nothing is written; accept succeeds with a clean fixture
  document and records user + timestamp + hash; editing a document makes the acceptance **stale** and the status
  flips back to "needs adopting"; the notification renders for main and **not** for a staff account (403 on the
  routes too); the PDF returns `%PDF-` with the acceptance certificate and the signature block for the operator
  agreement; accepting twice keeps both rows.

**Browser proof (mandatory)** — Chromium against a temp SQLite DB on 5057: main profile sees the notification
bar on the dashboard, opens the pack, sees the blockers listed for the privacy notice/operator agreement (today
they genuinely contain `[TO CONFIRM]`), accepts a clean fixture document and watches the status change, opens a
**print-media** render of `/popia/pack.pdf` (or the print view) to confirm the certificate, version, page
footers and the signature block; a staff account sees **no** notification and gets 403 on `/popia`. Screenshots
at 1440px and 390px, 0 console errors, 0 overflow, every screenshot inspected with `vision_analyze`.
Finish with `fuser -k 5057/tcp`.

**Hard gate — do not soften this:** the app must **not** present a document containing `[TO CONFIRM]`-style
tokens as acceptable. Blockers B1–B5 in the review need real answers from Sano (Information Officer name and
registration, effective/review dates, legal entity + registration number, published URL, sub-processor rows and
signatures). Until those land, the UI says *"Pending details — 4 fields must be completed before this can be
adopted"* and lists them. That list is the tick's hand-off to Don; it is not a failure of the phase.

**Not in scope:** nothing in this phase edits the pack's wording (that is Don/Sano/their attorney's call), no
document is emailed to anyone, and no document may be marked adopted on Sano's behalf by us.
