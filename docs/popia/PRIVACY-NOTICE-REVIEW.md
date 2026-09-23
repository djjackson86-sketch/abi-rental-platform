# Review — Privacy Notice pack vs POPIA (Act 4 of 2013)

**Reviewer:** Hermes (assistant) · **Date:** 2026-09-23 · **Reviewed:** `docs/popia/` —
`PRIVACY-NOTICE.md` (v1.0), `RETENTION-POLICY-AND-SCHEDULE.md`, `POPIA-COMPLIANCE-ACTION-PLAN.md`,
`OPERATOR-AGREEMENT-ABI-SANO.md`
**Basis:** s18(1) notice checklist, s11 lawful basis, s14 retention, s19–21 security/operator duties, s22 breach
notification, s55 Information Officer, s69 marketing, s71 automated decisions, s72 cross-border — plus the
Information Regulator's guidance and 2025–2026 enforcement practice.
**Not legal advice.** Sano's attorney/accountant should sign it off before publication.

---

## 1. Verdict

**As a privacy notice, this is strong — materially better than the average SA rental notice — and it will hold
up against the s18(1) checklist.** It covers all eight statutory disclosure heads, writes them in plain English,
and goes further than the Act demands in three places (named cross-border destinations with the reasoning, a
marketing rule stated per s69, and an explicit "no automated decision-making" statement).

**It cannot be published as it stands**, because it still contains four unresolved fields — one of them the
Information Officer's name — and because three content gaps would attract a reviewer's question. None of them is
structural; all are a short edit.

**The bigger exposure is not the notice at all.** It is the operational duties around it: an **unregistered
Information Officer** (s55(2)), an **unsigned operator agreement** (s21(1)), and **breach readiness** (s22).
Enforcement in the Regulator's own 2026 notice turned on exactly those three things, not on wording.

---

## 2. s18(1) checklist — line-by-line

| s18(1) requirement | Where | Result |
|---|---|---|
| (a) what information is collected, and **the source** where not from the data subject | §2 | **Partial** — the categories table is thorough, but it never states the source, and one collection channel is missing (see G3, G4) |
| (b) name and address of the responsible party | §1 (229 Summit Road, Midrand; tel; email) | **Partial** — trading name and physical address are there, but the **legal entity** (Pty Ltd / CC / sole proprietor) and registration number are not (see B3) |
| (c) purpose of collection | §3 purpose table | **Pass** — nine purposes, each with its lawful basis named |
| (d) voluntary or mandatory + consequences of not providing | §3 closing paragraph | **Pass** — explicitly voluntary, with the practical consequence (no hire, no invoice, no deposit refund) |
| (e) any law requiring/authorising collection | §3, "Legal obligation" row | **Partial** — the basis is asserted but **no statute is named** (see G1) |
| (f) transfer outside SA + level of protection | §5 | **Pass** — names the EU (Ireland) and US (Oregon) legs and the s72 route for each. Best-practice level |
| (g) recipients or categories of recipients | §4 | **Pass** — six named categories, including debt collectors/attorneys, which most notices hide |
| (h) rights (access s23, correction/deletion s24, objection s11(3), opt-out s69, complaint s74) + Regulator's contact details | §7 + §11 | **Pass** — full rights list, how to exercise them, proof-of-identity caveat, fee caveat, and the Regulator's address, complaints email, general email and website |

Beyond s18: s11 lawful basis ✅ (§3) · s14 retention ⚠️ (summary correct, details in the retention policy —
see G5) · s19 security measures ✅ (§9) · s22 breach notification ✅ (§9 names both the Regulator and affected
people) · s69 marketing ✅ (§8) · s71 automated decisions ✅ (§3) · children ✅ (§2) · special personal
information ✅ (§2) · rectification/objection mechanics ✅ (§7).

---

## 3. Blockers — fix before publication

- **B1 · Information Officer name is blank** (`PRIVACY-NOTICE.md:18` — `[NAME TO CONFIRM]`). This is the one
  field a reviewer looks for first, and it is also the statutory weak point: **s55(2) says the officer may only
  take up their duties once registered**, registration is free at
  https://eservices.inforegulator.org.za, and the Regulator's own 2026 enforcement notice cited failure to
  register. Fill in the name **and** confirm the registration is done — the notice should never print a name for
  an unregistered officer.
- **B2 · Effective date and last-reviewed date are blank** (`:3`). A version "1.0" with no date cannot be
  evidenced as "the version in force when this customer consented".
- **B3 · The responsible party is described only by trading name.** Add the legal entity and registration
  number (`Sano Trailers (Pty) Ltd · Reg no. …`) or, if a sole proprietor, "… t/a Sano Trailers" with the
  proprietor's name. "Who is the responsible party" must be answerable from the notice alone.
- **B4 · The published-home URL is blank** (`:203` — `[WEBSITE URL]`). Point it at the app's `/privacy` page
  (which Feature P builds from this very document) and add the branch/counter copy line for walk-ins.
