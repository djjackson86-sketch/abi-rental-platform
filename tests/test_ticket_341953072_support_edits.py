"""Ticket ABI-341953072 support edits.

Requested edits:
1. Orders page: a card called "Unsent Invoices" showing, in red, the number of
   FINALIZED invoices that have not been emailed yet, excluding draft orders.
2. Invoice PDFs: space between "Thank you for your business." and "Banking
   details".
3. Invoice PDFs: in the bottom totals table, align the "Total with VAT" value
   with the other values.
4. Remove "Other" from the payment options.

(1) rides on ``order_counts()``, so the card follows the page's own filters and
the branch scope exactly like every other card, and its predicate is the same one
the order's document list already draws as a red "Unsent" badge (ticket
ABI-341953065): a finalized invoice whose ``email_status`` is still 'not_sent'
with no ``sent_at``. Draft orders never qualify, so a booking still being built
never nags about its proforma — and a draft (proforma) invoice never qualifies
either, because only a finalized invoice is counted.

(3) is one root cause rather than a nudge: ``_pdf_right_text()`` measured with the
flat 0.5/0.56 width factors, which are ~15% wide on a bold run, so the bold
"Total with VAT" amount ended ~3.5pt further left than the plain rows above it.
Measuring with the real glyph widths (``_pdf_exact_text_width``) puts every
right-aligned run on the same edge.

(4) keeps ``other`` in ``PAYMENT_METHODS`` and in the day report's "Total other
payments" line so historic rows still render and report, and the ledger edit form
re-shows the option ONLY for a row that already holds it — editing such a row can
never silently rewrite its method.
"""
import os
import re
import tempfile
from pathlib import Path

import pytest
from flask import session as flask_session

from app import create_app
from app.db import get_db
from app.services.access import MODULE_KEYS
from app.services.orders import order_counts
from app.services.payments import PAYMENT_METHODS, normalise_payment_method
from app.services.pdf_documents import (
    BANKING_THANK_YOU_GAP,
    SUMMARY_VALUE_RIGHT_EDGE,
    _invoice_template_pdf,
    _pdf_exact_text_width,
    _pdf_text_width,
)
from tests.test_pdf_text_encoding import (
    _drawn_text_positions,
    _many_items,
    _sample_document,
    _sample_settings,
)

