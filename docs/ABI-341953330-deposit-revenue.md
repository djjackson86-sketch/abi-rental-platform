# ABI-341953330 — deposit revenue

Refundable security deposits are held funds. Dashboard period/day cards, the
monthly revenue chart, category cards and Orders revenue now share one calculation.
A deposit application recognises rental earnings once, on its stored payment date
(or capture date). It is never Other Revenue. Deposit refunds and transfers to
customer credit are not revenue adjustments. Cash/EFT/card receipts and payouts
still represent cash movement and intentionally need not equal revenue.

Return charges already belong to the order total. The booked Reports calculation
therefore subtracts the refundable deposit without adding applied funds to those
charges for a second time. Damage-waiver and no-deposit receipts retain their
existing signed-receipt treatment.

No migration, historical payment rewrite or balance adjustment is made. Editing
an existing application no longer overwrites its capture timestamp. Reversal uses
the existing soft-archive mechanism; reprocessing creates a new active application.

## Historical allocation limits

Payments do not separately identify a rent component and a deposit component.
For security-deposit orders the report conservatively reserves the contractual
deposit first across chronological paid receipts. Only receipts above that reserve,
plus active deposit applications, are revenue, capped by the non-deposit order
charges. A partial receipt may therefore remain held until the reserve is covered.
This convention avoids inventing a historical payment split or counting a held
security deposit as rent. Imported legacy balance receipts have no deposit data
and retain their existing Other Revenue classification.

Reports reflect the current order charges and active payment/application records,
not an immutable historical journal. Past application edits may already have
changed capture dates; missing original dates cannot be recovered by this fix.
Historical category gaps on ordinary receipts remain Other Revenue, but the
application itself is explicitly attributed to rentals. No cancellation fee or
new charge is invented when an application exceeds recorded rental charges.

## Verification before push

- 62 passed: new deposit regressions, category/chart tests, real Chrome desktop and
  mobile chart filtering, Other-method tests and financial-report deposit tests.
- Python compileall and git diff --check passed.
- The exact shared SQL ran read-only on production libSQL for all branches and
  branches 1/2 (approximately 0.85 seconds per query). No production data written.
- Five unrelated existing failures reproduced on untouched origin/master 0d58a43:
  customer-verification pickup gate, EFT full-page render contract, Orders due
  total contract, and two historical currency-format contracts. No claim that
  the complete repository test suite is green.
