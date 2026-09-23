# Feature C — Proper public booking page (multi-trailer) + store categories with photos

**Ask (Don, 2026-09-23):** "we need to setup a better online booking page for the app. Refer to trailerpro app
and that public booking flow — base rentals on this but public should be able to select more than 1 trailer.
This must pull through as a public order in the app. On online store page in admin user should be able to link
the trailer categories (multiple trailers in 1 category) with a photo that will be used in store. Go to Sano
trailers website to acquire default photos — these should be changeable by user."

**References:**
- TrailerPro public booking flow — `docs/plans/reference-notes-trailerpro-bubblebounce.md` §2. Read §2c–§2e
  first: the reference page (`/mnt/d/Claude/trailer-rental-app/src/app/booking/[slug]/page.tsx`) is a
  **single-trailer-class, single-unit** flow (`available[0]`) that persists the whole tenant state JSON via
  `POST /api/business/[slug]/state`. So it is a **UX-shape reference only** — the multi-item selection,
  the per-item availability check and a real structured order are ABI's own work (see §2d/§2e).
- Sano Trailers default photos — **already harvested**: 15 full-resolution images in
  `static/img/trailer-categories/` (12 mapped to `/trailer-hire` categories, 3 alternates), with sources and
  dimensions in `docs/plans/sano-trailer-photo-sources.md`. Two categories on the site have **no usable
  photo**: *mobile kitchen trailers* and *bobcat trailers* — the seed script must leave those groups on the
  text fallback rather than inventing an image. Note the files are large (0.4–1.9 MB, ~11 MB total): §C1
  re-encodes to a web-friendly size before storing bytes in the DB.

