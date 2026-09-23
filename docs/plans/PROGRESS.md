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
| 4 | A4 client page vehicles panel + browser proof | pending | | |
| 5 | D1 trailer identity + return-matching service | pending | | |
| 6 | D2 scan-to-return screen + marks returned + proof | pending | | |
| 7 | B1 branch portal schema + link + QR | pending | | |
| 8 | B2 public form + dedupe + "am I already a customer?" | pending | | |
| 9 | B3 admin QR/link page with A4 print + browser proof | pending | | |
| 10 | C1 store categories with photos + multi-trailer linking | pending | | |
| 11 | C2 multi-trailer public booking flow | pending | | |
| 12 | C3 full-suite + end-to-end local proof + close-out | pending | | |

Status values: `pending` · `in-progress` · `done` · `partial` · `blocked`.

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
identifier `MVL1CC53` (wrong) and `colour` was empty; after it `engine_number = 4B11LC0187`,
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
4. **Open for Don:** which of the disc's three identifier fields (`4024048GB8LY` / `KP35XKGP` / `SHS812W`) is
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
  `KP35XKGP` = **number plate**, `SHS812W` = **NaTIS registration number** ("Natis reg is last one"),
  `4024048GB8LY` = the disc's licence number (by elimination — the disc's own *Lisensienommer*). Written into
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
- **Plates are normalised on write** (`kp 35 xkgp` → `KP 35 XKGP`) and compared on a whitespace-free
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
  `created id=1 registration='KP 35 XKGP' registration_number='SHS812W' licence_number='4024048GB8LY'
  expiry='2027-03-31' tare=None gvm=None source='scan'` · `owner of 'KP35XKGP' -> Smoke Client` ·
  `counts: {'total': 1, 'scan': 1, 'manual': 0, 'import': 0, 'blank_registration': 0}` ·
  `duplicate refused: Registration KP35XKGP is already recorded for Smoke Client — transfer it
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
   licence number (`4024048GB8LY`), field 6 = plate (`KP35XKGP`), field 7 = NaTIS registration number
   (`SHS812W` — Don's "Natis reg is last one")**; the synthetic fixture
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

    licence_number = 'KP35XKGP' | disc_licence_number = '4024048GB8LY' | registration_number = 'SHS812W'
    make = 'MITSUBISHI' model = 'ASX' colour = 'WHITE' engine_number = '4B11LC0187'
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
