# PROGRESS — POPIA wizard programme (SEPARATE from the main programme)

**Do not confuse this ledger with `PROGRESS.md`.** That one belongs to the main tick programme. This file belongs
to the POPIA wizard programme only. Never write the other one's ledger.

**Goal (Don, 2026-09-23):** a POPIA page in Settings that walks Sano through becoming POPIA compliant — prefilled
from what the app already knows — generates the privacy notice from their answers (kept as light as legally
possible), and lets them download our operator notice (how Jackapp stores the data).

**Plan doc:** `docs/plans/2026-09-23-popia-setup-wizard.md` (§W1, §W2) — read it, and the boundaries section at the
bottom of it, before doing anything.

**Gating:** this programme starts only when every row in `PROGRESS.md` reads `done`. Until then the gate script
prints a constant and the model is not woken at all — no message, no cost. Do not "help out" the main programme.

## Phase table

| # | Phase | Status | Tick | Notes |
|---|---|---|---|---|
| W1 | POPIA setup wizard in Settings + generated notice + publish gate | pending | | 5 steps, prefill from app data, Information Officer registration check + link, the practice questions (CCTV, marketing, ID copies, third parties, payments, under-18s, credit checks), Jinja-generated light notice, DB-stored + versioned; all eight s18(1) items asserted present |
| W2 | Operator notice ("how Jackapp stores your data") + download/print | pending | | plain-English technical notice: hosts + regions, access, backups, encryption, incident path, deletion, contact; versioned; printed via §Q1's route |

Status values: `pending` · `in-progress` · `done` · `partial` · `blocked`

## Rules for every tick

1. **Claim once.** Take `docs/plans/.tick-popia.lock` (write the timestamp + phase). If it exists and is under 45
   minutes old, another tick is running — do nothing, append one ledger line, stop.
2. **One phase per tick.** Finish it, or record `partial`/`blocked` with the reason. Never start a second phase.
3. **Never touch** `PROGRESS.md`, `.tick.lock`, the phase table in `2026-09-23-ABI-programme.md`, or any file
   belonging to an unfinished main-programme phase.
4. **Reuse, don't rebuild** §P1 (`consent_records`, `app/services/consent.py`, `/privacy`) and §Q1 (`/popia` pack,
   `document_acceptances`, print route). Missing dependency → `blocked`, not a rebuild.
5. **Tests first-class:** new tests for the phase, then the **whole suite** (must stay green — it was 896+ after
   §P1). Use the main repo's venv: `/mnt/d/Hermes/abi-rental-platform/.venv/bin/python -m pytest`.
6. **Browser proof** at 1440px **and** 390px, screenshots actually looked at, 0 console errors, 0 overflow.
7. **Commit explicit paths only** — never `git add -A`, never `git stash`, never another worker's files. Same branch
   as the main programme.
8. **Update this file:** set the phase row to `done` (or `partial`/`blocked`) and append a tick line with the
   evidence. Keep it to a few lines.

## Tick log

(nothing yet — waiting for the main programme to finish)

<!-- Append one block per tick, newest last:
### Tick N — <timestamp> — <phase> — <status>
- what was built (files)
- tests: N new, full suite N green
- browser proof: N/N, errors, overflow
- commit <sha>
- next: <phase>
-->