DAY = '2026-09-27'
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    application = create_app({
        'TESTING': True,
        'DATABASE': path,
        'SECRET_KEY': 'test',
        'ADMIN_EMAIL': 'admin@abi.local',
        'ADMIN_PASSWORD': 'admin123',
    })
    yield application
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client, name=None, password='admin123'):
    with client.application.app_context():
        if name is None:
            row = get_db().execute(
                "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
        else:
            row = get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
    assert row is not None
    return client.post('/login', data={'user_id': str(row['id']), 'password': password},
                       follow_redirects=True)


def add_staff(client, app, name='Unsent Staff', branch_id=1, password='staff123'):
    login(client)
    client.post('/settings/users/permissions', data={'module': list(MODULE_KEYS)},
                follow_redirects=True)
    client.post('/settings/users/add', data={
        'name': name, 'password': password, 'branch_id': str(branch_id),
    }, follow_redirects=True)
    with app.app_context():
        user_id = get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()['id']
    client.post('/logout')
    return client.post('/login', data={'user_id': str(user_id), 'password': password},
                       follow_redirects=True)


# --- seeding helpers ---------------------------------------------------------

def _insert_order(db, number, status, branch_id=1):
    db.execute(
        """INSERT INTO orders (order_number, booking_type, collect_branch_id, return_branch_id,
        status, payment_status, start_at, end_at, subtotal, tax_total, total, due_total,
        notes, created_at)
        VALUES (?, 'return', ?, ?, ?, 'payment_due', ?, ?, 100, 15, 115, 115, '', ?)""",
        (number, branch_id, branch_id, status, f'{DAY}T09:00:00', f'{DAY}T17:00:00',
         f'{DAY}T08:00:00'),
    )
    return db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()['id']


def _insert_document(db, order_id, document_type='invoice', status='finalized',
                     email_status='not_sent', sent_at='', number=''):
    db.execute(
        """INSERT INTO documents (order_id, document_type, status, number, pdf_path,
        sent_at, sent_to, email_status, email_error, revision_number, revised_at, created_at)
        VALUES (?, ?, ?, ?, '', ?, '', ?, '', 0, '', ?)""",
        (order_id, document_type, status, number, sent_at, email_status, f'{DAY}T10:00:00'),
    )


def seed_invoice_mix(app):
    """Every branch of the "unsent invoice" predicate, on one branch mix.

    COUNTS  : ORD-42001 reserved, finalized + unsent
              ORD-42002 started, TWO finalized + unsent on the one order (counted
                        once each — the card counts documents, not orders)
              ORD-42006 returned, branch 2, finalized + unsent
    SKIPS   : ORD-42003 started, email already prepared ('prepared' + sent_at)
              ORD-42004 started, email already sent ('sent' + sent_at)
              ORD-42005 started, invoice still a DRAFT (proforma)
              ORD-42007 DRAFT order with a finalized invoice
              ORD-42008 started, finalized QUOTE, unsent (not an invoice)
              ORD-42009 started, finalized invoice whose email FAILED — the badge
                        already treats any non-'not_sent' status as handled, and
                        the card must not disagree with it
    """
    with app.app_context():
        db = get_db()
        ids = {
            'reserved_unsent': _insert_order(db, 'ORD-42001', 'reserved'),
            'two_unsent': _insert_order(db, 'ORD-42002', 'started'),
            'prepared': _insert_order(db, 'ORD-42003', 'started'),
            'sent': _insert_order(db, 'ORD-42004', 'started'),
            'draft_invoice': _insert_order(db, 'ORD-42005', 'started'),
            'other_branch': _insert_order(db, 'ORD-42006', 'returned', branch_id=2),
            'draft_order': _insert_order(db, 'ORD-42007', 'draft'),
            'quote_only': _insert_order(db, 'ORD-42008', 'started'),
            'failed_email': _insert_order(db, 'ORD-42009', 'started'),
        }
        _insert_document(db, ids['reserved_unsent'], number='INV-42001')
        _insert_document(db, ids['two_unsent'], number='INV-42002')
        _insert_document(db, ids['two_unsent'], number='INV-42002R')
        _insert_document(db, ids['prepared'], email_status='prepared', number='INV-42003')
        _insert_document(db, ids['sent'], email_status='sent', sent_at=f'{DAY}T11:00:00',
                         number='INV-42004')
        _insert_document(db, ids['draft_invoice'], status='draft')
        _insert_document(db, ids['other_branch'], number='INV-42006')
        _insert_document(db, ids['draft_order'], number='INV-42007')
        _insert_document(db, ids['quote_only'], document_type='quote', number='QUO-42008')
        _insert_document(db, ids['failed_email'], email_status='failed', number='INV-42009')
        db.commit()
        return ids


def counters(app, user_id=1, role='owner', **filters):
    ctx = app.test_request_context('/orders')
    ctx.push()
    flask_session['user_id'] = user_id
    flask_session['user_role'] = role
    try:
        return order_counts(**filters)
    finally:
        ctx.pop()


def _cards(html, marker):
    """{label: (value, classes)} for the metric cards of one rendered row."""
    match = re.search(rb'<section class="[^"]*"[^>]*' + marker + rb'[^>]*>(.*?)</section>',
                      html, re.S)
    assert match, f'the metric section for {marker!r} was not rendered'
    cards = {}
    for card in re.finditer(
            rb'<div class="([^"]*metric-card[^"]*)"><small>(.*?)</small><b>(.*?)</b></div>',
            match.group(0), re.S):
        cards[card.group(2).decode().strip()] = (card.group(3).decode().strip(),
                                                card.group(1).decode())
    return cards


def _unsent_badges(html):
    """The order's own document list counts, as the red "Unsent" badge."""
    return html.count('data-document-email-indicator="unsent"')


# --- (1) the Unsent Invoices card -------------------------------------------

def test_the_card_counts_finalized_unemailed_invoices_and_excludes_drafts(app):
    seed_invoice_mix(app)
    counts = counters(app)
    # ORD-42001 + both invoices of ORD-42002 + ORD-42006 = 4 documents.
    assert counts['unsent_invoices'] == 4
    # The card never changes an existing figure.
    assert counts['total'] == 9


def test_a_second_invoice_on_the_same_order_is_counted_and_the_order_list_agrees(client, app):
    """The card must equal what the reader can see on the orders it names."""
    ids = seed_invoice_mix(app)
    counts = counters(app)
    login(client)

    total_badges = 0
    for key in ('reserved_unsent', 'two_unsent', 'other_branch'):
        page = client.get(f'/orders/{ids[key]}').get_data(as_text=True)
        total_badges += _unsent_badges(page)
    assert total_badges == counts['unsent_invoices'] == 4
    # The one deliberate difference from the per-order badge: a DRAFT order's own
    # document list still shows "Unsent" against its finalized invoice, while the
    # card (which the client asked to exclude draft orders) does not count it.
    draft_page = client.get(f'/orders/{ids["draft_order"]}').get_data(as_text=True)
    assert _unsent_badges(draft_page) == 1
    assert counts['unsent_invoices'] == 4


def test_a_draft_order_and_a_draft_invoice_never_qualify(app):
    ids = seed_invoice_mix(app)
    counts = counters(app)
    assert counts['unsent_invoices'] == 4
    with app.app_context():
        db = get_db()
        # Finalize the draft order's invoice and the proforma in one go: only the
        # proforma becomes a real, finalized invoice; the draft order's invoice
        # stays out because its ORDER is still a draft.
        db.execute("UPDATE documents SET status = 'finalized' WHERE order_id = ?",
                   (ids['draft_invoice'],))
        db.commit()
    assert counters(app)['unsent_invoices'] == 5
    with app.app_context():
        # Take the draft order out of draft: now its finalized invoice qualifies.
        get_db().execute("UPDATE orders SET status = 'started' WHERE id = ?",
                         (ids['draft_order'],))
        get_db().commit()
    assert counters(app)['unsent_invoices'] == 6


def test_the_card_follows_the_page_filters_and_the_branch_scope(app):
    seed_invoice_mix(app)
    assert counters(app)['unsent_invoices'] == 4
    # Branch 2 holds one of them, branch 1 the other three.
    assert counters(app, branch_id=2)['unsent_invoices'] == 1
    assert counters(app, branch_id=1)['unsent_invoices'] == 3
    # The status filter the rail applies narrows the card the same way.
    assert counters(app, status='returned')['unsent_invoices'] == 1
    assert counters(app, status='reserved')['unsent_invoices'] == 1
    assert counters(app, status='draft')['unsent_invoices'] == 0
    # A search that matches nothing empties the card with the list.
    assert counters(app, query='ORD-42003')['unsent_invoices'] == 0


def test_the_orders_page_shows_the_red_card_to_main_and_to_staff(client, app):
    seed_invoice_mix(app)
    login(client)
    page = client.get('/orders')
    assert page.status_code == 200
    main_cards = _cards(page.data, b'id="orders-metrics"')
    assert main_cards['Unsent Invoices'][0] == '4'
    assert 'is-alert' in main_cards['Unsent Invoices'][1]

    add_staff(client, app)
    staff_page = client.get('/orders')
    assert staff_page.status_code == 200
    staff_cards = _cards(staff_page.data, b'orders-staff-metrics')
    # The main row counts every branch; a branch-1 staff account counts its own,
    # which is exactly what the branch scope reports.
    assert main_cards['Unsent Invoices'][0] == '4'
    assert staff_cards['Unsent Invoices'][0] == '3'
    assert 'is-alert' in staff_cards['Unsent Invoices'][1]

    # A branch-2 staff account sees only its own branch's invoice.
    add_staff(client, app, name='Unsent Depot', branch_id=2)
    depot_cards = _cards(client.get('/orders').data, b'orders-staff-metrics')
    assert depot_cards['Unsent Invoices'][0] == '1'


def test_the_is_alert_card_really_renders_its_number_in_red():
    """The client asked for the number in red; is-alert is what makes it red."""
    css = (ROOT / 'static' / 'css' / 'app.css').read_text(encoding='utf-8')
    assert re.search(r'\.metric-card\.is-alert b\{[^}]*color:var\(--danger\)', css), \
        'the is-alert metric card no longer paints its number with the danger colour'
    assert re.search(r'--danger:#d92d20', css)


# --- (2) space between the thank-you line and the banking block -------------

def _thank_you_and_banking(pdf_bytes):
    """(page, thank_you_y, banking_y) for every page drawing both lines."""
    positions = _drawn_text_positions(pdf_bytes)
    pairs = []
    for page in sorted({entry['page'] for entry in positions}):
        thank_you = [entry for entry in positions
                     if entry['page'] == page and entry['text'] == 'Thank you for your business.']
        banking = [entry for entry in positions
                   if entry['page'] == page and entry['text'] == 'Banking details']
        if thank_you and banking:
            pairs.append((page, thank_you[0]['y'], banking[0]['y']))
    return pairs


def test_the_thank_you_line_and_the_banking_block_have_real_space_between_them(app):
    document = _sample_document('invoice')
    with app.app_context():
        pdf_bytes = _invoice_template_pdf(document, _many_items(1), _sample_settings())
    pairs = _thank_you_and_banking(pdf_bytes)
    assert pairs, 'the invoice PDF drew no thank-you/banking pair at all'
    for _page, thank_you_y, banking_y in pairs:
        gap = thank_you_y - banking_y
        assert gap == pytest.approx(BANKING_THANK_YOU_GAP), (
            f'the gap between the thank-you line and the banking block is {gap}pt')
        # The old cramped 10pt gap is what the client reported.
        assert gap >= 20
    # and the block still sits below the totals' first line (ABI-341953061(7)).
    positions = _drawn_text_positions(pdf_bytes)
    total_y = next(entry['y'] for entry in positions if entry['text'] == 'Total without VAT')
    assert pairs[0][1] < total_y


def test_the_overflow_page_uses_the_same_gap(app):
    """The block that drops to a page of its own must not disagree (nor be cut)."""
    document = _sample_document('invoice')
    with app.app_context():
        pdf_bytes = _invoice_template_pdf(document, _many_items(30), _sample_settings())
    positions = _drawn_text_positions(pdf_bytes)
    banking_pages = {entry['page'] for entry in positions if entry['text'] == 'Banking details'}
    assert banking_pages, 'the banking block was not drawn at all'
    checked = 0
    for _page, thank_you_y, banking_y in _thank_you_and_banking(pdf_bytes):
        assert thank_you_y - banking_y == pytest.approx(BANKING_THANK_YOU_GAP)
        checked += 1
    assert checked == len(banking_pages)
    # Nothing was pushed through the page floor to make room for the bigger gap.
    assert min(entry['y'] for entry in positions) > 40


# --- (3) the totals table's amounts share one right edge --------------------

def _totals_rows(pdf_bytes):
    """{label: {'font', 'size', 'x', 'text', 'amount_x'}} for the totals table."""
    positions = _drawn_text_positions(pdf_bytes)
    labels = {}
    for entry in positions:
        if entry['y'] < 60:
            continue
        if entry['x'] > 380 and entry['size'] == 8.8 and not entry['text'].startswith('R'):
            amount = next((other for other in positions
                           if other['y'] == entry['y'] and other['x'] > 380
                           and other['text'].startswith(('R', '-R'))), None)
            if amount:
                labels[entry['text']] = {**entry, 'amount': amount}
    return labels


def test_every_totals_amount_ends_on_one_right_edge(app):
    document = _sample_document('invoice')
    document.update({'subtotal': 1000.0, 'tax_total': 150.0, 'total': 1150.0, 'due_total': 1150.0})
    with app.app_context():
        pdf_bytes = _invoice_template_pdf(document, _many_items(1), _sample_settings())
    rows = _totals_rows(pdf_bytes)
    assert {'Total without VAT', 'Total with VAT'} <= set(rows), sorted(rows)

    edges = {label: row['amount']['x'] + _pdf_exact_text_width(
        row['amount']['text'], row['amount']['size'],
        bold=row['amount']['font'] == 'F2') for label, row in rows.items()}
    assert len(set(round(edge, 2) for edge in edges.values())) == 1, edges
    assert round(next(iter(edges.values())), 1) == round(SUMMARY_VALUE_RIGHT_EDGE, 1)

    # Non-vacuous both ways: the bold row really is bold, and measuring it with
    # the crude factors (the bug) is what broke the alignment.
    bold = rows['Total with VAT']['amount']
    assert bold['font'] == 'F2', 'the Total with VAT amount is no longer the bold row'
    crude = {label: round(row['amount']['x'] + _pdf_text_width(
        row['amount']['text'], row['amount']['size'], bold=row['amount']['font'] == 'F2'), 2)
        for label, row in rows.items()}
    assert len(set(crude.values())) > 1, (
        'the crude measurement now agrees with the exact one, so this test proves nothing')


def test_the_item_row_amounts_share_the_column_edges_too(app):
    """The same helper right-aligns the line items, so they move together."""
    document = _sample_document('invoice')
    document.update({'subtotal': 1000.0, 'tax_total': 150.0, 'total': 1150.0, 'due_total': 1150.0})
    with app.app_context():
        pdf_bytes = _invoice_template_pdf(document, _many_items(3), _sample_settings())
    positions = _drawn_text_positions(pdf_bytes)
    item_rows = {}
    for entry in positions:
        if entry['size'] == 8 and entry['text'].startswith(('R', '-R')) and entry['x'] > 240:
            item_rows.setdefault(entry['y'], set()).add(
                round(entry['x'] + _pdf_exact_text_width(entry['text'], entry['size']), 2))
    single_edge_rows = {}
    for y, edges in item_rows.items():
        # one edge per money column, repeated down the rows
        for edge in edges:
            single_edge_rows.setdefault(edge, 0)
    counts = {}
    for y, edges in item_rows.items():
        for edge in edges:
            counts[edge] = counts.get(edge, 0) + 1
    assert counts, 'no item money rows were drawn'
    # every column edge is used by every item row (3 items -> 3 hits per edge)
    assert set(counts.values()) == {3}, counts


# --- (4) "Other" removed from the payment options ---------------------------

def test_no_payment_form_offers_other_any_more(client, app):
    with app.app_context():
        db = get_db()
        db.execute("""INSERT INTO customers (name, email, phone, created_at)
        VALUES ('Unsent Customer', 'unsent@example.test', '+270****0000', ?)""", (f'{DAY}T08:00:00',))
        order_id = _insert_order(db, 'ORD-42050', 'started')
        db.commit()
    login(client)
    page = client.get(f'/orders/{order_id}').get_data(as_text=True)
    assert 'value="other"' not in page
    # the three real methods are untouched by the removal
    for option in ('value="cash"', 'value="eft"', 'value="card"'):
        assert option in page, option

    # Nothing else in the app writes the option either: the single remaining
    # occurrence is the conditional one on the ledger edit form.
    hits = [path for path in (ROOT / 'templates').rglob('*.html')
            if 'value="other"' in path.read_text(encoding='utf-8')]
    assert [path.name for path in hits] == ['edit.html'], [str(p) for p in hits]


def test_a_legacy_other_row_still_edits_and_keeps_its_method(client, app):
    with app.app_context():
        db = get_db()
        db.execute("""INSERT INTO customers (name, email, phone, created_at)
        VALUES ('Legacy Customer', 'legacy@example.test', '+270****1111', ?)""", (f'{DAY}T08:00:00',))
        order_id = _insert_order(db, 'ORD-42051', 'started')
        customer_id = db.execute("SELECT id FROM customers WHERE email = 'legacy@example.test'").fetchone()['id']
        db.execute("UPDATE orders SET customer_id = ? WHERE id = ?", (customer_id, order_id))
        rows = {}
        for method in ('other', 'manual', 'cash'):
            db.execute(
                """INSERT INTO payments (order_id, amount, method, reference, status,
                payment_date, deleted_at, created_at)
                VALUES (?, 10, ?, '', 'paid', ?, '', ?)""",
                (order_id, method, f'{DAY}T09:00:00', f'{DAY}T09:00:00'))
            rows[method] = db.execute('SELECT id FROM payments ORDER BY id DESC LIMIT 1').fetchone()['id']
        db.commit()
    login(client)

    other_page = client.get(f'/payments/{rows["other"]}/edit').get_data(as_text=True)
    assert 'value="other" selected' in other_page
    other_page_manual = client.get(f'/payments/{rows["manual"]}/edit').get_data(as_text=True)
    assert 'value="other"' not in other_page_manual
    assert 'value="manual" selected' in other_page_manual

    # Saving the legacy Other row back leaves it as Other...
    saved = client.post(f'/payments/{rows["other"]}/edit', data={
        'amount': '12.50', 'method': 'other', 'reference': 'Voucher',
        'payment_date': f'{DAY}T09:30',
    }, follow_redirects=True)
    assert saved.status_code == 200
    # ...and saving a plain row cannot reintroduce it from the form.
    client.post(f'/payments/{rows["cash"]}/edit', data={
        'amount': '10.00', 'method': 'cash', 'reference': '',
        'payment_date': f'{DAY}T09:30',
    }, follow_redirects=True)
    with app.app_context():
        stored = {row['id']: (row['method'], row['amount']) for row in get_db().execute(
            'SELECT id, method, amount FROM payments WHERE order_id = ?', (order_id,)).fetchall()}
    assert stored[rows['other']] == ('other', 12.5)
    assert stored[rows['manual']] == ('manual', 10)
    assert stored[rows['cash']] == ('cash', 10)


def test_the_service_layer_still_accepts_other_for_legacy_rows():
    assert 'other' in PAYMENT_METHODS
    assert normalise_payment_method('other') == 'other'
    assert normalise_payment_method('OTHER') == 'other'
