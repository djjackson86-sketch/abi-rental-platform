# Feature A — Staff licence-disk scanner → vehicle allocated to a client

**Ask (Don, 2026-09-23):** "incorporate scanning tool for staff (refer to trailerpro app) that they can scan a
license disk of a car and that pulls up car details on app and can be allocated to a certain client. This then
updates client page. See if we can also get towing capacity from disk."

**Reference:** TrailerPro's Walk-in licence work — `/mnt/d/Hermes/Trailerpro/Archive/Decode_SA_Drivers_Licence/`
(`app.py`, `decoder.py`, sample `Archive/pdf417.PNG`) and the `sa-licence-pdf417-scanner-rebuild` skill. That
work covers the **driver's licence card** (RSA-encrypted, 720-byte payload, 4 public keys in `keys/`). A
**vehicle licence disk** barcode is plain text, so only the image→barcode stage is shared, not the crypto.

Existing ABI pieces to imitate, not reinvent:
- `app/routes/admin.py:197` `scan_barcode()` — the house pattern for a scan screen (GET form, POST lookup,
  flash + redirect, `get_company_settings()` in the render call).
- `app/services/access.py` — `MODULES` list, staff default permission list (`MODULE_KEYS` default in
  `app/db.py` `staff_permissions_json`), and `_ENDPOINT_MODULE_RULES` (add `scan_vehicle`).
- `templates/admin/layout.html` — nav entry; `templates/admin/scan_barcode.html` — the page shell to copy.
- `app/services/customers.py` — `create_customer(form)`, `_clean()`, `customer_summary_for()`,
  `customer_has_history()`, `delete_customer()` (cascade discipline lives here).
- `tests/conftest.py` — fixture/app-client conventions used by all 37 existing test modules.

---

## A1 (phase 1) — Port the NaTIS disc parser + decode spike

**Objective:** land a Python parser that is a faithful port of the proven TypeScript one, and pick the
image→barcode engine by measurement against a real disc photo.

**The reference implementation (already written, already shipping in TrailerPro)**
`/mnt/d/Claude/trailer-rental-app/src/`:
- `lib/saDiscParser.ts` (379 lines) — **the parser to port.** Documents the three known disc layouts
  (`saDiscParser.ts:5-8`): (1) label-value pairs (`Make MITSUBISHI\nEngine number 4B11LC0187`),
  (2) delimited fields (pipe `|`, semicolon `;`, percent `%`, newline), (3) fixed-width positional (older
  discs). Merge precedence to preserve: label-value > percent-delimited > token/fixed-width
  (`saDiscParser.ts:341-357`), plus its `confidence: 'high' | 'low'` output and the "extracted — please
  verify before saving" UX.
- `lib/saPdf417Decode.ts` (216 lines) — browser decode via `@zxing/library` `BrowserPDF417Reader`.
- `lib/ocrUtils.ts` (242 lines) — Tesseract.js OCR of the disc face (expiry date, licence no.) — the
  fallback when the barcode will not decode. Treat as a **stretch goal**, not a requirement.
- `components/ui/PDF417Scanner.tsx` (451 lines) — the scanner overlay UI.
- The disc's full field set and the driver's-card/RSA path are documented in
  `docs/plans/reference-notes-trailerpro-bubblebounce.md` §1a–§1d — read §1b and §1d before you start.

**Files**
- Create `scripts/spike_disc_decode.py` — standalone (no app import): loops over every image in
  `tests/fixtures/disc/` and `~/disc-samples/`, tries the candidate engines (server-side `zxing-cpp` first,
  then `pdf417decoder`), prints per-engine results: decoded? payload length, first 200 chars, plus the
  decode time. Also accept a raw text file so a payload read by a phone scanner app can be pasted in.
- Create `docs/plans/disc-decode-findings.md` — the measured decision, with the raw output pasted in.
- Create `app/services/vehicle_disk.py` — public surface:
  - `decode_disc_image(data: bytes) -> list[str]` (raw barcode payloads; image-variant generation inside)
  - `parse_disc_text(text) -> dict` — **port of `saDiscParser.ts`**, returning the same keys it produces:
    `licence_number` (the NUMBER PLATE, e.g. `GP 12 XX GP`), `registering_authority`, `control_number`,
    `registration_number`, `make`, `model`, `colour`, `vin`, `engine_number`, `expiry_date` (YYYY-MM-DD),
    `tare_kg`, `gvm_kg`, `vehicle_type`, `confidence` (`high`/`low`), plus `unparsed_fields: dict` and the
    verbatim `raw_text` so staff can eyeball anything the parser did not recognise. Note there is **no**
    towing capacity / GCM in the payload (see master plan D3) — do not invent one.
- Create `tests/test_programme_20260923_vehicle_disk.py`.

