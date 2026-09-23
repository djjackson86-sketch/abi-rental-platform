# Licence-disc decode — findings (programme phase 1 / A1, 2026-09-23)

**Engine decided: server-side `zxing-cpp`. Decision D2 is now settled by measurement on a REAL
disc photograph — not by preference.**

**Parser landed:** `app/services/vehicle_disk.py` — a field-by-field-verified port of TrailerPro's
`saDiscParser.ts`, plus a positional branch for the modern layout (below), plus the two failure
kinds the scan UI needs to tell apart.

---

## 1. The real sample

`/home/djjac/disc-samples/disc-2026-09-23.jpg` (720×1280, 196 KB) — photographed by Don, top of a
SA *"MOTORVOERTUIGLISENSIE EN LISENSIESKYF"* disc: heading, the PDF417 band, and the upper field
labels. `vision_analyze` on it reads the heading, the Afrikaans title, and confirms a dense
2D/PDF417-style barcode with solid end bars (it cannot read the values — the disc's value fields
are cropped out of the photo, which is exactly why the barcode path matters).

The photo is **deliberately not committed**: it carries a real number plate, VIN and engine
number. The committed fixtures are synthetic mirrors of the same *structure* (see §4).

## 2. The measurement — both engines, same images, same variant ladder

`.venv/bin/python scripts/spike_disc_decode.py --known-samples --budget 150` (full output: the
script re-runs it; the run behind these numbers took ~40 s wall clock).

```
engine   : zxing-cpp -> installed
engine   : pdf417decoder -> installed
variants : 11 (original, grayscale, contrast-1.8, contrast-2.5, upscale-150, upscale-2x,
              upscale-3x-gray-contrast, topcrop-40, middlecrop-70, rotate90, rotate270)

== SUMMARY ==
  zxing-cpp: 2/12 image(s) decoded
      disc-2026-09-23.jpg: DECODED (original, 0.022s)      <-- the real disc
      pdf417.PNG: no decode (1.345s)
      087a197e44f0c426e494c68a7f36874aaae62d9d.jpeg: DECODED (upscale-150, 0.321s)
      ... 9 other driver's-licence photos: no decode
  pdf417decoder: 3/12 image(s) decoded
      disc-2026-09-23.jpg: DECODED (original, 0.251s)      <-- the real disc, 11x slower
      pdf417.PNG: DECODED (original, 1.181s)               <-- driver's-licence card (binary)
      sa-license-test.jpeg: DECODED (upscale-150, 2.421s)  <-- driver's-licence card (binary)
```

Both engines decode the real disc **on the original image, first try** — no crop, no upscale. The
only images where `pdf417decoder` wins are two *driver's-licence cards*, whose payloads are
720-byte RSA blobs ("binary-or-non-text", len=677) and are **not** ABI's use case: ABI scans the
vehicle disc, which is plain text.

**Decision, therefore:**

| | zxing-cpp | pdf417decoder |
|---|---|---|
| Real disc | **yes, 22 ms** | yes, 251 ms |
| Wheel | ~1.1 MB (2.0 MB installed) | 36 KB + numpy + opencv-python ≈ **115 MB installed** |
| Extra wins | — | 2 driver's-licence cards (irrelevant) |

`zxing-cpp==3.1.1` + `Pillow==12.3.0` are what `requirements.txt` ships. `pdf417decoder` is
**deliberately not a dependency** — it stays supported in `app/services/vehicle_disk.py` as an
optional second engine (the engine loop skips an uninstalled engine instead of failing), so a dev
venv can `pip install pdf417decoder` if a stubborn photo ever needs it. That removes ~115 MB from
the Render image for zero loss on the real input.

The variant ladder is kept regardless: on the reference corpus `upscale-150` was what saved a
glare-heavy phone photo, and a badly-lit counter photo is the realistic failure mode.

## 3. The real payload is POSITIONAL, and the port handles it

```
%MVL1CC53%0148%4522A001%1%4024048GB8LY%KP35XKGP%SHS812W%Station wagon / Stasiewa%MITSUBISHI%ASX%White / Wit%JMYXTGA2WDZ000956%4B11LC0187%2027-07-31%
```

148 characters, `%`-delimited, **no labels and no `|`/newline separators** — so it cannot be
sniffed by field content alone. What *is* self-describing is field 2: `0148` is the record's own
length. `parse_natis_positional()` uses that as a hard gate (plus a 17-char VIN and a parseable
expiry) and only then treats the layout as positional. Anything that fails the gate falls through
to the reference heuristics unchanged, so older/other disc eras keep working.

Measured parse of the **real 148-char payload** before and after that branch:

| field | reference sniffer only | with the positional branch |
|---|---|---|
| `licence_number` | `KP35XKGP` | `KP35XKGP` |
| `make` / `model` | `MITSUBISHI` / `ASX` | `MITSUBISHI` / `ASX` |
| `colour` | *(empty)* | **`WHITE`** |
| `vin` | `JMYXTGA2WDZ000956` | `JMYXTGA2WDZ000956` |
| `engine_number` | **`MVL1CC53`** (the form identifier — wrong) | **`4B11LC0187`** |
| `expiry_date` | `2027-07-31` | `2027-07-31` |
| `vehicle_type` | `STATION WAGON` | `STATION WAGON` |
| `tare_kg` / `gvm_kg` | `None` / `None` | `None` / `None` |
| `confidence` | `high` | `high` |

Two consequences worth knowing:

1. **No tare, no GVM anywhere in this payload.** A modern disc simply does not carry the mass
   fields `saDiscParser.ts` looks for (and never a towing capacity — D3 stands). So `tare_kg` and
   `gvm_kg` are blank-by-default in phase 2 and `towing_capacity_kg` is strictly staff-typed.
