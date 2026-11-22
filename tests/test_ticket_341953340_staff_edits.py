"""Ticket ABI-341953340 — four staff-facing edits.

1. the order page's customer name is a link through to the customer record;
2. the customer page prints the WhatsApp number directly below the cellphone;
3. the Customers "Awaiting verification" queue shows the newest 3 and hides the
   rest behind a "Show all (N)" button (the badge keeps the real total);
4. the dashboard's "Outstanding orders" holds only **returned** orders, so
   reservation/started hires no longer sit in it.

Pinned end to end: the real Flask app renders the real templates off a seeded
SQLite book. The parked reserved/started debts are also asserted to still be on
the Orders page, because ticket 4 only changes the dashboard panel.
"""
import pytest
from bs4 import BeautifulSoup

from app.db import get_db
from test_ticket_341953282_dashboard import app, client, login, add_order  # noqa: F401

DAY = '2026-11-01T09:00:00'


def make_customer(db, name, **fields):
    values = {
        'phone': '',
        'customer_type': 'individual',
        'custom_fields_json': '{}',
        'branch_id': 1,
        'created_at': DAY,
    }
    values.update(fields)
    db.execute(
        "INSERT INTO customers (name, phone, customer_type, custom_fields_json, branch_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (name, values['phone'], values['customer_type'], values['custom_fields_json'],
         values['branch_id'], values['created_at']),
    )
    return db.execute("SELECT id FROM customers WHERE name = ?", (name,)).fetchone()['id']


def add_pending(db, customer_id, reference, name, created_at, phone='082 000 0000'):
    db.execute(
        "INSERT INTO portal_registrations (reference, customer_id, branch_id, name, email, phone,"
        " status, created_at) VALUES (?, ?, 1, ?, ?, ?, 'pending', ?)",
        (reference, customer_id, name, f'{reference.lower()}@example.com', phone, created_at),
    )


def detail_rows(html):
    """(label, value, element) for the customer page's contact-information rows."""
    soup = BeautifulSoup(html, 'html.parser')
    rows = []
    for row in soup.select('.detail-row'):
        label = row.find('span')
        value = row.find('b')
        if label is None or value is None:
            continue
        rows.append((label.get_text(' ', strip=True), value.get_text(' ', strip=True), row))
    return rows


# --------------------------------------------------------------- 1) order page customer link

def test_order_page_turns_the_customer_name_into_a_link_to_the_customer(app, client):
    with app.app_context():
        db = get_db()
        customer_id = make_customer(db, 'Linked Customer', phone='082 111 2222')
        order_id = add_order(db, 'LINK-340', customer_id, 'returned', 250)
        db.commit()

    login(client)
    html = client.get(f'/orders/{order_id}').get_data(as_text=True)
    soup = BeautifulSoup(html, 'html.parser')

    links = soup.select('a.customer-record-link')
    assert len(links) == 2, 'the header subtitle and the Customer panel Name row'
    assert {link['href'] for link in links} == {f'/customers/{customer_id}'}
    assert all(link.get_text(' ', strip=True) == 'Linked Customer' for link in links)

    header_link = soup.select_one('.page-header a.customer-record-link')
    assert header_link is not None, 'the name under the order number is the header link'

    name_row = next(row for row in soup.select('.list-row')
                    if row.find('span') and row.find('span').get_text(strip=True) == 'Name')
    assert name_row.find('a')['href'] == f'/customers/{customer_id}'
    assert name_row.find('a').get_text(' ', strip=True) == 'Linked Customer'

    # The customer page itself is a real page, not a dead href.
    assert client.get(f'/customers/{customer_id}').status_code == 200


def test_order_without_a_customer_keeps_plain_text(app, client):
    with app.app_context():
        db = get_db()
        order_id = add_order(db, 'NOLINK-340', None, 'draft', 0)
        db.commit()

    login(client)
    html = client.get(f'/orders/{order_id}').get_data(as_text=True)
    assert 'customer-record-link' not in html
    name_row = next(row for row in BeautifulSoup(html, 'html.parser').select('.list-row')
                    if row.find('span') and row.find('span').get_text(strip=True) == 'Name')
    assert name_row.find('a') is None
    assert 'No customer' in html or '—' in html


# ------------------------------------------------------- 2) customer page WhatsApp placement

