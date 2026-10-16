"""Customer history starts an unsaved draft for that customer, not a generic draft."""
from html.parser import HTMLParser

import pytest

from test_ticket_341953074_customer_credit_card import (
    app, client, login, seed_customer, seed_order,
)
from app.db import get_db


class Elements(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.links = {}
        self.inputs = {}
        self.href = None
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a':
            self.href = attrs.get('href')
        if tag == 'input' and attrs.get('id'):
            self.inputs[attrs['id']] = attrs.get('value', '')

    def handle_endtag(self, tag):
        if tag == 'a':
            self.href = None

    def handle_data(self, data):
        if self.href and data.strip():
            self.links[data.strip()] = self.href


@pytest.mark.parametrize('has_history', [False, True])
def test_history_links_preselect_customer_without_creating_order(app, client, has_history):
    login(client)
    with app.app_context():
        db = get_db()
        seed_customer(db, 'Different customer')
        customer_id = seed_customer(db, 'Intended customer')
        if has_history:
            seed_order(db, customer_id, 'HISTORY-227')
        db.commit()
        before = db.execute('SELECT COUNT(*) FROM orders').fetchone()[0]
    response = client.get(f'/customers/{customer_id}')
    assert response.status_code == 200
    links = Elements(response.get_data(as_text=True)).links
    labels = ['New order'] if has_history else ['New order', 'Create order']
    for label in labels:
        assert links[label] == f'/orders/new?customer_id={customer_id}'
        response = client.get(links[label])
        assert response.status_code == 200
        inputs = Elements(response.get_data(as_text=True)).inputs
        assert inputs['customer-id'] == str(customer_id)
        assert 'Intended customer' in inputs['customer-search']
    with app.app_context():
        assert get_db().execute('SELECT COUNT(*) FROM orders').fetchone()[0] == before


def test_validation_retains_customer_then_saves_draft_for_them(app, client):
    login(client)
    with app.app_context():
        db = get_db()
        seed_customer(db, 'Not the intended customer')
        customer_id = seed_customer(db, 'Draft customer')
        db.commit()
    payload = {'customer_id': str(customer_id), 'booking_type': 'return',
               'collect_branch_id': '1', 'return_branch_id': '1',
               'start_date': '2026-10-19', 'start_time': '10:00',
               'end_date': '2026-10-18', 'end_time': '10:00',
               'custom_name': 'Ticket test service', 'custom_unit_price': '100',
               'quantity': '1', 'custom_billing_mode': 'fixed'}
    response = client.post('/orders/new', data=payload)
    assert response.status_code == 200
    inputs = Elements(response.get_data(as_text=True)).inputs
    assert inputs['customer-id'] == str(customer_id)
    assert 'Draft customer' in inputs['customer-search']
    with app.app_context():
        assert get_db().execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 0
    payload['end_date'] = '2026-10-20'
    response = client.post('/orders/new', data=payload)
    assert response.status_code == 302
    with app.app_context():
        rows = get_db().execute('SELECT id, customer_id, status FROM orders').fetchall()
        assert len(rows) == 1
        assert rows[0]['customer_id'] == customer_id
        assert rows[0]['status'] == 'draft'
        assert response.headers['Location'].endswith(f"/orders/{rows[0]['id']}")


def test_generic_new_order_remains_unselected(client):
    login(client)
    response = client.get('/orders/new')
    assert response.status_code == 200
    inputs = Elements(response.get_data(as_text=True)).inputs
    assert inputs['customer-id'] == ''
    assert inputs.get('customer-search', '') == ''
