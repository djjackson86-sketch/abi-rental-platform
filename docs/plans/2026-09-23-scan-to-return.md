# Feature D — Staff scan-to-return (scan a disc → the rental is marked returned)

**Ask (Don, 2026-09-23):** "Staff should also be able to scan in to return the trailer (either the trailer disc
or the corresponding car disc) — this then marks it as returned in the admin side."

**Why this is tractable:** feature A already puts a **customer vehicle** in the app (plate, VIN, engine
number) and phase 1 already decodes a NaTIS disc. This feature is the reverse lookup — a scanned plate has to
find the *open rental* it belongs to — plus a call into the return flow that already exists.

**Authoritative field mapping (from Don, 2026-09-23 — do not re-derive, do not guess):**

| Disc field (148-char payload) | Meaning | ABI column |
|---|---|---|
| `NB72XMGP` | **the number plate** (licence plate) | the primary identifier staff see |
| `QWR419V` | **NaTIS registration number** (Don: "Natis reg is last one") | `registration_number` |
| `5120367QP4HD` | the disc's licence number (by elimination — the disc's own *Lisensienommer*) | `licence_number` |

**Existing app pieces to reuse — never reimplement:**
- `TRANSITIONS["return"] = {"from": {"started"}, "to": "returned", "message": "Order returned"}`
  (`app/services/orders.py:1133`) and `transition_order(order_id, action)` (`orders.py:1292`), reached from the
  generic `@bp.post("/<int:order_id>/<action>")` route at `app/routes/orders.py:676`.
- The rest of the return workflow stays exactly where it is and is **not** shortcut by a scan:
  `update_return_checklist()` (`orders.py:1496`, route `orders.py:516`), `add_return_charges`,
  `settle_return_deposit`, `use_return_deposit`, `revise_started_return`, `return_damage_total`.
  A scan marks the order returned and sends staff to the order page to finish the checklist/deposit work.
- Product identity for trailers: `products` already has `sku`, `wheel_size`, `branch_id`; the inventory form is
  `templates/admin/inventory/form.html`, and `_clean()` in `app/services/products.py` decides what is stored.
- Session/branch scoping: reuse the shared `order_branch_clause` / `product_branch_clause` used elsewhere
  (`app/services/spare_wheels.py` shows the pattern) so a depot account can only return its own branch's rentals.

---

## D1 — Trailer identity + the return-matching service

**Objective:** a scanned disc can be resolved to the open rental(s) it could belong to, with the reason for each
match shown, and nothing guessed when it is ambiguous.

**Files**
- Modify `app/db.py` — additive columns for trailer identity (blank defaults, so every existing product is
  unaffected): `products.registration TEXT NOT NULL DEFAULT ''` (the plate), `products.licence_number TEXT NOT
  NULL DEFAULT ''` (disc licence number), `products.registration_number TEXT NOT NULL DEFAULT ''` (NaTIS reg).
  Partial unique index on `products(registration) WHERE registration <> ''` so one plate cannot sit on two
  trailers. Same `ensure_column` treatment in `run_migrations()`.
- Modify `app/services/products.py` — `_clean()` keeps the three fields when the post carries the inventory
  form's marker (copy the `maintenance_panel` marker pattern from ABI-341953038 so a sale/service save or a
  legacy post cannot blank them); `update_product` passes the existing values through.
- Modify `templates/admin/inventory/form.html` — a "Trailer identification" section for rental items only:
  number plate, licence number, NaTIS registration number, with help text explaining that these are what a
  disc scan will match against.
