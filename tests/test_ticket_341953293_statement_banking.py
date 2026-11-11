"""Branch-specific payment instructions; statement ledgers remain unchanged."""
import pytest

pymupdf = pytest.importorskip("pymupdf")

from app.db import get_db, now
from app.services.account_statements import account_statement
from app.services.customers import customer_statement
from app.services.pdf_documents import customer_statement_pdf_bytes, document_pdf_bytes
from tests.test_ticket_341953085_customer_statement import (
    app, client, login, seed_customer, seed_ticket_ledger,
)

BANKING = {
    'bank_name': 'Fixture Bank', 'bank_account_name': 'Statement Branch Ltd',
    'bank_account_number': '00123456789', 'bank_branch_code': '001122',
    'bank_account_type': 'Business cheque', 'bank_reference_note': 'Use your customer number',
}
LABELS = ['Bank', 'Account holder', 'Account number', 'Branch code', 'Account type', 'Reference']


@pytest.fixture(autouse=True)
def application_context(app):
    with app.app_context():
        yield


def branch(db, name, values):
    keys = list(values)
    return db.execute(
        f"INSERT INTO branches (name, created_at, updated_at, {', '.join(keys)}) "
        f"VALUES (?, ?, ?, {', '.join('?' for _ in keys)})",
        (name, now(), now(), *values.values()),
    ).lastrowid


def text_of(data):
    with pymupdf.open(stream=data, filetype='pdf') as pdf:
        return ''.join(page.get_text() for page in pdf)


