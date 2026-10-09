import email
from email import policy

import pytest

from app.db import get_db, init_db
from app.services.documents import effective_document_date, get_document, list_documents, save_document_date
from test_ticket_341953087_mark_document_sent import (
    app, client, login, login_staff, add_staff, _seed_order, _seed_document,
)


def seed(app, kind='invoice', status='draft'):
    with app.app_context():
        db = get_db()
        order_id = _seed_order(db)
        document_id = _seed_document(db, order_id, kind, status)
        if kind == 'invoice' and status == 'draft':
            db.execute("UPDATE documents SET number = '' WHERE id = ?", (document_id,))
        db.commit()
        before = dict(db.execute('SELECT * FROM documents WHERE id = ?', (document_id,)).fetchone())
        order = dict(db.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone())
    return document_id, before, order


@pytest.mark.parametrize('kind,status', [('invoice', 'draft'), ('invoice', 'finalized'), ('quote', 'draft'), ('quote', 'accepted')])
def test_save_reload_pdf_and_email_parity_preserves_document_and_order(client, app, kind, status):
    doc_id, before, order = seed(app, kind, status)
    login(client)
    original = client.get(f'/documents/{doc_id}/download.pdf')
    assert b'(2026-10-02)' in original.data
    result = client.post(f'/documents/{doc_id}/date', data={'document_date': '2024-02-29'}, follow_redirects=True)
    assert result.status_code == 200
    assert b'Document date saved' in result.data
    assert b'value="2024-02-29"' in result.data
    assert b'date:</strong> 2024-02-29' in result.data
    assert result.data.index(b'id="document-date"') < result.data.index(b'class="document-page')
    assert b'no-store' in result.headers['Cache-Control'].encode()
    assert b'date:</strong> 2024-02-29' in client.get(f'/documents/{doc_id}').data
    pdf = client.get(f'/documents/{doc_id}/download.pdf')
    assert pdf.status_code == 200 and b'(2024-02-29)' in pdf.data
    assert pdf.data != original.data
    assert 'no-store' in pdf.headers['Cache-Control']
    inline_pdf = client.get(f'/documents/{doc_id}/download.pdf?view=1')
    assert inline_pdf.data == pdf.data
    draft = client.post(f'/documents/{doc_id}/send-email', data={'to_email': 'date@example.test'})
    assert draft.status_code == 200
    message = email.message_from_bytes(draft.data, policy=policy.default)
    attachment = next(p for p in message.walk() if p.get_content_type() == 'application/pdf')
    assert attachment.get_payload(decode=True) == pdf.data
    with app.app_context():
        db = get_db()
        after = dict(db.execute('SELECT * FROM documents WHERE id = ?', (doc_id,)).fetchone())
        # Email preparation deliberately updates its own fields, not issue/creation/status/number.
        for key in before:
            if key not in {'document_date', 'email_status', 'sent_to', 'sent_at', 'email_error'}:
                assert after[key] == before[key], key
        assert after['document_date'] == '2024-02-29'
        assert dict(db.execute('SELECT * FROM orders WHERE id = ?', (order['id'],)).fetchone()) == order
        # Date filters retain creation-date semantics.
        assert any(d['id'] == doc_id for d in list_documents(start_date='2026-10-02', end_date='2026-10-02'))
        assert not list_documents(start_date='2024-02-29', end_date='2024-02-29')


@pytest.mark.parametrize('value', ['', '2025-02-29', '2024-02-30', '2026-13-01', '0000-01-01', '20261009', '2026-1-01', '2026-10-09T12:00', ' 2026-10-09', 'not-a-date'])
def test_invalid_date_does_not_write(client, app, value):
    doc_id, before, _ = seed(app)
    login(client)
    result = client.post(f'/documents/{doc_id}/date', data={'document_date': value}, follow_redirects=True)
    assert b'Enter a valid document date' in result.data
    with app.app_context():
        assert dict(get_db().execute('SELECT * FROM documents WHERE id = ?', (doc_id,)).fetchone()) == before


def test_missing_date_and_document(client, app):
    doc_id, _, _ = seed(app)
    login(client)
    assert b'Enter a valid document date' in client.post(f'/documents/{doc_id}/date', follow_redirects=True).data
    assert client.post('/documents/99999/date', data={'document_date': '2026-10-09'}).status_code == 404


@pytest.mark.parametrize('kind', ['contract', 'packing_slip'])
def test_unsupported_types(client, app, kind):
    doc_id, before, _ = seed(app, kind)
    login(client)
    assert b'id="document-date"' not in client.get(f'/documents/{doc_id}').data
    result = client.post(f'/documents/{doc_id}/date', data={'document_date': '2026-10-09'}, follow_redirects=True)
    assert b'Date editing is available for invoices and quotes only' in result.data
    with app.app_context():
        assert dict(get_db().execute('SELECT * FROM documents WHERE id = ?', (doc_id,)).fetchone()) == before


@pytest.mark.parametrize('branch,modules,expected', [(1, ['orders'], 302), (2, ['orders'], 404), (1, ['customers'], 403)])
def test_access(client, app, branch, modules, expected):
    doc_id, before, _ = seed(app)
    add_staff(app, branch_id=branch, modules=modules)
    login_staff(client, app)
    assert client.post(f'/documents/{doc_id}/date', data={'document_date': '2026-10-09'}).status_code == expected
    with app.app_context():
        row = get_db().execute('SELECT * FROM documents WHERE id = ?', (doc_id,)).fetchone()
        assert row['document_date'] == ('2026-10-09' if expected == 302 else before['document_date'])


def test_login_required(client, app):
    doc_id, before, _ = seed(app)
    assert '/login' in client.post(f'/documents/{doc_id}/date', data={'document_date': '2026-10-09'}).location
    with app.app_context():
        assert dict(get_db().execute('SELECT * FROM documents WHERE id = ?', (doc_id,)).fetchone()) == before


def test_legacy_migration_idempotence_and_timezone(app):
    doc_id, before, _ = seed(app)
    with app.app_context():
        db = get_db()
        db.execute('ALTER TABLE documents DROP COLUMN document_date')
        db.commit()
        init_db()
        doc = get_document(doc_id)
        assert doc['document_date'] == '' and doc['created_at'] == before['created_at']
        assert effective_document_date(doc) == '2026-10-02'
        init_db()
        assert effective_document_date(get_document(doc_id)) == '2026-10-02'
        db.execute("UPDATE documents SET created_at = '2026-10-02T23:30:00Z' WHERE id = ?", (doc_id,))
        db.commit()
        assert effective_document_date(get_document(doc_id)) == '2026-10-03'
        save_document_date(doc_id, '2024-02-29')
        init_db()
        assert effective_document_date(get_document(doc_id)) == '2024-02-29'
