# Feature P2/P3 — POPIA setup wizard, the generated notice, and the operator notice

**Ask (Don, 2026-09-23):** "create a popia page in settings to help Sano set this up — like a wizard that guides
them through the process to be POPIA compliant, gathers their info (uses default info we already have in the app),
asks if they have registered as an officer, gives them the link to do so, asks the relevant questions about CCTV
and so on (the questions you posed to me). After this is completed, the POPIA form generates and is used as the
privacy agreement — **keep this as light as legally possible** — after setting up their POPIA they can then also
download our operator notice (be slightly more informative on how we store the data)."

Two phases, because the wizard is the natural home for the "generate the notice" step and the pack
download/acceptance is a separate, smaller piece.

---

## W1 — POPIA setup wizard in Settings

### Where it lives

`/settings/popia` — **main/admin profile only**. Staff get 403. Follows the branch-option pattern already used by
`/scan-vehicle`: pick the branch **or "all branches"**, POST → redirect → one-shot flash.

### The steps

The wizard is a left-hand step rail (1–5) with a progress count, saved after every step (an abort or a closed tab
loses nothing), and a "come back later" state — nothing is compulsory until step 5.

**Step 1 — Who you are** *(pre-filled from what the app already knows)*
- registered business name · registration number · VAT number · trading name
- registered/business address · telephone · contact email
- branch list, pulled from the branches already in the app
- Pre-fill source: the existing settings/branch records. Every field is editable and **every field is confirmed on
  this screen** — no silent assumptions. Anything the app does not know is a blank input, never a guess.
- Each field carries a one-line "why we need this" note (it feeds the notice's s18(1)(b) item).

**Step 2 — Your Information Officer**
- full name · position · email · telephone
- **"Have you registered your Information Officer with the Information Regulator?"** → *Yes / Not yet*
  - *Not yet* → a clear call-to-action with the link **https://eservices.inforegulator.org.za**, what they will need
    (ID number, business details), that it is free, and a plain statement of why it matters: *s55(2) — an officer
    may only take up their duties after registration, and without a registered officer the head of the business
    carries the POPIA duties personally.* The wizard saves state and says "come back and tick this when done".
  - *Yes* → capture the registration date (and the reference if they have it).
- **Publish gate:** the generated notice cannot be published while this reads *Not yet*. It can be generated and
  previewed as a draft, and marked "registering". This is a deliberate hard stop — publishing a notice naming an
  unregistered officer is the exact finding the Regulator's 2026 enforcement notice cited.

**Step 3 — How you work** *(the questions from the notice review, asked in plain English)*
One question per row, with a "not sure" option that keeps the notice clause out until they answer:
1. Do you have CCTV at any branch? *(per branch — which ones, and is there signage?)*
2. Do you send marketing to customers (SMS / email / WhatsApp)? *(if yes: an opt-in at collection, and an opt-out in
   every message)*
3. Do you keep copies of ID documents, driver's licences or vehicle licence discs? *(if yes: the notice says why and
   how long, and the retention schedule gets a row)*
4. Do you share customer information with anyone else — debt collectors, insurers, attorneys, tracing agents,
   assessors? *(categories feed the s18(1)(g) recipients list)*
5. Do you use an accountant, IT support or other providers who can see customer information?
6. Do you take card or other electronic payments? *(names the payment provider as a category)*
7. Do you rent to anyone under 18, or hold information about children?
8. Do you keep the vehicle registration of the person hiring? *(already true — the disc scanner fills this — so this
   one is pre-set to "yes" with the reason shown)*
9. Do you need credit checks or reference checks?
- Every "yes" adds one plain-English paragraph to the generated notice; every "no" keeps it out. **The notice gets
  shorter when the answer is no** — that is the point of asking.

**Step 4 — Check it** — a live preview of the generated notice, with a "what we left out and why" list (each skipped
paragraph and the answer that skipped it). A refresh button and a "download a draft PDF".

**Step 5 — Publish** — writes the notice version (see below), makes `/privacy` live, links it from the store footer
and from the consent block on the public forms, and switches the app's POPIA state to *published*. Then shows the
"download the operator notice" card (W2).

### The generated notice — how it is built

- A **Jinja template** with the sections switched on by the answers, drafted from the **plain-English bare-bones
  structure** (`docs/popia/CLIENT-DATA-NOTICE-JACKAPP-SANO.md`), **not** the nine-page notice. Short sentences, no
  section citations, no schedules, no liability clauses. *Light, not thin.*
- **It must still make the data subject aware of all eight s18(1) items** — what is collected and the source, the
  responsible party's name and address, the purposes, whether supply is voluntary, the law requiring it, any
  cross-border transfer and its protection level, the recipients, and their rights plus the Regulator's contact
  details. *"Light" means plain English, not fewer legal items.* A test asserts all eight are present in the
  rendered output whatever the answers — that is the one non-negotiable.
- **Version + hash:** the wizard writes `notice_version` (date-stamped, e.g. `2026-09-23.1`) and
  `notice_content_hash` of the rendered text. Consent rows reference the version, so a customer's consent is always
  traceable to the exact wording they saw. Re-publishing bumps the version and re-notifies Sano for re-acceptance.
