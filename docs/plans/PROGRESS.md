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
| 2 | A2 vehicle data model + service | pending | | |
| 3 | A3 staff scan UI + allocate to client | pending | | |
| 4 | A4 client page panel + towing capacity + browser proof | pending | | |
| 5 | B1 branch portal schema + link + QR | pending | | |
| 6 | B2 public form + dedupe + safe lookup | pending | | |
| 7 | B3 admin links + A4 print sheet + browser proof | pending | | |
| 8 | C1 store categories with photos + bulk linking | pending | | |
| 9 | C2 multi-trailer public booking flow | pending | | |
| 10 | C3 full suite + end-to-end local proof + close-out | pending | | |

Status values: `pending` · `in-progress` · `done` · `partial` · `blocked`.

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

