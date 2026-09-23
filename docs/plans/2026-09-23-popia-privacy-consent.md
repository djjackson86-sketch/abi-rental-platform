# Feature P — POPIA privacy notice + client consent (public capture points)

**Ask (Don, 2026-09-23):** "add a popia privacy terms and click button for client to accept (similar to
trailerpro app) … also make a note on the public booking flow creation jobs for privacy agreement."

**The reference (TrailerPro's live app — mirrored deliberately):**
- Public page: `/mnt/d/Claude/trailer-rental-app/src/app/privacy/page.tsx` — a customer-facing
  "Privacy Notice — Trailer Rental Bookings / In accordance with the Protection of Personal Information Act
  (POPIA), Act 4 of 2013" page with a Back-to-booking button.
- On the booking form: `booking/[slug]/page.tsx` — a required, **unticked-by-default** checkbox
  (`id="popia-consent"`) worded *"I agree that my personal information (name, contact details and address) may
  be used to process this booking and generate an invoice, in accordance with POPIA."*, a **"View Privacy
  Notice"** link opening `/privacy` in a new tab (`target="_blank" rel="noopener noreferrer"`), and the submit
  button **disabled** until it is ticked (`disabled={!customer.name || !customer.phone || !consentGiven}`).
- **We go one better:** TrailerPro keeps consent in component state only. ABI **records** each acceptance
  (who, which notice version, when, from which channel) so the client can actually evidence consent.

**The source text already exists in this repo — do not write new legal copy:**
`docs/popia/PRIVACY-NOTICE.md` (11 KB, dated 2026-09-12), alongside `RETENTION-POLICY-AND-SCHEDULE.md`,
`POPIA-COMPLIANCE-ACTION-PLAN.md` and `OPERATOR-AGREEMENT-ABI-SANO.md`. Roles are settled there: **Sano
Trailers is the responsible party**; we (the developer) are the **operator**. The live page must carry the
responsible party's details and the **Information Regulator's contact details** — see the `popia-compliance`
skill §5 for the s18(1) checklist the page has to satisfy.

**Existing ABI pieces:**
- The whole public surface is `app/routes/public.py`; public pages extend `templates/base.html` and live in
  `templates/public/` (`store.html`, `product.html`, `confirmation.html`).
- Public endpoints are ungated in `app/services/access.py:_OPEN_ENDPOINT_PREFIXES`, so this page must expose
  nothing but the notice.
- The consent has to be recorded against the customer rows created by the portal (Feature B) and the public
  booking (Feature C) — both land in `customers` / `orders`.

---

## P1 — Privacy notice page, consent service, and the admin evidence trail

**Objective:** a real `/privacy` page served from the same wording as `docs/popia/PRIVACY-NOTICE.md`, a
consent record that survives an audit, and a reusable consent block the two public capture points (B and C)
must use — so consent is never bolted on later by hand.

**Files**
- Create `app/services/consent.py`:
  - `PRIVACY_NOTICE_VERSION = "2026-09-12"` — **must match the date line inside `docs/popia/PRIVACY-NOTICE.md`**
    (a test asserts this, so the page and the document can never drift apart silently).
  - `record_consent(customer_id, channel, accepted)` — refuses (`ValueError`) when `accepted` is falsy, and
    otherwise writes one row; `consent_for(customer_id)` → the latest record or `None`;
    `consent_summary(customer_id)` → the plain-language line the admin customer page shows, e.g.
    `POPIA consent — accepted 2026-09-23 (notice v2026-09-12, via the Midrand portal)`.
  - `consent_required_error()` — the single refusal message both public forms return.
- Modify `app/db.py` — additive `CREATE TABLE IF NOT EXISTS consent_records (id, customer_id INTEGER NOT NULL
  REFERENCES customers(id) ON DELETE CASCADE, consent_type TEXT NOT NULL DEFAULT 'popia_privacy', notice_version
  TEXT NOT NULL DEFAULT '', channel TEXT NOT NULL DEFAULT '', accepted_at TEXT NOT NULL)` in both `SCHEMA` and
  `run_migrations()`, with an index on `customer_id`. **Data minimisation on purpose:** no IP address, no user
  agent, no device fingerprint — only who/which version/which channel/when. Say so in a comment, because the
  next reader will wonder why the obvious columns are missing.
- Create `app/routes/public.py` additions: `GET /privacy` → render the notice (`store_enabled` must NOT gate it —
  a customer has to be able to read the notice even if the store is switched off). Add `View our privacy notice`
  to the store footer and to `templates/public/confirmation.html`.
- Create `templates/public/privacy_notice.html` — hand-written Jinja mirroring
  `docs/popia/PRIVACY-NOTICE.md` heading-for-heading, with `PRIVACY_NOTICE_VERSION` shown as the "last updated"
  line, back-to-where-you-came-from behaviour (a plain link back rather than TrailerPro's JS), and print-friendly
  CSS. Where the document still carries bracketed placeholders, **render only the confirmed content and list the
  outstanding fields in the tick's ledger entry** — never publish a `[SQUARE BRACKET]` to a customer.
- Create `templates/public/_consent_block.html` — the shared include both public forms use: unticked checkbox
  (`name="popia_consent" value="1"`, `id="popia-consent"`), the TrailerPro-derived wording *"I agree that my
  personal information (name, contact details and address) may be used to process this booking/registration and
  generate an invoice, in accordance with POPIA."*, a "View Privacy Notice" link to `public.privacy_notice`
  (`target="_blank" rel="noopener noreferrer"`), and a `required` attribute as the client-side hint only — the
  server-side check is what counts.
- Modify `templates/admin/customers/detail.html` — a last line in the customer's details: `consent_summary()`
  (or "No POPIA consent recorded" in the muted style). Read-only, no new permission.
- Create `tests/test_programme_20260923_popia_consent.py` — at minimum: `/privacy` returns 200 **with the store
  switched off** and contains every s18(1) element (data collected, responsible party name and address, purpose,
  voluntary/mandatory, recipients/categories, cross-border transfer, the data subject's rights, and the
  Information Regulator's contact details); the page shows the version line and contains **no `[` placeholder
  brackets**; `PRIVACY_NOTICE_VERSION` equals the date in `docs/popia/PRIVACY-NOTICE.md`; the consent block
  renders **unticked**; `record_consent` refuses a false/absent acceptance; a recorded consent is retrievable and
  appears on the customer page; deleting the customer removes the consent rows (no orphans); the table stores no
  IP/user-agent column (assert the schema, so the minimisation decision cannot be quietly undone).

**Acceptance:** `/privacy` is reachable, complete and placeholder-free; the shared consent block exists and is
unticked by default; consent rows carry customer + version + channel + timestamp; the admin customer page shows
the evidence line; new tests pass and `python3 -m compileall app tests -q` is clean.

**Must-know for the phases that follow:** Feature B's portal form (§B2) and Feature C's public booking form
(§C2) **must include this block** and **must refuse the submission server-side when it is unticked**, recording
the consent against `PRIVACY_NOTICE_VERSION` in the same request that creates the customer. Do not write a
second consent widget.

## Not in scope (say so rather than quietly skipping)

- Staff-side customer creation (`/customers/new`) does **not** capture consent in this phase. In-person
  counter capture is a real POPIA gap worth closing later — flagged as an open question for Don, not smuggled
  in here.
- The retention schedule is **not** turned into a scheduled deletion job by this phase (the
  `popia-compliance` skill is explicit that retention must become an actionable recurring report) — a good
  follow-up ticket, and `docs/popia/RETENTION-POLICY-AND-SCHEDULE.md` already holds the periods.
- Marketing consent (s69) stays the separate `marketing_opt_in` field on the customer record.