**Steps**
1. **Look for a real disc sample first:** `tests/fixtures/disc/`, `~/disc-samples/`,
   `/mnt/d/Claude/trailer-rental-app/public/test-assets/` (the TrailerPro app had bundled test images),
   and `/mnt/d/Hermes/Trailerpro/Archive/`. If no *disc* image exists, STOP the "real photo" half, log
   `blocked: needs a real licence-disc photo from Don`, and do the parser + synthetic-payload work instead.
   Do **not** fake a sample, and do not substitute the driver's-card `pdf417.PNG` — it is a different payload.
2. **Port the parser test-first.** Write fixtures directly from `saDiscParser.ts`'s documented shapes: one
   label-value payload, one `%`-delimited, one `|`/`;`-delimited, one fixed-width, one junk payload
   (expect `confidence: 'low'` and an empty-but-not-crashing result). If Node is available, you may run the
   real `.ts` through a tiny node script to generate extra fixtures — that makes the port verifiable rather
   than approximated. Say in the findings doc which route you used.
3. **Spike the engine:** `.venv/bin/pip install zxing-cpp`, run `scripts/spike_disc_decode.py`, then try
   `pdf417decoder` only if `zxing-cpp` fails. Record install size, decode success/failure and timing.
   Decide server-side (`zxing-cpp`) vs a vendored browser ZXing bundle, and justify it from the measurement.
   Paste raw output into `docs/plans/disc-decode-findings.md`.
4. `decode_disc_image` must try variants (original, grayscale, contrast, 2x, top-40% crop, rotate 90°) and
   raise a distinct `DiscDecodeError` for "no barcode found" vs "decoded but parsed to nothing" — the UI
   must tell those two apart.
5. Run `.venv/bin/pytest tests/test_programme_20260923_vehicle_disk.py -v`, then commit. If the engine
   needed a new dependency, add the **exact pinned** line to `requirements.txt` and note the image-size cost
   for Render in the findings doc.

**Acceptance:** the findings doc contains real command output (or an honest `blocked` entry), the engine
choice is justified by measurement, and the parser round-trips a synthetic payload into expected fields.

---

## A2 (phase 2) — Vehicle data model

**Objective:** a `vehicles` table owned by the customer, with cascade delete and a service layer.

