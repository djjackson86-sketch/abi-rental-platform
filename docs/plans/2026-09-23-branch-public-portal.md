# Feature B — Per-branch public portal: shareable link + printable QR + self-registration

**Ask (Don, 2026-09-23):** "a public portal on the app where we can send a link to a customer inline on site
(by WhatsApp — we just need the link, not the WhatsApp functionality) and a QR code for each branch (different
for each) that navigates to the same page as the link (links per branch). We should provide the client with a
page for each branch that can print the QR code on an A4 page. The link and QR bring up a page where the client
can fill in all their data which then pulls through to the clients page on app. Client can also search for their
name and number and app can confirm if they are an existing customer (does not provide all client details as this
would be a security risk). Before client details are added the app must make sure we are not duplicating the
client (refer to bubblebounce app where we have this flow to deal with duplicate customers)."

**Reference:** Bubblebounce's duplicate handling is documented in
`docs/plans/reference-notes-trailerpro-bubblebounce.md` §3. **Read it before building:** §3b/§3c record that
Bubblebounce has **no interactive "this is me / not me" screen** — it merges fully automatically (exact
phone → email → name, inside `rebuildClientsCache`, `bubblebounce_6/index.html:1650-1714`). So only the
**normalisation + matching logic** is reusable; the confirmation UI is **new work for ABI** (as asked for:
the client must decide, and the app must never silently pick).

Existing ABI pieces:
- `app/routes/public.py` — the whole public surface today (`/store`, product detail, `book_product`,
  confirmation). New portal routes belong here (blueprint `public`, already ungated in
  `app/services/access.py:_OPEN_ENDPOINT_PREFIXES` — so they must be hardened, see D7).
- `templates/public/store.html` / `base.html` — public page shell to copy.
- `app/db.py` `branches` table (name, code, phone, email, address…), `customers.branch_id` (already exists,
  used by the "new customers for the day" figure) and `create_customer(form)`.
- `app/routes/branches.py` + `templates/admin/branches/index.html` — where the admin half belongs.

---

## B1 (phase 5) — Schema, branch link, QR endpoint

**Objective:** every branch gets a stable public URL and a QR image; nothing customer-facing yet.

**Files**
- Modify `app/db.py` — `ensure_column` additions: `branches.public_slug TEXT NOT NULL DEFAULT ''`,
  `branches.portal_enabled INTEGER NOT NULL DEFAULT 1`, `branches.portal_intro TEXT NOT NULL DEFAULT ''`,
  `company_settings.public_base_url TEXT NOT NULL DEFAULT ''`. Backfill: a migration that fills an empty
  `public_slug` from the branch name (lowercase, non-alphanumerics → `-`, dedupe with `-2`, `-3`) and creates
  `CREATE UNIQUE INDEX IF NOT EXISTS idx_branches_slug ON branches(public_slug) WHERE public_slug <> ''`.
