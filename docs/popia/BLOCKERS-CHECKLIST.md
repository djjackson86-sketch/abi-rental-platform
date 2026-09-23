# POPIA blockers checklist — what Sano Trailers must supply

**Purpose:** the four facts only Sano can supply, so the privacy notice can be published and the app's POPIA pack
can be adopted. Nothing in the app will present the notice for acceptance while any item here is open (that gate
is built into phase 14 / Feature Q).

**Owner:** Don (to collect from Sano) · **Status:** open · **Where it lands:** `docs/popia/PRIVACY-NOTICE.md` v1.1
(tokens already in place) and `docs/popia/OPERATOR-AGREEMENT-ABI-SANO.md`

---

## 1. Registered name and registration number of the responsible party

- **Needed:** the exact registered legal name (`Sano Trailers (Pty) Ltd`? a CC? a sole proprietor trading as
  Sano Trailers?), and the registration number. If it is a sole proprietor, we need the proprietor's full name
  instead and the notice will read "*[Name]* t/a Sano Trailers".
- **Why:** s18(1)(b) requires the notice to identify the responsible party. A trading name alone does not.
- **Where:** `PRIVACY-NOTICE.md` §1, and the "Parties" block of the operator agreement.
- **Note:** also confirm which name the bank account is in — it is not always the trading name.

## 2. Information Officer — full name **and** registration

- **Needed:** the full name of the Information Officer, plus confirmation that they have been registered at
  https://eservices.inforegulator.org.za (free, and the officer may only take up their duties after
  registration — POPIA s55(2)). The officer is the head of the business by operation of law; the head may
  authorise a management-level employee **in writing** to act.
- **Why:** this is the single most common enforcement finding in this market — the Regulator's 2026 notice cited
  exactly this failure. The notice must never print a name for an unregistered officer.
- **Where:** `PRIVACY-NOTICE.md` §1 and the operator agreement's Parties block.
- **Bonus while you are there:** the same portal files breach notifications, so registering early matters.

## 3. Effective date

- **Needed:** the date Sano publishes the notice (usually the date it goes live on the app and at the branches).
- **Why:** consent has to be tied to a dated version — "the notice in force when this customer ticked the box".
- **Where:** `PRIVACY-NOTICE.md` header ("Effective date").

## 4. The public web address of the notice

- **Needed:** the URL of the `/privacy` page once it ships (it will be on the app's public domain, and mirrored
  at the branches as a printed copy at the counter).
- **Why:** s18 says the notice must be given where collection happens; a dead or missing link is the easiest
  thing for a complainant to point at.
- **Where:** `PRIVACY-NOTICE.md` §12 (and the store footer, once Feature P ships).

---

## Operator-side blockers — ours to answer (`docs/popia/OPERATOR-AGREEMENT-REVIEW.md`)

The agreement is the document Sano signs; these six items block that signature.

- **B1 · Whose name is on it?** The header still says `[OPERATOR LEGAL NAME]`, the Parties block says *"Donovan
  Jackson trading as Jackapp"*, and it notes the banking is *"in the name of Abi Solutions"* — three names for one
  contracting party. **Confirm the legal form**: Donovan Jackson personally, or a registered entity? If an entity,
  the sole-proprietor note and the whole reason for the liability cap are wrong. This unblocks the header, the
  Parties block and clause 9.3 at once.
- **B2 · Clause 9.3 — the liability cap must be decided** (it is still marked *RECOMMENDED WORDING*): fees-paid-in-
  12-months cap with only wilful/gross-negligence carved out (recommended) · a fixed rand cap · fees cap with no
  carve-outs · or uncapped (not recommended for a sole proprietor).
- **B3 · Clause 9.4 — PI/cyber insurance**: commit, or delete the clause.
- **B4 · Clause 11.3 — jurisdiction**: Magistrates' Court or High Court (Gauteng Division)?
- **B5 · Clause 11.4 — is it a schedule to a service agreement dated when?** (and if no such agreement exists, that
  leaves fees, support and uptime unrecorded anywhere — a commercial hole worth closing before signing).
- **B6 · Schedule C rows 4 and 5** — the email provider (verify: the Platform *does* send invoice/document emails,
  which makes the mail provider a live sub-processor) and IT support with production access.

**One win to bank:** the privacy notice's item 1 (registered name + registration number) is **already answered
here** — the agreement carries *Sano Trailers, registration 2018/521057/07, VAT 4880322591*. Confirm the exact
registered company name and that notice item is closed.

## Two further items that are **ours** (the developer's) to close, not Sano's

- **Operator agreement signatures** — `OPERATOR-AGREEMENT-ABI-SANO.md` is still unsigned and carries three
  `[TO CONFIRM]` sub-processor rows (email provider; IT support with production access). s21(1) requires the
  data-processing relationship to be in a **written contract**; a missing agreement is its own finding. We sign,
  Sano signs, Schedule C gets the real provider names.
- **Sub-processor list** — confirm the actual providers in use for email delivery and any IT support access, and
  their hosting locations, so Schedule C is truthful.

## Also worth deciding (not blockers, but they will come up)

1. **CCTV** — the notice now carries a conditional CCTV paragraph. Say per branch whether cameras exist; if none
   do, the paragraph gets deleted (a notice that retains something it never said it collects is worse than
   silence).
2. **PAIA** — Sano needs a PAIA manual and the annual PAIA report to the Regulator in the **1 April – 30 June**
   window. Confirm who is owning it (the action plan Part B assumes Sano).
3. **Marketing re-consent** — the customer import recorded almost no consent, so no email/SMS/WhatsApp marketing
   may go out until consent is captured again (s69).
4. **Retention must run, not just exist** — the schedule is written; a recurring report that lists records past
   their period and deletes or de-identifies them is what makes s14 real.
5. **Cross-border** — the notice discloses the US (Oregon) application host. Options and the recommendation are
   in the assistant's review, §5; the paper position is already valid under s72, so this is risk reduction
   rather than a defect.