- **B5 · The operator agreement is unsigned and still carries three `[TO CONFIRM]` sub-processor rows**
  (`OPERATOR-AGREEMENT-ABI-SANO.md:20`, `:352`, `:353`) and a missing contact person. s21(1) requires a written
  contract — the document existing is not the same as the duty being met, and a missing agreement has driven a
  real enforcement finding in this market. This is our (the developer's) side of the paperwork: get it signed
  by both parties.

---

## 4. Content gaps worth fixing (short edits, real questions avoided)

- **G1 · Name the statutes** in §3's "Legal obligation" row: Tax Administration Act 28 of 2011 s29, Companies
  Act 71 of 2008 s24, and — new with the trailer-rental work — the **National Road Traffic Act** for vehicle
  registration/hire records. s18(1)(e) asks *which* law; "required by law" invites the follow-up question.
- **G2 · CCTV is inconsistent.** §6 (retention) promises a CCTV rolling window, but §2 (what we collect) never
  mentions CCTV and §4 never names security/CCTV as a recipient. Either add CCTV to §2 + §4 (and put up the
  signage naming the responsible party, which is the real s18 duty for cameras) or delete the CCTV line from §6.
  A notice that retains something it never said it collects is worse than saying nothing.
- **G3 · The licence-disc scan is not disclosed as a collection channel.** This is now live work: the app
  captures **number plate, NaTIS registration number, VIN, engine number, make, model, colour and licence
  expiry** by scanning a vehicle licence disc. §2 says vehicle details are collected "where applicable and
  voluntarily provided", which understates it. Add a line naming the disc/barcode scan and stating that the
  registration number and VIN **are personal information** (they attach to an identifiable person).
- **G4 · Say where the information comes from** (s18(1)(a)) — normally the data subject. If any of it ever comes
  from a third party (tracing agent, insurer, credit bureau), that source must be named. One sentence covers it.
- **G5 · Deletion mechanics — add backups and non-reconstructibility.** §6 says records are deleted or
  de-identified; add "de-identified so that it cannot be reconstructed or linked back to you", and "where a
  backup is restored, we re-apply any deletions that happened after it was taken". The retention policy's §3
  covers this; the notice should echo one line of it.
- **G6 · The alternative contact never sees this notice.** §2 collects their name and number (provided by the
  customer), but that person is a data subject we hold information about. Add a line: "we will tell your
  alternative contact what we hold about them, and why" — cheap, and it removes the obvious follow-up.
- **G7 · Material-change notification.** §12 promises an annual review; add "where a change materially affects
  you, we will tell you" — and tie the version number to the app's `PRIVACY_NOTICE_VERSION` so the published page
  and this document can never drift (Feature P already pins this with a test).

---

## 5. The risk that is bigger than the notice (say it plainly)

1. **Information Officer registration (s55(2))** — free, one sitting on the eServices portal, and the single
   most common finding. Until it is done, "the head is held liable for all POPIA matters".
2. **Operator agreement (s21(1))** — needs both signatures. Ours to chase; the document is ready apart from
   three sub-processor rows.
3. **Breach response readiness (s22)** — the notification duty runs to **both** the Regulator and the affected
   people, and the Regulator's portal is where it is filed. Have the register and the contact list before an
   incident, not during one.
4. **Cross-border (s72)** — the notice honestly discloses the **US (Oregon)** application host, which is the
   weak leg (no general adequacy finding; it rests on the provider's binding terms plus contractual necessity).
   **Render supports an EU (Frankfurt) region**: recreating the service there collapses the transfer to a single
   cleanly protected destination and simplifies §5 to one paragraph. Worth doing before the next review.
5. **PAIA runs in parallel** — a PAIA manual for Sano, and the annual PAIA report to the Regulator in the
   **1 April – 30 June** window (no extensions). Confirm the action plan assigns it.
6. **Retention must become a job, not a document** — the schedule is excellent, but nothing yet *runs* it. A
   recurring report that lists records past their period, then deletes or de-identifies them, is what makes s14
   real. (Also: SARS's public notice expects tax-relevant electronic records to be kept **in South Africa** —
   confirm the accountant holds an in-country copy.)
7. **Marketing consent** — the Booqable import showed almost no recorded consent. Nothing may be sent by email,
   SMS or WhatsApp until re-consent is captured (s69); the notice's §8 is the correct rule to implement against.

---

## 6. What "accept" and "print" mean, legally (for the in-app pack)

- **Privacy notice** — not a contract. Sano is the *author* of it: an in-app **"Adopted v1.0 by <name>, <date>"**
  record is precisely the evidence you want, and it is what makes the version number meaningful later.
- **Retention policy / action plan** — internal compliance documents: acceptance records the date they were
  adopted and by whom. Same mechanism, no signature needed.
- **Operator agreement** — this one *is* a contract and s21(1) requires it **in writing**. An in-app acceptance
  is an electronic signature; under the Electronic Communications and Transactions Act 25 of 2002 that is valid,
  provided the acceptance is recorded with the document's identity/version and the person accepting. To be safe
  in practice: print output should carry the full signature block, a statement of when and by whom it was
  accepted electronically, and a **document hash**, and we should countersign our side so both parties are on the
  record. Offer Sano wet-ink as an option — some businesses still want it, and it costs nothing to support.
- **Print must produce the full pack** (notice, retention schedule, action plan, operator agreement + the
  acceptance certificate), because the Regulator's framework expectation is that these exist as documents Sano
  can produce on demand.
- **Nothing may be presented for acceptance while it still holds `[TO CONFIRM]` text** (see §3) — that gate is
  build into phase 14.
