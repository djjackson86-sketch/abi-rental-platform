# Review — Data Processing (Operator) Agreement, Sano Trailers ↔ Jackapp

**Reviewer:** Hermes (assistant) · **Date:** 2026-09-23 · **Reviewed:** `docs/popia/OPERATOR-AGREEMENT-ABI-SANO.md`
(388 lines, 11 clauses + Schedules A–C + signature block, unsigned)
**This is the document that says who is responsible for what between us and Sano** — not the customer-facing
privacy notice (reviewed separately in `PRIVACY-NOTICE-REVIEW.md`).
**Basis:** POPIA ss19, 20, 21, 22, 72 and the Information Regulator's guidance; ECTA 25 of 2002 for electronic
signature; the `popia-compliance` skill's operator-agreement checklist.
**Not legal advice** — both parties' attorneys should review before signature.

---

## 1. Verdict

**This is a genuinely good operator agreement — well above the market average for a small-business SaaS
relationship — and its structure is right.** It states the roles before anything else (clause 1), which is the
single most important thing to get right and the thing most agreements get wrong. It carries every clause POPIA
s21 is there to produce, it is honest about the US leg of the hosting, and it does the two things that protect
*us* specifically: it says plainly that Jackapp is a sole proprietorship whose owner is personally liable, and it
puts a real liability cap in front of that exposure.

**It is not signable yet.** There are **six blockers** — one of them is a factual question that changes the
meaning of the liability clause, and one is the clause the skill specifically says can never be left as a
placeholder (9.3, the cap). Everything else is a bracketed number or a decision that takes one line to fix.

**And it is missing the one thing you went looking for:** there is no single "who is responsible for what" page.
The duties are correctly split across clause 4 (operator) and clause 6 (responsible party), but a signer has to
assemble the picture themselves. **Schedule D (responsibility matrix) is drafted at the bottom of this review,
ready to paste in** — that is the page a client reads before signing.

---

## 2. What it gets right (do not change these)

- **Roles first, and correctly:** 1.2 Sano is the responsible party; 1.3 Jackapp processes *on instruction and on
  behalf of* Sano; 1.5 records that the operator is **not** the information officer and may not process for its
  own purposes. This is the distinction that most agreements fumble.
- **s21(1) named expressly** (1.4) — the agreement exists *because* the Act requires it in writing.
- **Complete s21 substance:** process only on instruction (3.1/4.1), confidentiality flowing to every person who
  touches the data (4.2), security measures with a "may not be materially reduced" brake (4.3 + Schedule B),
  immediate breach notification with content requirements (4.4), assistance with data-subject rights (4.5),
  audit and records (4.7), staff access discipline (4.9), data minimisation (4.10), return-or-delete (clause 10).
- **Sub-processors done properly** (5.1–5.3): named in Schedule C, 30 days' notice of change, right to object,
  flow-down of no-less-protective obligations, and the operator stays fully liable.
- **Breach mechanics that actually work** (4.4): 24-hour backstop, specified content, and the correct allocation —
  the operator must **not** notify the Regulator or data subjects "on Sano's behalf", because that duty is Sano's
  under s22. That is exactly right and rarely seen.
- **Return, delete, and the backup trap** (10.1–10.3): machine-readable export *or* deletion that prevents
  reconstruction, written confirmation, and 10.3 requiring deletions to be re-applied after a restore.
- **Schedule B is specific** — named accounts, no shared logins, branch scoping, TLS, provider encryption at
  rest, named hosts, monitoring, real (not flag-based) deletion. Specifics are what a Regulator can accept.
- **The sole-proprietor reality is stated out loud** and the cap rationale is written into the document
  (note under Parties + the italic note under 9.3). That is the right way to protect a natural person.

---

## 3. Blockers — resolve before signature

