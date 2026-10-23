import email
from email import policy

import pytest

from app.db import get_db, init_db
from app.services.documents import get_document, save_purchase_order
from test_ticket_341953022_document_item_pagination import pdf_pages, drawn_right_edge
from test_ticket_341953162_document_date import seed
from test_ticket_341953087_mark_document_sent import app, client, login, login_staff, add_staff


def save(client, doc_id, value):
    client.get(f'/documents/{doc_id}')
    with client.session_transaction() as session:
        token = session['_csrf_tokens']['document_purchase_order']
    return client.post(f'/documents/{doc_id}/purchase-order', data={
        'purchase_order': value, 'csrf_token': token,
    }, follow_redirects=True)


def printed(body):
    return body.split(b'<section class="document-page', 1)[1]


@pytest.mark.parametrize('status', ['draft', 'finalized'])
def test_persistence_pdf_email_and_accounting_unchanged(client, app, status):
    doc_id, before, order = seed(app, status=status)
    login(client)
    result = save(client, doc_id, '  PO-123 / Client & Sons  ')
    assert result.status_code == 200 and b'Purchase Order saved' in result.data
    page = printed(client.get(f'/documents/{doc_id}').data)
    assert b'PO-123 / Client &amp; Sons' in page
    assert page.index(b'invoice-date-line') < page.index(b'invoice-purchase-order') < page.index(b'invoice-order-block')
    pdf = client.get(f'/documents/{doc_id}/download.pdf')
    assert pdf.status_code == 200
    text = '\n'.join(run['text'] for page in pdf_pages(pdf.data) for run in page['runs'])
    assert 'Purchase Order' in text and 'PO-123 / Client & Sons' in text
    draft = client.post(f'/documents/{doc_id}/send-email', data={'to_email': 'po@example.test'})
    message = email.message_from_bytes(draft.data, policy=policy.default)
    attachment = next(p for p in message.walk() if p.get_content_type() == 'application/pdf')
    assert attachment.get_payload(decode=True) == pdf.data
    with app.app_context():
        db = get_db()
        after = dict(db.execute('SELECT * FROM documents WHERE id = ?', (doc_id,)).fetchone())
        assert after['purchase_order'] == 'PO-123 / Client & Sons'
        for key in before:
            if key not in {'purchase_order', 'email_status', 'sent_to', 'sent_at', 'email_error'}:
                assert before[key] == after[key], key
        assert dict(db.execute('SELECT * FROM orders WHERE id = ?', (order['id'],)).fetchone()) == order


@pytest.mark.parametrize('value', ['', '  \n\t  '])
def test_empty_and_clearing_suppress_box(client, app, value):
    doc_id, _, _ = seed(app)
    login(client)
    assert b'invoice-purchase-order' not in printed(client.get(f'/documents/{doc_id}').data)
    original_pdf = client.get(f'/documents/{doc_id}/download.pdf').data
    save(client, doc_id, 'PO-FILLED')
    assert b'invoice-purchase-order' in printed(client.get(f'/documents/{doc_id}').data)
    save(client, doc_id, value)
    assert b'invoice-purchase-order' not in printed(client.get(f'/documents/{doc_id}').data)
    assert client.get(f'/documents/{doc_id}/download.pdf').data == original_pdf
    with app.app_context():
        assert get_document(doc_id)['purchase_order'] == ''


def test_independent_invoices_escaping_and_length(client, app):
    doc_id, _, _ = seed(app)
    with app.app_context():
        db = get_db()
        other = db.execute("INSERT INTO documents (order_id, document_type, status, number, created_at) VALUES (?, 'invoice', 'draft', '', '2026-10-02')", (get_document(doc_id)['order_id'],)).lastrowid
        db.commit()
    login(client)
    value = '<script>alert("x")</script> & \'quoted\''
    result = save(client, doc_id, value)
    assert b'<script>alert' not in result.data
    assert b'&lt;script&gt;' in result.data
    with app.app_context():
        assert get_document(other)['purchase_order'] == ''
    assert b'500 characters or fewer' in save(client, doc_id, 'x' * 501).data
    with app.app_context():
        assert get_document(doc_id)['purchase_order'] == value
    assert b'Purchase Order saved' in save(client, doc_id, 'W' * 500).data
    pdf = client.get(f'/documents/{doc_id}/download.pdf').data
    pages = pdf_pages(pdf)
    text = '\n'.join(run['text'] for page in pages for run in page['runs'])
    assert text.count('W') == 500
    assert 'Amount due' in text
    runs = pages[0]['runs']
    label = next(run for run in runs if run['text'] == 'Purchase Order')
    date = next(run for run in runs if run['text'] == '2026-10-02')
    order_heading = next(run for run in runs if run['text'] == 'Order')
    assert label['y'] < date['y']
    reference_runs = [run for run in runs if run['text'].startswith('WW')]
    assert reference_runs[-1]['y'] > order_heading['y']
    assert all(drawn_right_edge(run) < 566 for run in reference_runs)


