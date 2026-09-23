# Reference Notes — TrailerPro & Bubblebounce implementations for ABI Rental Platform

Grounded notes on three existing implementations ABI Rental Platform must imitate.
All paths and line numbers below were inspected directly on the WSL host (`djjac`).
No repository was modified. Nothing below is reconstructed from memory — every claim
traces to a file that exists at the path given.

Sources inspected:

- **TrailerPro live app source (Next.js):** `/mnt/d/Claude/trailer-rental-app`
- **TrailerPro standalone licence-decoder archive (Flask):** `/mnt/d/Hermes/Trailerpro/Archive/Decode_SA_Drivers_Licence`
- **Bubblebounce staff app (single-file HTML):** `/mnt/d/Hermes/Bubblebounce/bubblebounce_6/index.html`
- **ABI Rental Platform (target):** `/mnt/d/Hermes/abi-rental-platform` (Flask + Jinja + SQLite/Turso)

---

## 1. SA vehicle/driver licence PDF417 barcode scanning (TrailerPro)

There are **two** separate implementations, and they are not the same thing:

- A standalone **Flask app** that decodes the **driver's licence card** (back-side PDF417 → RSA decrypt → fields).
- The **live Next.js app** that scans the **vehicle licence disc** (NaTIS barcode, plain text) and does **OCR** on the disc/plate.

### 1a. Driver's licence card decoder (standalone Flask app)

Location: `/mnt/d/Hermes/Trailerpro/Archive/Decode_SA_Drivers_Licence/`

| File | Purpose |
|---|---|
| `app.py` (170 lines) | Flask UI + JSON endpoints + diagnostics |
| `decoder.py` (365 lines) | PDF417 image decode + RSA decrypt + field parse |
| `templates/index.html` (151 lines) | Mobile-friendly upload / sample test page |
| `requirements.txt` | Runtime deps |
| `keys` (file) | Notes on RSA key material |
| `QUICK_WEBAPP.md`, `README.md`, `RENDER.md` | Docs |

**Decoding library:** `pdf417decoder` (PyPI, v1.0.8). Imported in `decoder.py`:

```python
# decoder.py:10-11
from pdf417decoder import PDF417Decoder
from PIL import Image, ImageEnhance, ImageOps
```

`requirements.txt` (full):

```text
Flask>=3.0,<4
Pillow>=10,<12
pdf417decoder>=1.0.8
```

**RSA decryption.** The SA licence card barcode payload is RSA-encrypted. The module
hardcodes four RSA public keys and decrypts the 720-byte payload block-by-block
(5 × 128-byte blocks + 1 × 74-byte block):

```python
# decoder.py:13-38 (abridged — V1/V2 magic bytes + RSA public keys)
V1 = bytes([0x01, 0xE1, 0x02, 0x45])
V2 = bytes([0x01, 0x9B, 0x09, 0x45])

PK_V1_128 = """
-----BEGIN RSA PUBLIC KEY-----
MIGXAoGBAP7S4cJ+M2MxbncxenpSxUmBOVGGvkl0dgxyUY1j4FRKSNCIszLFsMNwx2XWXZg8H53gpCsxDMwHrncL0rYdak3M6sdXaJvcv2CEePrzEvYIfMSWw3Ys9cRlHK7No0mfrn7bfrQOPhjrMEFw6R7VsVaqzm9DLW7KbMNYUd6MZ49nAhEAu3l//ex/nkLJ1vebE3BZ2w==
-----END RSA PUBLIC KEY-----
""".strip()
# … PK_V1_74, PK_V2_128, PK_V2_74 follow …
```

```python
# decoder.py:161-187 (abridged)
def decrypt_data(data: bytes) -> bytes:
    header = data[:6]
    pk128 = PK_V1_128
    pk74 = PK_V1_74
    if header[:4] == V2:
        pk128 = PK_V2_128
        pk74 = PK_V2_74
    all_bytes = bytearray()
    n128, e128 = _parse_pkcs1_pubkey(pk128)
    start = 6
    for _ in range(5):
        block = data[start : start + 128]
        value = int.from_bytes(block, byteorder="big", signed=False)
        output = pow(value, e128, n128)
        all_bytes += output.to_bytes(128, byteorder="big", signed=False)
        start += 128
    n74, e74 = _parse_pkcs1_pubkey(pk74)
    block = data[start : start + 74]
    value = int.from_bytes(block, byteorder="big", signed=False)
    output = pow(value, e74, n74)
    all_bytes += output.to_bytes(74, byteorder="big", signed=False)
    return bytes(all_bytes)
```

