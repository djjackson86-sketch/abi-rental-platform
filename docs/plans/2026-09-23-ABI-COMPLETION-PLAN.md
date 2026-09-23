# ABI Rental Platform — COMPLETION PLAN (single-owner, no cron)

**Created:** 2026-09-23 16:10 SAST · **Owner:** the assistant (this plan is MINE to execute — it is **not** a cron
programme) · **Decided by Don:** "this cron approach doesn't seem to be working that well, let's cancel the jobs and
recheck everything they have done and then move everything into a plan that you must complete"

---

## 1. Why the cron programme was cancelled

It delivered ten solid phases, but the tick machinery cost more than it saved:

| Failure | Cost |
|---|---|
| A tick crashed at 13:02 leaving an orphan lock + no artefacts | 10 consecutive slots lost (~20 min) |
| Phase 9's slot crashed *after* its suite but before its commit, and its proof server was left reparented to systemd holding port 5059 | ~30 min, plus manual repair |
| The 45-minute stale window meant a dead worker's lock blocked the tree for 45 min | fixed mid-flight to 20 min, but the window still outran two long phases |
| The gated POPIA-wizard job's 20-fire budget was eaten by 3-minute gate checks while it waited | it marked itself **completed at 14:33 without ever building W1/W2** |

**Both jobs are now removed** (`b925af49c5a7` main tick, `d52aa0f40bdf` wizard tick — the latter replaced and then
removed along with the dead original `b1d33af7d6cf`). No other project's jobs were touched.

## 2. Audit — what the ticks actually delivered (verified by me, not self-reported)

**Branch:** `feature/abi-programme-2026-09-23`, 26 commits. `master` = `origin/master` = `8ec51e8` — **the branch
has never been pushed; the live site and live database are untouched.**

| Phase | Feature | Commit | Status |
|---|---|---|---|
| 1 A1 | NaTIS licence-disc parser (822-line module) + measured engine decision | `d8cc156` | done |
| 2 A2 | `vehicles` table + service, one owner per registration | `2755e84` | done |
| 3 A3 | Staff `/scan-vehicle` screen + allocate/transfer to a client | `00b7c88` | done |
| 4 A4 | Client-page Vehicles panel with in-place edit/remove | `bc4a18d` | done |
| 5 D1 | Trailer identity + return-matching service (`returns.py`) | `dde0d26` | done |
| 6 D2 | `/scan-return` screen → marks the rental returned | `6f144ce` | done |
| 7 P1 | `/privacy` page + `consent_records` + consent block (D11 gate) | `a8c69df` | done |
| 8 B1 | Per-branch public link + in-process QR (`/portal/<slug>`, `/qr.png`) | `fb2cc24` | done |
| 9 B2 | Public registration form + dedupe decision + safe "am I already a customer?" | `6939107` | done |
| 10 B3 | `/settings/portal` admin page: per-branch link, live QR, A4 print sheet | `045c4d9` | done |
| — | PoPIA scrub of the real disc identifiers (personal data) | `404cc24` | done |

Test growth: 747 → 767 → 794 → 808 → 845 → 864 → 896 → 919 → **1002 green** at phase 10.

