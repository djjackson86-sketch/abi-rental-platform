# Operator contract — implementation and review notes

The owner requested replacing the downloadable information record with a written contract that both parties can sign electronically. The former client information notice remains in the repository as historical wording; current Settings downloads serve `SANO-JACKAPP-OPERATOR-CONTRACT.md` and an unsigned fillable PDF.

## What the contract covers

Responsible-party/operator roles; authorised processing scope and duration; lawful documented instructions; confidentiality; section 19 security undertakings; necessary providers and section 72 overseas-transfer obligations; immediate operator incident notification under section 21(2); customer-request assistance; return/export/deletion and retained backups; existing commercial terms; and electronic signing by both authorised representatives.

## Signing mechanism

- Fourteen AcroForm fields, including separate signature-name, signing-date and unchecked intention declarations for each party.
- Legal-party details may be prefilled, but representative names, signature names, dates and declarations are never pre-signed.
- Sano's full legal name must be confirmed by its representative. Jackapp's legal identity was taken from the existing operator draft: Donovan Jackson trading as Jackapp, a sole proprietorship. Both parties should confirm the details before signing.
- Ordinary electronic signing in a compatible PDF editor, not cryptographic/advanced electronic-signature certification or identity verification. A suitable third-party electronic-signature service remains an alternative.
- Both parties exchange and retain the same completed contract; effectiveness is on the second signature. Downloading neither signs nor stores an acceptance in the app.
- The source-text SHA-256 identifies the contract terms only, not the completed PDF or signers.

## Review points before actual execution

This is drafting/implementation work, not a legal opinion. Have the parties' authorised representatives and, where appropriate, a South African lawyer review the actual terms and legal identities. Confirm any existing service agreement's amendment or signature requirements. Confirm suitable overseas-provider safeguards; mentioning section 72 is not evidence that provider terms have been verified.

No new indemnity, penalty or arbitrary liability cap has been invented. Valid existing limits, if any, are preserved. The operator is a sole proprietor and is personally exposed to contractual obligations; a separate liability limitation should be negotiated if wanted, not assumed.

## Verification

Lint signing-page layout for field collisions and page bounds. Inspect the actual generated PDF widgets, blank signature/date fields and unticked declarations. Fill both sides only with synthetic test values, save and reopen to prove field persistence while the contract terms remain unchanged. Render and inspect the unsigned signing page. Test the canonical `/settings/popia/operator-contract.pdf` plus both older aliases, owner-only guards, unchanged customer notice publication and no production record writes.

## Legal sources

- POPIA sections 19–21: https://www.gov.za/sites/default/files/gcis_document/201409/3706726-11act4of2013protectionofpersonalinforcorrect.pdf
- ECTA sections 12, 13 and 22: https://www.gov.za/sites/default/files/gcis_document/201409/a25-02.pdf
