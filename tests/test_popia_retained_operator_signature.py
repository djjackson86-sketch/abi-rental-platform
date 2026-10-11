"""The retained operator-signed PDF is immutable; only Sano can complete its own fields."""
import hashlib
import json
from pathlib import Path
import pytest
from app.services import popia_contract


def test_retained_signature_and_sano_pending():
    fitz = pytest.importorskip('pymupdf')
    data = popia_contract.contract_pdf()
    doc = fitz.open(stream=data, filetype='pdf')
    fields = {w.field_name: (w.field_value, w.field_flags) for p in doc for w in (p.widgets() or [])}
    assert len(fields) == 14
    assert fields['jackapp_electronic_signature'][0] == 'Donovan Jackson'
    assert fields['jackapp_representative'][0] == 'Donovan Jackson'
    assert fields['jackapp_signed_date'][0] == '11-10-2026'
    assert fields['jackapp_signing_declaration'][0] != 'Off'
    assert all(flags & fitz.PDF_FIELD_IS_READ_ONLY for name, (value, flags) in fields.items() if name.startswith('jackapp_'))
    assert fields['sano_signing_declaration'][0] == 'Off'
    assert all(fields['sano_'+suffix][0] == '' for suffix in ['representative','capacity','signed_date','electronic_signature'])
    unsigned = fitz.open(stream=popia_contract.unsigned_contract_pdf(), filetype='pdf')
    assert ''.join(p.get_text() for p in list(doc)[:-1]) == ''.join(p.get_text() for p in list(unsigned)[:-1])
    for page in doc:
        for word in page.get_text('words'):
            assert word[0] >= 40 and word[2] <= page.rect.width - 40
    # Only synthetic Sano values are used for the save/reopen probe.
    operator_before = {k:v for k,v in fields.items() if k.startswith('jackapp_')}
    for page in doc:
        for w in page.widgets() or []:
            if not w.field_name.startswith('sano_'): continue
            w.field_value = next(v for v in w.button_states()['normal'] if v != 'Off') if w.field_name.endswith('signing_declaration') else 'Synthetic Sano signer'
            w.update()
    reopened = fitz.open(stream=doc.tobytes(), filetype='pdf')
    operator_after = {w.field_name:(w.field_value,w.field_flags) for p in reopened for w in (p.widgets() or []) if w.field_name.startswith('jackapp_')}
    assert operator_after == operator_before


def test_changed_terms_refuse_silent_resigning(monkeypatch):
    monkeypatch.setattr(popia_contract,'contract_text',lambda: 'different terms')
    with pytest.raises(ValueError,match='fresh agreement'):
        popia_contract.contract_pdf()


def test_exact_download_and_signing_record():
    folder = popia_contract.CONTRACT_PATH.parent
    record = json.loads((folder/'SANO-JACKAPP-OPERATOR-SIGNING-RECORD.json').read_text())
    assert record['sano_signed'] is False
    assert record['identity_or_cryptographic_verification'] is False
    assert record['signed_pdf_sha256'] == hashlib.sha256(popia_contract.contract_pdf()).hexdigest()
    assert record['contract_text_sha256'] == hashlib.sha256(popia_contract.contract_text().encode()).hexdigest()
