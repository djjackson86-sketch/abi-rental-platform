"""The three revenue figures must agree (ticket ABI-341952993, item 2).

Client report: "on main profile: when filtered to today, the GROSS REVENUE,
REVENUE FOR THE DAY, and Revenue Card at order pages does not seem to correspond
properly".

They now all report the same thing — money actually RECEIVED, by payment date:

* the dashboard "Gross revenue" card is the windowed version of the same query
  behind "Revenue for the day", so choosing Today reproduces that card;
* the Orders page "Revenue received" card applies the page's own filters (order
  set, branch, pickup-date window) on top of the same received basis;
* deleted payments and payments that are not `paid` never count anywhere.

The booked/recognised basis is deliberately different and still lives on
Reports, whose card is relabelled "recognised (booked)".
"""
import os
import re
import tempfile
from datetime import date, timedelta

import pytest

from app import create_app
from app.db import get_db
from app.services.orders import order_counts
from app.services.reports import dashboard_day_metrics, dashboard_period_metrics
from app.services.timezone import local_now_iso

#: The business day the dashboard's "for the day" cards use.
DAY = local_now_iso()[:10]
OLDER = (date.fromisoformat(DAY) - timedelta(days=3)).isoformat()

RECEIVED_TODAY = 500.0        # the only money that counts for the day
RECEIVED_EVER = 500.0 + 700.0  # ...plus the older payment, once no window is set


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


@pytest.fixture()
def seeded(app):
    """Four orders that all start on the business day, with four payments that
    separate "received" from "booked" and from "not really received at all"."""
    with app.app_context():
        db = get_db()
        specs = (
            # number, order total, payment amount, payment date, payment status, deleted
            ('REV-TODAY', 500.0, 500.0, DAY, 'paid', ''),
            ('REV-OLDER', 700.0, 700.0, OLDER, 'paid', ''),
            ('REV-DELETED', 999.0, 999.0, DAY, 'paid', f'{DAY}T12:00:00'),
            ('REV-PENDING', 888.0, 888.0, DAY, 'pending', ''),
        )
        for number, total, amount, pay_date, pay_status, deleted_at in specs:
            db.execute(
                """INSERT INTO orders (order_number, booking_type, collect_branch_id, return_branch_id,
                status, payment_status, start_at, end_at, subtotal, tax_total, deposit_total, total,
                due_total, notes, created_at)
                VALUES (?, 'return', 1, 1, 'started', 'paid', ?, ?, ?, 0, 0, ?, ?, '', ?)""",
                (number, f'{DAY}T09:00:00', f'{DAY}T17:00:00', total, total, total, f'{DAY}T08:00:00'),
            )
            order_id = db.execute("SELECT id FROM orders WHERE order_number = ?", (number,)).fetchone()['id']
            db.execute(
                """INSERT INTO payments (order_id, amount, method, reference, status, payment_date,
                deleted_at, created_at) VALUES (?, ?, 'cash', '', ?, ?, ?, ?)""",
                (order_id, amount, pay_status, pay_date, deleted_at, f'{pay_date}T10:00:00'),
            )
        db.commit()
    return specs


def login(client):
    with client.application.app_context():
        user_id = get_db().execute(
            "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()['id']
    return client.post('/login', data={'user_id': str(user_id), 'password': 'admin123'},
                       follow_redirects=True)


def card_value(page, label):
    """The rendered value of a metric card, whatever its exact markup."""
    match = re.search(re.escape(label) + r'</small><b>(.*?)</b>', page)
    assert match, f'the {label} card is missing'
    return match.group(1)


# --- the service layer -------------------------------------------------------

def test_the_day_revenue_and_the_today_range_are_the_same_number(app, seeded):
    with app.app_context():
        assert dashboard_day_metrics(day=DAY)['revenue'] == RECEIVED_TODAY
        assert dashboard_period_metrics(DAY, DAY)['revenue'] == RECEIVED_TODAY
        # No window: everything ever received against those orders — the older
        # payment joins in, the deleted and the pending ones never do.
        assert dashboard_period_metrics()['revenue'] == RECEIVED_EVER


def test_the_orders_page_card_uses_the_received_basis_too(app, seeded):
    with app.app_context():
        assert order_counts(start_date=DAY, end_date=DAY)['revenue'] == RECEIVED_TODAY
        assert order_counts()['revenue'] == RECEIVED_EVER
        # A window that holds no payments holds no revenue, whatever the orders say.
        assert order_counts(start_date='2020-01-01', end_date='2020-01-02')['revenue'] == 0


# --- the rendered pages the client actually compared -------------------------

def test_the_gross_revenue_card_is_the_revenue_for_the_day_card(client, app, seeded):
    login(client)
    page = client.get(f'/dashboard?start_date={DAY}&end_date={DAY}').get_data(as_text=True)

    gross = card_value(page, 'Gross revenue')
    day = card_value(page, 'Revenue for the day')
    assert gross == day == 'R500.00'
    # The card says what it measures, so the windowed basis cannot be mistaken
    # for the booked value of the orders raised in the window.
    assert 'Received ·' in page


def test_the_orders_page_card_matches_when_filtered_to_the_day(client, app, seeded):
    login(client)
    page = client.get(f'/orders?start_date={DAY}&end_date={DAY}').get_data(as_text=True)
    assert card_value(page, 'Revenue received') == 'R500.00'

    # Unfiltered, the same card carries every payment received against the orders
    # on screen — a consistent basis, just a wider window.
    all_orders = client.get('/orders').get_data(as_text=True)
    assert card_value(all_orders, 'Revenue received') == 'R1200.00'
