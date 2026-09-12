# POPIA Compliance Action Plan — Sano Trailers

**Version:** 1.0 · **Prepared:** [TODAY] · **Status:** to be worked through by Sano Trailers (the responsible party)

This is the practical to-do list behind the Privacy Notice and the Retention Schedule. It reflects what the
Information Regulator has actually been enforcing, not theory.

---

## Part A — Do these first (the Regulator's priority list)

### A0. Who is the responsible party, and who is the operator? (read this before A1)

Getting this wrong assigns the duties to the wrong business.

| Role | Who | What it means |
|---|---|---|
| **Responsible party** | **Sano Trailers** — it decides why and how its customers' information is processed | Carries the POPIA obligations: lawful processing, notices, security, breach notification, data-subject requests, retention, and it is the party the Regulator acts against |
| **Operator** | **ABI / the platform provider** (and the hosting, database and email providers) — they process the information *for* Sano Trailers under contract | Does **not** register an information officer for that processing. Must have a written operator agreement, process only on Sano Trailers' instruction, keep it confidential, secure it, and **report a suspected compromise to Sano Trailers immediately** (POPIA ss 20, 21) |

**Practical consequences:**

- **Sano Trailers must register its own Information Officer** (A1 below). The Information Officer is, by law, **the head of that business** — the owner/MD/CEO — not an outside developer.
- **ABI/the developer does not register as Sano Trailers' Information Officer.** But the developer **is** a responsible party for its own processing — its own staff, its own clients, its own prospects and lead lists — and if it processes personal information for its own purposes it must register its own Information Officer for its own business.
- **An operator agreement must be signed** between Sano Trailers and every operator, including the developer and the hosting/database/email providers. Section 21(1) puts the duty on the responsible party, and enforcement has turned on missing operator agreements. It must cover: process only on instruction, confidentiality, the security measures, sub-processors, breach notification without delay, cross-border terms (s72), and help with data-subject requests.
- **Outsourcing does not transfer liability.** POPIA s21(1) keeps the responsible party answerable; the operator's liability is largely contractual.

### A1. Register the Information Officer ⚠️ compulsory before the duties begin

POPIA section 55(2): **an Information Officer may only take up their duties once registered with the Regulator.**
Registration is **free of charge**.

- The Information Officer **already exists by operation of law** — it is the **head of the business** (for a sole proprietorship, the proprietor; for a company, the CEO/MD or equivalent). You do not "appoint" one; you register who it is.
- The head may **authorise another employee at management level, in writing**, to carry the role operationally — the Regulator's Guidance Note allows delegation only to management level and above. Deputies go below that.
- Register on the Regulator's **eServices portal**: https://eservices.inforegulator.org.za
- **Each subsidiary/separate entity registers its own** officer — one registration does not cover a group.
- Keep proof of registration on file.

**Why it matters even though non-registration is not itself an offence:** until the officer is registered they cannot lawfully take up their duties, the Regulator has no point of contact, and **in the absence of a registered officer the head of the business is held liable for all POPIA matters**. Registration is also how you obtain the eServices credentials used for **breach reporting** and **PAIA annual reports** — you do not want to be creating that login during an incident.

**Why this is first:** the Regulator's enforcement notice of **22 May 2026** (Central Johannesburg TVET College) listed "failure to register the Information Officer" as a finding, and ordered registration within 31 days.

**Who:** the head of Sano Trailers [TO CONFIRM] · **Due:** this week

### A2. Adopt and publish the Privacy Notice

- Publish the Privacy Notice where personal information is collected: the website, the booking/enquiry form, the rental agreement, and the counter where details are taken.
- Add a short collection statement to the **public booking form**: *"We use your details to quote, book and service your rental — see our Privacy Notice at [URL]."*
- Give a version to customers who complete a paper rental agreement.

**Due:** with the next website update

### A3. Put a real consent and opt-out mechanism on marketing

**Current position in the system: of the ~4,650 imported customers, effectively ONE recorded marketing consent.**

- Do **not** send marketing email/SMS/WhatsApp to the imported base. Consent is absent, and POPIA section 69 requires it for non-customers (and an opt-out opportunity at collection for existing customers).
- Add a **marketing opt-in checkbox (unticked by default)** to the booking form and the in-store capture, and record **when and how** consent was given.
- Run a **re-consent campaign** if the client wants to market to the old base — a clear, single-purpose, easily refused ask, with the result recorded per customer.
- Every marketing message must carry a working opt-out, and the opt-out must be honoured permanently.

**Due:** before any marketing is sent

### A4. Breach response — write it down and be ready

POPIA section 22 requires notifying **the Regulator and every affected person** as soon as reasonably possible
after discovering a compromise.

- Write a one-page **incident response procedure**: who to call, how the breach is contained, how it is logged, who decides to notify, the notification template, and the record kept afterwards.
- Register on eServices **now** — breach reports are filed through the same portal, and you do not want to be creating credentials during an incident.
- Keep a **breach register** even for near-misses.

