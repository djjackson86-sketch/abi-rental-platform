# Sano live shop integration

Release authorised by Don. Base: current origin/master 05c435b including all recent Telegram changes through ABI-341953244. Never copy stale main or sandbox trees over it. Isolated worktree feature/sano-shop-live; merge only scoped new modules from catalogue/enquiries lanes. Fetch and reconcile origin/master before publishing; fast-forward push only.

## Acceptance
- Approved older black homepage: standalone logo, normal Home/Store/Trailer hire/Repairs navigation and cart; exactly three service cards, no striped ribbon/footer. One rental selection/customer capture page.
- ABI Online store manages persistent homepage and inventory-linked product/category imagery, text, publication and shared link. Existing category editor preserved.
- Real ABI products/pricing/branch stock, no sample imports or parallel inventory database. Public visibility and maintenance gates enforced on reads and writes. Rental/sales separate.
- Enquiries submitted atomically into new enquiry/line tables with published privacy consent, validation, spam limits, payload-bound idempotency and private opaque receipts. No automatic stock reservation/customer/order/invoice/payment/revenue changes.
- Enquiry inbox/status/assignment/notes branch-scoped server side; staff push to exact assigned branch only, safe direct enquiry deep links. Normal staff-controlled quote/order handoff with no retyping and existing customer verification retained.
- Permissions and CSRF on new admin writes; resized persistent DB image storage; safe public media. libSQL-shaped rows and real remote transaction semantics verified.
- Spec review then quality review. Combined feature + affected Telegram regression checks, real browser mobile+desktop editor -> DB -> public image and enquiry -> branch inbox -> order handoff.
- Verify latest remote ancestor preserved, Render commit live, public storefront and authenticated Online store + enquiry/config reads before completion. No unapproved production test enquiries/customer/order/stock mutations.

## Verification recorded

- New shop suite: 80 passed (front/catalogue/enquiries/integration/branch-hours plus affected legacy store tests). Real browser sweep: 100 checks passed, including upload persisted through DB to public shop, public enquiry writing only enquiry rows, exact branch-only notification/deep link, branch isolation, internal note/status/assignment and normal New order prefill -> one draft -> idempotent replay.
- Recent Telegram/push regression suite: 242 passed.
- Broad legacy `test_app` + branch-hours + product-stock suite: 239 passed / 19 failed, identical count and list to the unmodified live baseline worktree. No new failures.
- Release gate: production read-only inventory baseline stored in `docs/verification/pre-release-data-baseline.json`; compare after deploy.
