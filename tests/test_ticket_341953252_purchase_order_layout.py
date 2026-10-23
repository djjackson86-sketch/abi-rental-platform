import pytest
from test_ticket_341953250_purchase_order import app, client, login, seed, save, printed, pdf_pages, drawn_right_edge


@pytest.mark.parametrize('status', ['draft', 'finalized'])
@pytest.mark.parametrize('value', ['', 'PO-7', '1234567890', 'CLIENT-REFERENCE-LONGER-THAN-TEN', 'W' * 500])
def test_inline_reference_layout_and_persistence(client, app, status, value):
    doc_id, _, _ = seed(app, status=status)
    login(client)
    response = save(client, doc_id, value)
    assert response.status_code == 200
    body = client.get(f'/documents/{doc_id}').data
    assert b'type="text" size="10" maxlength="500"' in body
    assert b'<textarea id="purchase-order"' not in body
    assert b'Purchase Order: <input id="purchase-order"' in body
    page = printed(body)
    pdf = client.get(f'/documents/{doc_id}/download.pdf').data
    pages = pdf_pages(pdf)
    runs = pages[0]['runs']
    if not value:
        assert b'invoice-purchase-order' not in page
        assert not any(r['text'].startswith('Purchase Order:') for r in runs)
        return
    assert b'<p class="invoice-purchase-order">Purchase Order: ' + value.encode() + b'</p>' in page
    label = next(r for r in runs if r['text'].startswith('Purchase Order:'))
    heading = next(r for r in runs if r['text'] in ['Invoice', 'Proforma Invoice'])
    order = next(r for r in runs if r['text'] == 'Order')
    assert label['x'] == heading['x'] == order['x'] == 455
    assert label['size'] == heading['size']
    reference = [r for r in runs if order['y'] < r['y'] <= label['y'] and r['x'] == label['x']]
    assert all(drawn_right_edge(r) <= 569.1 for r in reference)
    assert ''.join(r['text'] for r in reference).replace(' ', '').removeprefix('PurchaseOrder:') == value
    assert b'0.965 0.976 0.992 rg' not in pdf
    assert any(r['text'] == 'Amount due' for p in pages for r in p['runs'])
    if len(value) <= 10:
        assert label['text'] == f'Purchase Order: {value}'
        assert order['y'] == 625
        assert len(pages) == 1