**Already in the app (do not rebuild):**
- Categories exist as `product_groups` (`app/db.py`, `templates/admin/inventory/group_form.html` — "Create
  category headings/folders, then assign rental products, sales items, or stock items to them") and
  `products.product_group_id` is already populated per product.
- The public store is `app/routes/public.py` + `templates/public/store.html` (cards currently show
  `{{ p.name[:2].upper() }}` as a fake image block — that is what the category photo replaces).
- `_build_order_payload(form)` (`app/services/orders.py:626`) and `create_order` handle **one** product line
  today, including the branch default, availability/overbooking checks and customer blocking. Extending to many
  lines must go through this same validation, not around it.
- Uploads: the app has none. Per programme decision **D4**, image bytes go in the database (Render's disk is
  ephemeral) and are served by a route.

---

## C1 — Categories with photos + bulk trailer linking

**Objective:** each category (product group) carries a photo used by the store, staff can assign many trailers
to it at once, and the Sano photos ship as sensible defaults that staff can replace.

**Files**
- Modify `app/db.py` — `ensure_column`: `product_groups.image_blob BLOB`, `product_groups.image_mime TEXT NOT
  NULL DEFAULT ''`, `product_groups.image_filename TEXT NOT NULL DEFAULT ''`, `product_groups.image_source TEXT
  NOT NULL DEFAULT ''` (`'default:sano'` for the shipped photos, `'upload'` for staff ones),
  `product_groups.becomes_store_visible INTEGER NOT NULL DEFAULT 1`.
- Create `app/services/group_images.py` — `set_group_image(group_id, file_storage)`: accept png/jpeg/webp only,
  hard size cap (e.g. 4 MB), verify with Pillow (`Image.open(...).verify()`), **downscale to max 1600px wide**
  and re-encode to JPEG/WebP to keep rows small, store bytes + mime; `clear_group_image(group_id)`;
  `group_image_bytes(group_id)`.
- Create `scripts/seed_default_group_images.py` — idempotent: for each file in
  `static/img/trailer-categories/`, match to a `product_groups` row by slug-ish name match (e.g.
  `6ft-trailer.jpg` → a group whose name contains "6ft"), set `image_source='default:sano'`, and **never
  overwrite** a group that already has an uploaded image. Print a table of what it matched and what it skipped.
- Modify `app/services/products.py` / `app/routes/inventory.py` — group form gains a photo field
  (preview, upload, "Remove photo", "Use default" hint); new bulk action
  `POST /inventory/groups/<id>/assign` taking `product_ids[]` from a checklist of rental products
  (with a search/filter box) that sets `product_group_id` for the ticked rows and reports how many moved;
  the group page lists its current products.
- Modify `app/routes/public.py` — `GET /store/category-image/<int:group_id>` serving the stored bytes with the
  right `Content-Type` + `Cache-Control` (public route, no auth, 404 when none) and the store now groups
  products by active, store-visible `product_groups` (sorted by `sort_order`) showing the photo as the section
  header with its description; products with no group stay in an "Other" section.
- Modify `templates/admin/inventory/group_form.html`, `templates/admin/inventory/index.html`,
  `templates/public/store.html`, `static/css/app.css`.
- Create `tests/test_programme_20260923_group_images.py` — upload sets bytes/mime and a bogus file is refused;
  a 5000px image is downscaled; clear removes them; the store route serves the exact bytes and 404s when empty;
  the seed script is idempotent and does not clobber an upload; the bulk assign moves exactly the ticked
  products and leaves others alone; the store page renders one section per category with the photo URL; a
  category with no image falls back to the existing text block (no broken image icon); cascade: deleting a group
  leaves products without orphans (`product_group_id` → NULL is already the FK behaviour — assert it).

---

## C2 — Multi-trailer public booking flow

**Objective:** a customer picks a rental period and picks **more than one** trailer, and it arrives as one
public order with several lines for staff to confirm.

**Files**
- Modify `app/services/orders.py` — add `build_multi_item_order_payload(form)` (or extend
  `_build_order_payload` with a list-aware path, keeping the single-item callers untouched and reusing the same
  customer-blocking, branch-default and availability logic). Items arrive as parallel form lists
  (`product_id[]`, `quantity[]`), de-duplicated by product, refusing unknown/inactive/maintenance/`public_visible=0`
  products and refusing overbooking per item.
- Modify `app/routes/public.py`:
  - `GET/POST /store/book` — the new booking page: branch/location chooser, start + end date/time,
    then a trailer list (grouped by category, with category photo) where each row has a quantity/select control,
    live rental-day maths, a running estimate (subtotal, VAT via the existing global VAT rate, deposit total),
    and a single customer block (name/email/phone) that reuses the **§B2 dedupe flow** and carries the **§P1 consent block**: a link to the privacy notice plus a required, unticked-by-default acceptance — TrailerPro's own wording — with the POST refused server-side when it is unticked and the acceptance recorded against `PRIVACY_NOTICE_VERSION`
    (`app/services/portal_intake.py`) so a public booking cannot create a duplicate client either.
  - Keep `POST /store/products/<id>/book` working for one-click bookings from a product page — it should
    redirect into the new page pre-filled rather than staying a second implementation.
  - The created order: `status='draft'`, one `order_items` row per selected trailer, `booking_type` and branch
    fields set, a public-source marker (`source_system='public'`, `source_id` = a booking reference) and an
    order note recording "Public online booking" so staff can tell it apart — mirroring what
    `/store/products/<id>/book` does today but for many lines.
- Create `templates/public/book.html` + update `templates/public/confirmation.html` to list every line item.
- Create `tests/test_programme_20260923_public_multi_booking.py` — two trailers in one booking produce one order
  with two items and the correct money maths (assert exact figures from a seeded price/VAT); overbooking one
  trailer refuses the whole booking with a clear message and writes **nothing**; a maintenance-flagged or
  hidden trailer cannot be selected even by a crafted POST; a duplicate customer phone links instead of
  creating a second row; a blocked customer's booking is refused; an empty selection is refused; the
  the confirmation page lists both lines; the customer page shows the order; an **unticked consent box refuses the whole booking and writes nothing**, while a ticked one stores a `consent_records` row tied to the new customer and the notice version.

---

## C3 — End-to-end local proof, full suite, close-out

**Objective:** prove all three features work together locally, on one running app, and hand Don a report.

**Steps**
1. `.venv/bin/pytest -q` — the **full** suite must be green (baseline before the programme was ~700 tests;
   record the real count and duration). `python3 -m compileall app tests -q` and `git diff --check` clean.
2. Real-browser end-to-end on a temp SQLite DB (port 5057, seeded: 3 branches, 6 trailers across the Sano
   categories, 1 staff account with `scan_vehicle`, 1 client):
   - **A:** staff signs in → `/scan-vehicle` → disk fixture → allocate to a client → client page shows it.
   - **B:** open the branch QR URL → submit the portal form as a brand-new client → confirm the client exists
     on `/customers` under that branch; then submit again with the same phone → confirm the duplicate panel
     appears and "This is me" links instead of duplicating.
   - **C:** `/store/book` → pick 2 trailers over a weekend → submit → confirm the confirmation page lists both
     and the staff Orders list shows one public draft order with two items.
   - 1440px + 390px, 0 console errors, 0 horizontal overflow; screenshots for every step, **inspected with
     `vision_analyze`**.
3. Write the close-out: append the final ledger entry, add a section to
   `~/Documents/Obsidian Vault/Extended Memory/Projects/ABI Rental Platform.md` covering all three features
   (what was built, files, how to test, what is deliberately left, the open questions for Don), and prepare a
   **deploy proposal** for Don — the branch name, the commit list, the migration list, and the new
   `requirements.txt` entries that Render will install. **Do not push and do not deploy**: Don approves the
   deploy explicitly.
