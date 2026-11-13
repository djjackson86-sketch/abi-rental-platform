"""Display wording only: invoice identity and financial data remain unchanged."""
from email import policy
from email.parser import BytesParser

import pytest
import pymupdf

from app.db import get_db
from app.routes.documents import _prepare_email_draft
from app.services.documents import DOCUMENT_TYPES, display_document_label
from test_ticket_341953162_document_date import seed
from test_ticket_341953087_mark_document_sent import app, client, login


@pytest.mark.parametrize('kind,status,label', [
    ('invoice', 'finalized', 'Tax Invoice'),
    ('invoice', 'draft', 'Proforma Invoice'),
    ('quote', 'draft', 'Quote'),
    ('contract', 'draft', 'Contract'),
    ('packing_slip', 'draft', 'Packing slip'),
])
def test_labels_views_pdf_email_and_records(client, app, kind, status, label):
    doc_id, before, order = seed(app, kind, status)
    login(client)
    view = client.get(f'/documents/{doc_id}')
    assert view.status_code == 200
    assert f'<strong>{label}</strong>'.encode() in view.data
    assert f'<p class="eyebrow">{label}</p>'.encode() in view.data
    assert label.encode() in client.get('/documents').data
    assert label.encode() in client.get(f'/orders/{order["id"]}').data
    pdf = client.get(f'/documents/{doc_id}/download.pdf')
    assert pdf.status_code == 200
    with pymupdf.open(stream=pdf.data, filetype='pdf') as rendered:
        assert len(rendered) == 1
        text = rendered[0].get_text()
        assert label in text
        if kind in {'invoice', 'quote'}:
            assert f'{label} date:' in text
            spans = [s for b in rendered[0].get_text('dict')['blocks'] if 'lines' in b
                     for line in b['lines'] for s in line['spans']]
            heading = next(s for s in spans if s['text'] == label)
            assert heading['bbox'][0] == 455
            assert heading['bbox'][2] < 569
    with app.app_context():
        db = get_db()
        document = db.execute('SELECT * FROM documents WHERE id = ?', (doc_id,)).fetchone()
        assert display_document_label(document) == label
        assert dict(document) == before
        assert DOCUMENT_TYPES['invoice']['prefix'] == 'INV'
        if kind in {'invoice', 'quote'}:
            _, eml, _ = _prepare_email_draft(doc_id, 'test@example.test')
            message = BytesParser(policy=policy.default).parsebytes(eml)
            assert str(message['Subject']).startswith(label)
            assert f'Please find attached {label}' in message.get_body(preferencelist=('plain',)).get_content()
            attachment = next(message.iter_attachments())
            assert attachment.get_payload(decode=True) == pdf.data
            # Literal custom messages must not be rewritten.
            _, custom_eml, _ = _prepare_email_draft(doc_id, 'test@example.test', message='My custom Invoice text')
            custom = BytesParser(policy=policy.default).parsebytes(custom_eml)
            assert 'My custom Invoice text' in custom.get_body(preferencelist=('plain',)).get_content()
        assert dict(db.execute('SELECT * FROM documents WHERE id = ?', (doc_id,)).fetchone()) == before
        assert dict(db.execute('SELECT * FROM orders WHERE id = ?', (order['id'],)).fetchone()) == order