def test_customer_branch_not_logged_in_branch_and_invoice_unchanged(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        cid = seed_ticket_ledger(db)
        correct = branch(db, 'Statement branch', BANKING)
        other = branch(db, 'Other branch', {**BANKING, 'bank_account_number': 'WRONG-ACCOUNT'})
        db.execute('UPDATE customers SET branch_id=? WHERE id=?', (correct, cid))
        db.execute('UPDATE users SET branch_id=?, can_view_all_branches=1', (other,))
        db.commit()
        view = customer_statement(cid, '2026-09-01', '2026-09-30', generated_at='2026-10-01')
        doc_id = db.execute('SELECT id FROM documents ORDER BY id LIMIT 1').fetchone()['id']
        invoice_before = document_pdf_bytes(doc_id)
        assert view['banking_details'] == BANKING
        pdf = customer_statement_pdf_bytes(view)
        text = text_of(pdf)
        for label, value in zip(LABELS, BANKING.values()):
            assert f'{label}: {value}' in text
        assert 'WRONG-ACCOUNT' not in text
        assert document_pdf_bytes(doc_id) == invoice_before
        # Changing only payment instructions cannot affect any statement figure.
        db.execute('UPDATE branches SET bank_account_number=? WHERE id=?', ('0099887766', correct))
        changed = customer_statement(cid, '2026-09-01', '2026-09-30', generated_at='2026-10-01')
        assert {k: v for k, v in view.items() if k != 'banking_details'} == {
            k: v for k, v in changed.items() if k != 'banking_details'}
        db.commit()
    response = client.get(f'/customers/{cid}/statement?date_from=2026-09-01&date_to=2026-09-30')
    assert response.status_code == 200
    assert response.mimetype == 'application/pdf'
    assert 'Account number: 0099887766' in text_of(response.data)


def test_two_customer_branches_keep_their_own_accounts(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        for index in (1, 2):
            cid = seed_customer(db, f'Customer {index}', f'b{index}@example.com')
            bid = branch(db, f'Banking Branch {index}', {**BANKING, 'bank_account_number': f'ACCOUNT-{index}'})
            db.execute('UPDATE customers SET branch_id=? WHERE id=?', (bid, cid))
            text = text_of(customer_statement_pdf_bytes(customer_statement(cid)))
            assert f'Account number: ACCOUNT-{index}' in text
            assert f'ACCOUNT-{3-index}' not in text


@pytest.mark.parametrize('mode', ['unassigned', 'blank', 'partial'])
def test_missing_partial_banking_and_empty_statement(client, app, mode):
    login(client)
    with app.app_context():
        db = get_db()
        cid = seed_customer(db)
        # A populated unrelated branch must never act as a fallback.
        branch(db, 'Unrelated', BANKING)
        if mode != 'unassigned':
            values = {key: '' for key in BANKING}
            if mode == 'partial':
                values.update(bank_account_number='00001234', bank_reference_note='   ')
            bid = branch(db, 'Customer branch', values)
            db.execute('UPDATE customers SET branch_id=? WHERE id=?', (bid, cid))
        view = customer_statement(cid)
        text = text_of(customer_statement_pdf_bytes(view))
        assert 'None in this period' in text
        assert 'R0.00' in text
        assert 'Fixture Bank' not in text
        if mode == 'partial':
            assert 'Banking details' in text
            assert 'Account number: 00001234' in text
            assert 'Reference:' not in text
            assert 'Bank:' not in text
        else:
            assert 'Banking details' not in text


def test_account_statement_inherits_same_banking(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        cid = seed_customer(db)
        bid = branch(db, 'Account branch', BANKING)
        db.execute('UPDATE customers SET branch_id=?, credit_allowed=1, credit_limit=5000 WHERE id=?', (bid, cid))
        view = account_statement(cid)
        assert view['banking_details'] == BANKING
        text = text_of(customer_statement_pdf_bytes(view))
        assert 'ACCOUNT STATEMENT' in text
        assert 'Account number: 00123456789' in text


@pytest.mark.parametrize('row_count', [0, 18, 20, 24, 26, 27, 28, 29, 30, 75, 120])
def test_banking_pagination_and_every_activity_row(row_count, tmp_path):
    view = {
        'customer_id': 1, 'statement_number': 'CUSTOMER-STATEMENT-1',
        'customer_lines': ['Fixture Customer'], 'issuer_name': 'Fixture Branch',
        'period_label': 'Full history', 'generated_at': '2026-11-01',
        'outstanding_balance_display': 'R123.00', 'banking_details': BANKING,
        'activity': [dict(date='2026-11-01', type='Invoice', detail=f'ROW-{i:04d}',
                          amount_display='R1.00', balance_display=f'R{i+1}.00') for i in range(row_count)],
    }
    data = customer_statement_pdf_bytes(view)
    with pymupdf.open(stream=data, filetype='pdf') as pdf:
        text = ''.join(p.get_text() for p in pdf)
        assert text.count('Banking details') == 1
        for i in range(row_count):
            assert text.count(f'ROW-{i:04d}') == 1
        for label, value in zip(LABELS, BANKING.values()):
            assert f'{label}: {value}' in text
        for index, page in enumerate(pdf, 1):
            assert f'Page {index} of {len(pdf)}' in page.get_text()
            spans = [s for b in page.get_text('dict')['blocks'] if 'lines' in b
                     for line in b['lines'] for s in line['spans']]
            banking = [s for s in spans if s['text'].startswith(('Banking details', *[v + ':' for v in LABELS]))]
            for span in banking:
                assert span['bbox'][0] >= 26
                assert span['bbox'][2] <= 560
                assert span['bbox'][3] < 790  # safely above footer at y=802
                for other in spans:
                    if other is not span:
                        assert not (pymupdf.Rect(span['bbox']) & pymupdf.Rect(other['bbox'])).get_area()
        if row_count in (0, 27, 75):
            out = tmp_path
            out.mkdir(parents=True, exist_ok=True)
            (out / f'statement-{row_count}.pdf').write_bytes(data)
            for i, page in enumerate(pdf):
                page.get_pixmap(matrix=pymupdf.Matrix(1.2, 1.2)).save(str(out / f'statement-{row_count}-page{i+1}.png'))


def test_long_reference_wraps_without_truncation_or_footer_overlap():
    note = ' '.join(f'PAYREF{i:04d}' for i in range(1100))
    view = {'banking_details': {**BANKING, 'bank_reference_note': note}}
    data = customer_statement_pdf_bytes(view)
    with pymupdf.open(stream=data, filetype='pdf') as pdf:
        text = ''.join(p.get_text() for p in pdf)
        for i in range(1100):
            assert text.count(f'PAYREF{i:04d}') == 1
        assert len(pdf) > 2
        for p in pdf:
            for block in p.get_text('dict')['blocks']:
                for line in block.get('lines', []):
                    for span in line['spans']:
                        assert span['bbox'][2] <= 560
                        assert span['bbox'][3] < 820