- Create `app/services/returns.py`:
  - `match_open_rentals(parsed: dict, session_scope) -> list[dict]` — candidates, each with `order_id`,
    `order_number`, `customer_name`, `matched_on` (`trailer_plate` / `customer_vehicle_plate` /
    `vehicle_vin` / `vehicle_engine`), and `evidence` (the actual value matched). Resolution order:
    1. **trailer plate** → a `products` row whose `registration` (or `licence_number`/`registration_number`)
       equals the scanned value → the `started` orders holding that product via `order_items`;
    2. **customer vehicle plate** → a `vehicles` row from feature A → that customer's `started` orders;
    3. **VIN / engine number** → the same vehicle lookup as a secondary signal.
    Only `started` orders are returnable (`TRANSITIONS["return"]["from"]`), so anything else is listed as
    "not returnable yet" rather than offered.
  - `returnable_order(order_id, session_scope)` — the guard the UI calls before posting (raises with a plain
    message when the order is not `started`, not in scope, or already returned).
  - `mark_returned_via_scan(order_id, user_id, parsed)` — records the audit trail (new additive columns
    `orders.return_scan_at TEXT NOT NULL DEFAULT ''`, `orders.return_scan_registration TEXT NOT NULL DEFAULT ''`,
    `orders.return_scan_source TEXT NOT NULL DEFAULT ''` ∈ `trailer_disc` / `vehicle_disc`) and then calls the
    existing `transition_order(order_id, "return")`. **Do not duplicate any transition logic** — if the
    existing call refuses, surface its message verbatim.
- Create `tests/test_programme_20260923_returns_match.py`.

**Steps (TDD)** — a scanned trailer plate finds the started order that holds trailer X; a scanned customer-car
plate finds that customer's open rental; an ambiguous scan (two started orders / a car with two open rentals)
returns two candidates and marks nothing; a reserved (not yet picked up) order is reported as not returnable;
an unknown plate returns an empty list with a clear message; another depot's rental is out of scope for a
branch-limited account; the audit columns are written only by `mark_returned_via_scan`; inventing no match is
better than guessing — assert the empty case explicitly.

**Acceptance:** matching is scope-correct and ambiguous-safe, `transition_order` untouched, new tests pass and
`python3 -m compileall app tests -q` is clean.

---

## D2 — The scan-to-return screen + real-browser proof

**Objective:** staff scan a disc on a phone and the rental comes back on the admin side, with the return
checklist still there to finish.

**Files**
- Create `app/routes/returns.py` (blueprint `returns`) — mirror `app/routes/vehicles.py` from A3 so the two scan
  screens behave identically, and register it in `app/__init__.py`:
  - `GET /scan-return` — mobile-first capture page: `<input type="file" accept="image/*" capture="environment">`,
    a paste-the-barcode-text box and a "type the plate" box (so a wet/damaged disc never blocks a return).
  - `POST /scan-return` — decode → `match_open_rentals()` → render the review screen. **One candidate:** show it
    with its evidence and a single "Mark returned" button. **Two or more:** list them and require an explicit
    choice. **None:** say what was scanned and link to the Orders list filtered to started orders.
  - `POST /scan-return/confirm` — guard with `returnable_order()`, call `mark_returned_via_scan()`, flash
    `"<Trailer/vehicle plate> returned via disc scan"` and redirect to the order detail page so the existing
    checklist/deposit flow continues.
- Create `templates/admin/scan_return.html` (capture + review + confirm states) and add a nav entry beside
  "Scan a vehicle licence disk" in `templates/admin/layout.html`.
- Modify `app/services/access.py` — module key `scan_return` ("Scan to return a trailer") in `MODULES`, plus the
  `returns.` endpoint rule (a depot account without it must get 403).
- Modify `templates/admin/orders/detail.html` only if the audit trail needs surfacing: a small line
  "Returned via disc scan — <plate>, <date>" when `return_scan_at` is set.
- Create `tests/test_programme_20260923_scan_return_flow.py` — the full loop (scan → confirm → status becomes
  `returned`, `picked_up_at` and stock behaviour unchanged), 403 without the module, ambiguous case refuses to
  act until a choice is posted, a crafted POST for an out-of-scope/not-started order is refused with the
  existing message, the audit columns land, and the double-scan case (scanning the same disc twice) reports
  "already returned" instead of quietly doing nothing.

**Browser proof (mandatory)** — Chromium against a temp SQLite DB on 5057 (seeded: 2 branches, 1 trailer with a
plate, 1 customer with a scanned vehicle, 1 `started` order): scan the **trailer** disc fixture → return it →
status reads Returned → scan the **car** disc for a second started order → return it → status reads Returned;
screenshot the capture, review and post-return states at 1440px and 390px, assert 0 console errors and 0
horizontal overflow, inspect every screenshot with `vision_analyze`, and report what was actually seen.
Finish with `fuser -k 5057/tcp`.