@pytest.mark.parametrize('kind', ['quote', 'contract', 'packing_slip'])
def test_other_document_types_unchanged(client, app, kind):
    doc_id, before, _ = seed(app, kind)
    login(client)
    assert b'id="purchase-order"' not in client.get(f'/documents/{doc_id}').data
    with client.session_transaction() as session:
        session['_csrf_tokens'] = {'document_purchase_order': 'test-po-token'}
    response = client.post(f'/documents/{doc_id}/purchase-order', data={'csrf_token': 'test-po-token', 'purchase_order': 'NO'}, follow_redirects=True)
    assert b'Purchase Order is available for invoices only' in response.data
    with app.app_context():
        assert get_document(doc_id)['purchase_order'] == ''


@pytest.mark.parametrize('branch,modules,expected', [(1, ['orders'], 302), (2, ['orders'], 404), (1, ['customers'], 403)])
def test_access(client, app, branch, modules, expected):
    doc_id, _, _ = seed(app)
    add_staff(app, branch_id=branch, modules=modules)
    login_staff(client, app)
    with client.session_transaction() as session:
        session['_csrf_tokens'] = {'document_purchase_order': 'test-po-token'}
    assert client.post(f'/documents/{doc_id}/purchase-order', data={'csrf_token': 'test-po-token', 'purchase_order': 'PO'}).status_code == expected
    with app.app_context():
        assert get_document(doc_id)['purchase_order'] == ('PO' if expected == 302 else '')


def test_anonymous_csrf_and_missing_document(client, app):
    doc_id, _, _ = seed(app)
    assert '/login' in client.post(f'/documents/{doc_id}/purchase-order').location
    login(client)
    for token in [None, '', 'wrong']:
        data = {'purchase_order': 'NO'}
        if token is not None:
            data['csrf_token'] = token
        assert client.post(f'/documents/{doc_id}/purchase-order', data=data).status_code == 400
    assert client.post('/documents/999999/purchase-order').status_code == 404
    with app.app_context():
        assert get_document(doc_id)['purchase_order'] == ''


def test_additive_migration_is_idempotent(app):
    doc_id, before, _ = seed(app)
    with app.app_context():
        db = get_db()
        db.execute('ALTER TABLE documents DROP COLUMN purchase_order')
        db.commit()
        init_db()
        after = dict(db.execute('SELECT * FROM documents WHERE id = ?', (doc_id,)).fetchone())
        assert after == before
        save_purchase_order(doc_id, 'PO-KEPT')
        init_db()
        assert get_document(doc_id)['purchase_order'] == 'PO-KEPT'


def test_long_reference_preserves_every_item_across_pages(client, app):
    doc_id, _, order = seed(app)
    with app.app_context():
        db = get_db()
        for index in range(30):
            db.execute('INSERT INTO order_items (order_id, custom_name, quantity, unit_price, line_subtotal, line_tax, line_total) VALUES (?, ?, 1, 100, 100, 15, 115)',
                       (order['id'], f'PO item {index:02d}'))
        db.commit()
    login(client)
    save(client, doc_id, 'W' * 500)
    pages = pdf_pages(client.get(f'/documents/{doc_id}/download.pdf').data)
    assert len(pages) > 1
    runs = [run for page in pages for run in page['runs']]
    for index in range(30):
        assert sum(run['text'] == f'PO item {index:02d}' for run in runs) == 1
    assert any(run['text'] == 'Amount due' for run in runs)
    first_runs = pages[0]['runs']
    order = next(run for run in first_runs if run['text'] == 'Order')
    header = next(run for run in first_runs if run['text'] == 'PRODUCT')
    assert header['y'] < order['y'] - 25


def test_quote_pdf_ignores_purchase_order_even_if_populated(client, app):
    doc_id, _, _ = seed(app, 'quote')
    login(client)
    original = client.get(f'/documents/{doc_id}/download.pdf').data
    with app.app_context():
        db = get_db()
        db.execute('UPDATE documents SET purchase_order = ? WHERE id = ?', ('PO-IGNORED', doc_id))
        db.commit()
    assert client.get(f'/documents/{doc_id}/download.pdf').data == original
    assert b'invoice-purchase-order' not in printed(client.get(f'/documents/{doc_id}').data)