**Note:** the 22 May 2026 enforcement notice also cited failure to notify a security compromise.

---

## Part B — The compliance framework the Regulator expects to see

The same enforcement notice directed the responsible party to submit a **POPIA Compliance Framework** made up of
four documents. That is a good shopping list of what "showing your work" looks like:

| # | Document | Status here |
|---|---|---|
| 1 | **Privacy Policy / Notice** | ✅ drafted — `PRIVACY-NOTICE.md` |
| 2 | **Retention Policy and Schedule** | ✅ drafted — `RETENTION-POLICY-AND-SCHEDULE.md` |
| 3 | **Incident Response Policy** | ⬜ to write (A4 above) |
| 4 | **Information Privacy and Security Policy** | ⬜ to write (Part C below) |

Plus, under PAIA:

| # | Item | Note |
|---|---|---|
| 5 | **PAIA Manual** | Required of every private body. Must list the records we hold and how to request them. |
| 6 | **PAIA Annual Report** | Submitted to the Regulator **once a year**, in the window **1 April – 30 June**. No extensions. |

---

## Part C — Security and access (POPIA sections 19–21)

- **Unique logins for every staff member** — no shared passwords. (Currently: Head office admin, Thabisile Skosana, Pumella Siwundla — Midrand, Zimbili Ndlovu — Rooderport.)
- **Least privilege:** staff see only what their job needs, and branch-limited staff see only their branch. Already enforced in the system.
- **Remove access on the day someone leaves.**
- **Operator agreements** (POPIA sections 20–21): written terms with every third party that touches personal information — hosting, database, email, IT support, accountant. They must keep it confidential, secure it, use it only on our instruction, and tell us about breaches.
- **Backups** that are actually restorable, encrypted, and covered by the same retention rules.
- **Annual review** of who has access, and staff awareness training — at least once a year, with a record that it happened.

---

## Part D — Handling people's rights (the day-to-day obligations)

Create a simple procedure for when a customer asks:

| Request | What we must do | Where |
|---|---|---|
| **Access** — "what do you have about me?" | Verify identity, then provide a record of the personal information held | POPIA s23 |
| **Correction / deletion** | Correct inaccurate data; delete what we are not allowed to keep. If a law requires us to keep it (tax, company), say so and explain | POPIA s24 |
| **Objection to processing** | Stop processing on reasonable grounds | POPIA s11(3) |
| **Marketing opt-out** | Honour immediately and permanently | POPIA s69 |
| **Complaint** | Give them the Regulator's details | POPIA s74 |

Keep a **register of every request**: date received, who, what was asked, what was done, when it was closed.

**Practical note:** the booking system can search customers by name, email or phone, and every customer record
is individually addressable — so an access request is answerable today. What is missing is the *procedure and
the register*, not the capability.

---

## Part E — Specific findings from our data migration (worth acting on)

1. **~4,650 customer records were imported from Booqable** — real personal information, including phone numbers, addresses, vehicle registrations and alternative contacts. The systems now hold live personal information in a foreign-hosted database, so the Privacy Notice (including the cross-border section) needs to be live.
2. **Marketing consent is effectively absent** (1 of 4,650). Do not market to this base without re-consent (A3).
3. **Outstanding balances of R353,805.67 were in the source data but were deliberately NOT imported** — good for data minimisation, but note that money owed is still legally owed and the debtors records for it must still be retained (item 5 of the Retention Schedule).
4. **ID/licence fields were removed** from the customer form earlier — keep it that way. If verification copies are ever uploaded, they need their own short retention rule and secure handling.
5. **Vehicle registration numbers** are personal information tied to a person's vehicle — they are in scope and are covered by the notice and the retention schedule.
6. **SARS record-location rule:** electronic records required by the Tax Administration Act should be kept at a place physically in South Africa unless a senior SARS official authorises otherwise. If the only copy of tax-relevant records is in a foreign-hosted system, confirm with the accountant that an in-country copy (e.g. the accounting package) is retained.
7. **Deletion must actually work.** A quarterly job should list records past their retention period and delete or de-identify them — otherwise the schedule exists only on paper.

---

## Part F — Suggested sequence

1. **This week:** register the Information Officer (and deputies) on eServices; confirm the contact address for privacy requests.
2. **Next:** publish the Privacy Notice; add the collection statement + unticked marketing opt-in to the booking forms; write the Incident Response Policy.
3. **Then:** Information Privacy and Security Policy; operator agreements signed; PAIA Manual.
4. **Ongoing:** PAIA Annual Report by **30 June** each year; annual review of the notice, schedule and access list; quarterly retention cleanup report.

---

*Prepared as a practical implementation plan. Confirm the owner, Information Officer and every bracketed detail
with Sano Trailers, and have their attorney or compliance advisor review before publishing. Nothing here is
legal advice.*
