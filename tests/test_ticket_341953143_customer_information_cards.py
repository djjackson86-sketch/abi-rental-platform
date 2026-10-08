"""New-order card grouping is presentation-only; edit markup remains unchanged."""
from html.parser import HTMLParser

from test_ticket_341953074_customer_credit_card import (
    app, client, login, seed_customer, seed_order, seed_credit,
)


class CardParents(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.stack = []
        self.parents = {}
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.parents[attrs['id']] = list(self.stack)
        if tag not in {'input', 'img', 'br', 'hr', 'meta', 'link'}:
            self.stack.append((tag, attrs.get('class', '')))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                self.stack = self.stack[:index]
                break


def test_new_order_search_is_separate_from_all_metric_cards(client, app):
    login(client)
    with app.app_context():
        from app.db import get_db
        from app.services.legacy_balances import upsert_legacy_balance
        db = get_db()
        customer_id = seed_customer(db, 'Card Layout Customer')
        seed_order(db, customer_id, 'ORD-CARDS-143', due=400)
        seed_credit(db, customer_id, 250)
        db.commit()
        upsert_legacy_balance(customer_id, 'cards-143', 'ticket143', 125)
        db.commit()
    response = client.get(f'/orders/new?customer_id={customer_id}')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    parents = CardParents(body).parents
    group = ('div', 'new-order-metric-cards')
    assert group not in parents['customer-search']
    for card in ('previous-balance-card', 'legacy-balance-card',
                 'customer-credit-card', 'orders-to-date-card'):
        assert parents[card][-1] == group
    assert 'R400.00' in body and 'R250.00' in body and 'R125.00' in body
    assert '.new-order-metric-cards>.stat-card[hidden]{display:none}' in body
    assert 'grid-template-columns:minmax(0,1fr)' in body


def test_edit_order_keeps_existing_picker_group(client, app):
    login(client)
    with app.app_context():
        from app.db import get_db
        db = get_db()
        customer_id = seed_customer(db, 'Edit Card Layout')
        order_id = seed_order(db, customer_id, 'ORD-CARDS-EDIT-143', status='draft')
        db.commit()
    response = client.get(f'/orders/{order_id}/edit')
    assert response.status_code == 200
    parents = CardParents(response.get_data(as_text=True)).parents
    assert parents['previous-balance-card'][-1] == ('div', 'form-grid customer-picker-grid')
    assert parents['customer-search'][-2] == ('div', 'form-grid customer-picker-grid')
