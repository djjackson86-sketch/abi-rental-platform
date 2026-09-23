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

- One dispatcher job, schedule `every 30m`, `repeat=10`, `continuity=true`, `deliver=origin`.
- Each fire = fresh agent = fresh context window. It reads this file + `PROGRESS.md` + the phase's feature
  plan, implements the **next unfinished phase**, appends the ledger entry, reports to Don, and stops.
- When all 10 phases are `done`, the tick replies "Programme complete" and does no further work.

## Phase table

| # | Phase | Feature plan | Output |
|---|---|---|---|
| 1 | Licence-disk decode spike + decision | staff-vehicle-scan.md §A1 | `app/services/vehicle_disk.py` + findings doc + tests |
| 2 | Vehicle data model + service | staff-vehicle-scan.md §A2 | `vehicles` table, migrations, CRUD, cascade tests |
| 3 | Staff scan UI + allocate to client | staff-vehicle-scan.md §A3 | `/scan-vehicle` route + template + access module |
| 4 | Client page vehicles panel + towing capacity + browser proof | staff-vehicle-scan.md §A4 | customer detail panel, Playwright proof, feature A signed off |
| 5 | Branch portal schema + link + QR | branch-public-portal.md §B1 | `branches.public_slug`, `/portal/<slug>`, QR endpoint |
| 6 | Public form + dedupe + "am I already a customer?" | branch-public-portal.md §B2 | public portal page, dedupe flow, safe lookup |
| 7 | Admin QR/link page with A4 print + browser proof | branch-public-portal.md §B3 | print sheet, copy link, feature B signed off |
| 8 | Store categories with photos + multi-trailer linking | public-booking-and-store-categories.md §C1 | group images (DB-stored), bulk assign, Sano defaults |
| 9 | Multi-trailer public booking flow | public-booking-and-store-categories.md §C2 | new public booking page → one order, many lines |
| 10 | Full-suite + end-to-end local proof + close-out | all three docs | green suite, e2e Playwright proof, Obsidian note |

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
- **D3 — Never invent a vehicle figure.** The disk's real field set (per `saDiscParser.ts:10-18`, confirmed by
  the recon notes) is: registering authority, control number, **licence number (= the number plate)**,
  vehicle registration number, make, model/description, colour, VIN (17 chars), engine number, licence expiry
  date, **tare (kg)** and **GVM (kg)** — that is all. **There is no towing capacity and no GCM on a licence
  disk.** So store only those fields as `source='scan'`, and treat `towing_capacity_kg` as a staff-entered
  number with help text saying where to get it (vehicle papers / handbook / manufacturer), shown next to the
  disk's tare + GVM for reference. No guessed, derived-by-default or "typical" values, ever.
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

## Open questions for Don (answers welcome any time; do not block on them)

1. **Towing capacity — answered by evidence: it is not on the disk.** The NaTIS payload carries tare (kg) and
   GVM (kg) only. So where should staff get the towing figure — the vehicle's papers, the manufacturer's
   handbook, or your own rule of thumb? Plan: a staff-typed field shown next to the disk's tare/GVM, never
   auto-filled (D3). Confirm or correct.
2. Should a scanned vehicle be allowed on more than one client (co-signer / business vehicle), or strictly
   one owner? (Plan assumes one owner, with an explicit "transfer to another client" action and a warning.)
3. Portal link per branch — does each branch keep a permanent slug (e.g. `/portal/midrand`) or should links
   be revocable tokens? (Plan assumes a stable, editable slug.)