**My independent verification (not the ticks' claims):**
- `python -m compileall app tests` → clean
- **Full suite re-run by me: see §6 (result recorded there)**
- Real HTTP smoke on a fresh temp DB (`DATABASE_PATH=/tmp/abi_audit.db PORT=5057`):
  `/health` 200 · `/login` 200 · `/privacy` 200 · `/dashboard`, `/scan-vehicle`, `/scan-return`, `/settings/portal`
  all **302 to login** (auth intact) · **`/portal/branch-1` 200 text/html** · **`/portal/branch-1/qr.png` 200,
  `image/png`, 674 bytes, real PNG magic** · slugs backfilled deterministically (`Branch 1` → `branch-1`)
- New schema really is applied: `branches.public_slug`/`portal_enabled`/`portal_intro`, tables `vehicles`,
  `consent_records`

**The one fact that governs the whole public side:** `docs/popia/PRIVACY-NOTICE.md` still carries Sano's **five open
facts**, so the D11 gate is doing its job — `/privacy` serves *"Our privacy notice is being finalised"* and the
public portal/booking write paths are **deliberately shut** (verified: no bracket token leaks, the portal page shows
the counter message). Everything public stays dark until those five facts land or Don accepts an interim notice.

## 3. What is LEFT to build

Order matters: C1 → C2 → C3 finish the public product; Q1 then W1/W2 finish the POPIA product.

- **T1 — Hygiene (done, 16:05):** killed the stale 09:16 `app.py` (pid 558440) that had held port 5057 all day and
  was flagged by eight separate ticks. 5057 is free for smoke runs again.
- **T2 — Phase 11 (§C1) store categories with photos + multi-trailer linking. ✅ DONE — `7fc4f1f`**
  Verified by me after the builder ran out of iterations: 20 new tests, **full suite 1022 passed in 590s**,
  compile clean; rendered HTML shows **4 store sections** (2 photo headers, 2 name fallbacks) with the ungrouped
  and hidden-category trailers folded into a trailing "Other" section; `/store/category-image/<id>` serves 200 with
  real JFIF bytes and **404** when the category has no photo; Playwright at 1440px + 390px over the store, groups
  list and both group-form variants: **0 console errors, 0 horizontal overflow**; full-page screenshot inspected.
  Only the web re-encodes (12 JPEGs, 644 KB) were committed — the 11 MB of originals stay untracked. Original detail: plan:
  `docs/plans/2026-09-23-public-booking-and-store-categories.md` §C1. Trailer categories already exist as
  `product_groups` with `products.product_group_id` populated, so this is **extend, not rebuild**. Deliverable:
  a photo per category, stored **in the DB** (D4 — Render's filesystem is ephemeral), a bulk "link these trailers to
  this category" action, and the 15 harvested Sano photos as defaults. **Guardrail 2:** `static/img/trailer-categories/`
  holds 11 MB of raw source material — commit only **web-sized re-encodes (≤1200px, ~100–200 KB each)**, never the
  originals. Missing categories to source or stub: mobile kitchen trailers, bobcat trailers.
- **T3 — Phase 12 (§C2) multi-trailer public booking flow.** One public booking → **one order with many lines**
  (unlike TrailerPro's single-item flow). Must go through the existing availability/blocking checks; `_build_order_payload`
  in `app/services/orders.py` is single-item today and needs deliberate extension. Reuses §B2's dedupe flow and
  §P1's consent block — the consent box must be required, refused server-side, and recorded against the notice version.
- **T4 — Phase 13 (§C3) end-to-end local proof + close-out.** Full suite, a real Playwright end-to-end pass over the
  public journey (portal → registration → booking) and the staff journey (scan in → allocate → scan out), plus a
  close-out note in the vault.
- **T5 — Phase 14 (§Q1) POPIA document pack in-app.** Notification bar for the main profile, a `/popia` pack page,
  `document_acceptances` (document key, version, content hash, who, when, channel), the A4 print route with an
  acceptance certificate, and the **hard gate: nothing containing an unresolved placeholder can ever be accepted.**
- **T6 — W1 POPIA setup wizard in Settings** (`docs/plans/2026-09-23-popia-setup-wizard.md` §W1, written today).
  `/settings/popia`, main profile only: five steps, **prefilled from data the app already holds**, the Information
  Officer question with the registration link (eservices.inforegulator.org.za) and a publish gate while unregistered,
  the practice questions (CCTV per branch, marketing, ID copies, third parties, payments, under-18s, credit checks),
  then it **generates** the light plain-English notice and publishes it to `/privacy`.
  **"Light" = plain English, no citations, no schedules, no liability padding — never fewer than the eight s18(1)
  items, and a test must assert all eight are present for every answer combination.**
- **T7 — W2 the operator notice** ("how Jackapp stores your data"): hosts and **actual regions**, access model,
  backups (including that deletions are re-applied after a restore), encryption, the incident path, deletion at exit,
  contact — downloadable and printable through §Q1's print route.
- **T8 — Carry-overs and decisions waiting on Don:**
  - **(a) Sano's five POPIA facts — ANSWERED 2026-09-23: "this will be sorted with the POPIA wizard."** So the
    facts are **collected by §W1**, not typed in by hand, and the wizard's step 5 publish is what lifts the D11 gate.
    Consequence: **§W1 is now on the critical path for the whole public side** — until Sano completes the wizard,
    `/privacy` stays interim and portal/booking registration stays shut. Nothing else can open that door, and no
    placeholder may be invented to force it open.
  - **(b) `/customers` — ANSWERED: "let's keep it global then."** No branch scoping for the clients list; that closes
    the flag three ticks raised. Do not add a branch dimension to customers.
  - **(b2) QR codes — Don: "only 1 QR code."** Read as: the QR sheet / printed PDF carries **that branch's single
    QR**, not a grid of every branch. Confirm in one word if that is wrong; the build follows this reading.
  - **(c) QR sheet PDF — ANSWERED: yes, build it.** A real server-side PDF endpoint for the branch QR sheet
    (one QR, that branch's link, branch address) rather than relying on the browser's print dialog. Folded into
    **T4** (it reuses the app's existing `pdf_documents.py` machinery) and must be verified by generating the PDF
    and looking at it, per the house standard.
  - **(d) Untracked debris to clean or keep:** `=1.18.0`, `app/__init__.py.backup`, `backups/`, `test_invoice.pdf`,
    `.hermes/`.
  - **(e) POPIA merge rule — ANSWERED: "yes, when we commit, delete my real vehicle info."** At merge time:
    (1) merge to `master` as **one squashed commit** (never push the intermediate commits — they still carry the real
    plate / NaTIS reg / disc licence / VIN / engine number in their diffs), (2) **delete the feature branch**, which
    makes the tainted commits unreachable, (3) `git gc --prune=now` to purge those objects, then (4) **prove it**:
    `git log --all -S <each of the five real identifiers>` must come back empty, and `git rev-list --all | wc -l`
    must not include the old chain. Only then is the real vehicle information gone from the repository for good.

## 4. How "done" is proven (the standard I hold myself to)

1. `python -m compileall app tests` clean.
2. The **whole** suite green (not just the new file) — quote the real count.
3. Real HTTP/DB evidence for anything claimed: status codes, byte counts, DB rows, decoded QR.
4. UI work is not done until it has been **looked at**: Playwright screenshots at 1440px and 390px, inspected with
   `vision_analyze`, 0 console errors, 0 horizontal overflow.
5. Commit with an explicit path list; never `git add -A`; never push; never touch the live database or site.
6. Nothing is reported as working unless I ran it. A blocker named honestly beats a fabricated pass.

## 5. Next action

**T2 is done. Start at T3 (phase 12, §C2 — multi-trailer public booking flow)** and work down the list, verifying
each before moving on. T8a (the five POPIA facts) is answered — the wizard collects them — and T8b/T8c/T8e are
decided, so nothing blocks T3–T7.

**Lessons from T2, to apply to every remaining item:**
1. A single builder can hit its iteration cap mid-phase. Recovered fine here because the work was on disk; from now
   on the brief says **commit the implementation as soon as the new tests pass**, then run the full suite and the
   browser proof, then commit the ledger — so an interrupted builder still leaves committed work.
2. **Verify with structure as well as eyes.** The first store screenshot was a cropped viewport and read as "no
   category sections at all" — wrong. Fetching the rendered HTML and counting sections settled it, and the
   full-page screenshot confirmed it. Do both.
3. **The whole suite is the gate, and it takes ~10 minutes on this filesystem** (1022 tests). Start it in the
   background early and do the browser proof while it runs.
4. **Port hygiene:** Chromium refuses some ports (`ERR_UNSAFE_PORT` — 5061 is blocked); 5057 works. Kill any
   leftover server on your port first, and kill the one you started when you are done (`kill -9 <pid>`, not pkill,
   which can match its own command line).
5. **POPIA check before every commit:** `grep` the staged files for the five real disc identifiers. My own plan
   document and the ledger both carried the real plate in a command example and had to be fixed.

## 6. Audit result (recorded when the suite finished)

- `compileall`: **clean**
- **Full suite re-run by me at 16:15 on the branch tip `85aa4cf`: `1002 passed in 576.08s (0:09:36)` — 0 failures.**
- HTTP smoke, fresh temp DB on port 5057: `/health` 200 · `/login` 200 · `/privacy` 200 (interim page, no bracket
  token leaked) · `/dashboard`, `/scan-vehicle`, `/scan-return`, `/settings/portal` all 302 → login · `/portal/branch-1`
  200 text/html · `/portal/branch-1/qr.png` 200 `image/png`, 674 bytes, valid PNG magic.
- **Verdict: phases 1–10 are real, working and locally signed off.** Nothing is pushed; `master` = `origin/master`
  = `8ec51e8`; the live site and live database are untouched. The port 5057 smoke server used for the audit was
  started against a throwaway DB (`/tmp/abi_audit.db`) and shut down after the probes.