- Create `app/services/portal.py` — `slugify(name)`, `ensure_slug(branch_id)`, `branch_by_slug(slug)`,
  `portal_url(branch, base_url=None)` (uses `public_base_url` setting, else the request's `url_root`),
  `qr_png_bytes(url)` (see below), `all_portal_links()`.
- **QR generation:** `.venv/bin/pip install "qrcode[pil]"` (pure-Python + Pillow, tiny — Pillow is already
  installed). Add `qrcode[pil]==<version>` to `requirements.txt`. No external QR service (the link must never
  leave the app), no vendored blob.
- Modify `app/routes/public.py` — `GET /portal/<slug>/qr.png` (image/png, long `Cache-Control`, 404 when the
  slug is unknown or the portal is disabled) and `GET /portal/<slug>` returning a placeholder "coming next
  phase" only if the real form is not built yet.
- Create `tests/test_programme_20260923_portal_links.py` — slug backfill is stable across two migrations runs,
  slug collisions resolve deterministically, unknown slug 404s, disabled portal 404s, QR endpoint returns a
  real PNG (`\x89PNG` magic) that decodes back to the URL (verify with `qrcode`'s own decoder or by asserting
  size + magic, and say which you used), and `public_base_url` wins when set.

**Acceptance:** tests pass; QR is generated in-process; slugs survive a restart.

---

## B2 (phase 6) — Public form, dedupe flow, safe existing-customer lookup

**Objective:** a client scans the QR (or taps the link), fills in their data, the app checks for duplicates,
and the client record lands on the staff Customers page against the right branch.

**Files**
- Create `app/services/portal_intake.py`:
  - `normalise_phone(value)` (strip spaces/`+27`→`0`, keep digits), `normalise_email(value)`.
  - `find_possible_matches(name, phone, email)` → list of `{customer_id, confidence, masked_display}`
    (`masked_display` = first name + surname initial + last 4 digits of phone — never more).
  - `create_or_link_customer(form, branch_id, decision)` where `decision` ∈ `{'create','link:<id>'}`;
    `link` **only fills blank fields** on the existing record and never overwrites a populated one; records
    `source_system='portal'` + `source_id=<slug>` (columns already exist on `customers`).
  - `lookup_public(name, phone)` → `{'found': bool, 'display': '...'}` with the masked display only, plus a
    hard rate limit (e.g. max 10 lookups per IP per 5 minutes, in-process dict with timestamps).
- Modify `app/routes/public.py` — `GET/POST /portal/<slug>/register`:
  - GET renders the form (branch name/address at the top so the client knows which branch they are at).
  - POST validates with the same rules as staff-side `_clean()` (name required, email format, phone shape),
    requires the POPIA consent checkbox, checks the honeypot field is empty, runs dedupe:
    - no match → create the customer, show a success page with a reference.
    - match found → **do not create**; re-render the form with a "Is this you?" panel listing each candidate's
      masked display, with `This is me` (posts `decision=link:<id>`) and `None of these — I'm new`
      (posts `decision=create`). The customer's answers decide; the app never silently picks.
  - `GET /portal/<slug>/check` (or `POST`) for the standalone "am I already a customer?" search box.
- Create `templates/public/portal_form.html`, `templates/public/portal_confirm.html`,
  `templates/public/portal_exists.html` — mobile-first, no admin chrome, big tap targets, inline field errors.
- Create `tests/test_programme_20260923_portal_intake.py` — phone normalisation table (`+27 82 123 4567`,
  `0821234567`, `082 123 4567` all match), email case-insensitivity, no-match creates with the branch id and
  `source_system='portal'`; a phone match does NOT create a second row and the "This is me" post links the
  submission while leaving the original record's populated fields untouched; "None of these" creates a second
  row deliberately; lookup returns masked data only (assert the response body does **not** contain the full
  surname/email/address/balance); the honeypot swallows a bot post; missing consent is refused; the rate limit
  kicks in; a disabled portal 404s; a blocked customer is not resurrected as a new record (respect `is_blocked`).

**Acceptance:** the duplicate-safe flow works end-to-end in tests, no personal data leaks in any public
response, and a created client appears on `/customers` with the branch filter showing them under that branch.

---

## B3 (phase 7) — Admin links + A4 print sheet + browser proof

**Objective:** staff can copy a branch's link for WhatsApp and print an A4 QR sheet per branch.

**Files**
- Modify `app/routes/settings.py` or `app/routes/branches.py` — add `portal` pages:
  `GET /settings/portal` (module key `settings` or `online_store`) listing every branch with: the branch name,
  the full link in a `readonly` input, a **Copy link** button (`navigator.clipboard.writeText`, with a
  fallback that selects the text), the QR preview (`<img src=".../qr.png">`), a "Print QR sheet" link to
  `/settings/portal/<branch_id>/print`, and an enable/disable toggle + editable slug (409 on a duplicate slug).
- Create `templates/admin/portal_index.html` and `templates/admin/portal_print.html` — the print sheet is a
  standalone A4 layout: Sano Trailers logo (`static/img/sano-trailers-email-logo.jpg`), branch name + address,
  a large QR (≥ 300px on paper), the plain URL underneath as selectable text, and one line of instructions
  ("Scan to register your details before you hire"). `@media print` must hide all admin chrome; use
  `@page { size: A4; margin: 12mm }`.
- Modify `templates/admin/layout.html` — nav entry "Customer portal" (respect the module gate).
- Modify `app/services/access.py` — map the new endpoints to a module.
- Create `tests/test_programme_20260923_portal_admin.py` — links page lists all branches, the print page renders
  the QR + link + branch address, a duplicate slug is refused with 409, the module gate blocks a staff account
  without the module, a disabled branch's QR 404s, and the print template contains the print-only CSS hooks.

**Browser proof (mandatory)**
- Chromium against a temp SQLite DB on 5057 with 3 branches (Midrand/Roodepoort/Pretoria names, real addresses
  from `docs/plans/sano-trailer-photo-sources.md` if available).
- 1440px and 390px: links page shows one QR per branch, **Copy link** actually puts the exact URL on the
  clipboard, print page screenshotted in **print media** (`page.emulate_media(media="print")`) to prove no admin
  chrome bleeds in. 0 console errors, 0 horizontal overflow. Also do the real flow once end-to-end: open the
  QR's URL, submit the form, and confirm the client appears on `/customers`.
- `vision_analyze` every screenshot; record what you saw. `fuser -k 5057/tcp` afterwards.

**Acceptance:** Feature B signed off locally with screenshots a human would accept as "print this on A4".
