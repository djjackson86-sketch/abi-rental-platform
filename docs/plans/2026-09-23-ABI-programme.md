# ABI Rental Platform — 2026-09-23 Programme (3 features, local-first, 30-min cron ticks)

> **For Hermes:** this is the master programme. Each cron tick implements exactly ONE phase from the
> phase table below, then appends one entry to `docs/plans/PROGRESS.md` and stops.

**Goal:** add (A) a staff licence-disk scanner that captures car details and allocates the vehicle to a
client, (B) a per-branch public self-registration portal (shareable link + printable QR per branch) with
duplicate-safe customer creation, and (C) a proper multi-trailer public booking flow plus store
categories that carry photos. **All local first** — nothing is pushed or deployed by the programme.

**Repo:** `/mnt/d/Hermes/abi-rental-platform` (Flask + Jinja + SQLite local / Turso live, branch `master`)

**Tech stack:** Flask 3, Jinja2 server-rendered templates, vanilla JS in-page, SQLite (`instance/abi_rental.db`)
locally via `libsql-client` abstraction, pytest 8 + Playwright 1.62 (chromium already installed) for real-browser proof.

---

## Non-negotiable guardrails (every tick)

1. **Local only.** Work in `/mnt/d/Hermes/abi-rental-platform` only. **Never `git push`**, never deploy, never
   touch live Turso (`turso-access.txt` is off-limits for writes), never POST to the live site.
2. **Git.** Work on branch `feature/abi-programme-2026-09-23` (create from `master` in phase 1). Commit per
   task on that branch. `master` stays untouched for review. **Stage explicit paths — never `git add -A` or
   `git add .`**: the tree carries untracked junk (`backups/`, `.hermes/`, `=1.18.0`, `test_invoice.pdf`,
   `app/__init__.py.backup`, `"Customer comms/"`) that must stay out, and the raw 11 MB of harvested Sano
   photos in `static/img/trailer-categories/` are **source assets**: phase 8 commits only the web-sized
   re-encodes (≤1200px, ~100–200 KB each), not the full-resolution originals.
3. **venv only.** Use `.venv/bin/python`, `.venv/bin/pytest`, `.venv/bin/pip`. Port 5057 for smoke runs;
   clean up with `fuser -k 5057/tcp` when finished.
4. **Follow house conventions.** Additive schema via `SCHEMA` + `run_migrations()` `ensure_column`; no
   destructive migrations; cascade deletes / no orphans; DB↔UI parity; tests per phase in
   `tests/test_programme_20260923_*.py`; `python3 -m compileall app tests -q` clean.
5. **Real proof, never fabricated.** Run the commands, read the output, look at rendered pages with
   `vision_analyze` before claiming a UI works. If something cannot be verified, say so in the ledger.
6. **One phase per tick.** If the phase is bigger than the tick, finish the smallest *safe, committed*
   slice, write down exactly what remains, and stop. Never leave the tree broken: the full suite must be
   green (or the failure documented) at the end of every tick.
7. **Ledger.** Append to `docs/plans/PROGRESS.md`: phase, status (`done` / `partial` / `blocked`), files,
   commands run + real results, open questions. The next tick starts by reading it.

## Cron protocol

- One dispatcher job, schedule `*/30 * * * *` — **fixed wall-clock slots at :00 and :30**, so phases really
  start 30 minutes apart regardless of how long the previous phase took (the interval form `every 30m` is
  measured from the *completion* of a run, which stretched a ~25-minute phase into a ~55-minute cycle —
  observed on tick 1: fired 10:22, finished 10:47, next fire 11:17).
- `repeat=14` for a 10-phase programme: enough headroom to absorb any slot that gets skipped because a phase
  ran long, without the job nagging forever afterwards.
- `continuity=true`, `deliver=origin`. Each fire = fresh agent = fresh context window.
- **Overlap guard (mandatory first step of every tick):** the job writes `docs/plans/.tick.lock` when it starts
  and removes it when it finishes. A tick that finds an existing lock **less than 45 minutes old** must append
  one line to the ledger saying the slot was skipped and stop immediately — never two agents in one working
  tree. A lock older than 45 minutes is stale: note it, delete it, and carry on.
- When all 10 phase rows read `done`, the tick replies "Programme complete" and does no further work.

## Phase table