**Fields extracted** (the `DrivingLicense` dataclass, `decoder.py:41-62`):

```python
# decoder.py:41-62
@dataclass
class DrivingLicense:
    vehicleCodes: list[str]
    surname: str
    initials: str
    PrDPCode: str
    idCountryOfIssue: str
    licenseCountryOfIssue: str
    vehicleRestrictions: list[str]
    licenseNumber: str
    idNumber: str
    idNumberType: str
    licenseCodeIssueDates: list[str]
    driverRestrictionCodes: str
    PrDPermitExpiryDate: str
    licenseIssueNumber: str
    birthdate: str
    licenseIssueDate: str
    licenseExpiryDate: str
    gender: str
    image_width: int
    image_height: int
```

The parser walks the decrypted payload using nibble/date delimiters (`parse_data`,
`decoder.py:254-327`), keyed on the `0x82` marker, and extracts licence number, 13-char
ID number, dates, gender, and image dimensions. `decode_and_parse_image` (line 330) wraps
the whole pipeline and returns `asdict(parsed)` plus `raw_barcode_base64` and `attempts`.

**UI capture & submit (this app).** `templates/index.html` is a plain HTML form that posts
a file (or a hidden `mode=sample`) to `/decode`; the JSON API is `/api/decode`:

```python
# app.py:127-140 (abridged — JSON API)
@app.post("/api/decode")
def api_decode():
    mode = request.form.get("mode", "upload")
    if mode == "sample":
        if not SAMPLE_IMAGE.exists():
            return jsonify({"ok": False, "error": f"Sample image missing: {SAMPLE_IMAGE}"}), 404
        return jsonify(decode_and_parse_image(SAMPLE_IMAGE))
    upload = request.files.get("image")
    if not upload or not upload.filename:
        return jsonify({"ok": False, "error": "Missing image upload"}), 400
    suffix = Path(upload.filename).suffix or ".png"
    result = decode_upload_bytes(upload.read(), suffix=suffix)
    return jsonify(result), (200 if result.get("ok") else 422)
```

The HTML file input uses `capture="environment"` for the rear camera:

```html
<!-- templates/index.html:84-88 -->
<form method="post" action="/decode" enctype="multipart/form-data">
  <input type="hidden" name="mode" value="upload">
  <input type="file" name="image" accept="image/png,image/jpeg,image/webp,image/bmp" capture="environment">
  <button type="submit">Upload and decode</button>
</form>
```

**Explicit limitation** (`QUICK_WEBAPP.md:9`): *"It does **not** test front-of-card OCR or vehicle licence discs."* So this Flask app is **driver's-licence-card only**.

### 1b. Vehicle licence disc scanning (live Next.js app)

Location: `/mnt/d/Claude/trailer-rental-app/src/`

The **live** TrailerPro app scans **vehicle licence discs** (not driver cards). Relevant files:

| File | Purpose |
|---|---|
| `components/ui/PDF417Scanner.tsx` (451 lines) | Camera/file scanner overlay component (ZXing) |
| `lib/saPdf417Decode.ts` (216 lines) | Browser PDF417 decode via ZXing |
| `lib/saDiscParser.ts` (379 lines) | Parse NaTIS **disc** barcode text (plain text) |
| `lib/ocrUtils.ts` (242 lines) | Tesseract.js OCR (disc + number plate) |
| `lib/saLicenceLocalDecode.ts` (26 lines) | **Stub** — driver-card RSA decode is NOT wired in the Next.js build |

**Decode library (browser):** ZXing — `@zxing/library` `BrowserPDF417Reader` (dynamic import):

```ts
// lib/saPdf417Decode.ts:90-92
async function decodeImageUrlWithJs(url: string, timeoutMs = 15000): Promise<string> {
  const { BrowserPDF417Reader } = await import('@zxing/library');
  const reader = new BrowserPDF417Reader();
```

