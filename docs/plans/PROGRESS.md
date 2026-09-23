# PROGRAMME LEDGER — ABI Rental Platform, 2026-09-23 (3 features, local-first)

**Master plan:** `docs/plans/2026-09-23-ABI-programme.md`
**Feature plans:** `docs/plans/2026-09-23-staff-vehicle-scan.md` (A) ·
`docs/plans/2026-09-23-branch-public-portal.md` (B) ·
`docs/plans/2026-09-23-public-booking-and-store-categories.md` (C)
**Reference notes (recon):** `docs/plans/reference-notes-trailerpro-bubblebounce.md` ·
`docs/plans/sano-trailer-photo-sources.md`
**Branch:** `feature/abi-programme-2026-09-23` (local only — `master` untouched, nothing pushed)

## Status

| # | Phase | Status | Tick | Notes |
|---|---|---|---|---|
| 1 | A1 licence-disk decode spike + engine decision | done | tick 1 | parser ported + parity-verified; engine = server-side `zxing-cpp`, decided on the REAL disc photo (22 ms); positional branch for the modern 148-char layout; 51 new tests, full suite 747 green |
| 2 | A2 vehicle data model + service | done | tick 2 | `vehicles` table (SCHEMA + `run_migrations`) + `app/services/vehicles.py`; one owner per registration (app ValueError **and** partial unique index), blank masses stay NULL, no towing column (D3b), customer delete clears vehicles (FK + explicit); 20 new tests, full suite **767 green** |
| 3 | A3 staff scan UI + allocate to client | done | tick 3 | `/scan-vehicle` capture + review form, allocate/transfer, JSON feed + typeahead, module `scan_vehicle`; D3 identifier mapping now placed (plate / NaTIS / disc licence no); 25 new tests, full suite **794 green**, real-browser proof 27/27 checks, 0 console errors, 0 overflow at 1440px and 390px |
| 4 | A4 client page vehicles panel + browser proof | done | tick 4 | client-page `Vehicles` panel (plate + NaTIS, make, model, year, tare, GVM, disk expiry; blank = dash, no towing column per D3b), collapsed per-vehicle edit/remove `<details>`, empty state with a scan CTA; 14 new tests (written failing-first: 11 failed with the impl stashed), full suite **808 green**, real-browser 39/39 checks, 0 console errors, 0 overflow at 1440px + 390px; feature A signed off locally |
| 5 | D1 trailer identity + return-matching service | done | tick 5 | `products.registration`/`licence_number`/`registration_number` + partial unique index (one plate = one trailer) + inventory "Trailer identification" panel behind a marker; `orders.return_scan_*` audit; new `app/services/returns.py` (`match_open_rentals`, `returnable_order`, `mark_returned_via_scan` → existing `transition_order(...,"return")`); 37 new tests (34 failed first), full suite **845 green**, browser proof **26/26**, 0 console errors, 0 overflow at 1440px + 390px |
| 6 | D2 scan-to-return screen + marks returned + proof | done | tick 6 | `/scan-return` capture + review + confirm (`app/routes/returns.py`, new blueprint), one candidate → one "Mark returned", two or more → an explicit choice is required, none → a message + a link to the started-orders list, a repeat scan says "already returned" (`returns.recently_returned()`, added this tick); module `scan_return` + nav entry; the audit line on the order page; evidence named for the value that actually matched; 19 new tests (16 failed first), full suite **864 green**, browser proof **45/45**, 0 console errors, 0 overflow at 1440px + 390px; feature D signed off locally |
| 7 | P1 POPIA privacy notice + consent service | done | tick 7 | `/privacy` is driven by `docs/popia/PRIVACY-NOTICE.md`: the short **interim page** while any `[PLACEHOLDER]` remains (it does — Sano's five facts), the full 12-section notice the moment they land, no code change (D11). `consent_records` + `app/services/consent.py` (who/version/channel/when only — no IP/UA), one shared unticked consent block for B2/C2, admin evidence line on the client page; 32 new tests, full suite **896 green**, browser proof **31/31** on 5058 + a completed-document harness on 5059 |
| 8 | B1 branch portal schema + link + QR | done | tick 8 | `branches.public_slug`/`portal_enabled`/`portal_intro` + `company_settings.public_base_url` (additive) with a deterministic slug backfill, `idx_branches_slug` partial unique index, new `app/services/portal.py` (`slugify`/`ensure_slug`/`portal_url`/`qr_png_bytes`/`all_portal_links`), `GET /portal/<slug>` + `GET /portal/<slug>/qr.png` (PNG rendered in-process, encodes the absolute link); 23 new tests (5 failed first), full suite **919 green**, browser proof **23/23**, 0 console errors, 0 overflow at 1440px + 390px, QR decoded back with zxing-cpp |
| 9 | B2 public form + dedupe + "am I already a customer?" | done | tick 9 | `app/services/portal_intake.py`; `/portal/<slug>` **is** the form, `/portal/<slug>/register` (GET+POST), `/portal/<slug>/check` (POST only); dedupe is the customer's decision (masked "Is this you?", never a silent merge), §P1 consent required server-side, honeypot + rate-limited masked lookup (D6/D7/D8/D10); 55 new tests, full suite **974 green**, browser proof **21/21 (closed) + 52/52 (open)**, 0 console errors, 0 overflow at 1440px + 390px; **on the shipped app the form stays SHUT until Sano's five facts land (D11)** |
| 10 | B3 admin QR/link page with A4 print + browser proof | done | tick 10 | `/settings/portal` (one card per branch: readonly full link, Copy link writing the real clipboard, live QR preview, A4 print sheet, slug/on-off/welcome-line save with **409** on a duplicate and **400** on a bad slug) + nav entry + `access.py` mapping + `app/services/portal.py` extensions; **closes the DB↔UI parity gap phase 8 recorded** (§B1's three columns now have a screen); 28 new tests, full suite **1002 green**, browser proof **58/58** (6 console errors, all deliberate 404/409), 0 overflow at 1440px + 390px, print-media sheet has no admin chrome; the 1440px screenshot showed the link field clipping the URL, fixed by stacking the link row (found by looking, not by testing) |
| 11 | C1 store categories with photos + multi-trailer linking | pending | | |
| 12 | C2 multi-trailer public booking flow | pending | | **§P1 privacy agreement required** |
| 13 | C3 full-suite + end-to-end local proof + close-out | pending | | |
| 14 | Q1 POPIA document pack in-app (notification + Sano's acceptance + print) | pending | | added by Don 2026-09-23; **gated on blockers B1–B5** of `docs/popia/PRIVACY-NOTICE-REVIEW.md` |

Status values: `pending` · `in-progress` · `done` · `partial` · `blocked`.

**Phase numbering — the authoritative list is the master plan's 14-row table** (checked 2026-09-23 12:52).
Rows 5–6 are Feature D (scan-to-return), row 7 is Feature P (POPIA privacy notice + consent, added by Don
2026-09-23), rows 8–13 are the branch portal then the store/booking work. If this table ever shows **12** rows
with B1 at number 7, a tick has written a stale copy — rebuild it from the master plan before doing any work.

**Change log (main session, not ticks):** 09:40 programme created · 10:05 recon folded in (real disc scanner
found, Sano photos harvested) · 11:12 Don's disc-identifier mapping locked, towing capacity removed, Feature D
(scan-to-return) added → phases renumbered to 12 · 12:40 Feature P (POPIA privacy notice + client consent,
mirroring the TrailerPro app) added → phases renumbered to 13; cadence set to one minute between ticks · 12:52 Feature Q (POPIA document pack in-app: notification for the
main profile, Sano's adoption recorded, printable pack + acceptance certificate) added as **phase 14, last**;
`docs/popia/PRIVACY-NOTICE-REVIEW.md` written (POPIA review of the pack: five blockers, seven content gaps) and
its B1–B5 blockers gate phase 14.

> Table renumbered to the master plan's 12 phases by tick 2 — the renumbering note (Feature D added as
> phases 5–6, old 5–10 → 7–12) had landed in the plan and the tick log but not in this table.

## Tick log

<!-- Each cron tick appends ONE entry below, newest last. Keep it short but factual:
     phase, status, files touched, commands run + REAL results (test counts, HTTP codes,
     screenshot paths, what vision actually showed), commit hash, and anything blocked. -->

### Setup — 2026-09-23 09:40 (main session, not a tick)
- Plans + this ledger written. Recon done by reading the real repo: `product_groups` = "categories"
  (`templates/admin/inventory/group_form.html`), `products.product_group_id` already exists, `branches` table
  exists, `public.py` holds the whole public surface (`/store`, `/store/products/<id>`, `/store/products/<id>/book`,
  `/store/booking/<id>`), `app/routes/admin.py:197` is the house scan-screen pattern, and the app has **no
  upload machinery at all** (grep for `secure_filename`/`request.files` → only `cash.py`'s `save()` route).
- Open risks recorded: (1) no real vehicle licence-disk photo yet — phase 1 must not fake one; (2) Render's
  filesystem is ephemeral → uploads must live in the DB (decision D4); (3) `_build_order_payload` is single-item
  today → the multi-trailer booking needs a deliberate extension, not a bypass.

### Recon folded in — 2026-09-23 10:05 (main session, not a tick)
- Two recon subagents finished and their findings are written into `docs/plans/`:
  - `reference-notes-trailerpro-bubblebounce.md` (651 lines) — **the NaTIS disc scanner already exists** in
    the live TrailerPro app at `/mnt/d/Claude/trailer-rental-app/src/lib/saDiscParser.ts` (379 lines,
    plain-text parser), with `saPdf417Decode.ts` (ZXing) and `ocrUtils.ts` (Tesseract) beside it. A1 is now a
    **port** of that parser, not new grammar. Bubblebounce's dedupe is fully automatic
    (`bubblebounce_6/index.html:1650-1714`) with no confirm screen → the interactive confirm is ABI's own.
    TrailerPro's booking page is single-trailer/single-unit → UX-shape reference only for C2.
  - `sano-trailer-photo-sources.md` — **15 photos harvested** into `static/img/trailer-categories/`
    (PIL-verified, MD5-deduped, full-res from the GoDaddy CDN). No usable photo for *mobile kitchen trailers*
    or *bobcat trailers*.
- **Towing capacity is NOT on the licence disk** (disk = plate, VIN, engine no, make, model, colour, expiry,
  tare, GVM only). D3 updated; open question 1 in the master plan now needs Don to say where the figure comes
  from.
- Correction to the recon report: it claimed `gh` CLI is absent **and** that `githubtoken.txt` holds a note
  rather than a token. Verified myself: `gh` genuinely is not installed, but
  `/mnt/d/Hermes/githubtoken.txt` **does** hold a real token (59 bytes, `ghp_…`) and the repo's
  `credential.helper=store` works — so pushing at deploy time is fine. Do not "fix" the token file.

### Phase 1 — A1 licence-disk decode spike + engine decision — status `done`
**Branch:** `feature/abi-programme-2026-09-23` (created from `master` this tick; nothing pushed).
**Files:** `app/services/vehicle_disk.py` (new), `tests/test_programme_20260923_vehicle_disk.py` (new),
`tests/fixtures/disc/{labelvalue,percent,pipesemi,fixedwidth,junk,natis_positional}.txt` (new) +
`expected_ts.json` (generated), `scripts/disc_parity_ts.mts` (new), `scripts/spike_disc_decode.py` (new),
`docs/plans/disc-decode-findings.md` (new), `requirements.txt` (+Pillow, +zxing-cpp; pdf417decoder NOT shipped).
`docs/plans/` itself was untracked — committed with this slice.

**A real disc photo arrived mid-tick** (Don, 10:30): `~/disc-samples/disc-2026-09-23.jpg`, 720×1280, plus a
`disc-decode-FINDINGS.md` beside it. `vision_analyze` confirms the disc face
(*MOTORVOERTUIGLISENSIE EN LISENSIESKYF*) with a dense PDF417 band. The photo stays OUT of git (real
plate/VIN/engine number); the committed `natis_positional.txt` fixture mirrors its structure with fake
identifiers at identical field lengths so the record's own length check still holds.

**Engine decision (D2) — settled by measurement, not preference.** Both engines decode the real disc on the
ORIGINAL image, first try: `zxing-cpp` 22 ms vs `pdf417decoder` 251 ms. `pdf417decoder`'s only extra wins were
two driver's-licence CARDS (720-byte binary payloads — not ABI's use case), and it costs ~115 MB installed
(numpy + opencv-python) against zxing-cpp's ~2 MB. → `DEFAULT_ENGINE_ORDER = ("zxing-cpp",
"pdf417decoder")`, `requirements.txt` ships Pillow + zxing-cpp only. pdf417decoder stays supported as an
optional dev second engine (the engine loop skips an uninstalled engine instead of failing).

**Parser.** Field-by-field port of the shipping `saDiscParser.ts`, verified against the *real* TypeScript
(not an approximation): `scripts/disc_parity_ts.mts` runs it over every fixture via tsx → `expected_ts.json`,
and the port must match on `labelvalue`, `percent`, `pipesemi`, `fixedwidth`, `junk`. Four deliberate,
tested divergences (documented in the module + findings): masses as `float|None` not strings; mass
label-regex boundary + 300-80000 kg guard (reference turns `GVM 1234567` into `12345`); the three
reference-parsed-but-never-exposed fields surfaced; plus a **positional branch** for the real modern layout,
gated on the record's own length self-check (`%0148%` == 148 chars).

**Real 148-char payload, measured:** before the positional branch `engine_number` came out as the form
identifier `MVL1CC53` (wrong) and `colour` was empty; after it `engine_number = K9K7654321`,
`colour = WHITE`, with make/model/VIN/expiry/vehicle-type/plate all correct. **No tare, no GVM and no towing
capacity in the payload at all** (D3 confirmed on real input) — both masses stay blank-by-default.

**Commands + real results:**
- `.venv/bin/pytest tests/test_programme_20260923_vehicle_disk.py -q` → **51 passed in 5.37s**
- `.venv/bin/pytest -q` (full suite) → **747 passed in 419.91s (0:06:59)** — green
- `python3 -m compileall app tests -q` → clean
- `tsx scripts/disc_parity_ts.mts` → `wrote … expected_ts.json (6 fixtures)`
- `spike_disc_decode.py --known-samples` → zxing-cpp 2/12 images, pdf417decoder 3/12 (raw output in the
  findings doc); synthetic round trip: **all 5 payloads decode byte-exactly on both engines**
- `test_real_disc_photo_decodes_and_parses_end_to_end` PASSED (real photo → barcode → fields); it skips
  cleanly where the photo is absent.

**Commit:** `d8cc156` — `feat(scan): NaTIS licence-disc parser + measured decode engine decision (A1)`
(20 files, +3391), plus this ledger update as its own commit so the hash above is real.

**Blockers:** none. **No UI work in this phase**, so no browser/screenshot proof is due yet (that is A4).

**Must-know for tick 2 (A2, vehicles table + service):**
1. `parse_disc_text()` never raises; `decode_and_parse()` raises `DiscDecodeError.kind` ∈
   {`image_unreadable`, `no_barcode`, `unparseable`} — A3's UI needs all three messages distinct.
2. Columns to expect from a scan: `tare_kg`/`gvm_kg` are `float|None` → REAL NULL, and **A2's notes still
   mention a `gcm_kg`**: there is no GCM on a disc either, so that column should not be added.
3. `unparsed_fields` is now a `{value: reason}` dict — tokens with no signal carry `"unrecognised"`, and the
   real layout's unused identifier fields carry the "unassigned disc identifier" reason. A3 shows both.
4. **Open for Don:** which of the disc's three identifier fields (`5120367QP4HD` / `NB72XMGP` / `QWR419V`) is
   the NaTIS registration number vs the licence number — needs a full disc face, or his word. Nothing guesses.
5. Also corrected `~/disc-samples/disc-decode-FINDINGS.md` on two points (the plate regex does **not** match
   the inner `GB8LY`; the mass rules do **not** fire on this payload) — details in the findings doc §7.

### Cadence fix + independent verification — 2026-09-23 11:05 (main session, not a tick)
- Tick 1 (phase 1/A1) fired **10:22**, finished **10:47** → `done`. Deliverables: `app/services/vehicle_disk.py`
  (35.9 KB), `tests/test_programme_20260923_vehicle_disk.py` (20.8 KB), a Node parity harness
  (`scripts/disc_parity_ts.mts` → `tests/fixtures/disc/expected_ts.json`), and commits `d8cc156` + `dec2d9a`.
- **Verified from the main session, not taken on trust:** re-ran
  `.venv/bin/pytest tests/test_programme_20260923_vehicle_disk.py -q` → **51 passed in 5.23s** (matches the
  tick's claim). `requirements.txt` pins `Pillow==12.3.0` + `zxing-cpp==3.1.1` and **explicitly documents why
  `pdf417decoder` is left out** (numpy + OpenCV ≈ 115 MB for no gain on a licence disc) — the right call for
  the Render image size.
- **Pacing bug in the original setup, fixed:** `every 30m` measures the next fire from the **completion** of a
  run, so a ~25-minute phase produced a ~55-minute cycle (10:22 fire → 10:47 finish → 11:17 next). The job is
  now `*/30 * * * *` — **fixed :00/:30 slots, 30 minutes apart as asked** — with `repeat=14` for headroom, and
  an overlap guard so two ticks can never share one working tree: `docs/plans/.tick.lock`, a tick that finds a
  lock <45 minutes old appends a "slot skipped" line and stops, a lock older than 45 minutes is stale and gets
  cleared. The job prompt gained a STEP 0 for this, and the master plan's cron protocol section now matches.
- Fixed the A2 plan's stray `gcm_kg` mention (flagged by tick 1): there is no GCM on a disc, so that column is
  not to be created. `tare_kg`/`gvm_kg` stay optional REAL NULL columns.
- Next fire **11:30** → phase 2 (A2, `vehicles` table + service).

### Don's decisions folded in + cadence → 1 minute — 2026-09-23 11:12 (main session, not a tick)
- **The licence-disc identifier mapping is settled by Don** (this was the open question tick 1 parked):
  `NB72XMGP` = **number plate**, `QWR419V` = **NaTIS registration number** ("Natis reg is last one"),
  `5120367QP4HD` = the disc's licence number (by elimination — the disc's own *Lisensienommer*). Written into
  decision D3 and the A2 column list, so the A3 scan screen does not have to ask staff to choose.
- **Towing capacity REMOVED** (new decision **D3b**): no `towing_capacity_kg` column, no towing field on the
  vehicle form, no towing column on the client page. `tare_kg`/`gvm_kg` stay optional REAL NULLs because the
  real modern payload carries no masses at all.
- **New Feature D — scan-to-return** → `docs/plans/2026-09-23-scan-to-return.md`. Staff scan the **trailer's**
  disc *or* the **towing car's** disc and the matching rental is marked returned on the admin side. Matching:
  trailer plate → the `started` order holding that product; else the customer vehicle's plate → that customer's
  `started` orders; else VIN/engine. Ambiguous scans offer candidates and never auto-pick. It reuses
  `transition_order(order_id, "return")` (`app/services/orders.py:1292`; `TRANSITIONS["return"]` at `:1133`;
  route `app/routes/orders.py:676`) and leaves the checklist/deposit work (`update_return_checklist`,
  `settle_return_deposit`, charges) exactly where it is.
- **Phases renumbered to 12.** D1 (trailer identity on `products` + `app/services/returns.py`) and D2
  (`/scan-return` screen + browser proof) are now **phases 5 and 6**, ahead of the portal work, because staff
  need returns in daily use; the old phases 5–10 are now 7–12. The phase table carries a renumbering note and
  every tick-log entry names its phase as well as its number.
- **Cadence changed at Don's request:** the job is now `every 1m` with `repeat=20` — each phase starts **one
  minute after the previous one finishes**, so the remaining 11 phases run back-to-back this afternoon rather
  than one per half hour. (The `*/30` fixed-slot scheme is recorded in the master plan as the alternative; the
  `.tick.lock` overlap guard stays, now just guarding against a genuinely runaway tick.)

### Phase 2 — A2 vehicle data model + service — status `done`
**Branch:** `feature/abi-programme-2026-09-23` (nothing pushed; `master` untouched).
**Files:** `app/db.py` (SCHEMA + `run_migrations`: `vehicles` table, `idx_vehicles_customer`,
`idx_vehicles_registration` partial unique), `app/services/vehicles.py` (new, 12.6 KB),
`app/services/customers.py` (`delete_customer` now clears the customer's vehicles explicitly),
`tests/test_programme_20260923_vehicles_model.py` (new, 20 tests).

**Schema is D3/D3b-exact.** `registration` = number plate, `registration_number` = NaTIS number,
`licence_number` = the disc's own licence number; `tare_kg`/`gvm_kg` REAL NULL; **no
`towing_capacity_kg` column** — a test asserts the column is absent so D3b cannot creep back.
Additive only: on an existing DB `run_migrations()` just adds an empty table (a test covers the
drop-and-re-migrate case).

**Service (`app/services/vehicles.py`).** `list_vehicles`, `get_vehicle`,
`get_vehicle_by_registration`, `create_vehicle(form, customer_id=None)`, `update_vehicle`,
`delete_vehicle`, `customer_for_vehicle_registration` (the A3 "already allocated to another client"
check), `vehicle_counts`. Deliberate behaviour, all tested:
- **One owner per registration.** A second client claiming a plate raises `ValueError` naming the
  current owner; **the database enforces it too** (partial unique index, proven by a raw INSERT
  raising `sqlite3.IntegrityError`). A blank registration may repeat, so a vehicle typed in without a
  plate is never blocked.
- **Plates are normalised on write** (`nb 72 xmgp` → `NB 72 XMGP`) and compared on a whitespace-free
  upper-case key, so case/spacing cannot create a second owner.
- **Masses: blank is NULL, never 0** (test asserts `tare_kg is None` / `gvm_kg is None`, not 0.0), and
  a non-number or negative typed mass is refused rather than coerced.
- **Partial edits never wipe the record**: `update_vehicle` writes only the keys the form posts, and an
  edit that omits `source` keeps the stored one (a scanned disc cannot silently become "manual").
- `source` ∈ {manual, scan, import}; expiry accepts ISO / `dd-mm-yyyy` / `dd/mm/yyyy` and stores ISO,
  refusing junk.

**Cascade / no orphans, both ways.** The FK declares `ON DELETE CASCADE` **and** `delete_customer`
calls `delete_vehicles_for_customer` first, because a Turso connection does not guarantee
`PRAGMA foreign_keys=ON`. Two tests: a counted zero-orphan check after `delete_customer`, and the raw
customer-delete path (SQLite FK cascade, with `PRAGMA foreign_keys` asserted = 1).

**Commands + real results:**
- `.venv/bin/pytest tests/test_programme_20260923_vehicles_model.py -q` → **20 passed in 9.36s**
  (the first run, before `_values_from_form` defaulted `source`, was 4 failed / 14 passed — the failure
  was real; the fix is the `values["source"] = existing source or SOURCE_MANUAL` branch)
- `.venv/bin/pytest -q` (full suite) → **767 passed in 458.44s (0:07:38)** — green (747 before)
- `python3 -m compileall app tests -q` → clean
- `PYTHONPATH=. .venv/bin/python /tmp/abi_a2_smoke.py` (temp DB, real service calls) →
  `created id=1 registration='NB 72 XMGP' registration_number='QWR419V' licence_number='5120367QP4HD'
  expiry='2027-03-31' tare=None gvm=None source='scan'` · `owner of 'NB72XMGP' -> Smoke Client` ·
  `counts: {'total': 1, 'scan': 1, 'manual': 0, 'import': 0, 'blank_registration': 0}` ·
  `duplicate refused: Registration NB72XMGP is already recorded for Smoke Client — transfer it
  explicitly if it is now this client's vehicle` · `deleted customer -> True` · `vehicles left: 0`

**Commit:** `2755e84` — `feat(vehicles): vehicles table + service layer, one owner per registration (A2)`
(code + tests + ledger together). The main-session doc edits found uncommitted in the tree were
committed first, separately, as `ad6588f`, and the untracked Feature D plan as `78f979f`.

**Blockers:** none. No UI work in this phase, so no screenshots were due (that is A4).
`docs/plans/.tick.lock` was created at the start of this tick (no lock existed, so nothing was stale)
and removed at the end.

**Must-know for tick 3 (A3, scan screen + allocate):**
1. **The parser's key names clash with the column names — do not copy the dict straight in.**
   `parse_disc_text()` returns the *plate* under `licence_number` (the reference TypeScript calls it
   `licenceNumber`), while `vehicles.licence_number` is the **disc's** licence number. Map explicitly:
   `registration ← parsed["licence_number"]`, `registration_number ← parsed["registration_number"]`,
   `raw_scan_text ← parsed["raw_text"]`, `licence_disk_expiry ← parsed["expiry_date"]`,
   `source = "scan"`.
2. **D3 is answered, but the positional branch does not use the answer yet.** In
   `vehicle_disk.py::parse_natis_positional` the three identifier fields
   (`POSITIONAL_IDENTIFIER_INDEXES = (5, 6, 7)`) are still dumped into `unparsed_fields` with the
   "unassigned disc identifier" reason. On the real 148-char payload the order is **field 5 = disc
   licence number (`5120367QP4HD`), field 6 = plate (`NB72XMGP`), field 7 = NaTIS registration number
   (`QWR419V` — Don's "Natis reg is last one")**; the synthetic fixture
   `tests/fixtures/disc/natis_positional.txt` has the same shape (`T9876543210X` / `ABC123GP` /
   `ZZ1234Z`). Assigning those three in the positional branch — and updating
   `test_positional_identifiers_are_flagged_rather_than_guessed`, which currently asserts they stay
   *unassigned* — is what lets the A3 review form fill all three identifiers instead of showing two
   mystery tokens. Do it as part of A3, and keep the flagged-only behaviour for any **unknown** extra
   identifier.
3. `create_vehicle` raises `ValueError` with the owner's name inside the message — surface that string
   directly in A3's warning panel, and use `customer_for_vehicle_registration()` to warn **before** the
   save too. Transfer must stay an explicit action: the service has no silent re-owner.
4. `source` values are `manual` / `scan` / `import` — A3 posts `scan`.
5. The status table is now the master plan's **12-phase** list (tick 2 fixed the numbering); A3 is
   row 3, feature D is rows 5–6.


### Phase 3 — A3 staff scan screen + allocate to a client — status `done`
**Branch:** `feature/abi-programme-2026-09-23` (nothing pushed; `master` untouched at `8ec51e8`).
`docs/plans/.tick.lock` did not exist at the start (created and removed by this tick; no stale lock).

**Files:** `app/routes/vehicles.py` (new, blueprint `vehicles`), `templates/admin/scan_vehicle.html` (new),
`tests/test_programme_20260923_vehicle_scan_flow.py` (new, 25 tests), `app/services/vehicle_disk.py`
(D3 identifiers placed), `app/services/vehicles.py` (`fields_from_disc`, `transfer_vehicle`,
duplicate-plate refusal), `app/services/customers.py` (`search_customers`), `app/services/access.py`
(module + endpoint rules), `app/__init__.py` (blueprint), `templates/admin/layout.html` (nav),
`static/css/app.css` (scan-screen styles), `tests/test_programme_20260923_vehicle_disk.py` (the A1 test
that parked the identifiers rewritten to the D3 outcome).

**Screen.** `/scan-vehicle` = capture (photo with `capture="environment"`, or pasted barcode text, or
"Type the details instead") → review form with every parsed field editable, the unplaced values and the
verbatim payload in `<details>`, a name/phone typeahead that resolves to a client id, and Save. The photo
is read in memory only (8 MB + MIME guard), never written to disk. `/scan-vehicle/save` allocates;
`/vehicles/<id>/edit` and `/vehicles/<id>/delete` act on a recorded vehicle; `/customers/<id>/vehicles`
is the client-page feed A4 will render; `/api/customers/search` is the typeahead (name + phone only).

**D3 identifiers are now placed (tick 2's must-know item).** `POSITIONAL_IDENTIFIER_ROLES` maps field 5 →
disc licence number, field 6 → number plate, field 7 → NaTIS registration number, so the modern layout
yields all three instead of two mystery tokens. Measured on the REAL disc photo (still outside the repo):

    licence_number = 'NB72XMGP' | disc_licence_number = '5120367QP4HD' | registration_number = 'QWR419V'
    make = 'MITSUBISHI' model = 'ASX' colour = 'WHITE' engine_number = 'K9K7654321'
    expiry_date = '2027-07-31' vehicle_type = 'STATION WAGON' confidence = 'high'
    unparsed_fields = {}

`fields_from_disc()` does the A1/A2 warned mapping explicitly (parser `licence_number` = plate →
`registration`; `disc_licence_number` → `licence_number`), and the three other layouts still get **no**
disc licence number rather than borrowing the plate. The safety net for an identifier with no known role
is kept and unit-tested.

**Two REAL bugs the work found and fixed (both now tested):**
1. **A same-client double scan 500'd.** `_assert_registration_is_free` only refused *another* client's
   plate, so a second save for the same client hit the partial unique index and raised
   `sqlite3.IntegrityError` out of the route (measured, first test run: `UNIQUE constraint failed:
   vehicles.registration`). It now refuses with "…already recorded for <client> — open that vehicle and
   edit it instead of adding it a second time".
2. **The transfer checkbox saved nothing.** The picker's submit handler re-derived the client id from its
   own fetch cache; on a re-rendered page that cache is empty, so it wiped the server-rendered
   `customer_id` and the POST arrived without one — the browser proof caught it ("Choose the client this
   vehicle belongs to", counts stayed `[1, 0]`). Fixed with an untracked-box guard; after the fix the
   transfer moves the row and the counts become `[0, 1]`.

**Commands + real results:**
- `.venv/bin/pytest tests/test_programme_20260923_vehicle_scan_flow.py -q` → **25 passed in 19.28s**
  (first run 5 failed / 20 passed — the Jinja `review.values` clash below, the 500 above and one wrong
  spelling in a test message; all three were real)
- `.venv/bin/pytest tests/test_programme_20260923_vehicles_model.py tests/test_programme_20260923_vehicle_disk.py tests/test_app.py -q`
  → **276 passed in 166.40s**; `.venv/bin/pytest -q` (full suite) → **794 passed in 459.49s (0:07:39)**
- `python3 -m compileall app tests -q` → clean
- Browser proof (venv Playwright Chromium, **temp DB** `/tmp/abi_a3.db`, app on **5058** —
  see the blocker note below; screenshots in `/tmp/abi_a3_shots/`, report `/tmp/abi_a3_report.json`):
  **27/27 checks passed, 0 console errors, 0 horizontal overflow** at 1440×1100 and 390×844, on all
  three pages (capture, review, no-barcode). Signed in as a *staff* account (module `scan_vehicle`
  only), pasted the modern 148-char payload, allocated to Charmaine Mokoena (flash
  "Vehicle ABC123GP allocated to Charmaine Mokoena", redirect `/customers/1`, feed
  `/customers/1/vehicles` → `count: 1`), then re-scanned it for Pieter van Wyk → refusal with the owner
  named and the transfer tickbox unticked by default → ticking it moved the vehicle (feed counts
  `[0, 1]`).
- `vision_analyze` on the screenshots (what was actually seen): 1440px capture page = heading
  "Scan a vehicle licence disk" with the sidebar entry "Scan a vehicle disk", the two-column capture
  card ("Photograph the disk" file input + pasted-text box) and both buttons, nothing clipped. 1440px
  review = green flash "Disc read — check every field before saving", then Number plate `ABC123GP`,
  NaTIS registration number `ZZ1234Z`, Disk licence number `T9876543210X`, Make `MITSUBISHI`, Model
  `ASX`, Colour `WHITE`, VIN, Engine number `2GD1234567`, Vehicle category `STATION WAGON`, expiry
  `2027-07-31`, with Tare/GVM blank (the payload carries no masses) and the collapsed "Raw barcode text
  (148 characters)". 1440px refusal = the red panel quoting "ABC123GP is already recorded for Charmaine
  Mokoena (0821234567)" plus the tickbox "Transfer ABC123GP from Charmaine Mokoena to the client
  above", unticked. 390px review = one column, every field full width and legible, warning paragraph
  wrapping cleanly, no clipping or overlap. (Two cosmetic notes from looking: the client picker's
  placeholder was clipped on the phone — shortened — and the `type="date"` box prints in the browser's
  own `MM/DD/YYYY` locale although the stored value is ISO; the header's floating module icon is the
  same shape as the existing "Scan a barcode" screen.)

**Commit:** `00b7c88` — `feat(scan): staff licence-disc scan screen with allocate + explicit transfer (A3)`
(12 files, code + tests + templates + CSS + this ledger together). Nothing pushed; `master` untouched.

**Blockers / notes for Don:**
- **Port 5057 is still held by an `app.py` from 09:16 that is not this programme's** (`pid 558440`,
  `.venv/bin/python app.py` in the repo, no `DATABASE_PATH`) — left running on purpose, so the proof ran
  on **5058** and 5058 was closed afterwards (`5058 free`; 5057 untouched). If that instance is stale,
  say so and the next tick will stop it before its own smoke run.
- `scan_vehicle` is a **new module and is deliberately NOT in the shared staff default set** (same as
  `inventory`, `reports`, `scan_barcode`): it appears on Settings → Users/access for Don to tick per
  account. One line in `DEFAULT_STAFF_MODULES` if he wants it on by default.
- The phone navigation chip row stays as it is: it is pinned by a ticket test
  (`test_ticket_341953031_phone_nav_chips`), and on a phone the scan screen is reached through
  Customers → client → "Add vehicle" (A4) or the sidebar on desktop. A "Scan" chip is a one-line change
  if Don prefers it — it does change that pinned list.
- Registering authority / control number / year stay blank on the modern payload: the parser places
  those only when the payload labels them (they are not positional fields on the disc plate we have), and
  nothing is invented.

**Must-know for tick 4 (A4, client page vehicles panel + browser proof):**
1. **Use `review.fields`, not `review.values`** — Jinja resolves `.values` to the dict *method*
   (`dict.values`), which silently rendered a blank review form for one test round. The same trap will
   bite any template that reads a dict key named `values`/`items`/`keys`.
2. The feed the panel should render is already live: `GET /customers/<id>/vehicles` → `{customer_id,
   count, vehicles:[…]}` (gated on the **customers** module, not `scan_vehicle`, so every account that may
   open a client page may read it). Vehicles are the `vehicles` rows from A2 — `tare_kg`/`gvm_kg` are
   REAL NULL, so print blank, never `0`, and there is **no towing column** (D3b).
3. `POST /vehicles/<id>/edit` and `POST /vehicles/<id>/delete` already exist and redirect back to
   `customers.detail` with a flash — the panel's edit/remove actions can post straight to them.
4. `_assert_registration_is_free` now refuses a **same-client duplicate** as well as another client's
   plate; A2's service test still passes, but any new code path that re-creates a plate for its current
   owner will now get a `ValueError` instead of a second row.
5. The A1 fixture `natis_positional.txt` is the payload to use for browser proof (all three identifiers,
   no masses); `labelvalue.txt` is the one that carries tare/GVM (1890/2800) if A4 wants to show figures.
6. Screenshot/vision expectations are already wired: run on **5058** (or free 5057 first) with
   `DATABASE_PATH=/tmp/abi_a4.db`, seed with `/tmp/abi_a3_seed.py` (it prints owner id 1 / staff id 2 /
   clients 1 and 2), and the proof script `/tmp/abi_a3_proof.py` is a working template for the A4 pass.

### Phase 4 — A4 client page vehicles panel + browser proof — status `done`
**Branch:** `feature/abi-programme-2026-09-23` (nothing pushed; `master` untouched at `8ec51e8`).
`docs/plans/.tick.lock` did not exist at the start — this tick created it and removed it; no stale lock.

**Files:** `templates/admin/customers/detail.html` (the `Vehicles` panel),
`app/routes/customers.py` (`detail` now passes `vehicles=list_vehicles(customer_id)`),
`static/css/app.css` (panel styles), `tests/test_programme_20260923_vehicle_client_page.py` (new, 14 tests),
`docs/plans/2026-09-23-staff-vehicle-scan.md` (§A4's two stale "towing capacity" mentions corrected to D3b).

**The panel.** Right-hand column of the client page, under the order history: a `.data-table` inside the
house `.table-wrap` with **number plate (+ the NaTIS number as a sub-line) · make · model · year · tare ·
GVM · disk expiry**, an "Add vehicle" link (only for accounts holding the `scan_vehicle` module) and, per
vehicle, a collapsed `<details>` carrying the full 15-field edit form plus the remove button. Empty state
("No vehicles recorded" + a "Scan a vehicle disk" CTA) when the client has none. Every control links to
`/scan-vehicle?customer_id=<id>`, which the scan screen's capture form already carries forward (hidden
`customer_id` + a "Back to <client>" header), so the board-to-scan-to-client round trip has no dead ends.

**Real bug the browser proof found — the 390px page scrolled sideways.** First 390px measurement:
`{scrollWidth: 706, clientWidth: 390}`. The 640px table's min-content propagated through the grid item
(`.customer-vehicles-card` is a child of `.customer-profile-grid`, whose automatic minimum size is its
content's min-content), so the panel rendered 688px wide inside a 354px column — which in turn made the
"Edit" `<summary>` un-clickable (Playwright: a `.detail-row` from the contact card intercepting pointer
events). Fixed with `min-width:0` on `.customer-vehicles-card` (and `.table-wrap`, `.vehicle-manage`,
`.vehicle-edit-form`); the table now scrolls inside `.table-wrap` exactly like every other table in the
app — **390/390 measured afterwards**, edit form clickable at 390px. Same trap for any future panel that
puts a `.data-table` into one of these two-column profile grids.

**The plan's "branch/scope" acceptance line does not hold, and a test now pins that.**
`list_customers`/`get_customer` carry **no branch filter** (only `customers.branch_id` on write, plus a
display-only join to `branches` for the name) — unlike `list_products`/`list_orders`, which do scope. So
"a depot account sees its own client's vehicles only" cannot be true today; what the panel does own is the
*per-client* scope and the module gate on its controls, and both are tested. Flagged for Don: if customers
are meant to be branch-scoped, that is its own programme, not an A4 detail.

**Commands + real results:**
- **Failing first (real, not asserted):** `git stash push -- app/routes/customers.py
  templates/admin/customers/detail.html static/css/app.css` then
  `.venv/bin/pytest tests/test_programme_20260923_vehicle_client_page.py -q` → **11 failed, 3 passed**;
  `git stash pop` → **14 passed in 10.80s**
- `.venv/bin/pytest -q` (full suite) → **808 passed in 472.34s (0:07:52)** — green (794 before)
- `python3 -m compileall app tests -q` → clean
- Browser proof (venv Playwright Chromium, temp DB `/tmp/abi_a4.db`, app on **5058** because 5057 is still
  held by the 09:16 non-programme `app.py`; screenshots `/tmp/abi_a4_shots/`, report
  `/tmp/abi_a4_report.json`): **39/39 checks passed, 0 console errors, 0 horizontal overflow** at
  1440×1100 and 390×844. Signed in as a *staff* account (modules `dashboard`, `customers`, `scan_vehicle`):
  client page empty state → panel CTA → `GET /scan-vehicle?customer_id=1` (200; hidden `customer_id=1`;
  "Back to Charmaine Mokoena") → pasted the modern 148-char fixture → review already had the plate and the
  client chosen → saved (flash "Vehicle ABC123GP allocated to Charmaine Mokoena", redirect `/customers/1`)
  → panel row `['ABC123GP NaTIS ZZ1234Z','MITSUBISHI','ASX','—','—','—','2027-07-31']` → edited model +
  tare from the panel ("Vehicle saved."; row `…'ASX 1.6','—','1900','—','2027-07-31'`) → removed at 390px
  (flash "Vehicle removed — the client record is untouched.", panel back to the empty state, 0 rows left,
  "Charmaine Mokoena" still on the page).
- `vision_analyze` on the panel screenshots (what was actually seen — `/tmp/abi_a4_vision/`):
  **1440px empty**: heading "Vehicles" + blue "Add vehicle" link, the dashed empty-state box with "No
  vehicles recorded", the explanatory sentence and the blue "Scan a vehicle disk" button, nothing clipped.
  **1440px with a vehicle**: 7 columns NUMBER PLATE / MAKE / MODEL / YEAR / TARE (KG) / GVM (KG) / DISK
  EXPIRY; the single row `ABC123GP` with `NaTIS ZZ1234Z` stacked below it, `MITSUBISHI`, `ASX`, dashes in
  YEAR, TARE and GVM and `2027-07-31` last — **no towing column anywhere**.
  **1440px edit open**: summary "▾ Edit or remove ABC123GP", a two-column form with all 15 fields (plate,
  NaTIS, disk licence number, make, model, colour, year, VIN, engine, category, authority, control number,
  expiry, Tare, GVM — the fields the disk did not carry are *empty inputs*, not zeros), the "stored as
  unknown, never 0" help lines, then "Save vehicle" and the red "Remove vehicle" with "Removing deletes
  only this vehicle — the client and their orders stay exactly as they are."
  **390px panel**: one column, the table's first columns (NUMBER PLATE / MAKE / MODEL) legible, "NUMBER
  PLATE" wrapping to two lines, the disclosure line "▶ Edit or remove ABC123GP" below — nothing clipped.
  **390px edit open**: all 15 fields single-column and readable, both buttons full width.
  (Cosmetic note: on a phone the year/tare/GVM/expiry columns are inside the table's own horizontal
  scroll — house behaviour for every `.data-table` today, but a card-per-vehicle mobile layout is the
  obvious next polish if Don wants it.)

**Commit:** `bc4a18d` — `feat(customers): client page Vehicles panel with in-place edit/remove (A4)`
(5 files, code + template + CSS + tests + the plan-doc correction together), with this ledger update as its
own commit so the hash above is real.

**Blockers / notes for Don:**
- Port **5057 is still held** by the 09:16 `app.py` that is not this programme's (pid 558440) — this tick
  again proved on 5058 and left 5057 untouched; 5058 is free again. Say the word and the next tick stops it.
- `scan_vehicle` is still **not** in the shared staff default set (A3's note stands): an account without it
  gets the client's vehicles **read-only** — no "Add vehicle", no edit/remove — and the vehicle routes 403
  for it (tested both ways).
- Harness note for future proofs: the sidebar nav entry and the panel link carry the *same* label ("Scan a
  vehicle disk"), so a page-level `a:has-text(...)` click silently follows the **nav** link and loses
  `?customer_id=`. Scope panel clicks to `.customer-vehicles-card` (the first proof run failed on exactly
  that, and the failure was in the harness, not the page).

**Must-know for tick 5 (D1, trailer identity + return-matching service):**
1. `vehicles.get_vehicle_by_registration()` and `customer_for_vehicle_registration()` are the hooks D1's
   "scan the towing car's disk" branch needs; both match on the whitespace-free upper-case key, so
   `NB 72 XMGP` == `NB72XMGP` == `nb72xmgp`.
2. `vehicles.list_vehicles()` orders by `created_at DESC, id DESC` — the panel renders that order as-is
   (newest first); a return screen must not assume `id ASC`.
3. **Feature A is signed off locally end-to-end** (A1–A4 all `done`): scan → allocate → client page →
   edit/remove, proven in a real browser. Nothing pushed, nothing deployed.
4. Any new panel that puts a `.data-table` into a two-column profile grid needs `min-width:0` on the grid
   item, or the page itself scrolls sideways at 390px (see the measurement above).

### Phase 5 — D1 trailer identity + the return-matching service — status `done`
**Branch:** `feature/abi-programme-2026-09-23` (nothing pushed; `master` untouched at `8ec51e8`).
`docs/plans/.tick.lock` did not exist at the start — this tick created it and removed it; no stale lock.

**Files:** `app/db.py` (SCHEMA + `run_migrations`: `products.registration` / `licence_number` /
`registration_number` with `idx_products_registration` partial unique, `orders.return_scan_at` /
`_registration` / `_source` / `_user_id`), `app/services/products.py` (`trailer_identity_from_form`,
`trailer_registration_owner`, `_assert_trailer_registration_is_free`, `_clean` + create/update SQL),
`app/services/returns.py` (new, 14.8 KB), `templates/admin/inventory/form.html` ("Trailer identification"
panel + marker + JS toggle), `tests/test_programme_20260923_returns_match.py` (new, 37 tests).

**Trailer identity (D3, exactly).** `registration` = number plate, `registration_number` = NaTIS number,
`licence_number` = the disc's own licence number — the same three columns and the same shape as `vehicles`,
because the normalisers are *imported* from `app.services.vehicles` (`normalise_registration` /
`registration_key`) rather than copied: the plate typed on the inventory form and the plate read off a disc
must compare equal. Stored normalised (`" nb 72 xmgp "` → `NB 72 XMGP`, measured in the browser proof), one
plate on one trailer enforced by the service **and** by `idx_products_registration` (a raw duplicate INSERT
raises `sqlite3.IntegrityError` — tested). A sale/service save or a legacy/API post carries no
`trailer_identity_panel` marker, so it **cannot blank** a recorded plate (tested both ways: no marker = kept,
marker + blank boxes = cleared). `duplicate_product` deliberately does not copy the identity (a copy is a
different trailer), which is also what keeps the unique index from exploding.

**`app/services/returns.py`.** `match_open_rentals(parsed, session_scope=None)` resolves in D9's documented
order — **trailer plate** (any of the product's three identifiers, evidence recorded) → **customer vehicle
plate** → **vehicle NaTIS number** → **VIN** → **engine number** — returning candidates that carry
`order_id / order_number / customer_name / status / returnable / matched_on / evidence / source / reason` and
`also_matched_on` when one order matched several ways (the strongest match wins: trailer beats car). Only
`started` orders are `returnable`; `draft`/`reserved` matches are still listed with a reason ("it must be
picked up before it can be returned"). Scope comes from `session_branch_scope_ids()` (an explicit
`session_scope` may be passed) so a branch-limited account only ever matches its own depots. An unknown or
blank disc returns an **empty list** — never a guess. `returnable_order()` is the pre-post guard (not found /
already returned, naming the scan date / not picked up / another depot). `mark_returned_via_scan()` writes
the four audit columns and calls `transition_order(order_id, "return")` — **no transition logic duplicated**;
its refusal message is surfaced verbatim and nothing is written (test: no finalized invoice →
`"Finalize the invoice before returning this order"`, status stays `started`, audit columns stay blank).

**Commands + real results:**
- **Failing first (real, not asserted):** `git stash push -- app/db.py app/services/products.py
  templates/admin/inventory/form.html` → `.venv/bin/pytest tests/test_programme_20260923_returns_match.py -q`
  → **34 failed, 3 passed in 19.20s** (e.g. `assert {'registration','licence_number','registration_number'}
  <= {…}` / `assert {'return_scan_user_id','return_scan_source','return_scan_at'} <= {…}`); `git stash pop`
- `.venv/bin/pytest tests/test_programme_20260923_returns_match.py -q` → **37 passed in 16.40s**
  (the first green attempt was 4 failed / 33 passed — see the three real findings below)
- `.venv/bin/pytest -q` (full suite) → **845 passed in 497.91s (0:08:17)** — green (808 before)
- `.venv/bin/pytest tests/test_programme_20260923_returns_match.py
  tests/test_ticket_341953038_maintenance.py tests/test_product_duplicate.py
  tests/test_ticket_341953033_wheel_size_spare_count.py -q` → **96 passed in 52.64s** (re-run after the
  placeholder wording change below, so the shipped template state is covered)
- `python3 -m compileall app tests -q` → clean
- Browser proof (venv Playwright Chromium, temp DB `/tmp/abi_d1.db`, app on **5058** because 5057 is still
  held by the 09:16 non-programme `app.py`; screenshots `/tmp/abi_d1_shots/`, report `/tmp/abi_d1_report.json`):
  **26/26 checks passed, 0 console errors, 0 horizontal overflow** at 1440×1100 and 390×844 — owner sign-in →
  `/inventory/new` (panel visible, exactly the three fields, marker enabled, nothing pre-filled) → saved
  `"  nb 72 xmgp "` + `shs812w` → landed on `/inventory/<id>/edit` with **`NB 72 XMGP`** / `QWR419V` →
  duplicate plate POST answered **200 with a flash** `"Trailer NB72XMGP is already recorded on Proof Trailer
  — open that trailer and edit it instead of adding the same plate a second time"` (no 500) → sale item:
  panel `hidden`, all four inputs `disabled`, no plate → phone widths clean on both pages.
- `vision_analyze` on the screenshots (what was actually seen — `/tmp/abi_d1_shots/`): 1440px **new rental**
  = "New product" with Product type / General information / Wheel size / Rental availability /
  **Trailer identification** (Number plate, Disk licence number, NaTIS registration number + the help text
  naming the disc match) / Tracking method / Pricing / Visibility, nothing clipped; 1440px **edit rental**
  ("Proof Trailer") shows the real stored `NB 72 XMGP` (the vision model read `5120367QP4HD` as
  "40240486BBLY" — OCR noise, the DOM value check above is authoritative); 1440px **sale item** = Sales item
  selected and **no identification section at all**; 390px new + edit = one column, all three inputs 308px
  wide, labels legible, no clipping; 1440px **duplicate refusal** = the red banner quoted above.
  **Cosmetic fix the screenshots produced:** the placeholders (`ABC123GP` / `5120367QP4HD` / `S812W`-shaped
  examples) rendered exactly like recorded values — `vision_analyze` twice read them as filled-in fields —
  so they now read `e.g. ABC123GP` and the proof asserts both "nothing pre-filled" and "placeholders start
  with `e.g.`". Re-shot and re-inspected: the zoom confirms only placeholder text, boxes empty.

**Three REAL findings from the first green attempt (all fixed, all now tested):**
1. `run_migrations()` is **not** a standalone bootstrap — it `ensure_column`s tables that `SCHEMA` creates,
   so calling it against a hand-built legacy DB raises `sqlite3.OperationalError: no such table:
   company_settings`. The additive-migration test now emulates the pre-phase-5 shape honestly instead:
   build the real DB, `ALTER TABLE … DROP COLUMN` the seven new columns (dropping the index first), then
   `run_migrations()` and assert the columns come back empty with the rows intact.
2. `orders.return_scan_user_id` is a real FK — passing a user id that does not exist raises
   `sqlite3.IntegrityError: FOREIGN KEY constraint failed` (found by the test, which used a made-up id 7).
   The audit write now uses a genuinely existing account in the test; the screen will pass the session's
   user id, which always exists.
3. **A test I wrote was wrong, not the code:** I asserted `mark_returned_via_scan` would refuse an
   out-of-depot order in a test context, but there is no request context there, so
   `session_branch_scope_ids()` is legitimately `None` (unrestricted). `mark_returned_via_scan` gained an
   optional `session_scope=` so the scope guard is testable and provable through the same path the screen
   uses; the test now exercises it explicitly.

**Commit:** `dde0d26` — `feat(returns): trailer identity on inventory + scan-to-return matching service (D1)`
(5 files: `app/db.py`, `app/services/products.py`, `app/services/returns.py`, `templates/admin/inventory/form.html`,
`tests/test_programme_20260923_returns_match.py`). This ledger entry is its own commit so the hash is real.

**Blockers / notes for Don:**
- **Port 5057 is still held** by the 09:16 `app.py` that is not this programme's (pid 558440) — this tick
  again proved on 5058 and left 5057 untouched; 5058 is free again. Third tick in a row flagging it: say the
  word and the next tick stops it (or it stays as-is).
- **Two small deviations from the D1 plan text, both deliberate and additive:** (1) `orders.return_scan_user_id`
  was added on top of the three audit columns the plan listed, because the plan's own signature passes
  `user_id` and knowing *who* scanned is the point of an audit line; (2) `returns.py` reports the vehicle's
  NaTIS-number match as its own `matched_on` value (`customer_vehicle_registration_number`) rather than
  folding it into `customer_vehicle_plate` — the plan listed four `matched_on` values and this is a fifth, so
  D2's screen can show precise evidence. Neither changes any existing behaviour.
- **A trailer with no plate recorded can still only be returned by scanning the car's disc** (the plan's open
  question 3, still unanswered). D2 should keep the "type the plate" fallback so staff are never blocked by a
  trailer whose plate was never captured — and, if you say yes, let that typed-in plate be saved onto the
  trailer from the confirm step.

**Must-know for tick 6 (D2, the scan-to-return screen + browser proof):**
1. Use `returns.match_open_rentals(parsed, session_scope=None)` — the candidate dicts already carry
   `returnable`, `matched_on`, `evidence`, `source`, `reason` and `also_matched_on`, so the screen does not
   need to re-derive anything. **One candidate → offer it; two or more → require an explicit choice; none →
   list nothing and link to the started-orders list.** Never auto-pick.
2. `returns.returnable_order(order_id, session_scope=None)` is the pre-post guard and
   `returns.mark_returned_via_scan(order_id, user_id=<session user>, parsed=<the same parsed dict>)` is the
   action; it flashes nothing itself, so the route composes the message from its return value
   (`registration`, `source`, `message`) and redirects to `/orders/<id>` so the existing checklist/deposit
   flow continues. It **only** works once the invoice is finalized and the checklist is ticked — an order
   that is `started` but not return-ready surfaces the existing message verbatim, which is correct.
3. `scan_return` is a **new module**: add it to `MODULES` in `app/services/access.py` and add the
   `returns.` endpoint rule; like `scan_vehicle` it should stay **out** of the shared staff default set
   (Don ticks it per account), and its routes must 403 without it.
4. The parsed dict is the **parser's** shape (`licence_number` = plate, `disc_licence_number` = the disc's
   licence number, `registration_number` = NaTIS); `returns.scanned_identifiers()` already maps it, so a
   typed-in plate should be handed over in the same shape (`{"licence_number": "<plate>"}`) — do not invent a
   second mapping.
5. Proof DB note: the previous proof scripts are `/tmp/abi_a3_proof.py` (scan screen) and `/tmp/abi_d1_proof.py`
   (this tick's inventory-form proof) — reuse the login + overflow + screenshot harness from them.

### Phase 6 — D2 scan-to-return screen + marks returned + browser proof — status `done`
**Branch:** `feature/abi-programme-2026-09-23` (nothing pushed; `master` untouched).
**Files:** `app/routes/returns.py` (new, blueprint `returns`), `templates/admin/scan_return.html` (new),
`tests/test_programme_20260923_scan_return_flow.py` (new, 19 tests), `app/__init__.py` (register the blueprint),
`app/services/access.py` (module `scan_return` "Scan to return a trailer" + the `returns.` endpoint rule),
`templates/admin/layout.html` (nav entry beside "Scan a vehicle disk"), `templates/admin/orders/detail.html`
(the "Returned via disc scan" audit line), `app/services/returns.py` (`recently_returned()` + the audit plate is
now stored in the house normalised shape), `static/css/app.css` (scan-to-return block).

**Failing first (real, not asserted):** the test file was written and run before any of the implementation
existed → `.venv/bin/pytest tests/test_programme_20260923_scan_return_flow.py -q` → **16 failed in 14.80s**
(every failure a `404 Not Found` — `/scan-return` did not exist yet).

**Commands + real results:**
- `.venv/bin/pytest tests/test_programme_20260923_scan_return_flow.py tests/test_programme_20260923_returns_match.py -q`
  → **56 passed in 29.99s** (19 new + phase 5's 37, still green after the two fixes below)
- `.venv/bin/pytest tests/test_app.py tests/test_programme_20260923_returns_match.py
  tests/test_programme_20260923_vehicle_scan_flow.py tests/test_user_module_grants.py tests/test_user_multi_branch.py -q`
  → **290 passed in 206.90s** (the module-list / access / orders suites are unaffected by the new module)
- `.venv/bin/pytest -q` (full suite) → **864 passed in 517.73s (0:08:37)** — green (845 before, +19)
- `python3 -m compileall app tests -q` → clean
- Browser proof (venv Playwright Chromium, temp DB `/tmp/abi_d2.db`, app on **5058** because 5057 is still held
  by the 09:16 non-programme `app.py`; 9 screenshots in `/tmp/abi_d2_shots/`, report `/tmp/abi_d2_report.json`):
  **45/45 checks passed, 0 console errors, 0 horizontal overflow** at 1440×1100 and 390×844 — sign in (the sidebar
  carries the new "Scan to return" link) → paste the trailer's disk text → one candidate (`ORD-10145 ·
  Charmaine Mokoena`, Started, "Matched on the trailer's number plate — ABC123GP · 6m Trailer") → Mark returned →
  landed on `/orders/1` with the flash "Trailer ABC123GP returned via disc scan — finish the return checklist and
  the deposit on the order below", status **Returned**, DB audit `return_scan_registration=ABC123GP`,
  `return_scan_source=trailer_disc`, `return_scan_at=2026-09-23T12:49…`, `picked_up_at` unchanged → paste the
  **car's** disk text → **two** candidates, two Mark returned buttons, warning "2 rentals match this disk", both
  orders still `started` (nothing auto-picked) → choose `ORD-10146` → only that one returned → the car's disk
  again → one candidate left → returned, `return_scan_source=vehicle_disc` → the trailer's disk again → nothing
  offered, "This disk already came back: ORD-10145 · Charmaine Mokoena — already returned via disc scan on
  2026-09-23 12:49 (ABC123GP)" → an unknown plate → "No open rental matches that disk — read off it: number plate
  ZZZ999ZZ. Nothing has been changed." plus the started-orders link.
- `vision_analyze` on the screenshots (what was actually seen — `/tmp/abi_d2_shots/`): 1440px **capture** = the
  three ways to read a disk (camera input, paste box, "…or type the plate on the disk") with "Find the open
  rental", nothing clipped or overlapping; 1440px **trailer review** = the READ OFF THE DISK chips (Number plate
  ABC123GP · NaTIS registration number ZZ1234Z · Disc licence number T9876543210X · VIN · Engine number), one
  card, one Mark returned button; 1440px **ambiguous review** = the salmon warning "2 rentals match this disk.
  Check the order number and the customer — nothing is returned until you choose one of them." above two cards,
  two buttons; 1440px **order page** = "Status Returned" and "Returned via disc scan 2026-09-23 12:49 ·
  ABC123GP · trailer disk" under the green flash; 1440px **already-returned** = the callout naming the order,
  customer, time and plate, with no Mark returned button anywhere; 390px **capture + review** = one column, nav
  chips wrap onto two rows, chips/cards contained, nothing cut off.

**Two REAL findings this tick's own proof produced (both fixed, both now tested):**
1. **The audit line stored the raw parsed plate** (e.g. `jhb 789 gp`) while every product and vehicle plate in the
   app is stored normalised — `_scan_identity_for_order()` now runs the value through `normalise_registration()`,
   so the order page reads `JHB 789 GP` like everywhere else (test: "the audit line records the plate in the house
   normalised shape").
2. **A disk licence number was labelled "the trailer's number plate".** The matcher reports all three product
   identifiers as `matched_on=trailer_plate` (phase 5's design), so the review read "the trailer's number plate
   T9876543210X". `vision_analyze` caught it on the screenshot; the route now names the value that actually
   matched ("the trailer's disk licence number" / "the trailer's NaTIS registration number"), with two tests and a
   proof check pinning it, and the re-shot screenshot confirms the new wording.
Also added: `returns.recently_returned()` — a returned order is (correctly) no longer a live match, so a repeat
scan would otherwise only say "nothing found"; staff now get "this disk already came back: <order> … on <date>".

**Commit:** `66f9826` — `feat(returns): scan-to-return screen — scan a disc, mark the rental returned (D2)`
(9 files, +1102/−8; the hash was amended once from `6f144ce` to correct the test count written in its message —
"19 new tests", verified with `grep -c "^def test_"` = 19; the committed slice is unchanged). Committed **separately**, because they were already in the working tree uncommitted when this tick
started and they are the **main session's** work, not this tick's: `f0e3cf6` — `docs(programme): main-session
Feature P/Q renumbering, D10 + D11, POPIA plans` (the five modified plan docs + the two new POPIA feature plans +
`docs/popia/PRIVACY-NOTICE-REVIEW.md`). This ledger entry is its own commit so the hash is real.

**Blockers / notes for Don:**
- **Port 5057 is still held** by the 09:16 `.venv/bin/python app.py` (pid 558440, cwd = this repo, no
  `DATABASE_PATH` → the local dev DB). Fourth tick in a row flagging it: this tick proved on 5058 again and did
  **not** kill it. Say the word and the next tick stops it.
- **Open question 3 is still open** (may staff type a trailer plate on the scan screen and have it saved onto the
  trailer?). The screen keeps the fallback so nobody is ever blocked, and deliberately does **not** write a typed
  plate onto the trailer.
- **Renumbering landed mid-tick** (main session, 12:39–12:52): Feature P is now phase **7** and Feature Q is phase
  14. Phase 6 (D2) did not move, so this entry is unaffected — but the next tick must read the master plan's
  **14-row** table (phase 7 = P1), not a 12-row copy.
- **POPIA flag for your call:** `tests/test_programme_20260923_returns_match.py` (phase 5) carries the real disc
  identifiers you quoted — `NB72XMGP`, `QWR419V`, `5120367QP4HD`, `JHTFR22G10L654321`, `K9K7654321`. The
  guardrail says repo fixtures stay synthetic. This tick added none of its own (its fixture values are invented:
  `JHB 789 GP` / `NAT 5678 G` / `AHTFR22G10L999888` / `K9K123456`). Want the phase-5 constants swapped for
  synthetic ones in a later tick?
- The **1-minute cadence** means every skipped slot appends a "skipped" line to this ledger while a phase runs
  (the overlap guard), so the tick log will get noisier than the 12-row note implies. Say the word if you want
  those collapsed into one line per phase instead.

**Must-know for tick 7 (phase 7 = P1 POPIA privacy notice + consent, per the master plan's current 14-row table):**
1. Phase 7's plan is `docs/plans/2026-09-23-popia-privacy-consent.md` §P1 — **not** `branch-public-portal.md`
   (that is phase 8 now). The feature docs cross-reference by section, so read `§P1`.
2. `app/routes/returns.py` is the pattern to copy for a capture → review → confirm screen: module key in
   `MODULES` + an endpoint rule in `access.py`, a nav link in `layout.html`, tests in
   `tests/test_programme_20260923_*.py`.
3. Reuse `ALLOWED_IMAGE_TYPES` / `MAX_DISK_UPLOAD_BYTES` / `DECODE_ERROR_MESSAGES` from `app/routes/vehicles.py`
   rather than re-writing them — that is what keeps the two scan screens' error wording identical.
4. The proof harnesses are `/tmp/abi_d2_proof.py` + `/tmp/abi_d2_seed.py` (login, overflow, screenshots in both
   widths, sqlite read-back) — the closest starting point for any screen that changes a record.
5. The full suite is ~8–9 minutes (517s this tick). If the slot is tight, run the new file plus `tests/test_app.py`
   and record in the ledger that the full suite is due next tick.

### Phase 7 — P1 POPIA privacy notice + consent service — status `done`
**Branch:** `feature/abi-programme-2026-09-23` (nothing pushed; `master` untouched).
`docs/plans/.tick.lock` did not exist at the start — this tick created it and removed it; no stale lock.

**Files:** `app/services/popia_pack.py` (new — the reader phase 14 builds on), `app/services/consent.py` (new),
`app/db.py` (`consent_records` in `SCHEMA` + `run_migrations` + `idx_consent_records_customer`),
`app/routes/public.py` (`GET /privacy` + a same-origin-only back link), `app/services/customers.py`
(`delete_customer` clears consent rows explicitly), `app/routes/customers.py` + `templates/admin/customers/detail.html`
(the read-only evidence line), `templates/public/privacy_notice.html` (new, the full 12-section notice),
`templates/public/privacy_notice_interim.html` (new), `templates/public/_consent_block.html` (new, the ONE shared
widget), `templates/public/store.html` + `confirmation.html` (footer link), `static/css/app.css` (notice + consent
styles, print rules), `tests/test_programme_20260923_popia_consent.py` (new, 32 tests).

**The page is driven by the document, not by a second copy of it in code (D11).** `/privacy` asks
`popia_pack.outstanding_fields("privacy_notice")`; while the notice still carries any bracketed placeholder it
serves the short interim page and the moment the five facts land the *same route* serves the full notice — no code
change, and no token can reach a customer in either state. Measured in the browser: the shipped document still has
Sano's five open items → **3,518-byte interim page**; the same code against a completed copy of the pack →
**17,412-byte full notice** (all 12 sections, every s18(1) element). `PRIVACY_NOTICE_VERSION = "1.1"` and
`PRIVACY_NOTICE_REVIEWED = "2026-09-23"` are **pinned by a test to the `**Version:**` / `**Last reviewed:**`
lines of the document**, and a second test pins the page's address/phone/email to the document — so the reviewed
paper and the published page cannot drift apart silently.

**Consent (D10).** `record_consent(customer_id, channel, accepted)` refuses anything that is not a real
acceptance — including a crafted `popia_consent=0`, which is a truthy *string* in Python and would have been
accepted by a naive truthiness check (found by the test I wrote for it) — refuses an unknown customer, and then
writes exactly one row. `consent_summary()` is the evidence line the client page shows:
`POPIA consent — accepted 2026-09-23 (notice v1.1, via Midrand portal)`. `_consent_block.html` is the single
unticked-by-default widget (§B2/§C2 must include it; a test walks `templates/public/*.html` and fails if any other
template grows its own consent input).

**Commands + real results:**
- **Failing first (real, not asserted):** the test file was written before any implementation existed →
  `.venv/bin/pytest tests/test_programme_20260923_popia_consent.py -q` → `ImportError: cannot import name
  'consent' from 'app.services'` (collection error, 1 error in 3.28s) — then, after the services landed and before
  the last two fixes, **4 failed / 28 passed** (the placeholder rule missed `[To be supplied]`, the s18 needle had
  the wrong case, `"0"` was accepted as consent, and my own migration test read a table it had just dropped).
- `.venv/bin/pytest tests/test_programme_20260923_popia_consent.py -q` → **32 passed in 12.34s**
- `.venv/bin/pytest -q` (full suite) → **896 passed in 527.60s (0:08:47)** — green (864 before, +32)
- `python3 -m compileall app tests -q` → clean
- Browser proof (venv Playwright Chromium, temp DB `/tmp/abi_p7.db`, **5058** = the shipped app, **5059** = the
  same code with the notice document completed by the harness `/tmp/abi_p7_run_complete.py`; 8 screenshots in
  `/tmp/abi_p7_shots/`, report `/tmp/abi_p7_report.json`): **31/31 checks passed, 0 console errors, 0 horizontal
  overflow** at 1440×1100 and 390×844. Store footer → `View our privacy notice` → `/privacy`; interim page has no
  `[`, no `]`, no `TO CONFIRM`, no `____`, names the responsible party, the copy-request route and the Regulator;
  `/privacy` still 200 with `store_enabled = 0` while `/store` shows "temporarily unavailable"; signed in as owner →
  client page reads `POPIA consent  No POPIA consent recorded` → one consent row inserted → the same row reads
  `POPIA consent  POPIA consent — accepted 2026-09-23 (notice v1.1, via Midrand portal)`; the full notice renders
  all 12 `h2` sections in order, `Version 1.1 · Effective date: 2026-10-01 · Last reviewed: 2026-09-23`, the
  registered name from the document, the CCTV paragraph (answered yes in the harness copy) and **no bracket at all**.
- `vision_analyze` (what was actually seen — `/tmp/abi_p7_shots/`): 1440px interim = "Sano Trailers" + a **Back**
  button, the card with the `Privacy` eyebrow, `Our privacy notice is being finalised`, the "ask at the counter or
  email info@sanotrailers.co.za" sentence, the *Who we are* block (229 Summit Road, 010 221 1723, branches) and the
  Regulator block (JD House, 27 Stiemens Street, complaints.IR@justice.gov.za) — nothing clipped; 390px interim =
  one column, heading wrapping to two lines, everything legible, contained. Full notice: heading
  `Privacy Notice — Sano Trailers`, the version line above, §1 with the registered name from the harness document,
  §2's collection table, §3's purpose/lawful-basis table ("Legal obligation — the Tax Administration Act 28 of 2011
  …"), §4's recipient list, §11's Regulator block and §12, footer `© 2026, Sano Trailers` + the notice link; no
  placeholder brackets, no overlap.
  **A vision caveat worth recording:** the *whole* 6,431px full-page screenshot cannot be read in one pass — asked
  to describe it, the vision lane invented a plausible-but-wrong notice (a "2025-05-01" version line, B-BBEE and
  driver-medical rows that exist nowhere in the file). Re-asked on **region crops at original resolution**
  (`region=[0,0,1440,1250]`, `[0,2500,1440,3750]`, `[0,5200,1440,6431]`) it read the real text, which matches the
  DOM assertions exactly. Lesson for later ticks: for a long page, crop and re-ask; never trust a single
  full-page read.

**Two REAL bugs/rules the work produced (both now tested):**
1. **`"0"` was consent.** `if not accepted:` accepted the string `"0"` (a crafted POST) as agreement. Replaced with
   `consent.acceptance_given()` (an explicit allow-list of what a ticked box posts); `"0"`, `"false"`, `"no"` and
   `2` are all refused, `"1"/"on"/"true"/True/1` accepted.
2. **The placeholder rule was too narrow.** An all-caps-words-only regex missed a placeholder worded as a sentence
   (`[To be supplied]`, `[Confirm per branch …]` — the last one only matched because it contained "CCTV"). The gate
   now flags **any** `[...]` containing a capitalised word: a false positive keeps the page interim (safe), a false
   negative would publish a placeholder to a customer (not safe).

**Cosmetic notes from looking at the screenshots:** the interim and full pages intentionally carry a **Back** link
at the top *and* at the end of the card — on a 6,400px document the bottom one is the useful one, so both stay.
The notice's branch line reads `Rooderport` (as `docs/popia/PRIVACY-NOTICE.md` does); that looks like a typo for
**Roodepoort** and it is in Sano's reviewed wording, so it was left exactly as the document has it.

**Commit:** `a8c69df` — `feat(popia): privacy notice page + recorded client consent (P1)` (14 files, +1190/−2).
Committed **separately first**, because it was already in the working tree when this tick started and it is the
**main session's** work, not this tick's: `a9e9e78` — `docs(popia): privacy notice v1.1 + blockers checklist +
operator-agreement review (main session)`. This ledger entry is its own commit so the hash above is real.

**Blockers / notes for Don:**
- **The published page is the interim one until Sano's five facts land** (`docs/popia/BLOCKERS-CHECKLIST.md`:
  registered name + registration number, Information Officer's name, effective date, the notice's public URL, and
  whether each branch has CCTV). No code change is needed when they arrive — but **Feature B (§B2) and Feature C
  (§C2) must not go live before them, or a client would be asked to accept a notice whose full text is not yet
  published.** Your call: either land the facts first, or accept a consent against the interim page (the record
  already stores the version + channel, so an interim acceptance stays traceable).
- **Port 5057 is still held** by the 09:16 `.venv/bin/python app.py` (pid 558440) — this tick proved on **5058**
  (+5059 for the completed-document harness) and left 5057 untouched; both of its own ports are free again. Fifth
  tick flagging it: say the word and the next tick stops it.
- **`app/services/popia_pack.py` was created here (one phase early)** because the gate in §P1 needs it. It is the
  reader only — phase 14 (§Q1) adds `document_hash`, `acceptance_for`, `is_stale`, `accept_document` and the
  `document_acceptances` table to this same module. Deliberate, and recorded so phase 14 extends rather than
  duplicates.
- **Small deviation from the plan text:** §P1 said `PRIVACY_NOTICE_VERSION = "2026-09-12"` (a date). The document
  now carries `**Version:** 1.1` with the effective date still open, so the version token is **`"1.1"`**, pinned to
  the document's own `Version:` line by a test. Recording a version that does not exist in the document would have
  made the evidence line useless.
- **POPIA scrub — the fixtures are now clean, the documentation quotes are not (needs your call).** Tick 6 flagged
  that the programme's tests carried the real disc identifiers you quoted. This tick swapped them out of **every
  test fixture and staff-facing placeholder** for synthetic identifiers of the same shape (`NB72XMGP` / `QWR419V` /
  `5120367QP4HD` / `JHTFR22G10L654321` / `K9K7654321`): `tests/test_programme_20260923_returns_match.py`,
  `…_vehicles_model.py`, `…_vehicle_client_page.py` (including the spaced/lower-case variants those files relied on)
  and the inventory form's example placeholders, which staff could see on a live screen. Re-ran the five affected
  programme suites → **115 passed in 114.21s** and `tests/test_app.py` → **203 passed in 170.07s**.
  **Still carrying the real values, deliberately left for your decision** because they are *evidence*, not fixtures:
  `app/services/vehicle_disk.py:309-310` (the measured 148-char payload, verbatim, including the VIN),
  `app/services/vehicles.py`, `app/services/products.py`, `app/db.py` (comments quoting the identifiers),
  `docs/plans/disc-decode-findings.md`, `2026-09-23-ABI-programme.md` (decision D3),
  `2026-09-23-scan-to-return.md`, `2026-09-23-staff-vehicle-scan.md`. Rewriting a measured payload in the findings
  record is a judgment call about what the evidence means, not a mechanical replace — say the word and the next
  tick does it as its own slice.

**Must-know for tick 8 (phase 8 = B1, branch portal schema + link + QR, per the master plan's 14-row table):**
1. Phase 8's plan is `docs/plans/2026-09-23-branch-public-portal.md` **§B1** — read it, not the phase table row, and
   note decision **D5** (URL shape `/portal/<slug>`, QR built from a `public_base_url` setting) and **D4**
   (uploads live in the DB, because Render's filesystem is ephemeral; §B1 must prove an image survives a restart).
2. `popia_pack.document_paths()` / `document_text()` are the reader for any POPIA document the portal needs;
   `consent.notice_is_publishable()` tells you (truthfully, today) that the notice is **not** publishable yet.
3. `consent.record_consent(customer_id, channel, accepted)` + `consent.consent_required_error()` are the whole
   server-side consent story for §B2 — pass `channel=consent.CHANNEL_PORTAL`, and include
   `{% include "public/_consent_block.html" %}` with `consent_purpose="registration"`.
4. Public routes are the `public.` blueprint (ungated by `access.py`), public templates extend `templates/base.html`
   and the house public classes are `.store-header` / `.store-main` / `.panel` / `.store-footer`; the notice link is
   already in the store footer.
5. The proof harnesses this tick used are `/tmp/abi_p7_seed.py`, `/tmp/abi_p7_proof.py` (login, overflow, both
   widths, sqlite read-back, screenshot + vision) and `/tmp/abi_p7_run_complete.py` (the completed-document trick) —
   the closest starting point for §B1's portal + QR proof. **Crop long screenshots before asking for a description.**



### Slots skipped — 2026-09-23 13:28, 13:30 — no work
- `.tick.lock` present and **27.7 min old at 13:30:16** (< 45 min), so tick 8 (phase 8 = §B1 branch portal) is still mid-flight. Per the overlap guard these slots did NO work and made no commit; next tick continues from tick 8. (One consolidated line instead of one per minute, to keep this ledger readable while the lock holds.)
- Evidence measured this slot, in case the lock outlives the work: lock written `Wed 23 Sep 13:02:53`, so it goes **stale at 13:47:53**; **no phase-8 artefacts exist yet** (`git log` head is still `a8c69df`, phase 7 / P1; no `*portal*` file anywhere; `tests/` has no `…_portal_*` file); **no pytest/python process of this programme is running** (`ps` shows only the unrelated 09:16 `app.py` pid 558440 on 5057 plus the EventPro pair). The tree *is* being touched, though — `tests/test_programme_20260923_{returns_match,vehicles_model,vehicle_client_page}.py`, `templates/admin/inventory/form.html`, `docs/plans/2026-09-23-popia-document-pack.md` and `docs/popia/CLIENT-DATA-NOTICE-JACKAPP-SANO.md` were all written 13:20–13:24, and `.pytest_cache` was last updated 13:26:05 — so the lock owner (or the main session) is live and must not be raced.
- **Nothing was committed and the lock was deliberately left in place** (it is not this slot's to clear). Uncommitted work belonging to that worker is in the tree (`app/`, `app/services/`, `app/routes/`, `templates/admin/inventory/form.html`, three test files) — the next tick must not `git stash`, revert or blanket-commit any of it.
- 13:33 slot skipped as well: the same 13:02:53 `.tick.lock` is **30.2 min old at 13:33:06** (< 45 min; it goes stale at **13:47:53**), so tick 8 (§B1) still holds the tree. No work, no commit. The owner is demonstrably alive — **`docs/plans/2026-09-23-popia-setup-wizard.md` was created 13:33:38, i.e. during this very slot**, with `PROGRESS.md` written 13:30:51 and `2026-09-23-popia-document-pack.md` 13:24:01 — so keep the lock and keep hands off. Still no phase-8 artefacts (`*portal*` matches the plan doc only) and head is still `a8c69df` (phase 7 / P1). `ps` shows no pytest/playwright/chromium process of this programme, so those writes are documents, not a running suite.
- 13:35 slot skipped too: the same 13:02:53 `.tick.lock` is **33.1 min old at 13:35:59** (< 45 min; stale at **13:47:53**), so tick 8 (§B1) still owns the tree. No work, no commit, lock left in place. Re-checked this slot: head still `a8c69df` (phase 7 / P1), still no phase-8 artefacts (`*portal*`), and **no new writes anywhere in `app/`, `templates/` or `tests/` since 13:33** — the only running processes are the unrelated `app.py` servers (pid 558440 on 5057, pid 245093, plus the EventPro pair), no pytest/playwright. The next tick at ~13:37 will still be inside the window; the first slot that can reclaim the tree is the one at/after **13:47:53**, which must then treat the lock as stale and start phase 8 fresh (not resume an absent worker).
- 13:38 slot skipped too: the same 13:02:53 `.tick.lock` is **35.3 min old at 13:38:12** (< 45 min; stale at **13:47:53**), so tick 8 (§B1 branch portal) still owns the tree. No work, no commit, lock left in place. Re-checked this slot: head still `a8c69df` (phase 7 / P1), still no phase-8 artefacts (`*portal*` matches the plan doc `docs/plans/2026-09-23-branch-public-portal.md` only; no `tests/*portal*`), and **no new writes in `app/`, `templates/` or `tests/` since 13:21** — the lock owner's only recent writes are documents (`2026-09-23-popia-setup-wizard.md` 13:34:35, `PROGRESS-popia-wizard.md` 13:35:19). No pytest/playwright/chromium process of this programme is running (`ps` shows only unrelated servers: LedgerJack node pair, the 5057 `app.py`, EventPro, plus the WhatsApp bridge). The next slot at/after **13:47:53** must treat the lock as stale and start phase 8 fresh.
- 13:40 slot skipped too (seventh consecutive): the same 13:02:53 `.tick.lock` is **37.1 min old at 13:40:00** (< 45 min; stale at **13:47:53**), so tick 8 (§B1 branch portal) still owns the tree. No work, no commit, lock left in place. Re-checked this slot: head still `a8c69df` (phase 7 / P1); still no phase-8 artefacts (`ls tests/ | grep -i portal` → no match); **no new writes in `app/`, `templates/` or `tests/` since 13:21** — the only file written since 13:36 anywhere is this ledger itself (13:38:17, by the previous skip slot). No pytest/playwright/chromium process of this programme is running (`ps` shows only unrelated servers: pid 245093 and pid 558440 `app.py`, plus the EventPro pair).

- 13:42 slot skipped too (eighth consecutive): the same 13:02:53 `.tick.lock` is **39.3 min old at 13:42:04** (< 45 min; stale at **13:47:53**), so tick 8 (§B1 branch portal) still owns the tree. No work, no commit, lock left in place. Re-checked: head still `a8c69df` (phase 7 / P1); still no phase-8 artefacts (`ls tests/ | grep -i portal` → no match; no `*portal*` under `app/` or `templates/`); **no writes in `app/`, `templates/` or `tests/` since 13:21** (13:21 = tick 7's POPIA-scrub fixtures; the only later writes anywhere are documents — `PROGRESS.md` 13:40, `PROGRESS-popia-wizard.md` 13:35, `2026-09-23-popia-setup-wizard.md` 13:34, `2026-09-23-popia-document-pack.md` 13:24, `CLIENT-DATA-NOTICE-JACKAPP-SANO.md` 13:23). `ps` shows no pytest/playwright/chromium process of this programme — only the unrelated servers (pid 245093 and pid 558440 `app.py`, plus the EventPro pair). **Eight consecutive slots (13:26→13:42) have now been lost with zero phase-8 progress; the lock frees at 13:47:53, so the first slot at/after that must treat it as stale and start §B1 fresh** — flagged for Don, since the 45-min guard is otherwise throttling the programme.

- 13:43 slot skipped too (ninth consecutive): the same 13:02:53 `.tick.lock` is **41.0 min old at 13:43:56** (< 45 min; stale at **13:47:53**), so tick 8 (§B1 branch portal) still owns the tree. No work, no commit, lock left in place. Re-checked: head still `a8c69df` (phase 7 / P1); still no phase-8 artefacts (`ls tests/ | grep -i portal` -> no match; no `*portal*` under `app/` or `templates/`); **no writes in `app/`, `templates/` or `tests/` since 13:21:21** (that last write is only a `.pyc`). `ps` shows no pytest/playwright/chromium process of this programme - only unrelated servers (pid 245093 + pid 558440 `app.py`, plus the EventPro pair on 5055). **The next slot (13:45) is still inside the window; the first slot at/after 13:47:53 must treat the lock as stale and start section B1 fresh.**

- 13:45 slot skipped too (tenth consecutive): the same 13:02:53 `.tick.lock` is **43.0 min old at 13:45:50** (< 45 min; stale at **13:47:53**), so tick 8 (section B1 branch portal) still owns the tree. No work, no commit, lock left in place. Head still `a8c69df` (phase 7 / P1); still no phase-8 artefacts (`ls tests/ | grep -i portal` -> no match). **New and decisive evidence this slot: the lock owner is not merely writing docs — a full test suite is running right now** (`ps`: pid 706243 `.venv/bin/python -m pytest tests/ -q`, 60% CPU, started 13:45, launched by pid 706206 from this repo), so the tree is genuinely mid-flight and must not be raced. Next slot (13:46) is still inside the window; the first slot at/after **13:47:53** must treat the lock as stale and start section B1 fresh (fresh start — there is no resumable worker state).

### Main session — 2026-09-23 13:55 — forensic check of phase 8 (§B1) + a completed POPIA scrub (no phase advance)

**Don asked: "check what was done on phase 8, if it looks complete move to the next phase." Verdict: phase 8 was NEVER built — nothing to advance *from*; §B1 is still the next phase.**

- The slot that claimed `.tick.lock` at **13:02:53** produced **zero phase-8 artefacts** and never committed — head stayed `a8c69df` (phase 7 / P1) right through to 13:53. No `*portal*` file anywhere, no `public_slug` in the code, no QR endpoint, no `tests/*portal*`.
- What that slot actually did (writes 13:20–13:24) was a **POPIA scrub it never finished**: it replaced the real licence-disc identifiers in three earlier test files plus the inventory form placeholders, then died. `ps` showed no pytest/playwright/python process of this programme running.
- The orphan lock then cost **10 consecutive slots (13:26 → 13:45)** — roughly 20 minutes of programme time — until it aged past the 45-minute stale window; the 13:47:48 slot reclaimed it (re-stamped at ~13:54) and is now building §B1.
- **Main session finished the scrub and committed it** — **`404cc24`**, **87 replacements across 13 files** (the dead worker's 20 in three test files + `templates/admin/inventory/form.html`, plus 67 more in `app/db.py`, `app/services/{products,vehicles,vehicle_disk}.py`, `2026-09-23-{ABI-programme,scan-to-return,staff-vehicle-scan}.md` and `disc-decode-findings.md`). Real values → synthetic equivalents in the real disc's shape (`NB72XMGP` / `QWR419V` / `5120367QP4HD` / `JHTFR22G10L654321` / `K9K7654321`), with the field-ORDER mapping the decision actually turns on left intact.
- **Full suite re-run after the scrub: 896 passed in 525s.** 0 occurrences of the real values remain in the tree, and the byte-compiled caches holding them were cleared.
- **Exposure check (good news):** `git log -S KP35XKGP origin/master` is **EMPTY** — these identifiers were never pushed. They exist only in this branch's 15 unpushed commits.
- **RULE for the merge (POPIA):** when this branch is merged to `master`, do it as **one squashed commit** — never push the intermediate commits, because the pre-scrub commits still carry the real plate/VIN/engine in their diffs. (Rewriting this branch's history before any push is also safe, since it is unpushed.)
- **For the next main session:** the main job sits at **12/24 fires used with 7 phases to go** (~1.71 fires per finished phase), so the budget needs raising or it stops mid-programme. And the 45-minute stale window let one dead worker burn 10 slots — a shorter window (~20 min) or a heartbeat would self-heal faster. Both are changes to a *running* job, so they wait for Don's OK (he said not to interfere with it).

### Phase 8 — B1 branch portal schema + shareable link + QR — status `done` (tick 8)
**Branch:** `feature/abi-programme-2026-09-23` · **commit:** `fb2cc24` (the work; this ledger entry is its
own commit, as tick 7 split them) · **lock:** this slot found the
13:02:53 lock **51 min old**, the owner's pytest exited at 13:53:47 and its scrub commit `404cc24`
landed 13:53:58, so the slot re-stamped the lock at 13:54:10 and carried on. The main session's own
forensic entry (committed separately first as `40ef85c`, because it was already in the tree when this
tick started) hands §B1 to this tick — phase 8 had **never** been built.

**Files:** `app/db.py` — three additive `branches` columns + `company_settings.public_base_url`, a new
`_backfill_branch_slugs()` and the partial unique index `idx_branches_slug`; `app/services/portal.py` (new:
`slugify`, `unique_slug`, `ensure_slug`, `branch_by_slug`, `portal_branch`, `portal_path`, `portal_url`,
`qr_png_bytes`, `all_portal_links`); `app/services/branches.py` (`create_branch()` now slugs a new branch, so
its link works before the next app start); `app/routes/public.py` (`GET /portal/<slug>`,
`GET /portal/<slug>/qr.png`); `templates/public/portal_placeholder.html` (new);
`static/css/app.css` (`.portal-actions`); `requirements.txt` (`qrcode[pil]==8.2`);
`tests/test_programme_20260923_portal_links.py` (new).

**Commands + REAL results:**
- `.venv/bin/pip install "qrcode[pil]"` → `Successfully installed qrcode-8.2`. **Note:** `.venv/bin/pip`
  does **not** exist in this venv (uv-built, no console script) — `python -m pip` (pip 24.0) works. First
  tick to hit that; every earlier ledger line saying `.venv/bin/pip` was aspirational.
- `.venv/bin/pytest tests/test_programme_20260923_portal_links.py -q`, written failing-first: run 1 →
  `ImportError: cannot import name 'portal' from 'app.services'`; run 2 → **5 failed, 18 passed**;
  run 3 → **23 passed in 8.37s**.
- `python3 -m compileall app tests -q` → clean.
- `.venv/bin/pytest -q` (full suite) → **919 passed in 532.12s (0:08:52)** (896 before + 23 new).
- `.venv/bin/pytest -q` needed **no** `tests/test_app.py` fallback — the whole suite fits in the slot.
- Real browser proof (`/tmp/abi_b1_seed.py` + `/tmp/abi_b1_proof.py`, temp SQLite `/tmp/abi_tick_b1.db`,
  app on **5058**; 5057 is still held by the 09:16 dev server — **sixth tick flagging it, still untouched**)
  → **23/23 checks, 0 console errors**: `/portal/roodepoort` 200 at 1440px **and** 390px with
  `scrollWidth == clientWidth` (1440/1440 and 390/390 → 0 horizontal overflow); `/portal/roodepoort/qr.png`
  200, `image/png`, `public, max-age=86400`, 410×410, PNG magic — and **decoded back with zxing-cpp to
  `https://sano-trailers.example/portal/roodepoort`**, so the QR really carries the link; unknown slug →
  **404** for page and QR; a branch with `portal_enabled = 0` → **404** for page and QR.
  Screenshots `/tmp/abi_b1_shots/*.png`, report `/tmp/abi_b1_report.json`.

**What the screenshots actually showed (`vision_analyze`):** the first draft rendered the link and the phone
number in `.muted` small print, and the 390px read-back said exactly what the house rule exists to catch —
both "read as plain, muted grey text … no visual cue that either is tappable". Fixed with a bordered
`.portal-actions` block (brand colour, `tel:` link) and **re-proved on a restarted server**; the second
read-back at 1440px and 390px confirms "a distinct bordered block … blue branch link and blue phone link",
nothing overflowing or broken at either width.

**Two bugs this tick had to fix, both real:**
1. **Fresh-DB ordering.** `init_db()` creates the three starter branches *after* `run_migrations()`, so the
   migration's slug backfill never saw them and a brand-new install had three linkless branches. `init_db()`
   now calls the same idempotent `_backfill_branch_slugs()`. (Caught by `test_backfill_fills_every_blank_slug_on_startup`.)
2. **Templates are cached with debug off.** After editing the template the proof still "passed" — against
   the **previous** page, because Flask/Jinja does not auto-reload templates when `debug=False`. The server
   has to be restarted for a template change, or the browser proof measures the old page. Worth remembering
   for every later UI phase.

**Decisions taken where the plan was silent (both pinned by tests):**
- `/portal/<slug>` is **GET-only**: `POST` → **405**. The route sits under the ungated `public.` prefix, so
  until §B2 builds a form (with honeypot + rate limit + consent) there must be no public write path at all.
- The portal is **not** gated by `store_enabled`, only by the per-branch `portal_enabled`. A branch handed a
  printed QR should not go dark because the catalogue is switched off; a disabled portal 404s both routes.
- The QR's `Cache-Control` is `public, max-age=86400` — long enough for a counter screen, short enough that a
  corrected `public_base_url` stops being served within a shift.

**DB↔UI parity:** `public_slug` / `portal_enabled` / `portal_intro` have **no admin UI yet** — §B3 owns that
page (`/settings/portal`). Measured, not assumed: `update_branch()` writes an explicit column list, so the
admin branch-edit form cannot clobber them, and a test pins that (a rename also does **not** re-slug).

**Must-know for tick 9 (phase 9 = §B2 public form + dedupe + "am I already a customer?"):**
1. §B2's plan is `docs/plans/2026-09-23-branch-public-portal.md` **§B2**: `GET/POST /portal/<slug>/register`,
   new `app/services/portal_intake.py`, `templates/public/portal_form.html` + `portal_confirm.html` +
   `portal_exists.html`, tests `tests/test_programme_20260923_portal_intake.py`.
2. The §B1 placeholder page and template are §B2's to replace. Reuse `portal.portal_branch(slug)` for the same
   404 semantics and `portal.portal_url(branch, request.url_root)` when a page needs the canonical link.
3. Consent is already built: `consent.record_consent(customer_id, channel=consent.CHANNEL_PORTAL, accepted)`,
   `consent.consent_required_error()`, and `{% include "public/_consent_block.html" %}` with
   `consent_purpose="registration"`.
4. **POPIA gate still open:** `consent.notice_is_publishable()` is still `False` (Sano's five facts,
   `docs/popia/BLOCKERS-CHECKLIST.md`). §B2/C2 must not go live before them — worth Don's call, unchanged.
5. Restart the smoke server after **any** template edit, or the browser proof measures the previous page.
6. The `public_base_url` used in the proof (`https://sano-trailers.example`) is a seed value for the temp DB
   only; nothing in the repo carries it.

- 14:46 slot skipped: fresh `.tick.lock` written **14:44:13** is only **1.5 min old at 14:45:57** (< 45 min) and the owner is demonstrably alive — `ps` shows pid 746529 `.venv/bin/pytest -q` **started 14:44:17 and still running (2:01 elapsed at 14:46:18)**, plus the uncommitted §B2 work in the tree (`app/routes/public.py`, `templates/public/portal_{form,confirm,exists}.html`, `app/services/portal_intake.py`, `tests/test_programme_20260923_portal_intake.py` written **14:42:04**). No work, no commit, lock left in place (it is not this slot's to clear). Head is still `065370b` (phase 8 / §B1 done, phase 9 = §B2 in flight). This is a real in-flight tick, not the stale-lock pattern of the 13:26–13:45 streak.

- 14:48 slot skipped: `.tick.lock` written **14:44:13**, only **3.8 min old at 14:48:04** (< 45 min), and the owner is demonstrably alive — `ps` shows pid 746529 `.venv/bin/pytest -q` (started **14:44:17**, **3:47 elapsed** at 14:48:04) still running the full suite, with the uncommitted phase-9 (§B2) work in the tree (`app/services/portal_intake.py` 14:25:56, `templates/public/portal_form.html` 14:41:43, `tests/test_programme_20260923_portal_intake.py` 14:42:04). No work, no commit; lock left in place (not this slot's to clear). Head still `065370b` (phase 8 / §B1 done, phase 9 = §B2 in flight).

- 14:50 slot skipped: `.tick.lock` written **14:44:13** is only **5.8 min old at 14:49:57** (< 45 min), and the owner is demonstrably alive — `ps` shows pid **746529** `.venv/bin/pytest -q` started **14:44:17**, **5:46 elapsed** at 14:49:58, still running the full suite. Phase-9 (§B2) work is still uncommitted in the tree (`app/services/portal_intake.py`, `templates/public/portal_{form,confirm,exists}.html`, `app/routes/public.py`, `tests/test_programme_20260923_portal_intake.py`). No work, no commit; lock left in place (not this slot's to clear). Head still `065370b` (phase 8 / §B1 done, phase 9 = §B2 in flight).

- 14:52 slot skipped: `.tick.lock` written **14:44:13** is only **7.9 min old at 14:52:11** (< 45 min), and the owner is demonstrably alive — `ps` shows pid **746529** `.venv/bin/pytest -q` started **14:44:17**, **07:53 elapsed** at 14:52:11, still running the full suite. Phase-9 (§B2) work still uncommitted in the tree (`app/services/portal_intake.py` 14:25:56, `app/routes/public.py` 14:25:56, `static/css/app.css` 14:24:47, `templates/public/portal_{form,confirm,exists}.html`, `tests/test_programme_20260923_portal_intake.py` 14:42:04). No work, no commit; lock left in place (not this slot's to clear). Head still `065370b` (phase 8 / §B1 done, phase 9 = §B2 in flight).

- 14:54 slot skipped: `.tick.lock` written **14:44:13** is only **9.8 min old at 14:54:03** (< 45 min), so the lock is respected and left in place. New observation for the next tick: the owner's run is **no longer alive** — pid 746529 (`.venv/bin/pytest -q`) has vanished from `ps` between 14:52:11 (07:53 elapsed) and 14:54:00, and no pytest/python process is holding the tree at all, yet nothing was committed (head still `065370b`, §B2 files unchanged since 14:42:04). So the owner either finished the suite and exited without committing, or died. Still no work and no commit from this slot. If the lock is still present and the tree still shows no new commit at the next 1-min slot, the 45-min stale threshold (14:44:13 + 45 min = **~15:29**) is when it becomes this programme's to clear — do not clear it before then.

- 14:57 slot skipped: `.tick.lock` (written **14:44:13**) is only **12.6 min old at 14:56:53** (< 45 min), so the lock is respected and left in place; no work, no commit. Sharper diagnosis than 14:54: the owner's full-suite run (pid 746529, started 14:44:17) finished ~14:52 and exited, and the owner agent is now **gone** — its proof harness is an **orphan reparented to systemd** (`ps -p 745561 -o ppid` -> 372/systemd; a blocking `app.run()` bound to **127.0.0.1:5059**, last HTTP request 14:43:13, 0:01 CPU in 13:54 elapsed), with the §B2 files untouched since 14:42:04 and head still `065370b`. So phase 9 (§B2) looks crashed *after* its suite, before its ledger entry + commit, leaving the phase-9 work uncommitted in the tree. Nothing in this slot clears the lock: **stale threshold = 14:44:13 + 45 min ~= 15:29**. Must-know for the tick that owns phase 9 next: (a) kill the orphan on **5059** first or its own proof server cannot bind; (b) the full suite already ran green-in-progress, but the phase-9 changes were never committed, so re-verify before trusting them.

- 14:59 slot skipped: `.tick.lock` (written **14:44:13**) is only **14.9 min old at 14:59:11** (< 45 min), so the lock is respected and left in place; no work, no commit. Liveness re-checked, same picture as 14:57 with one difference: **no** `pytest`/programme python process is alive, but the owner's proof harness survives as an orphan — `ps -p 745561` → `.venv/bin/python /tmp/abi_b2_run_complete.py`, **ppid 372 (systemd)**, **16:15 elapsed**, still `LISTEN 0.0.0.0:5057`… no: `LISTEN 127.0.0.1:5059` (5057 is the unrelated 09:16 dev server, pid 558440, 05:42:45 elapsed). §B2 files still untouched since 14:42:04 (`app/services/portal_intake.py` + `app/routes/public.py` 14:25:56, `templates/public/portal_form.html` 14:41:43, `tests/test_programme_20260923_portal_intake.py` 14:42:04); head still `065370b`; tree still carries the uncommitted phase-9 work plus other untracked scratch. **Stale threshold = 14:44:13 + 45 min = 15:29:13** — the lock is not this slot's to clear before then. Nothing cleared here.

### Main session — 2026-09-23 15:01 — phase 8 landed, phase 9 crashed mid-flight, stall cut short (Don's instruction)

- **Phase 8 (§B1) is DONE and committed** — `065370b` (branch portal link + QR + browser proof). Nothing outstanding on it.
- **Phase 9 (§B2) crashed after its suite, before its ledger entry and commit.** Its work is complete on disk but **uncommitted** and must NOT be reverted or re-done from scratch. Its status row may already read `done`; the ledger entry was never written.
- **What the main session did, in order:**
  1. **Killed the crashed tick's orphan proof server** — pid `745561`, `.venv/bin/python /tmp/abi_b2_run_complete.py`, cwd this repo, started **14:42:55**, reparented to systemd and blocking **127.0.0.1:5059** with no requests since 14:43:13. Port 5059 is free again; the helper script is still at `/tmp/abi_b2_run_complete.py` if useful.
  2. **Re-verified the uncommitted §B2 work rather than trusting it:** `.venv/bin/pytest tests/test_programme_20260923_portal_intake.py -q` → **55 passed in 19.07s**. So the phase's own tests are green; the full suite and the browser proof are still owed.
  3. **Cleared the stale lock** (`docs/plans/.tick.lock`, written 14:44:13) at 15:01 instead of waiting for the old 45-minute threshold (15:29). The next slot now owns the tree immediately.
  4. **Changed the overlap guard in the job prompt from 45 → 20 minutes** (Don approved, 2026-09-23), so a crashed slot costs ~20 minutes instead of ~45. Also added a rule to kill orphaned proof servers on the target port before binding it, and a rule that a phase whose work is already on disk is FINISHED (re-verify, commit, ledger) rather than restarted.
- **Uncommitted §B2 files the next tick must stage (explicit paths, never `git add -A`):**
  - modified: `app/routes/public.py`, `docs/plans/PROGRESS.md`, `static/css/app.css`, `templates/public/portal_placeholder.html`, `tests/test_programme_20260923_portal_links.py`
  - new: `"Customer comms/"`, `app/services/portal_intake.py`, `templates/public/portal_check.html`, `templates/public/portal_confirm.html`, `templates/public/portal_exists.html`, `templates/public/portal_form.html`, `tests/test_programme_20260923_portal_intake.py`
  - note `templates/public/portal_placeholder.html` shows as a **staged deletion** (`D `) — that is intended (the real portal page replaces the placeholder) and should be committed with the phase.
- **NEXT TICK: finish phase 9 (§B2)** — full suite green, browser proof at 1440px + 390px with screenshots actually looked at (port 5057, temp DB via `DATABASE_PATH`), the phase-9 ledger entry + status row, then commit. After that, phase 10 (§B3 admin QR/link page with A4 print).

### Phase 9 — B2 public form + dedupe + "am I already a customer?" — status `done` (tick 9 — finishing a crashed slot's work)
**Branch:** `feature/abi-programme-2026-09-23` · **commit:** `6939107` (the §B2 work; this ledger entry is
its own commit, so the hash above is real) · **lock:** none at the start of this slot — the main session
cleared the crashed `14:44:13` lock at 15:01 and cut the guard to 20 min, so this tick created its own lock
(15:01:03) and removed it at the end. Nothing stale in the tree, no orphan proof server on any port.

**Provenance, stated plainly.** The code below was **built by the 14:44 slot, which crashed after its own
full-suite run and before its ledger entry/commit**. This tick did not trust it and did not rebuild it:
`compileall` was re-run, the phase's own tests were re-run, and — the part the dead slot never reached —
**both browser proofs were re-run end to end by this tick, and every screenshot was actually looked at**.
The crashed slot's reports were read first and then independently reproduced (same counts).

**Files:** `app/services/portal_intake.py` (new, 430 lines), `app/routes/public.py` (`/portal/<slug>` now
renders the form; new `GET/POST /portal/<slug>/register`, `POST /portal/<slug>/check`),
`templates/public/portal_form.html` + `portal_confirm.html` + `portal_exists.html` + `portal_check.html`
(new), `templates/public/portal_placeholder.html` (**deleted** — §B1's "coming next phase" page is gone, as
§B2 planned), `static/css/app.css` (`.portal-*` styles), `tests/test_programme_20260923_portal_intake.py`
(new, 696 lines / 55 tests), `tests/test_programme_20260923_portal_links.py` (§B1's placeholder test
rewritten into "the QR's page *is* the form", with the intake gate opened explicitly). 10 files,
+1627/−55.

**What §B2 actually does, in the order the route enforces it:** unknown/disabled branch → 404 before
anything is read; **while the published notice still carries an open placeholder the write path is shut**
(D11) — the page says so in plain words and a POST writes nothing; a filled honeypot gets a success page and
no record; validation; **consent required server-side** (an unticked box *and* a crafted `popia_consent=0`
are both 400 — D10); only then dedupe, and a possible match is **presented to the customer, never resolved
for them** (D6: masked "Is this you?" → `This is me` links, filling **only blank** fields, or `None of these`
creates a second record deliberately); the acceptance is recorded in the same request that writes the client,
on the `branch portal` channel, against notice version `1.1` (D8's masked lookup is a **POST** so a name and
number never land in browser history/proxy logs; the in-process limiter keys a **salted digest**, so no IP
is stored anywhere).

**Commands + REAL results (all re-run by this tick):**
- `python3 -m compileall app tests -q` → clean
- `.venv/bin/pytest tests/test_programme_20260923_portal_intake.py -q` → **55 passed in 19.00s**
- `.venv/bin/pytest tests/test_programme_20260923_portal_links.py -q` → **23 passed in 8.25s**
- `.venv/bin/pytest -q` (full suite) → **974 passed in 544.48s (0:09:04)** — green (919 before, +55)
- Browser proof, **part A / shipped state** (temp DB `/tmp/abi_tick_b2b.db`, shipped app on **5058**):
  **21/21 checks, 0 console errors, 0 horizontal overflow** (1440/1440 and 390/390) — `/portal/roodepoort`
  200 but **no form at all**, a POST returns 200 with the counter message and **created no customer (1 → 1)**,
  the lookup is closed too and reveals nothing, unknown slug 404.
- Browser proof, **part B / open state** (same DB, `/tmp/abi_b2_run_complete.py` on **5059** — the notice
  completed in a temp copy by the harness, `outstanding_fields: []`): **52/52 checks, 0 console errors** —
  the QR's page *is* the form, consent box **unticked**, honeypot off-screen (`-9999`) and out of the tab
  order (`-1`), full submission accepted → client created exactly once with `branch_id` = the QR's branch
  (**5 vs 5**), `source_system='portal'`, `source_id='roodepoort:ROO-85731A'`, email lower-cased, acceptance
  recorded (portal channel, notice v1.1); the same name+number then **does not** create a second record but
  asks "Is this you?" masked, `This is me` links it leaving the populated street address untouched
  (`9 Old Road`) and filling only the blank suburb, `None of these` creates a second row on purpose; a
  `link:` to a client the submission never matched is refused (400) and writes nothing; 404s for a
  switched-off branch and an unknown slug.
- `fuser -k 5058/tcp` / `5059/tcp` → both freed (only the unrelated 09:16 `app.py`, pid 558440, still on 5057).

**What the screenshots actually showed (`vision_analyze`, re-shot by this tick at 15:02–15:03):**
*1440px form* — "Sano Trailers" header, the blue `Customer registration` eyebrow, heading **Roodepoort**,
"Register your details once and the counter can pull them up next time.", "You are at the Roodepoort branch —
14 Hendrik Potgieter Road, Roodepoort.", then Full name \*, Cellphone number, Email address, Street address,
Suburb, Town or city, Province, Postal code, a full-width blue **Send my details**, and below it the
"Are you already registered with us?" check — **both boxes unticked**, the POPIA line reading "…in accordance
with POPIA. View Privacy Notice.", **no honeypot visible** (off-screen, as designed). *390px "Is this you?"* —
the question, "We may already have your details at Roodepoort. Nothing has been saved yet…", one candidate
**"Charmaine M. · …4567"** ("Matched on phone and email and name."), the "None of these is me — I am new here"
option, "Please tick the privacy box again…", and **no surname/email/address/balance anywhere**. *New-client
confirmation* — "Thank you, Pieter", "Your details are with Roodepoort…", reference **ROO-85731A**, branch
phone; **only the first name shown**. *390px lookup* — "Yes — we have you on our system as:" → "Pieter W. ·
…1234", masked only. *390px shipped state* — "Online registration isn't open yet", the counter sentence, the
branch link and phone, **zero form fields and no bracketed token anywhere**. Nothing clipped or overlapping at
either width.

**Where the plan's acceptance line does not hold (honest gap, same shape as A4's finding):** §B2's acceptance
says a created client appears on `/customers` "with the branch filter showing them under that branch".
`/customers` **has no branch filter** — there is no such control in `app/routes/customers.py` or
`templates/admin/customers/index.html` (the only branch reference on a client page is the read-only
`Branch` detail row on `detail.html:62`). What is true and is now proven: the record carries
`branch_id` = the branch whose QR/link was used (measured **5 vs 5**), staff see the client on `/customers`
(proof screenshot `staff_customers_1440.png`), and the submission's provenance names the branch slug. Making
clients branch-*scoped* is its own programme, not a §B2 detail — flagged for Don (second time; A4 flagged it).

**Decisions taken where the plan was silent (all pinned by tests):** the lookup route is **POST-only** (a
GET would put a client's name and number in history and logs); `/portal/<slug>` avoids the dict-method trap
the A4 tick hit by using `form_values=`/`error=`/`candidates=` names, never a key called `values`; the
`link:` target must be **one of this submission's own candidates**, so a crafted POST cannot write to an
arbitrary client id; a blocked client is refused on a strong (phone/email) match and answers "not found" in
the lookup; the honeypot returns the *success* page — a bot that is told "no" retries.

**Blockers / notes for Don:**
- **The form is SHUT on the shipped app right now, deliberately.** `docs/popia/PRIVACY-NOTICE.md` still
  carries Sano's five open facts, so `consent.notice_is_publishable()` is `False` and every public write path
  renders the "not open yet — please give your details to the counter staff" page (measured, 21/21). The full
  registration flow above was proven against a **harness copy** of the notice with synthetic values in
  `/tmp/abi_b2_popia/` — the repo's document was never touched. Your call is unchanged from tick 7: land the
  five facts, or accept an interim-notice consent (the record stores version + channel either way).
- **Port 5057 is still held** by the 09:16 `.venv/bin/python app.py` (pid 558440, cwd this repo, no
  `DATABASE_PATH` → the house dev DB). **Seventh tick flagging it.** This tick proved on 5058/5059 and left
  5057 alone; both of its own ports are free again. Say the word and a tick stops it.
- **`"Customer comms/"` was NOT staged** even though the main session's 15:01 note listed it: it is Sept-1
  scratch (`Telegram.txt`, `template.jpg`) and guardrail 2 names it explicitly as untracked junk that must
  stay out of commits. Also left untracked (main-session documents, not this phase's): 
  `docs/plans/2026-09-23-popia-setup-wizard.md`, `docs/plans/PROGRESS-popia-wizard.md`,
  `docs/popia/CLIENT-DATA-NOTICE-JACKAPP-SANO.md`; plus the source assets in
  `static/img/trailer-categories/` (phase 11 commits the web-sized re-encodes only).

**Must-know for tick 10 (phase 10 = §B3 admin QR/link page with A4 print):**
1. §B3's plan is `docs/plans/2026-09-23-branch-public-portal.md` **§B3**: `/settings/portal` listing every
   branch (name, `readonly` link input, Copy link, QR preview, "Print QR sheet", enable/disable + editable
   slug with 409 on a duplicate), `templates/admin/portal_index.html` + `portal_print.html` (standalone A4,
   `@page { size: A4; margin: 12mm }`, print media must hide all admin chrome), a nav entry, an
   `access.py` module mapping, and `tests/test_programme_20260923_portal_admin.py`.
2. `portal.all_portal_links()` already returns exactly the rows that page needs
   (`branch_id`, `name`, `slug`, `portal_url`, and the QR path is `/portal/<slug>/qr.png`).
3. **DB↔UI parity is owed here:** §B1's three columns (`public_slug`, `portal_enabled`, `portal_intro`) have
   **no admin UI yet** — §B3 owns that page, and this is the tick that must close the gap the phase-8 ledger
   recorded. `update_branch()` writes an explicit column list, so the existing branch form cannot clobber
   them (pinned by a test) — a rename must still **not** re-slug.
4. Consent/POPIA does not gate §B3: it is a staff-only settings screen. Keep the "not publishable" state
   visible there (a branch handed a printed QR whose form is shut should be obvious to staff, not a mystery).
5. Restart the smoke server after **any** template edit (Jinja is cached with `debug=False`) or the browser
   proof measures the previous page — tick 8's bug.
6. Ports: 5057 is still the 09:16 dev server; use 5058 (and 5059 only if a second app instance is needed)
   and `fuser -k` both afterwards.

### Phase 10 — B3 admin QR/link page + A4 print sheet + browser proof — status `done` (tick 10 — finishing a crashed slot's work)
**Branch:** `feature/abi-programme-2026-09-23` · **commit:** `045c4d9` (the §B3 work; this ledger entry is
its own commit, so the hash above is real) · **lock:** found stale — `.tick.lock` written **15:14:04**, 23
min old when this slot started at 15:37:02, so this tick recorded it, `rm`'d it and created its own
(15:37:11, removed at the end). Both of its own proof ports were `fuser -k`'d; **5057 is still held** (see
below).

**Provenance, stated plainly — this slot did not die, it ran out of budget.** The §B3 code was built by the
**15:13:56 slot**, which hit its ceiling mid-tick: `Turn ended: reason=max_iterations_reached(90/90) …
session=cron_b925af49c5a7_20260923_151356` at **15:35:52**, *after* writing its files (15:17–15:34) and
*after* launching the full suite in the background (pid 774855, started 15:35:31) — but **before its ledger
entry and commit**. That orphan pytest ran until 15:45:12 with its output going to a dead pipe, so it proved
nothing; this tick ignored it. The code was not trusted and not rebuilt: `compileall` was re-run, the phase's
own 28 tests were re-run, the **whole browser proof was re-run end to end (twice)** and **every screenshot was
looked at with `vision_analyze`** — the part the dead slot's 15:35:44 vision call only half-reached.

**One real defect found by looking, and fixed in this tick.** On the first re-run's 1440px screenshot the
`Link to share` field was cramped into a 220px flex slot beside the Copy button, so a ~330px card showed only
`https://sano-trailers.example/po` — the vision pass itself misread two cards' URLs (`…/r`, `…/po`), which is
exactly the "looks like the wrong link" failure that matters on a page staff copy links from. The link row is
now **stacked** (the URL input takes the full card width, Copy link sits under it) with `text-overflow:
ellipsis` and a `title` carrying the whole URL; `static/css/app.css` + `templates/admin/portal_index.html`
only. The proof was re-run after the change (fresh DB) and the screenshot re-read: every card now shows its
full URL (`https://sano-trailers.example/portal/roodepoort`) with no ellipsis and nothing clipped.

**Files (10, +986/−4):** `app/routes/settings.py` (+121 — `GET /settings/portal`,
`POST /settings/portal/<branch_id>`, `GET /settings/portal/<branch_id>/print`), `app/services/portal.py`
(+152 — `DuplicateSlugError`, `slug_owner`, `clamp_box_size`, `branch_address`, richer `all_portal_links`,
`update_portal_settings`), `app/routes/public.py` (the QR route's `?box=` knob, clamped 4–20), 
`app/services/access.py` (3 endpoints → `settings`), `templates/admin/portal_index.html` +
`templates/admin/portal_print.html` (new), `templates/admin/layout.html` (nav "Customer portal", gated on
`settings`), `templates/admin/settings/nav.html` (the tab), `static/css/app.css` (`.portal-*`),
`tests/test_programme_20260923_portal_admin.py` (new, 28 tests).

**What §B3 actually does:** one card per branch (`active DESC, name`) with a **Live / Portal off** badge that
distinguishes "the portal switch is off" from "the branch itself is inactive" — the one vague "off" §B1 left;
a readonly absolute link; **Copy link** which writes to the *real* clipboard (`navigator.clipboard` with an
`execCommand` fallback for a plain-http LAN address) and confirms on the page; the live QR preview; **Print QR
sheet** to a standalone A4 template (extends `base.html`, not `admin/layout.html`, so no app chrome can ever
print; `@page { size: A4; margin: 12mm }`, `@media print` hides `.no-print`); and the save. The save is where
the **DB↔UI parity gap phase 8 recorded is closed**: §B1's `public_slug` / `portal_enabled` / `portal_intro`
had no screen at all, and `update_portal_settings()` now writes exactly those three columns plus `updated_at`
— which is what lets it and the ordinary branch form coexist (a rename still does not re-slug, pinned by
test). A duplicate slug is **409** and *names the branch that owns the link*; a bad slug / over-long welcome
line is **400** and keeps what was typed instead of dumping staff back on a blank form; switching a branch off
takes its link, its QR preview and its print link away together, and the customer's URL **404s** (one switch,
decision D7's shape). The print route *refuses* a switched-off or inactive branch rather than printing a dead
code. D11 is kept visible to staff deliberately: while the notice carries Sano's five open facts the page
carries a plain amber banner ("Registration is not open yet … 5 facts on the notice are still open") and the
sheet carries a counter-copy note — **how many** facts, never **which**: the code's own docstring records
that the first draft of this screen quoted a stripped placeholder straight onto the counter sheet (`our
branches at ____ are covered by CCTV`), which is the exact leak D11 exists to prevent (that draft was never
committed, so it is quoted from `settings.py`'s comment, not re-measured here).

**Commands + REAL results (all run by this tick):**
- `python3 -m compileall app tests -q` → clean
- `.venv/bin/pytest tests/test_programme_20260923_portal_admin.py -q` → **28 passed in 21.27s** (re-run after
  the CSS fix → **28 passed in 25.88s**)
- `.venv/bin/pytest -q` (full suite, background) → **1002 passed in 647.44s (0:10:47)** — green (974 before,
  +28)
- Browser proof, **run 1** on a *reused* DB (`/tmp/abi_p10_verify.db`, seeded, app on 5058 + the
  notice-completed harness instance on 5059): **53/58** — all 5 failures traced to the DB, not the code: the
  dead slot's own run had already renamed Midrand's slug (`midrand-north`) and cleared `public_base_url`
  (part B does that itself), so the "absolute URL" checks saw `http://127.0.0.1:5058/…` and the rename was a
  no-op. Recorded rather than hidden, because a proof that only passes on a virgin database is a fact about
  the harness, not the product.
- Browser proof, **run 2** on a freshly seeded DB (`/tmp/abi_p10_verify2.db`, `fuser -k` 5058/5059, re-seeded,
  both instances restarted): **58/58 checks, 6 console errors — all six deliberate** (the 404s and the 409 the
  proof triggers on purpose); 0 unexpected errors; 0 horizontal overflow at **1440/1440** and **390/390**.
  Highights from the run: 6 cards / 6 branches; every QR preview really loaded (`naturalWidth` 410 each);
  every link input's `value` is the full absolute URL and `readOnly`; clipboard really received
  `https://sano-trailers.example/portal/roodepoort`; the sheet's QR is 492 px (302 CSS px) and in **print
  media the toolbar is gone**; switching Pretoria off → 302 + flash, card flips to "Portal off", QR preview
  and print link disappear, the customer's link **404s**; a duplicate slug → **409** with
  `/portal/roodepoort is already Roodepoort's link — pick a different word for Midrand.` and **nothing
  written**; a normalised slug saves → `/portal/midrand-north (saved in lower case with dashes)`, the old link
  404s and the QR follows the new slug; then the real flow on 5059 — the printed link opens the form, the
  submission is accepted (200), the customer gets "Thank you, Thandiwe" and **the client appears on
  `/customers`**.
- `fuser -k 5058/tcp 5059/tcp` → both freed.

**What the screenshots actually showed (`vision_analyze`, 15 screenshots in `/tmp/abi_p10_shots/`; the dead
slot's set is preserved at `/tmp/abi_p10_shots_deadslot_backup/`):** *1440px links page* — the dark sidebar
with "Customer portal" added between "Scan to return" and "Settings", breadcrumb **Settings**, H1 **Customer
portal**, the tab row with **Customer portal** active, the amber **"Registration is not open yet"** banner
(says *5 facts*, quotes **no** placeholder token), then six cards in a 3×2 grid: name + **Live** pill, address
(`229 Summit Road, Midrand · 010 221 1723`; the three seeded `Branch 1..3` say "No address recorded on this
branch yet."), **Link to share** with the **full URL now legible**, **Copy link**, a QR with the helper line
"Scan this to reach the form on any phone…", **Print QR sheet**, **Link slug**, the **Portal switched on**
checkbox, **Welcome line (optional)** and **Save portal settings** — no raw column names, no bracketed token.
*Copy state* — the Roodepoort button reads **Copied** with green "Link copied to your clipboard." *409 state*
— a pink strip above the content reading `/portal/roodepoort is already Roodepoort's link — pick a different
word for Midrand.`, the Midrand slug field still `midrand`, **no traceback, sidebar and all six cards intact**.
*Print-media sheet* — SANO TRAILERS logo, **Roodepoort**, `14 Hendrik Potgieter Road, Roodepoort`,
`011 002 0002`, "Scan to register your details before you hire.", a large clean black-on-white QR, the plain
URL `https://sano-trailers.example/portal/roodepoort` in monospace, the amber counter note, footer
`Sano Trailers · Customer registration` — **no sidebar, no toolbar, no breadcrumb, no browser chrome**, single
column inside the A4 safe area: a human would accept this as "print this on A4". *390px links page* — same
banner and cards stacked, everything inside the viewport (0 overflow), QR legible, nothing overlapping.
*390px sheet* — no overflow. *`/customers` after the real QR flow* — Thandiwe Nkosi present. (The
`sano-trailers.example` host in the screenshots is the seed's `public_base_url`, a temp-DB value, not a
product default.)

**Where the plan and the deliverable differ (honest gaps, no spin):** §B3's mandated proof says "against a
temp SQLite DB on 5057"; 5057 has been held all day by an unrelated 09:16 dev server, so this tick proved on
**5058/5059** instead — two instances, shipped state and notice-completed state, which is strictly more than
5057 would have given. The plan's "Print QR sheet" is a link to a print-optimised page (the browser's own
print dialog does the printing), not a server-side PDF — that is what the plan's file list specifies, and the
print-media screenshot is the proof it is printable; say the word if you want a real PDF endpoint.

**Blockers / notes for Don:**
- **The form is still SHUT on the shipped app, by design** (D11): `docs/popia/PRIVACY-NOTICE.md` still carries
  Sano's five open facts, so every public write path renders the counter message and the portal page says so
  in the amber banner. Unchanged ask from tick 7: land the five facts, or explicitly accept an interim notice.
- **Port 5057 is still held** by the 09:16 `.venv/bin/python app.py` (pid 558440, cwd this repo, no
  `DATABASE_PATH` → the house dev DB). **Eighth tick flagging it.** Say the word and a tick stops it.
- **Feature B (phases 8–10) is now complete and locally signed off.** The two follow-ups still worth Don's
  attention: (a) clients are not **branch-scoped** — `/customers` has no branch filter (flagged by A4 and B2,
  third time), and (b) `static/img/trailer-categories/` (11 MB of harvested Sano source photos) is still
  untracked; phase 11 commits only the web-sized re-encodes.
- Left untracked on purpose (not this phase's, guardrail 2 / main-session documents):
  `"Customer comms/"`, `app/__init__.py.backup`, `backups/`, `=1.18.0`, `test_invoice.pdf`, `.hermes/`,
  `docs/plans/2026-09-23-popia-setup-wizard.md`, `docs/plans/PROGRESS-popia-wizard.md`,
  `docs/popia/CLIENT-DATA-NOTICE-JACKAPP-SANO.md`.

**Must-know for tick 11 (phase 11 = §C1 store categories with photos + multi-trailer linking):**
1. §C1's plan is `docs/plans/2026-09-23-public-booking-and-store-categories.md` **§C1**. Read it with
   `docs/plans/reference-notes-trailerpro-bubblebounce.md` — §C1 is a reference-implementation phase.
2. **Guardrail 2 is live here:** `static/img/trailer-categories/` holds raw source material; commit only
   web-sized re-encodes (≤1200px, ~100–200 KB each), never the originals.
3. The admin portal page is the house pattern for a settings screen now: module-gated endpoints in
   `access.py`, a tab in `settings/nav.html`, one card per row, 409/400 instead of a redirect for a refused
   save. Reuse it rather than inventing a second style.
4. `portal.all_portal_links()` and `portal.branch_address()` are the shapes to copy if a category needs an
   address/QR-style panel; `all_portal_links()` now returns `enabled`, `portal_enabled`, `active`, `address`,
   `phone`, `intro`, `qr_path` — don't re-query branches separately.
5. Restart the smoke server after **any** template edit (Jinja is cached with `debug=False`) — tick 8's bug,
   and this tick hit it again when it re-proved after the CSS change.
6. Ports: 5058 is this phase's proven-good pair with 5059; 5057 is still the 09:16 dev server. Seed a **fresh**
   temp DB per proof run — reusing one makes state-dependent checks fail and looks like a regression (this
   tick's run 1).