- **Placeholders are impossible by construction:** the renderer refuses to output a page containing a `[...]` token;
  an unanswered required question blocks publication and names the question. This is stricter than the repo-document
  route in P1 and it is the reason the wizard exists.
- Stored as **text in the DB, not a file** (D4 — Render's filesystem is ephemeral).
- The detailed `PRIVACY-NOTICE.md` v1.1 stays in the repo as the **optional full form** (and as the source of
  wording for the wizard's paragraphs) — it is not thrown away, just not what a customer sees by default.

### Tests / proof
- Step gating: publication blocked while the officer is unregistered or a required question is unanswered; the block
  names the exact field.
- Pre-fill: every field the app knows arrives filled; a field it does not know arrives blank (never invented).
- Generation: CCTV yes → the CCTV paragraph appears; CCTV no → it does not; the "what we left out" list matches.
- All eight s18(1) items present across a grid of answer combinations (property-style over the toggles).
- Version bump on re-publish; the old consent rows keep their old version.
- Staff profile → 403; branch-scoped access respected like `/scan-vehicle`.
- Real-browser click-through at 1440px and 390px with screenshots **looked at**.

---

## W2 — the operator notice ("how Jackapp stores your data")

### The operator notice (our document, downloadable)

A plain-English **"How Jackapp stores and protects your data"** notice — more informative than the client notice on
the technical side, and shorter than the nine-page agreement:
- what we host and where (Render app host + Turso database — **state the actual regions**; per §6 of the skill the
  database is AWS eu-west-1 Ireland and the app host is Oregon, and the notice must be truthful about both)
- how access works (per-branch roles, admin-only screens, support access only when needed)
- backups (what, how often, retention, and that deletions are re-applied if a backup is restored)
- encryption in transit at rest and credential handling
- how an incident is handled (immediate notice to Sano → Sano notifies the Regulator and customers under s22)
- deletion at the end of the service
- who to contact
Generated from the same source-of-truth as the client notice, printable at A4, and **versioned** like the rest.

### Acceptance, notification and print *(built by the main programme at its phase 14 = §Q1 — listed here so W2 integrates with it instead of duplicating it)*
- **The app notification** (Don's original ask): a persistent banner in the main profile while the POPIA setup is
  incomplete — *"POPIA setup: 3 of 5 steps done"* — plus a one-line nudge once published if the operator notice has
  not been accepted. Dismissible only by completing or accepting, never by an X.
- **Accept** — recorded in `policy_acceptances` (document key, version, content hash, who, when, channel). Accepting
  the client notice and accepting the operator notice are separate records; the wizard's step 5 doubles as the
  acceptance of the generated notice.
- **Print** — A4 print-friendly page producing: the client notice, the operator notice, or the whole pack, each with
  the acceptance record on the cover (document, version, hash, who accepted, when) so it stands as evidence.
- The hard gate from the old Q1 stands: nothing containing an unresolved placeholder can be accepted.

*(The old Q1 plan file `2026-09-23-popia-document-pack.md` remains as the reference for the pack/acceptance
mechanics and the document manifest; this phase supersedes it as the build target.)*

---

## Programme boundaries — these are hard rules, not preferences

This is a **second, separate programme** from the one the main tick job runs. It runs **after** the main programme
completes, so the two never share the working tree at the same time (see below), and it must not disturb the main
programme's bookkeeping.

**Never touch, for any reason:**
- `docs/plans/PROGRESS.md` — the **main** programme's ledger (read it, but never write it)
- `docs/plans/.tick.lock` — the main job's overlap guard
- the phase table and decisions in `docs/plans/2026-09-23-ABI-programme.md` (read it; do not renumber or edit it)
- the files of a phase the main programme has not finished yet

**Own only these:**
- this document (`2026-09-23-popia-setup-wizard.md`)
- `docs/plans/PROGRESS-popia-wizard.md` — this programme's ledger
- `docs/plans/.tick-popia.lock` — this programme's overlap guard
- the application files W1/W2 create

**Sequencing.** The job is gated on the main programme's ledger: while any row in `PROGRESS.md` still reads
`pending`, `in-progress` or `blocked`, this job does **no work at all** — it does not wake the model (the gate
script makes that a no-op, so there is no message and no cost). It starts only once every main row reads `done`.

**Why gated rather than parallel:** two ticks committing in one working tree would fight over `git`'s index and each
would sweep the other's half-finished files into its commit. One tree, one writer, in sequence.

**Reuse, don't rebuild.** By the time W1 runs, the main programme has already built:
- §P1 — `consent_records` + `app/services/consent.py` + the `/privacy` page with its placeholder gate
- §Q1 — `/popia` pack page, `document_acceptances`, the notification bar, and the A4 print/acceptance-certificate route
W1 must **extend** those (`/privacy` switches source from the repo document to the generated notice) and W2 must
**plug into** them (its download/print goes through Q1's print route). If a W phase finds those missing, it stops and
records `blocked` in its ledger — it does not rebuild them.

**Git.** Same branch as the main programme. `git add` **explicit paths only** — never `git add -A`, never
`git stash`, never commit another worker's files. The 11 MB of full-res Sano photos and anything under
`~/disc-samples/` stay out of git (POPIA).