def test_whatsapp_row_sits_directly_below_the_cellphone_and_appears_once(app, client):
    with app.app_context():
        db = get_db()
        customer_id = make_customer(
            db, 'WhatsApp Person', phone='082 333 4444',
            custom_fields_json='{"whatsapp_number": "083 555 6666", '
                               '"alternative_contact_name": "Sister Thandi"}')
        db.commit()

    login(client)
    rows = detail_rows(client.get(f'/customers/{customer_id}').get_data(as_text=True))
    labels = [row[0] for row in rows]

    assert 'Cellphone No.' in labels, labels
    phone_index = labels.index('Cellphone No.')
    assert labels[phone_index + 1] == 'WhatsApp No.'
    assert rows[phone_index + 1][1] == '083 555 6666'
    assert rows[phone_index][1] == '082 333 4444'
    assert labels[phone_index + 2] == 'Marketing emails'

    # Once only: the generic custom-field loop must not print it again.
    assert sum('whatsapp' in label.lower() for label in labels) == 1, labels
    assert [value for label, value, _ in rows if value == '083 555 6666'] == ['083 555 6666']
    # Other custom fields still render normally.
    assert 'Sister Thandi' in [row[1] for row in rows]


def test_company_customer_keeps_vat_and_registration_rows_with_whatsapp(app, client):
    with app.app_context():
        db = get_db()
        customer_id = make_customer(
            db, 'WhatsApp Company', phone='011 444 5555', customer_type='company',
            custom_fields_json='{"whatsapp_number": "083 777 8888", '
                               '"vat_number": "4123456789", "company_reg_no": "2019/123456/07"}')
        db.commit()

    login(client)
    rows = detail_rows(client.get(f'/customers/{customer_id}').get_data(as_text=True))
    labels = [row[0] for row in rows]
    values = {label: value for label, value, _ in rows}

    assert labels[labels.index('Contact Person Cellphone No.') + 1] == 'WhatsApp No.'
    assert values['WhatsApp No.'] == '083 777 8888'
    assert values['VAT No'] == '4123456789'
    assert values['Company Reg No'] == '2019/123456/07'
    assert sum('whatsapp' in label.lower() for label in labels) == 1, labels


def test_a_customer_without_a_whatsapp_number_shows_a_dash(app, client):
    with app.app_context():
        db = get_db()
        customer_id = make_customer(db, 'No WhatsApp Person', phone='082 999 0000')
        db.commit()

    login(client)
    rows = detail_rows(client.get(f'/customers/{customer_id}').get_data(as_text=True))
    labels = [row[0] for row in rows]
    row = rows[labels.index('WhatsApp No.')]
    assert row[1] == '—'


# ------------------------------------------------- 3) awaits verification: 3 + show all

def seed_pending(app, count):
    names = []
    with app.app_context():
        db = get_db()
        for index in range(count):
            name = f'Pending Signup {index}'
            customer_id = make_customer(db, name, created_at=f'2026-10-{index + 1:02d}T09:00:00')
            add_pending(db, customer_id, f'PEND-{index}', name,
                        f'2026-10-{index + 1:02d}T09:00:00')
            names.append(name)
        db.commit()
    return names  # oldest first


def pending_queue(html):
    soup = BeautifulSoup(html, 'html.parser')
    rows_box = soup.find(id='pending-registrations-rows')
    assert rows_box is not None, 'the awaiting-verification queue must render'
    card = rows_box.find_parent('article')
    assert card is not None and 'pending-queue-card' in card.get('class', [])
    rows = rows_box.select('.pending-queue-row')
    button = card.select_one('[data-pending-toggle]')
    badge = card.select_one('.badge')
    return card, rows, button, badge