| # | Phase | Feature plan | Output |
|---|---|---|---|
| 1 | A1 NatIS disc parser + decode engine | staff-vehicle-scan.md §A1 | **done** — `app/services/vehicle_disk.py` + findings + 51 tests |
| 2 | A2 vehicle data model + service | staff-vehicle-scan.md §A2 | `vehicles` table, migrations, CRUD, cascade tests |
| 3 | A3 staff scan UI + allocate to client | staff-vehicle-scan.md §A3 | `/scan-vehicle` route + template + access module |
| 4 | A4 client page vehicles panel + browser proof | staff-vehicle-scan.md §A4 | customer detail panel, Playwright proof, feature A signed off |
| 5 | D1 trailer identity + return-matching service | scan-to-return.md §D1 | product plate columns, `app/services/returns.py`, tests |
| 6 | D2 scan-to-return screen + marks returned + proof | scan-to-return.md §D2 | `/scan-return` route + template + browser proof, feature D signed off |
| 7 | P1 POPIA privacy notice + consent service | popia-privacy-consent.md §P1 | `/privacy` page, shared consent block, `consent_records`, admin evidence line |
| 8 | B1 branch portal schema + link + QR | branch-public-portal.md §B1 | `branches.public_slug`, `/portal/<slug>`, QR endpoint |
| 9 | B2 public form + dedupe + "am I already a customer?" | branch-public-portal.md §B2 | public portal page, dedupe flow, safe lookup, **consent required** |
| 10 | B3 admin QR/link page with A4 print + browser proof | branch-public-portal.md §B3 | print sheet, copy link, feature B signed off |
| 11 | C1 store categories with photos + multi-trailer linking | public-booking-and-store-categories.md §C1 | group images (DB-stored), bulk assign, Sano defaults |
| 12 | C2 multi-trailer public booking flow | public-booking-and-store-categories.md §C2 | new public booking page → one order, many lines, **privacy agreement required** |
| 13 | C3 full-suite + end-to-end local proof + close-out | all five docs | green suite, e2e Playwright proof, Obsidian note |
| 14 | Q1 POPIA pack in-app: notification + Sano's acceptance + print | popia-document-pack.md §Q1 | `/popia` pack, notification bar, `document_acceptances`, printable pack + acceptance certificate |