**Files**
- Modify `app/db.py` — add `CREATE TABLE IF NOT EXISTS vehicles (...)` to `SCHEMA` **and** the same
  `CREATE TABLE IF NOT EXISTS` to `run_migrations()`, plus `ensure_column` calls for any later-added column.
  Columns: `id`, `customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE`, `registration
  TEXT NOT NULL DEFAULT ''` (**the number plate**, e.g. `KP35XKGP` — the primary identifier), `make`, `model`,
  `year TEXT`, `vin`, `engine_number`, `colour`, `licence_number` (the disc's licence number, `4024048GB8LY`),
  `registration_number` (the **NaTIS** registration number, `SHS812W`), `control_number`,
  `registering_authority`, `vehicle_type`, `tare_kg REAL`, `gvm_kg REAL` (both optional, REAL NULL —
  **no towing capacity column at all**, master plan D3b), `licence_disk_expiry`,
  `raw_scan_text TEXT NOT NULL DEFAULT ''`,
  `source TEXT NOT NULL DEFAULT 'manual'`, `created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET
  NULL`, `created_at`, `updated_at`. Index on `customer_id`; unique partial index on
  `(registration)` where `registration <> ''` (one owner per vehicle, per decision D3/open-question 1).
  **Caveat:** SQLite `ON DELETE CASCADE` needs `PRAGMA foreign_keys=ON` — check how existing tables rely on
  cascade (`delete_customer` in `app/services/customers.py`) and delete vehicle rows explicitly if the
  connection does not enforce FKs, exactly like the existing customer-order cleanup does.
- Create `app/services/vehicles.py` — `list_vehicles(customer_id)`, `get_vehicle(id)`,
  `create_vehicle(form, customer_id=None)`, `update_vehicle(id, form)`, `delete_vehicle(id)`,
  `customer_for_vehicle_registration(registration)`, `vehicle_counts()`.
- Create `tests/test_programme_20260923_vehicles_model.py`.

**Steps (TDD)** — write the test, watch it fail, implement, watch it pass, commit. Cover: create/read/
update/delete; customers table untouched by vehicle writes; deleting a customer leaves **zero** orphan
vehicle rows; a second customer cannot claim a registration that already has an owner (ValueError);
blank registration allowed more than once (partial index); `tare_kg`/`gvm_kg` accept blank (NULL)
and never default to 0; migration re-adds the table on an existing DB.

**Acceptance:** new tests pass, `python3 -m compileall app tests -q` clean, and the full suite still passes
(`.venv/bin/pytest -q` — expect ~700 tests, ~7 minutes; if it is too slow for the tick, run the new file plus
`tests/test_app.py` and record that the full suite is due in the next tick).

---

## A3 (phase 3) — Staff scan screen + allocate to a client

**Objective:** staff open a page on a phone, capture the disk (or paste/type), review parsed fields, pick the
client, save — and the client's page immediately shows the vehicle.

**Files**
- Create `app/routes/vehicles.py` (blueprint `vehicles`) — mirror the structure of `app/routes/customers.py`.
  Routes: `GET/POST /scan-vehicle` (`admin`-style gate → module `scan_vehicle`), `POST /scan-vehicle/save`,
  `POST /vehicles/<id>/edit`, `POST /vehicles/<id>/delete`, `GET /customers/<id>/vehicles` (JSON for the
  client page panel) and `GET /api/customers/search?q=` (name/phone typeahead for the allocate step).
- Create `app/routes/vehicles.py` upload handling: `request.files['disk_image']` → bytes → `decode_disc_image`
  → `parse_disc_text`; guard size (e.g. 8 MB) and content type; never trust the filename (the app has **no**
  upload machinery today — this is the first, so keep it local, in-memory, no temp files).
- Modify `app/__init__.py` — register the blueprint.
- Modify `app/services/access.py` — add `("scan_vehicle", "Scan a vehicle licence disk")` to `MODULES` and
  `("vehicles.", "scan_vehicle")`, `("vehicles.scan_vehicle", "scan_vehicle")` to `_ENDPOINT_MODULE_RULES`.
- Modify `templates/admin/layout.html` — nav entry next to "Scan a barcode".
- Create `templates/admin/scan_vehicle.html` — mobile-first capture: `<input type="file" accept="image/*"
  capture="environment">`, paste-raw-text box, "Enter details manually" fallback; after decode, a review form
  with every parsed field editable + `unparsed_fields` shown read-only in a details block; client picker
  (typeahead on name/phone) with "Create new client" inline; explicit warning panel when the registration is
  already allocated to another client, offering "Transfer to this client" (explicit confirm, never silent).
- Modify `static/css/app.css` — panel/form styles consistent with existing tokens (`var(--danger)` etc.).
- Create `tests/test_programme_20260923_vehicle_scan_flow.py`.

**Acceptance:** tests cover — staff without `scan_vehicle` gets 403; a good decode + chosen client creates the
vehicle and it appears on that client's page; a decode that fails to find a barcode flashes the distinct
message; a decoded-but-unparseable payload still lets staff type the fields; a registration already owned by
another client is refused with the transfer option; customer delete still leaves no orphans.

---

## A4 (phase 4) — Client page panel + real-browser proof

**Objective:** the client page shows "Vehicles" (with the figures the disc actually carries — **no towing
capacity**, removed by decision D3b), staff can edit or release a vehicle there, and the whole feature is
proven in a real browser at 390px and 1440px.

**Files**
- Modify `templates/admin/customers/detail.html` (or the partial it includes) — a `Vehicles` panel: table of
number plate / make / model / year / disk expiry, an "Add vehicle" button into
`/scan-vehicle?customer_id=<id>`, edit + remove actions, and empty state copy. **No towing column** — see the
master plan's D3b (towing capacity was removed by Don on 2026-09-23).
- Modify `app/routes/customers.py:detail` — pass `vehicles=list_vehicles(customer_id)`.
- Modify `app/services/customers.py` only if the customer form help text needs the new panel link.
- Create `tests/test_programme_20260923_vehicle_client_page.py` — panel renders per client, is branch/scope
  correct (a depot account sees its own client's vehicles only), an unrecorded tare/GVM shows blank rather than
  0 (there is no towing field to assert — D3b removed it), and the removal action leaves the customer intact.

**Browser proof (mandatory, this is how Don judges UI work)**
- Use `.venv/bin/playwright`'s Chromium (already installed in `~/.cache/ms-playwright`) against a **temp
  SQLite DB** (`DATABASE_PATH=/tmp/abi_a4.db`, seeded with 1 branch, 1 client, 1 staff account) on port 5057.
- Sign in as staff, run the full path: open `/scan-vehicle` → paste a decoded-text fixture (the real photo if
  one exists, else the fixture) → pick the client → save → open the client page → see the vehicle → edit the
  model and a mass → save → see the new figures. Capture screenshots at 1440px and 390px, click through,
  assert **0 console errors** and **0 horizontal page overflow** at both widths.
- `vision_analyze` the screenshots and say in the ledger what you actually saw (this is a hard requirement —
  do not describe a page you did not look at).
- `fuser -k 5057/tcp` afterwards; confirm the port is free.

**Acceptance:** panel works, tests pass, screenshots inspected with vision, ledger records the real
observations, and Feature A is marked signed off locally (no push, no deploy).