@pytest.mark.parametrize('count', [5, 4])
def test_five_pending_shows_three_and_a_show_all_button(app, client, count):
    names = seed_pending(app, count)
    login(client)
    card, rows, button, badge = pending_queue(client.get('/customers').get_data(as_text=True))

    assert len(rows) == count, 'every pending row stays in the page'
    visible = [row for row in rows if not row.has_attr('hidden')]
    hidden = [row for row in rows if row.has_attr('hidden')]
    assert len(visible) == 3
    assert len(hidden) == count - 3
    assert all(row.has_attr('data-pending-extra') for row in hidden)
    assert not any(row.has_attr('data-pending-extra') for row in visible)

    # Newest first: the visible three are the newest three submissions.
    visible_names = [row.find('b').get_text(' ', strip=True) for row in visible]
    assert visible_names == list(reversed(names))[:3]

    assert button is not None
    assert button.get_text(' ', strip=True) == f'Show all ({count})'
    assert button['aria-expanded'] == 'false'
    assert button['aria-controls'] == 'pending-registrations-rows'
    # The badge keeps the true total, not the visible three.
    assert f'{count} pending' in badge.get_text(' ', strip=True)


@pytest.mark.parametrize('count', [1, 2, 3])
def test_three_or_fewer_pending_has_no_toggle(app, client, count):
    seed_pending(app, count)
    login(client)
    card, rows, button, badge = pending_queue(client.get('/customers').get_data(as_text=True))

    assert len(rows) == count
    assert not any(row.has_attr('hidden') for row in rows)
    assert not any(row.has_attr('data-pending-extra') for row in rows)
    assert button is None
    assert f'{count} pending' in badge.get_text(' ', strip=True)


def test_no_pending_renders_no_queue(app, client):
    login(client)
    html = client.get('/customers').get_data(as_text=True)
    assert 'pending-registrations-rows' not in html
    assert 'Awaiting verification' not in html


# --------------------------------------- 4) dashboard outstanding orders: returned only

def seed_status_book(app):
    with app.app_context():
        db = get_db()
        customer_id = make_customer(db, 'Status Book Customer')
        ids = {
            'returned': add_order(db, 'RET-340', customer_id, 'returned', 400.00,
                                  created_at='2026-11-02T09:00:00'),
            'returned_partial': add_order(db, 'RET-PART-340', customer_id, 'returned', 50.00,
                                          payment_status='partially_paid', created_at=DAY),
            'reserved': add_order(db, 'RES-340', customer_id, 'reserved', 300.00, created_at=DAY),
            'started': add_order(db, 'STA-340', customer_id, 'started', 200.00, created_at=DAY),
            'draft': add_order(db, 'DRA-340', customer_id, 'draft', 100.00, created_at=DAY),
            'sales': add_order(db, 'SAL-340', customer_id, 'sales_repairs', 900.00, created_at=DAY),
        }
        db.commit()
    return ids


def owing_panel(html):
    soup = BeautifulSoup(html, 'html.parser')
    panel = soup.find(id='owing-orders')
    assert panel is not None
    return panel


@pytest.mark.parametrize('url', ['/dashboard', '/dashboard/partials/owing'])
def test_only_returned_orders_are_outstanding_on_the_dashboard(app, client, url):
    seed_status_book(app)
    login(client)
    response = client.get(url)
    assert response.status_code == 200
    panel = owing_panel(response.get_data(as_text=True))

    numbers = [link.get_text(strip=True) for link in panel.select('tbody tr td:nth-of-type(2) a')]
    assert numbers == ['RET-340', 'RET-PART-340'], numbers
    assert 'RES-340' not in panel.get_text(), 'a reservation is not an outstanding return'
    assert 'STA-340' not in panel.get_text(), 'a hire still out is not an outstanding return'
    assert 'DRA-340' not in panel.get_text()
    assert 'SAL-340' not in panel.get_text()

    total = 'R450.00'
    assert total in panel.select_one('.pill').get_text().replace(' ', '').replace('\xa0', '')
    assert '2 orders' in panel.select_one('.pill').get_text()


def test_parked_balances_are_still_on_the_orders_payment_due_list(app, client):
    seed_status_book(app)
    login(client)
    page = client.get('/orders?payment_status=payment_due').get_data(as_text=True)
    for number in ('RET-340', 'RET-PART-340', 'RES-340', 'STA-340'):
        assert number in page, number
    assert 'DRA-340' not in page


def test_dashboard_panel_helpers_pass_the_returned_filter(monkeypatch):
    from app.routes import admin

    calls = []

    def fake_list_orders(**kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(admin, 'list_orders', fake_list_orders)
    assert admin._dashboard_owing_orders() == []
    assert calls == [{'payment_status': 'payment_due', 'status': ['returned'], 'branch_id': None}]