Deps in `package.json`: `@zxing/library ^0.21.3`, `tesseract.js ^7.0.0`, `zxing-wasm ^3.0.2`.

**Driver-card local decode is a stub** — the RSA/720-byte logic from the Flask app was
*not* ported into the Next.js build:

```ts
// lib/saLicenceLocalDecode.ts:1-26 (abridged)
/**
 * Stub for SA driver's licence local decode.
 * The PDF417Scanner component imports this module but only uses it when
 * `validationMode === 'sa-licence'`. For licence disc scanning the
 * validationMode is 'none', so these exports are never called at runtime.
 */
export async function decodeSALicenceViaLocalApi(_file: File) {
  throw new Error('SA licence local decode is not available in the trailerpro build');
}
```

**Vehicle licence disc parser.** The NaTIS disc barcode is **plain text** (NOT RSA-encrypted).
`saDiscParser.ts` documents the known field set and parses label-value / pipe / percent /
fixed-width formats:

```ts
// lib/saDiscParser.ts:20-33 (abridged — SADiscData interface)
export interface SADiscData {
  licenceNumber:  string;   // The NUMBER PLATE (e.g. "GP 12 XX GP")
  make:           string;   // e.g. TOYOTA
  model:          string;   // e.g. HILUX
  vin:            string;   // 17-char VIN
  engineNumber:   string;
  expiryDate:     string;   // YYYY-MM-DD
  grossMass:      string;   // GVM kg
  tare:           string;   // Tare kg
  colour:         string;
  vehicleType:    string;   // e.g. MOTOR CAR, LDV
  confidence:     'high' | 'low';
  rawText?:       string;   // For debugging — the raw barcode text
}
```

**Where the scanner is used (staff UI):**

- `app/dashboard/management/stock/page.tsx` — add/edit trailer: `parseSADisc` at line 225, `<PDF417Scanner>` rendered at line 277.
- `app/dashboard/management/stock-summary/page.tsx` — `<PDF417Scanner>` at line 1958, `parseSADisc` at line 406.
- `app/dashboard/returns/page.tsx` — `<PDF417Scanner>` at line 514, `parseSADisc` at line 134, plus `scanLicensePlate` (Tesseract OCR) at line 17.

**How the staff UI captures and submits a scan (stock page):**

```tsx
// app/dashboard/management/stock/page.tsx:219-245 (abridged)
// PDF417 barcode scanner — called when scanner decodes a licence disc
function handleDiscScanned(rawText: string) {
  setDiskScannerOpen(false);
  setOcrState('scanning');
  setOcrInfo('Parsing licence disc data…');
  try {
    const disc = parseSADisc(rawText);
    const filled: string[] = [];
    setTrailerForm((f) => {
      const next = { ...f };
      if (disc.licenceNumber) { next.licensePlate = disc.licenceNumber; filled.push('plate'); }
      if (disc.expiryDate)    { next.licenseDate  = disc.expiryDate;    filled.push('expiry'); }
      if (disc.vin)           { next.vin          = disc.vin;           filled.push('VIN'); }
      return next;
    });
    if (filled.length > 0) {
      setOcrInfo(`Extracted: ${filled.join(', ')} — please verify before saving.`);
      setOcrState('done');
    }
  } catch { /* … */ }
}
```

```tsx
// app/dashboard/management/stock/page.tsx:275-283
{diskScannerOpen && (
  <PDF417Scanner
    onResult={handleDiscScanned}
    onClose={() => setDiskScannerOpen(false)}
    label="Scan Licence Disc"
    hint="Position the PDF417 barcode on your licence disc flat in the viewfinder"
  />
)}
```

The scan result **populates a trailer form** (plate / expiry / VIN); the user verifies and
saves, which writes into the store (see §2 for the persistence path). There is no separate
"scanner endpoint" — decoding happens entirely client-side (ZXing / Tesseract).

### 1c. Is there vehicle-registration / licence-disc scanning (vs driver's card)?

**Yes.** Two distinct mechanisms exist and are worth distinguishing:

1. **Vehicle licence disc barcode** → `lib/saDiscParser.ts` (plain-text NaTIS format). Used in stock/returns pages.
2. **Vehicle licence disc OCR** → `lib/ocrUtils.ts` (Tesseract.js). Extracts expiry date ("Date of expiry / Vervaldatum", last ISO date in OCR stream) and licence/plate number ("Licence no. / Lisensienr.").
3. **Driver's licence card** → the standalone Flask app (`Decode_SA_Drivers_Licence`, `pdf417decoder` + RSA). **Not wired into the live Next.js build** (stub `saLicenceLocalDecode.ts`).

### 1d. What to copy / what to change for ABI

- **Copy the disc approach (saDiscParser + ZXing), not the driver-card RSA path.** ABI is a vehicle rental
  platform: the licence **disc** (plate, VIN, expiry, GVM, tare) is the relevant input for vehicle
  registration — it is plain text and far simpler than the RSA-encrypted driver card.
- Copy the **client-side decode** pattern (`@zxing/library` `BrowserPDF417Reader` + `tesseract.js` OCR).
  ABI's Flask backend does not need to decode barcodes; decoding can run in the browser and POST the
  extracted fields. The Flask `pdf417decoder` path is heavier (Python + Pillow) and only needed if ABI
  must accept uploaded driver-card images server-side.
- **Change:** ABI needs a server-side persistence endpoint (its Flask/Turso DB), whereas TrailerPro
  persists whole-state JSON blobs via a generic `/api/business/[slug]/state` POST. ABI should POST parsed
  fields to a dedicated vehicle-registration route and store them as structured columns, not a JSON blob.