- **B1 · The operator's legal identity is ambiguous, and this one matters most.** The header still reads
  *"[OPERATOR LEGAL NAME]"* (line 3), while the Parties block says **"Donovan Jackson trading as Jackapp … banking
  in the name of Abi Solutions"**, legal form "sole proprietorship (no registration number)". Three different names
  for the contracting party. **Confirm: is the operator Donovan Jackson personally, or a registered entity (a
  company/CC called Abi Solutions)?**
  - If **Donovan personally** → the header becomes "Donovan Jackson t/a Jackapp", the sole-proprietor note and the
    whole rationale in 9.3 stand, and the contracting name should be the one on the bank account (or the bank
    account should be explained).
  - If **a registered entity** → the sole-proprietor note is wrong, there *is* a registration number, the
    corporate veil exists, and 9.3's "personal assets" justification changes completely.
  This is exactly the trap the skill warns about ("confirm whether the trading name or the bank-account name is
  the legal one — they differ more often than not"). One answer unblocks the header, the Parties block and 9.3.
- **B2 · Clause 9.3 — the liability cap is still "[RECOMMENDED WORDING — confirm before signature]".** This is the
  one clause that cannot sensibly remain a placeholder. Decide between: **(a)** the drafted cap (total fees paid in
  the preceding 12 months, carve-out only for wilful misconduct/gross negligence) ← recommended; **(b)** a fixed
  rand cap (e.g. R250 000); **(c)** the fees cap with no carve-outs at all (most protective of us); **(d)** no cap
  (unlimited personal exposure — not recommended for a sole proprietor). Also settle the fallback stated in the
  note: if Sano wants an uncapped data-breach exposure, offer **2× the 12-month fees** rather than removing the cap.
- **B3 · Clause 9.4 insurance is still "[Optional — TO CONFIRM]"** with a 30-day placeholder. Answer it: either
  delete the clause or commit to PI/cyber cover. As a sole proprietor, cover is the sensible companion to the cap —
  and it is also the answer to the "what if the cap is exceeded" question Sano will ask.
- **B4 · Clause 11.3 jurisdiction is a choice, not a placeholder:** *"[Magistrate's Court / High Court of South
  Africa, Gauteng Division — TO CONFIRM]"*. Pick one. Magistrates' courts are cheaper and need written consent for
  jurisdiction; the High Court (Gauteng Division, Johannesburg) suits a claim that may exceed the magistrates'
  monetary limit — which is a live consideration given regulator fines and the 9.3(c) carve-out.
- **B5 · Clause 11.4 — the service agreement reference.** *"It [is / is not] a schedule to the parties' service
  agreement dated [DATE]"*. Choose, and if there is no signed service agreement covering fees, scope and support,
  say "is not" and be aware of what that leaves open: **nothing in either document sets out what we are paid, what
  support is owed, or uptime** — a commercial hole, not a POPIA one, and worth closing before signature.
- **B6 · Schedule C rows 4 and 5 are unfilled** (email provider; IT support with production access). Two issues:
  1. The email row's note says "invoices are currently generated as local drafts and not sent by the Platform" —
     **verify against the current app**: ABI does send invoice/document emails, which makes the mail provider a
     live sub-processor processing personal information (customer name, email, invoice content). Name it and its
     location.
  2. IT support with production access: if the answer is "only Jackapp", say so — a named "None (operator's own
     principal only)" is a complete answer, and better than a blank that implies an undisclosed sub-processor.

---

## 4. Bracketed numbers to confirm (all sensible — confirm as drafted unless you disagree)

Clause 4.4 breach notice **24 hours** (with "immediately" in front of it) · 4.5 data-subject assistance **5
business days** · 4.7 records **12 months**, audit notice **14 days**, frequency **once per year** · 5.2 new
sub-processor notice **30 days** · 9.4 insurance **30 days** · 10.1 return/delete **30 days** · 11.2 termination
**30 days**, cure period **14 days**.
Two suggestions: in **4.4**, add *"and a written report within [5] business days"* after the initial notification
(the initial 24-hour call and the full report are different obligations); in **4.7**, the 12-month record period
should be at least as long as the longest retention period Sano itself must meet — cross-refer it to the Retention
Policy rather than fixing a shorter number.

## 5. Gaps worth closing (short additions, real value)

- **G1 · Responsibility matrix (Schedule D)** — drafted below. This is the page Don was looking for.
- **G2 · No clause on who pays for an incident.** Add: each party bears its own costs of responding, and the
  operator bears the reasonable cost of forensic investigation, containment and notification **where the incident
  arose from the operator's breach** of this Agreement.
- **G3 · Electronic signature is not addressed.** Add one sentence so the in-app acceptance we are building (and a
  scanned/printed PDF) works: the parties agree that an electronic signature satisfies any requirement of a
  written signature, in terms of the Electronic Communications and Transactions Act 25 of 2002, provided it
  identifies the party and the document (version/hash) — and that a copy printed from the Platform may be used as
  evidence of it. This is what makes Feature Q legally tidy.