> **Phases were renumbered three times on 2026-09-23 at Don's request.** (1) scan-to-return was added as Feature D
> and queue-jumped ahead of the portal work (phases 5–6, old 5–10 shifted to 7–12); (2) POPIA privacy notice +
> consent was added as Feature P and sits immediately before the first public capture point (phase 7, old 7–12
> shifted to 8–13); (3) Feature Q — the POPIA document pack in the app (notification + Sano's adoption + print)
> was added **last**, as phase 14, so it runs after everything it has to render is final. Tick-log entries carry the phase **name** as well as the number, and the feature docs
> reference each other by **section** (§B2, §C2) rather than by phase number, so renumbering cannot strand a
> cross-reference.

## Decisions already taken (do not re-litigate; raise in the ledger if evidence contradicts)

- **D1 — A vehicle licence disk decodes to PLAIN TEXT.** Verified in the recon notes
  (`docs/plans/reference-notes-trailerpro-bubblebounce.md` §1b): a *driver's licence card* PDF417 is
  RSA-encrypted (720-byte payload, keys in `/mnt/d/Hermes/Trailerpro/Archive/Decode_SA_Drivers_Licence/keys/`),
  but a **NaTIS vehicle licence disk barcode is not encrypted** — it decodes to plain text in one of three
  shapes: label-value pairs (`Make MITSUBISHI`), delimited fields (`|`, `;`, `%`, newline), or fixed-width
  positional (older discs). So the disk path needs **no crypto at all**: decode → text → parser.
  **The reference implementation already exists**: `/mnt/d/Claude/trailer-rental-app/src/lib/saDiscParser.ts`
  (379 lines) parses exactly this format, with `lib/saPdf417Decode.ts` (ZXing `BrowserPDF417Reader`) and
  `lib/ocrUtils.ts` (Tesseract OCR fallback) beside it. Port `saDiscParser.ts` to Python — do not invent a
  second grammar, and keep its `confidence: high|low` idea.
- **D2 — Engine: port the parser, spike the decode stage.** The *parser* is pure text handling and ports
  1:1 (the notes contain its exact field precedence: label-value > percent-delimited > token/FX-width).
  The *image → barcode* stage is the only real choice, and phase 1 decides it by measurement:
  (a) server-side `zxing-cpp` Python wheel (small; ABI already has Pillow; works for an uploaded photo and
  keeps one code path for camera + file + paste), vs (b) a vendored browser ZXing bundle as TrailerPro does.
  ABI is server-rendered Jinja with **no JS pipeline and an empty `static/js/`**, so (a) is the default
  unless the spike shows it fails on the real photo. Either way there are **two mandatory fallbacks** so
  staff are never blocked: paste/type the raw barcode text, and manual entry of the fields.
  Record the measured result and the resulting `requirements.txt` weight — it affects the Render deploy.
- **D3 — Never invent a vehicle figure, and the identifier mapping is settled.** On 2026-09-23 Don gave the
  definitive reading of the real disc payload: **`NB72XMGP` is the number plate**, **`QWR419V` is the NaTIS
  registration number** ("Natis reg is last one"), and `5120367QP4HD` is the disc's licence number (by
  elimination — it is the disc's own *Lisensienommer*). Store them in `vehicles.registration` (plate),
  `vehicles.registration_number` (NaTIS) and `vehicles.licence_number` (licence no). The disk also carries
  registering authority, control number, vehicle type, make, model, colour, VIN, engine number and expiry —
  and **no masses at all on the real modern payload**, hence `tare_kg`/`gvm_kg` are optional REAL NULL columns.
- **D3b — Towing capacity is REMOVED (Don, 2026-09-23).** It is not on the disc and the field is not wanted.
  Do **not** create `towing_capacity_kg`, do not surface a towing column anywhere, and do not carry it into the
  client page, the vehicle form or a report. If a future ticket asks for it, it comes back as a fresh decision.
- **D4 — Uploads live in the database, not on disk.** Render's filesystem is ephemeral, so a category photo
  (or any staff upload) written to `static/` disappears on redeploy. Store image bytes in a DB column and
  serve them through a route with cache headers. Phase 8 must prove an image survives an app restart.
- **D5 — Portal URL shape** `/portal/<branch-slug>`; the QR encodes the absolute URL built from a new
  `public_base_url` setting (falling back to the request host), so QR codes are stable across environments.
- **D6 — Dedupe is a decision, not a block.** Match on normalised phone + lowercased email + name
  similarity; on a hit, show the office "possible existing customer" and offer *This is me* (link the
  submission to that record, only filling blank fields, never overwriting) or *Not me* (create new). Port
  the shape from Bubblebounce (`bb-booking.html`) — the reference notes file records the exact flow.
- **D7 — Public submissions are low-trust.** Portal and booking routes are ungated (`public.` prefix) so
  they need: honeypot + simple rate limit, POPIA consent line, no secret/database detail in any response,
  and the created order stays `draft` with a public-source marker for staff to review (matching today's
  `/store/products/<id>/book` behaviour).
- **D8 — Lookup never leaks.** "Am I already a customer?" answers with match confidence + first name only
  (e.g. "We have a Charmaine M. on this number") — never full name, email, address, ID, balance or history.
- **D9 — Scan-to-return reuses the existing return flow (Don, 2026-09-23).** Staff scan a disc — either the
  **trailer's** disc or the **towing car's** disc — and the matching rental is marked returned on the admin
  side. Matching goes: trailer plate → the `started` order holding that product; else the customer vehicle's
  plate (feature A) → that customer's `started` orders; else VIN / engine number. An ambiguous scan offers the
  candidates and **never auto-picks**; a scan with no match says so and links to the started-orders list. The
  return itself is `transition_order(order_id, "return")` (`orders.py:1292`, `TRANSITIONS["return"]` at
  `orders.py:1133`, route `orders.py:676`) — **no transition logic is duplicated**, and the checklist/deposit
  work (`update_return_checklist`, `settle_return_deposit`, charges) stays exactly where it is: the scan marks
  the order Returned and hands staff to that page. Trailer plates live on `products.registration`.
- **D10 — POPIA consent is part of every public capture point (Don, 2026-09-23).** The client-facing capture
  screens — the per-branch portal form (§B2) and the public booking form (§C2) — must each show the privacy
  notice link and a **required, unticked-by-default** acceptance, worded as TrailerPro words it
  (`/mnt/d/Claude/trailer-rental-app/src/app/booking/[slug]/page.tsx`), with the submission **refused
  server-side** when it is unticked. Every acceptance is **recorded** (customer, notice version, channel,
  timestamp) — TrailerPro keeps it in component state only; ABI can evidence it. The `/privacy` page is served
  from the same wording as `docs/popia/PRIVACY-NOTICE.md` (Sano Trailers = responsible party, we = operator),
  and `PRIVACY_NOTICE_VERSION` is pinned to that document by a test so the published page and the reviewed
  document cannot drift apart. No IP address or user-agent is stored (data minimisation). See
  `popia-privacy-consent.md`.
- **D11 — The app may never adopt a document that is still unfinished (Feature Q).** The POPIA pack surfaces
  `docs/popia/*` inside the app for the main profile to read, adopt and print. `accept_document()` **refuses
  while any `[TO CONFIRM]`-style token remains** in that document, and the UI says exactly which fields are
  outstanding. The privacy notice, retention policy and action plan are **adoptions** (recorded with who +
  when + hash); the **operator agreement is a contract** — s21(1) wants it in writing, so its printed copy
  carries the signature block plus the electronic-acceptance statement (ECTA 25 of 2002) and our countersignature.
  Nothing in that phase edits the pack's wording: filling B1–B5 in `docs/popia/PRIVACY-NOTICE-REVIEW.md` is
  Sano's decision (and their attorney's sign-off).

## Open questions for Don (answers welcome any time; do not block on them)

1. Should a scanned vehicle be allowed on more than one client (co-signer / business vehicle), or strictly
   one owner? (Plan assumes one owner, with an explicit "transfer to another client" action and a warning.)
2. Portal link per branch — does each branch keep a permanent slug (e.g. `/portal/midrand`) or should links
   be revocable tokens? (Plan assumes a stable, editable slug.)
3. A trailer with no plate recorded in Inventory can only be returned by scanning the **car's** disc. Should
   staff be able to enter a trailer plate from the scan-return screen on the spot (and have it saved to the
   trailer)? (Plan assumes yes — it is one extra field on the confirm step, off until you say otherwise.)

**Resolved:** the licence-disc identifier mapping (NB72XMGP = plate, QWR419V = NaTIS reg, 5120367QP4HD =
licence number) — answered by Don 2026-09-23, now decision D3. Towing capacity — **removed**, see D3b.