- **Change:** keep the "extracted → please verify before saving" UX (fill form, don't auto-commit).
- **Gap to fill in ABI:** the driver's-licence-card RSA decoder exists only in the archived Flask app, not
  in the live build. If ABI needs driver-card data too, port `decoder.py` (RSA keys + `parse_data`) — but
  note its weak point is image quality, and it is not currently maintained in the live product.

---

## 2. TrailerPro public online booking flow

### 2a. File paths

| File | Purpose |
|---|---|
| `app/booking/[slug]/page.tsx` (880 lines) | Public customer-facing booking page (`'use client'`) |
| `app/api/business/[slug]/state/route.ts` (71 lines) | GET/POST whole business-state JSON |
| `lib/store.tsx` (489 lines) | Client store; `addRental`, persistence |
| `lib/branching.ts` | Availability/candidate-trailer helpers (imported by booking page) |
| `lib/pricing.ts` | `getTrailerClassDailyRate` |

Routing: the page is served at `/booking/<slug>` (e.g. `https://trailerpro.online/booking/budget-trailer-hire`).

### 2b. Request/response contract

There is **no dedicated "create booking" endpoint**. The booking page mutates client state via the
store (`addRental`), and the store persists the **entire** `StoreState` JSON blob through a generic
endpoint. The API route is:

```ts
// app/api/business/[slug]/state/route.ts:51-71 (abridged — POST)
export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ slug: string }> },
) {
  const { slug } = await params;
  try {
    const body = await req.json();
    const json = JSON.stringify(body);
    await db.businessAppState.upsert({
      where:  { slug },
      update: { storeJson: json },
      create: { slug,  storeJson: json },
    });
    invalidateCache(slug);
    return NextResponse.json({ ok: true });
  } catch (e) {
    return NextResponse.json({ ok: false }, { status: 500 });
  }
}
```

So the "contract" is: **`POST /api/business/<slug>/state`** with the full `StoreState` JSON
(`{ trailerClasses, trailers, rentals, branches, settings, company, … }`), returns `{ ok: true }`.
`GET` returns the same blob (30s in-memory TTL cache).

```ts
// lib/store.tsx:434-442 (abridged — addRental mutates in-memory state)
const addRental = useCallback((r: Omit<Rental, 'id'>) => {
  const id = Date.now().toString();
  const newRental: Rental = { ...r, id };
  setState((prev) => ({
    ...prev,
    rentals: [...prev.rentals, newRental],
    paidMap: { ...prev.paidMap, [id]: r.paid },
  }));
}, []);
```

State is also mirrored to `localStorage` and re-seeded to the server on load (`store.tsx:306-345`).

### 2c. How vehicle/trailer selection works

The page is a wizard. With branches it is 6 steps, without branches 5 steps:

```tsx
// app/booking/[slug]/page.tsx:308-338 (abridged — step indicator)
hasBranches
  ? [
      { n: 1, label: 'Trip & Branches' },
      { n: 2, label: 'Select Trailer' },
      { n: 3, label: 'Choose Dates' },
      { n: 4, label: 'Your Details' },
      { n: 5, label: 'Confirm' },
    ]
  : [
      { n: 1, label: 'Select Trailer' },
      { n: 2, label: 'Choose Dates' },
      { n: 3, label: 'Your Details' },
      { n: 4, label: 'Confirm' },
    ]
```

Selection state is a **single** trailer class (`selectedClass: TrailerClass | null`), plus trip type
(`'return' | 'oneway'`), collect/return branch, start/end dates, and a customer object:

```tsx
// app/booking/[slug]/page.tsx:81-89 (abridged)
const [step, setStep]                     = useState<1|2|3|4|5|6>(1);
const [selectedClass, setSelectedClass]   = useState<TrailerClass | null>(null);
const [bookingType, setBookingType]       = useState<'return' | 'oneway'>('return');
const [collectBranchName, setCollectBranchName] = useState('');
const [returnBranchName, setReturnBranchName] = useState('');
const [startDate, setStartDate]           = useState<Date | null>(null);
const [endDate, setEndDate]               = useState<Date | null>(null);
const [customer, setCustomer]             = useState({ name: '', phone: '', email: '', address: '', company: '', vatNumber: '', regNumber: '' });
```

On submit, it computes availability, picks the **first available unit** of that class, and creates
**one** rental:

```tsx
// app/booking/[slug]/page.tsx:198-260 (abridged)
function handleSubmitBooking() {
  if (!selectedClass || !startDate || !endDate) return;
  // … build occupiedIds from overlapping rentals …
  const available = getCandidateTrailersForBooking(
    trailers, rentals, serviceIds,
    selectedClass.id, selectedCollectBranch, hasBranches, bookingType, allowOneWayTrips,
  ).filter((t) => !occupiedIds.has(t.id))
   .sort((a, b) => a.id.localeCompare(b.id));

  if (available.length === 0) {
    setBookingError('No trailers available for these dates. Please choose different dates.');
    return;
  }
  const trailer = available[0];

  addRental({
    trailerId: trailer.id,
    trailerPlate: trailer.licensePlate,
    trailerClass: selectedClass.name,
    bookingType,
    collectBranchName: selectedCollectBranch ?? trailer.branch,
    returnBranchName: selectedReturnBranch ?? selectedCollectBranch ?? trailer.branch,
    customerName: customer.name,
    customerPhone: customer.phone,
    customerEmail:   customer.email   || undefined,
    customerCompany: customer.company || undefined,
    customerVat:     customer.vatNumber || undefined,
    customerReg:     customer.regNumber || undefined,
    customerAddress: customer.address || undefined,
    ratePerDayApplied: selectedRatePerDay,
    outDate: startStr,
    returnDate: endStr,
    status: 'planned',
    paid: false,
  });
  setStep(6);
}
```

### 2d. Can more than one item be selected at once?

**No.** The flow is strictly **one trailer class → one unit → one rental per booking**:

- `selectedClass` is a single value (`useState<TrailerClass | null>`), never a list.
- `handleSubmitBooking` assigns exactly `trailer = available[0]` (one unit).
- `addRental` appends exactly one `Rental` to state.

There is no quantity selector, no cart, and no multi-class selection anywhere in
`booking/[slug]/page.tsx`. ABI's multi-item requirement is a change, not a copy.

### 2e. What to copy / what to change for ABI

- **Copy** the wizard structure (select class → dates → customer details → confirm), the
  `toDateStr` local-timezone-safe date formatting (avoid `toISOString()` UTC shift for SA UTC+2),
  and the availability check that filters overlapping rentals by status
  (`['active','planned','overdue']`).
- **Copy** the "pick first available unit automatically" approach for the single-item case.
- **Change:** ABI must support **multiple items per booking**. Introduce a cart/line-items model
  (array of `{ trailerClassId, quantity }`) instead of a single `selectedClass`, and create N
  rentals (or rental line items) in one booking.
- **Change:** add a **dedicated booking endpoint** (e.g. `POST /api/bookings`) with a real
  request/response contract, instead of TrailerPro's generic whole-state JSON blob POST. ABI's
  SQLite/Turso schema should store bookings as rows, not one JSON document.
- **Change:** TrailerPro couples the public page to a client store (`useStore`) that loads/merges
  the whole tenant state client-side. ABI (Flask + Jinja) should fetch available vehicles
  server-side and POST a small booking payload.

---

## 3. Bubblebounce duplicate-customer handling

File: `/mnt/d/Hermes/Bubblebounce/bubblebounce_6/index.html` (6,938-line single-file app;
Turso/libSQL backend + localStorage config cache).

### 3a. How it detects a possible duplicate

Customers are a **derived cache** (`bb-clients` in localStorage) rebuilt from booking rows.
Normalization helpers:

```js
// index.html:1646-1648
function _normPhone(p){ return (p||'').replace(/\D/g,''); }
function _normEmail(e){ return (e||'').trim().toLowerCase(); }
function _normName(n){ return (n||'').trim().toLowerCase(); }
```

Contact is extracted from a booking row (dedicated columns, falling back to parsing the
pipe-delimited `notes` field for older rows):

```js
// index.html:1378-1398 (abridged)
function parseContact(b){
  const result={
    name:  b.customer_name||b.booked_by||'—',
    phone: b.customer_phone||null,
    email: b.customer_email||null,
    amount: b.amount!=null?Number(b.amount):null
  };
  if(!result.phone||!result.email||result.amount==null){
    const un=unpackNotes(b.notes||'');
    const stripped=(un.text||'').split('|||EXTRAS|||')[0];
    stripped.split(' | ').filter(Boolean).forEach(p=>{
      if(!result.phone&&p.startsWith('Phone: '))result.phone=p.replace('Phone: ','').trim();
      if(!result.email&&p.startsWith('Email: '))result.email=p.replace('Email: ','').trim();
      if(result.amount==null&&p.startsWith('Total: R')){
        const n=parseFloat(p.replace('Total: R','').replace(/,/g,'').trim());
        if(!isNaN(n))result.amount=n;
      }
    });
  }
  return result;
}
```

The dedupe core matches by **exact normalized phone → email → name**, in that priority:

```js
// index.html:1650-1679 (abridged — rebuildClientsCache matching logic)
function rebuildClientsCache(){
  let cache = {};
  try{ cache = getCfg('bb-clients',{})||{}; }catch(e){}

  bookings.forEach(b => {
    if(b.status === 'cancelled') return;
    const contact = parseContact(b);
    const name  = (contact.name||'').trim();
    const phone = (contact.phone||'').trim();
    const email = (contact.email||'').trim();
    if(!name && !phone && !email) return;

    const np = _normPhone(phone);
    const ne = _normEmail(email);
    const nn = _normName(name);

    // Find matching key
    let key = null;
    if(np){
      key = Object.keys(cache).find(k => _normPhone(cache[k].phone||'') === np) || null;
    }
    if(!key && ne){
      key = Object.keys(cache).find(k => _normEmail(cache[k].email||'') === ne) || null;
    }
    if(!key && nn){
      key = Object.keys(cache).find(k => _normName(cache[k].name||'') === nn) || null;
    }
    if(!key){
      key = 'c_' + (np || ne || nn).replace(/[^a-z0-9]/gi,'').slice(0,20) + '_' + Date.now() + '_' + Math.random().toString(36).slice(2,6);
    }
    // … merge/backfill fields, upsert booking snapshot, cap at last 3 …
```

### 3b. What it shows the user

The **Clients** page (`page-clients`) lists the merged customers: name, phone, email, "Last
booking" date, up to 3 booking rows, and per-client actions "Send Survey" / "Call". A search box
filters by name/phone/email:

```js
// index.html:1722-1742 (abridged — renderClients)
function renderClients(){
  let cache = {};
  try{ cache = getCfg('bb-clients',{})||{}; }catch(e){}
  const q = (_normName(document.getElementById('client-search')?.value||''));
  let list = Object.values(cache);
  if(q){
    const qDigits = q.replace(/\D/g,'');
    list = list.filter(c =>
      _normName(c.name||'').includes(q) ||
      (qDigits.length > 0 && _normPhone(c.phone||'').includes(qDigits)) ||
      _normEmail(c.email||'').includes(q)
    );
  }
  list.sort((a,b) => (b.lastSeen||'').localeCompare(a.lastSeen||''));
  // … renders name / phone / email / lastSeen / booking rows + Send Survey / Call buttons …
```

### 3c. Confirm "this is me / this is not me" / create new record

**There is no interactive "is this you?" confirmation, and no manual merge/keep-separate control.**
Duplication is resolved **deterministically and automatically**:

- If a booking's normalized phone, then email, then name matches an existing client key, the
  booking is **merged** into that client (and missing phone/email/name are backfilled).
- If nothing matches, a **new client record** is created with a generated key.

The "create new record" path in the UI is the **Staff New Booking** modal (`snbSave`), which
writes `customer_name` / `customer_phone` / `customer_email` onto a new `bb_bookings` row; the
client record is then auto-derived by `rebuildClientsCache()` on the next Clients-tab load.

```js
// index.html:4597-4632 (abridged — snbSave customer capture)
const cName=(document.getElementById('snb-cname').value||'').trim();
const cPhone=(document.getElementById('snb-cphone').value||'').trim();
const cEmail=(document.getElementById('snb-cemail').value||'').trim();
// …
const bk={
  booked_by: currentUser,
  item: p.name,
  accessories: accsChecked,
  date: _snb.startDate||today(),
  edate: _snb.endDate||_snb.startDate||today(),
  status: status,
  notes: packedNotes,
  source: 'staff',
  customer_name: cName,
  customer_phone: cPhone,
  customer_email: cEmail||null,
  amount: total,
};
// → INSERT INTO bb_bookings (… customer_name, customer_phone, customer_email, amount …)
```

POPI: clients inactive 5+ years are auto-purged (`index.html:1705-1710`).

### 3d. What to copy / what to change for ABI

- **Copy** the normalization idea (strip non-digits for phone, lowercase email/name) and the
  **phone → email → name** exact-match priority for detecting duplicates.
- **Copy** the "derived client list" concept if ABI wants a quick client directory from bookings —
  but ABI should store customers as **first-class DB rows**, not a localStorage-derived cache.
- **Change (important):** Bubblebounce's merge is **silent and automatic**, with no user-facing
  "this is me / this is not me" prompt. ABI explicitly needs the confirmation step, so ABI must
  add an interactive flow: on booking, run a fuzzy/exact lookup (phone/email/name) server-side,
  show candidate matches, and offer **"This is me" (link/merge) / "Not me" (create new) / "Create
  new record"** actions. That interaction does not exist anywhere in Bubblebounce to copy — it is
  a net-new build for ABI.
- **Change:** Bubblebounce matches **exact** values only. ABI should consider tolerant matching
  (normalized phone digit comparison already in place; optionally name similarity) to reduce
  near-miss duplicates.

---

## Gaps / caveats (honest)

- **Bubblebounce has no interactive duplicate-confirmation UI.** Its dedupe is fully automatic
  (exact phone→email→name merge). The "this is me / this is not me" requirement must be built new
  for ABI; only the matching/normalization logic is reusable.
- **Driver's-licence-card RSA decode is not in the live TrailerPro build** — it lives only in the
  archived standalone Flask app (`Decode_SA_Drivers_Licence`). The live app's `saLicenceLocalDecode.ts`
  is a throwing stub. The live app scans **vehicle licence discs** (plain-text NaTIS barcode via
  ZXing + Tesseract OCR), which is the more relevant path for ABI's vehicle-registration needs.
- **TrailerPro has no dedicated booking endpoint** and no multi-item selection; the public booking
  page posts the entire tenant state JSON to `/api/business/<slug>/state`. Both are things ABI must
  design differently.
- `gh` CLI is not installed on this host and `githubtoken.txt` at `/mnt/d/Hermes/` contains a note
  ("Git hub general - gh"), not a token, so GitHub was not queried; all findings come from the local
  repos listed above.