- **G4 · Notices clause.** No clause says how formal notices are given (email addresses, deemed receipt, who may
  give them). One short clause; it also makes the 24-hour breach notice enforceable in practice.
- **G5 · Personnel vetting.** 4.2 covers confidentiality duties; add that production access is granted only to
  people who have signed that undertaking (Schedule B says "individually identifiable accounts" but not "signed
  and vetted").
- **G6 · Clause 8 recommendation is now out of date.** Schedule C recommends moving the application host to
  **EU (Frankfurt)**; Don has declined that (2026-09-23). Replace it with the decision and the alternatives:
  keep Oregon on s72(a) binding terms + s72(c) contractual necessity (the position the agreement already
  describes and which is lawful today), or move the **application host to a South African region** (e.g. Fly.io
  Johannesburg, Turso remaining in Ireland) so the destinations become ZA + EU and the US question disappears —
  no Frankfurt involved. Whichever is chosen, **clause 8.3 already obliges us to notify a location change**, and
  Schedule C and the privacy notice's §5 must be updated in the same change.
- **G7 · Schedule A volumes are a point-in-time snapshot** ("±4 650 customer records; ±146 product records; 4
  user accounts"). Fine as drafted because it says "at signature" — just re-check the numbers on the day it is
  signed so the schedule is truthful.
- **G8 · One cross-reference win:** the privacy notice's outstanding blocker #1 asks for Sano's registered name
  and registration number. **This agreement already carries it** — *Sano Trailers, registration 2018/521057/07,
  VAT 4880322591*. Confirm the exact registered name (the number format indicates a private company) and the
  notice's item 1 is answered.

---

## 6. Draft Schedule D — who is responsible for what

*Paste-ready. Read it top to bottom and it answers the "who does what" question without either party having to
read eleven clauses.*

| Duty | Sano Trailers (responsible party) | Jackapp (operator) |
|---|---|---|
| Lawful basis for processing | **Decides and owns it** | Follows instruction; flags anything unlawful (3.4) |
| Privacy notice to customers | **Issues and maintains it** (§6.2) | Builds/renders it as instructed; stores the consent records |
| Consent capture and proof | **Owns the duty** | Records it and keeps the evidence (who/when/version) |
| Information Officer | **Appoints and registers** with the Regulator (§6.5) | Not the officer (1.5); assists on request |
| PAIA manual and annual report | **Owns both** (§6.2) | Provides system data needed for them |
| Retention periods | **Decides them** (§6.4) and instructs deletion | Implements deletion; re-applies it after a restore (10.3) |
| Data-subject requests (access, correction, objection) | **Decides and responds** | Assists within 5 business days (4.5) |
| Breach: notify the Regulator and data subjects | **Owns it** (POPIA s22) | Notifies Sano **immediately, within 24 hours** (4.4); does not notify third parties on Sano's behalf |
| Breach: investigate and contain | Informed and decides on disclosure | **Investigates, contains, reports, keeps Sano updated** (4.4) |
| Security measures | Satisfied via this Agreement (s21(1)) | **Establishes and maintains them** (4.3 + Schedule B) |
| Choosing and contracting cloud providers | Approves sub-processors and may object (5.1–5.2) | **Selects, contracts, flows obligations down, stays liable** (5.3) |
| Cross-border transfers | Acknowledges and may object (clause 8) | Ensures a s72 ground exists; **notifies before changing jurisdiction** (8.3) |
| Access governance (own users) | Keeps its own credentials secure, manages its staff (§6.6) | Individual accounts, least privilege, prompt revocation (4.9) |
| Marketing consent and opt-outs | **Owns consent** (§6.1, s69) | Implements the suppression/opt-out honouring |
| Audits | May audit once a year or after an incident (4.7) | Keeps records 12 months and makes them available (4.7) |
| On termination | Chooses export and/or deletion (10.1) | **Exports and/or deletes, and confirms in writing** (10.1) |

---

## 7. Suggested order of business

1. Answer **B1** (whose name is on this agreement) — it unblocks the header, the Parties block and the meaning of 9.3.
2. Decide **B2** (cap) and **B3** (insurance). We can recommend, Sano should agree.
3. Pick **B4/B5** (jurisdiction; the service-agreement reference) and close **B6** (email provider + IT support).
4. Fold in **G1–G6** (matrix, incident costs, electronic signature, notices, vetting, the updated clause 8 note).
5. Both parties' attorneys read it, then it is signed — in the app (electronic) and/or on paper, with a printed
   copy kept in Sano's POPIA file.
