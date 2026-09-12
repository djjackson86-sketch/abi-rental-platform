# Retention Policy and Schedule — Sano Trailers

**Version:** 1.0 · **Owner:** Information Officer · **Review:** annually, or when processing changes

POPIA does not set a single "keep it for X years" rule. **Section 14** says records of personal information must
**not be kept longer than necessary** for the purpose they were collected, unless keeping them is:

- **(a) required or authorised by law** → tax, company, employment statutes;
- **(b) reasonably required for our lawful purposes** → defending or pursuing a claim, recovering property;
- **(c) required by a contract** with the person; or
- **(d) consented to** by the person.

Then **section 14(4)–(5)** requires us to delete, destroy or de-identify the record as soon as reasonably
practicable once we are no longer allowed to keep it — and in a way that **prevents it being reconstructed**.

> **The practical rule:** where more than one law applies to the same record, keep it for the **longest** period
> any of them requires. For a company that is almost always **7 years** (Companies Act), even where tax law only
> asks for 5.

---

## 1. The schedule

| # | Record type | Keep for | Why (legal anchor) | At expiry |
|---|---|---|---|---|
| 1 | **Customer master data** — name, contact details, address, alternative contact | While an active customer, **then 3 years** after the last transaction | Contract + legitimate interest (recovery, repeat business); 3 years aligns with the prescription period for contractual claims | Delete or de-identify |
| 2 | **Rental agreements / orders** — what was hired, period, branch, return condition, damages | **7 years** | Companies Act s24 (general 7-year rule for company records); evidence for claims | Delete (backups age out) |
| 3 | **Invoices, credit notes, VAT records** | **5 years** from date of the relevant return, **or 7 years** as a company record | Tax Administration Act s29; Companies Act s24 | Delete |
| 4 | **Payment records, deposit refunds, proof of payment** | **5 years** (7 as company record) | TAA s29; Companies Act | Delete |
| 5 | **Outstanding-balance / debtors records** | **7 years**, or **3 years after the debt prescribes and collection is abandoned** | Companies Act; Prescription Act 68 of 1969 | Delete |
| 6 | **Identity document / driver's licence copies** taken for verification | **Delete as soon as verification is done and the rental is closed and settled** — never keep "just in case" | POPIA s14(1): no longer necessary once the purpose is met; s14(4) requires deletion | Secure delete |
| 7 | **Vehicle details** (make, colour, registration) of the towing vehicle | Same as the rental agreement — **7 years** | Companies Act; useful if a third party is traced | Delete |
| 8 | **Verification flag / notes** (e.g. "client verification: yes") | Same as the rental agreement — **7 years** | Evidence of the decision we made about the person (POPIA s14(3)) | Delete |
| 9 | **Complaints, disputes, incident and damage files** | **7 years**, or until the matter + appeal period is finally closed, whichever is longer | POPIA s14(3)(a); legal proceedings | Delete |
| 10 | **Marketing consent records and opt-outs** | Consent: while it is relied on. **Opt-out: indefinitely** | POPIA s69 — we must be able to prove we may contact you, and honour a refusal forever | Opt-out list retained de-identified |
| 11 | **Website / booking enquiries that never became a customer** | **12 months** | No lasting purpose | Delete |
| 12 | **Website technical logs** (IP, access) | **3 months** rolling | Security and fraud prevention only | Auto-delete |
| 13 | **CCTV footage** (if a branch has cameras) | **30 days** rolling, then longer only for a specific incident under investigation | Purpose-limited; incident clips become evidence | Overwrite / delete |
| 14 | **Staff / employment records** | **3 years** after termination (BCEA and tax minimums) | BCEA; TAA | Delete |
| 15 | **Job applicant records** (unsuccessful) | **6 months** | No lasting purpose | Delete |
| 16 | **Backups of live data** | Follow the live record's retention; **any record restored from backup must be re-deleted** | Consistency with s14 | Overwrite on rotation |
| 17 | **Sales / service history kept for reporting after the person is de-identified** | May be kept **indefinitely** once genuinely de-identified | POPIA no longer applies to information that cannot identify anyone | De-identify, then keep |

**Never keep:** copies of ID documents "just in case", credit-card numbers, or a second shadow copy of customer
data in someone's personal email, WhatsApp or desktop. Those are the records that cause breaches.

---

## 2. Legal holds (the exception that overrides everything above)

If any of the following is live, **stop deletion** and keep the record until it is resolved:

- SARS has notified us of an **audit, investigation, objection or appeal** → keep until concluded, even beyond 5 years.
- A **claim or legal proceeding** has been instituted or is reasonably contemplated → keep until finalised plus the appeal period.
- A **regulator, court or law-enforcement request** is pending.
- A **security compromise** is under investigation.

Record the hold, the reason and the date, and release it when the matter closes. The Information Officer keeps
this list.

---

## 3. How deletion is done (POPIA s14(5))

- **In the live system:** delete the record properly (not just hide it). Where a record must be kept for reporting, replace the identifying fields with a non-identifying reference — not with another identifier that can be linked back.
- **In backups:** allow them to age out on their normal rotation. If a backup is ever restored, re-apply the deletions before the data goes back into use.
- **On paper:** cross-shred or incinerate.
- **De-identification must be real:** strip everything that identifies, could identify, or could be linked to a person by any reasonably foreseeable method.

---

## 4. Who does what

| Role | Responsibility |
|---|---|
| **Information Officer** | Owns this policy; approves exceptions and legal holds; reviews annually |
| **Branch managers** | Make sure staff do not create shadow copies; escalate anything that looks like a deletion request |
| **System administrator** | Applies deletions in the booking system; keeps the retention schedule technically possible |
| **All staff** | Do not keep customer information in personal email/WhatsApp; do not copy ID documents |

---

## 5. Practical notes for our booking system (ABI/Sano platform)

1. **Retention must be actionable, not aspirational.** The system currently holds ~4,650 customer records, 146 products and order history. To prove compliance, run a **quarterly report** listing records that have passed their retention period, and delete or de-identify them.
2. **ID/licence fields were removed** from the customer form — good. If a copy of an ID is ever uploaded, it needs its own short retention rule and secure storage (item 6 above).
3. **Marketing consent is thin.** Of the imported customer base, effectively **one customer consented** to marketing. Treat the rest as **no consent**: do not send marketing to them without a proper re-consent campaign. (See the Compliance Action Plan.)
4. **Customer data is hosted outside South Africa** (AWS EU/Ireland and a global application host). For POPIA this is permitted under section 72 with a substantially similar level of protection, and it is disclosed in the Privacy Notice. **Separate rule worth knowing:** SARS' public notice on electronic record-keeping expects electronic records to be kept at a place physically in South Africa unless a senior SARS official authorises otherwise — so if the tax/accounting records required by the Tax Administration Act live only in this foreign-hosted system, that point should be checked with the accountant (keeping an accounting export in South Africa, e.g. on the office PC/accounting package, satisfies it in practice).
5. **Deleting a customer in the app** should not be the only mechanism — it must also be reflected in reporting extracts and any local exports.

---

## 6. Review log

| Date | Version | Change | By |
|---|---|---|---|
| [TODAY] | 1.0 | Initial policy | [TO CONFIRM] |

---

*Practical template prepared for Sano Trailers. Retention periods are a legal judgement on the facts of the
business: have the accountant or attorney confirm items 1–17 before relying on them, particularly the
company-vs-tax interaction and any FICA obligations if the business ever becomes an accountable institution.*
