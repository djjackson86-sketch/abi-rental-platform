"""The downloaded contract supports intentional ordinary PDF electronic signing by both parties."""
import hashlib
from io import BytesIO

import pytest
from app.services.popia_contract import contract_pdf, contract_text, signing_field_spec


def test_signing_layout_has_two_complete_blank_signatures_and_no_collisions():
    fields = signing_field_spec()['fields']
    assert len(fields) == 14
    for prefix in ('sano', 'jackapp'):
        by_name = {field['name']:field for field in fields}
        assert by_name[prefix+'_electronic_signature']['value'] == ''
        assert by_name[prefix+'_signed_date']['value'] == ''
        assert by_name[prefix+'_signing_declaration']['checked'] is False
        assert by_name[prefix+'_signing_declaration']['required']
    for index, field in enumerate(fields):
        x,y,right,top=field['entry_box']
        assert 0 <= x < right <= 595.28 and 0 <= y < top <= 841.9
        for other in fields[index+1:]:
            a,b,c,d=other['entry_box']
            assert min(right,c) <= max(x,a) or min(top,d) <= max(y,b)


def test_actual_pdf_widgets_and_unsigned_state():
    fitz=pytest.importorskip('pymupdf')
    doc=fitz.open(stream=contract_pdf(),filetype='pdf')
    widgets=[widget for page in doc for widget in (page.widgets() or [])]
    assert len(widgets)==14
    assert all(widget.field_type != fitz.PDF_WIDGET_TYPE_SIGNATURE for widget in widgets)
    # These are ordinary typed-name/intent fields, not certified digital signatures.
    by_name={widget.field_name:widget for widget in widgets}
    for prefix in ('sano','jackapp'):
        assert by_name[prefix+'_electronic_signature'].field_value == ''
        assert by_name[prefix+'_representative'].field_value == ''
        assert by_name[prefix+'_signed_date'].field_value == ''
        assert by_name[prefix+'_signing_declaration'].field_value == 'Off'
    text=' '.join(' '.join(page.get_text().split()) for page in doc)
    assert '11. Electronic signing and copies' in text
    assert 'Electronic signing' in text
    assert 'the date the second party signs' in text
    assert 'cryptographic signature' in text
    assert hashlib.sha256(contract_text().encode()).hexdigest() in text
    for page in doc:
        assert abs(page.rect.width-595.276)<.1 and abs(page.rect.height-841.89)<.1
        for word in page.get_text('words'):
            assert word[0]>=40 and word[2]<=page.rect.width-40,word
    doc.close()


def test_both_parties_can_complete_save_and_reopen_form_without_losing_terms():
    fitz=pytest.importorskip('pymupdf')
    data=contract_pdf()
    doc=fitz.open(stream=data,filetype='pdf')
    original_terms=' '.join(page.get_text() for page in list(doc)[:-1])
    expected={}
    for page in doc:
        for widget in page.widgets() or []:
            prefix=widget.field_name.split('_',1)[0]
            if widget.field_name.endswith('_signing_declaration'):
                value=next(value for value in widget.button_states()['normal'] if value!='Off')
            elif widget.field_name.endswith('_signed_date'):
                value='11-10-2026'
            else:
                value='Synthetic '+prefix+' test signer'
            widget.field_value=value
            widget.update()
            expected[widget.field_name]=value
    filled=doc.tobytes()
    doc.close()
    reopened=fitz.open(stream=filled,filetype='pdf')
    actual={widget.field_name:widget.field_value for page in reopened for widget in (page.widgets() or [])}
    assert actual==expected
    assert ' '.join(page.get_text() for page in list(reopened)[:-1]) == original_terms
    reopened.close()


def test_contract_terms_cover_operator_security_without_fabricated_compliance():
    text=contract_text()
    for term in ['section 21', 'section 19', 'documented instructions', 'confidential',
                 'immediately', 'section 72', 'second party signs', 'not a certificate',
                 'Donovan Jackson', 'personally']:
        assert term in text
    assert '[OPERATOR LEGAL NAME]' not in text
    assert 'AES-256' not in text