2. **The three identifier fields are NOT guessed.** `4024048GB8LY`, `KP35XKGP`, `SHS812W` are
   three identifiers in a row (the printed labels are *Voertuigregistrasienommer*, *Lisensie…*,
   *Voertuigidentifikasienommer*); the counter's plate regex takes the one that looks like a Gauteng
   plate (`KP 35 XK GP`) as the licence number and the other two are returned in `unparsed_fields`
   with the reason, for a staff member to place. Which is which needs a **full disc face** — open
   question for Don, and the code never guesses (D3).

## 4. Parser parity — verified against the live TypeScript, not approximated

`scripts/disc_parity_ts.mts` runs the *actual shipping* `saDiscParser.ts`
(`/mnt/d/Claude/trailer-rental-app/src/lib/`, read-only, outside this repo) over every fixture and
writes `tests/fixtures/disc/expected_ts.json`. `tests/test_programme_20260923_vehicle_disk.py`
then asserts our port matches it field-by-field for `labelvalue`, `percent`, `pipesemi`,
`fixedwidth` and `junk`, and separately asserts the *deliberate* divergences.

```
$ /home/djjac/.hermes/hermes-agent/node_modules/.bin/tsx scripts/disc_parity_ts.mts
wrote tests/fixtures/disc/expected_ts.json (6 fixtures): fixedwidth, junk, labelvalue,
natis_positional, percent, pipesemi

$ .venv/bin/pytest tests/test_programme_20260923_vehicle_disk.py -q
51 passed in 5.37s
```

Deliberate differences from the reference (all documented in the module, all tested):

| # | Difference | Why |
|---|---|---|
| 1 | `tare_kg`/`gvm_kg` are `float \| None`, not strings | phase 2 stores REAL columns; blank must be NULL, never 0 |
| 2 | label-value masses need `(\d{3,5})(?!\d)` and 300-80000 kg | measured: the reference turns `GVM 1234567` into `12345`. D3 = never invent a figure; the guard can only blank a value |
| 3 | `registering_authority`, `control_number`, `registration_number` exposed | the reference parses them into locals and never surfaces them; A1's spec asks for them |
| 4 | positional branch for the modern layout | §3 — the real disc. Guarded by the record's own length self-check |
| 5 | `unparsed_fields` + `raw_text` returned | the scan screen shows staff anything unplaced before saving |

Carried over **unchanged and on purpose** (measured, flagged rather than silently "fixed"):

* the percent branch assigns the NaTIS registration number to `engine_number` when it appears
  before the engine number (`percent` fixture: `T9876543210`) — the positional gate covers the
  real modern layout, older percent payloads keep reference behaviour;
* the documented "fixed-width positional" layout has **no positional parser** in the reference:
  spaces are not separators, so a space-separated payload arrives as one token and only the
  plate/VIN/engine regexes fire (measured on `fixedwidth`: make, model, colour, expiry and both
  masses are empty). ABI carries this over; the type-the-fields fallback covers it.

## 5. What the engine layer gives the UI (phase 3)

`decode_disc_image()` raises `DiscDecodeError` with distinct `kind`s, and they are tested:

* `image_unreadable` — the bytes are not an image;
* `no_barcode` — no PDF417 found in any variant (message tells staff to move closer/flatter);
* `unparseable` — a barcode WAS read but held no disc fields (a different message, and the UI
  still offers manual entry).

Plus the two mandatory fallbacks from D2 are cheap to add because `parse_disc_text` never raises:
paste the raw barcode text, or type the fields.

## 6. Reproduce

```bash
cd /mnt/d/Hermes/abi-rental-platform
/home/djjac/.hermes/hermes-agent/node_modules/.bin/tsx scripts/disc_parity_ts.mts   # fixtures
.venv/bin/pytest tests/test_programme_20260923_vehicle_disk.py -v                    # 51 tests
.venv/bin/python scripts/spike_disc_decode.py --known-samples                        # the spike
.venv/bin/python scripts/spike_disc_decode.py --text /tmp/barcode.txt                # pasted payload
```

`test_real_disc_photo_decodes_and_parses_end_to_end` runs photograph → barcode → fields and
**skips cleanly** when `~/disc-samples/disc-2026-09-23.jpg` is absent, so the suite is green on a
machine without Don's photo.

## 7. Open for Don / next tick

1. **Which identifier is which** (`4024048GB8LY` vs `KP35XKGP` vs `SHS812W`) — needs a photo of a
   full disc face, or your word for it. Until then the two unused ones are surfaced for staff.
2. **Towing capacity source** (master-plan open question 1) — confirmed absent from the payload;
   the field stays staff-typed with help text.
3. **pdf417decoder** — dropped from `requirements.txt` (−115 MB). Say the word if you would rather
   keep the second engine installed in production.

### Correction to / note on `~/disc-samples/disc-decode-FINDINGS.md`

That note (written beside the photo) is right about the headline — zxing-cpp succeeds on the real
disc, the heavy engine is unnecessary, the layout is positional, there is no mass data — and this
tick follows it on all four. Two of its details did **not** reproduce here, and the difference
matters for the port:

* It says the plate regex "scans `4024048GB8LY` and matches the inner `GB8LY`". Measured: it does
  not — `\b` blocks that match (the `8` before `G` is a word character), so the plate lands
  correctly on `KP35XKGP`. The percent branch is wrong about other things, not this one.
* It says the `\d{3,5}` mass rules "can fire on unrelated tokens". Measured: they do not on this
  payload — the only bare number, `0148`, is rejected by the 300-80000 kg range check.
