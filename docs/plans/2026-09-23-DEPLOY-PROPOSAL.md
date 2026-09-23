# Deploy proposal — ABI Rental Platform programme (23 September 2026)

**Status: NOT pushed, NOT deployed. This is the approval document.** Nothing leaves this machine
until Don says go: the branch has never been pushed, `master` and `origin/master` are both still
`8ec51e8`, and the live site at `abi-rental-platform.onrender.com` is untouched.

## What is being proposed

One squash-merge of `feature/abi-programme-2026-09-23` onto `master`, then a push. Render
auto-deploys on push to `master`, so the push **is** the deploy. **35 commits**, all verified
locally (`compileall` clean, full suite green, real-browser proofs).

The merge rule Don set still applies: squash to **one clean commit** on `master`, delete the
feature branch, `git gc --prune=now`, then prove with `git log --all -S` on all five real vehicle
identifiers that they are gone. The identifiers live only in the unpushed commits' diffs; the
squash is what keeps them out of the public history for good.

## What Render installs (requirements.txt)

Three additions, nothing removed, no native/system libraries to install on their side:

- `Pillow==12.3.0` — image loading for the disc-scan variant ladder
- `zxing-cpp==3.1.1` — the measured disc-decode engine (~2 MB wheel, reads the real disc photo in 22 ms)
- `qrcode[pil]==8.2` — the branch portal QR, rendered in process (pure Python, reuses Pillow)

`pypdfium2` was installed **in the local venv only** to render the QR sheet PDF for visual
verification. It is deliberately **not** in `requirements.txt`: the app never renders a PDF to an
image, so it must not become a production dependency.

## Database migrations

**No manual step.** Every one of these is applied by the app itself on boot (`init_db` /
`ensure_column`), all idempotent, all additive:

| Table | Change |
| --- | --- |
| `vehicles` | **new table** (+ index on `customer_id`) |
| `consent_records` | **new table** (+ index on `customer_id`) |
| `branches` | `public_slug`, `portal_enabled`, `portal_intro` |
| `company_settings` | `public_base_url` |
| `orders` | `return_scan_at`, `return_scan_registration`, `return_scan_source`, `return_scan_user_id` |
| `product_groups` | `image_blob`, `image_mime`, `image_filename`, `image_source`, `becomes_store_visible` |
| `products` | `registration`, `licence_number`, `registration_number` |

Plus a **one-off backfill**: every branch gets a `public_slug` derived from its name,
deterministic and collision-resolving (two runs produce the same answer; proven by test).

Nothing is dropped, nothing is rewritten, and no existing column changes meaning — which is also
what makes a rollback safe: if the code is rolled back, the extra columns simply sit unused.

## ⚠️ One decision needed at deploy time: are the branch portals publicly visible?

`branches.portal_enabled` defaults to **1**, so on deploy every existing branch gets a live
`/portal/<slug>` URL. The *forms* are still shut (see below), so such a page collects nothing —
it says "Online registration for this branch is not open yet". But the pages themselves become
reachable, which is a wider surface than "nothing public until Sano's facts land".

Two options, both one line of SQL:

- **A — no portals until the wizard is done (recommended).** Set `portal_enabled = 0` for all
  branches at deploy, then switch each one on from the admin portal page as the wizard completes.
  Nothing public exists at all in the meantime.
- **B — portals on, forms shut.** The page is visible but says registration is not open yet.
  Simpler, and the gate is the same one the tests exercise.

## What stays SHUT after deploy (deliberate, decision D11)

The public side is gated on the privacy notice being complete, and `PRIVACY-NOTICE.md` still
carries **5 facts only Sano can supply** (entity + registration, Information Officer name +
registration, effective date, public URL, per-branch CCTV). Until the Settings POPIA wizard
generates a placeholder-free notice:

- `/privacy` serves the interim "being finalised" page;
- `/store/book` refuses a booking request (the booking page says online booking is not open yet);
- public registration at `/portal/<slug>` refuses to record anyone.

So **deploying now is safe**: it ships the staff-side capability (disc capture, vehicle records,
portal management, store categories, the printable QR sheet) and the whole public surface stays
dark until it is legally ready. Turning the public side on is the wizard's job, not the deploy's.

## What staff get on day one

- **Disc scan → vehicle record** (`/scan-vehicle`): photograph or paste a licence disc, review
  every parsed field, allocate it to a client.
- **Branch portals** (`/settings/portal`): per-branch slug, on/off switch, welcome line, share
  link, QR image, the on-screen print sheet and now the **A4 PDF** (`/portal/<slug>/qr.pdf`).
- **Store categories** (`/store`): four Sano categories with replaceable photos, bulk trailer
  linking, and an "Other" group for anything ungrouped.
- **Multi-trailer public booking** (built, gated off): one order with a line per trailer, with
  per-trailer availability checks and the returning-customer dedupe question.

## Rollback

- Code: Render keeps the previous deploy; one click re-deploys `8ec51e8`.
- Data: the migrations are additive, so an older app version ignores the new columns and tables.
  Nothing needs undoing. The only irreversible act in the plan is the `git gc` after the squash,
  which is why the identifier purge is verified **before** the branch is deleted.

## Verification backing this proposal

- Full suite green on the branch (count in the closing ledger entry, `docs/plans/PROGRESS.md`).
- `python -m compileall app tests` clean; `git diff --check` clean.
- Real-browser end-to-end at 1440px and 390px on one running app: disc capture → allocation,
  portal registration → duplicate handling, `/store/book` → one order with two line items, with
  every screenshot inspected, 0 console errors and 0 horizontal overflow.
- The A4 QR sheet: the served PDF renders as true A4 (595×842 pt) and the code scanned back off
  the **rendered page** decodes to exactly the address printed beneath it.
- Every suite re-run that carries a number in this programme's ledger was run on a tree that was
  not being edited while it ran.
