"""Ticket ABI-341953341 - the trailer service / maintenance type list.

The dashboard's "Service / maintenance type" select, its server-side
validation and the day report all read the one list in
``trailer_service.TRAILER_SERVICE_TYPES``.  The owner asked for six new brake
and axle jobs to be offered and for "Mechanism repairs" to be dropped.

These tests pin the list itself (labels, order, "Custom" last), that every new
label survives validation and is stored literally, that an off-list label is
still refused, and that a logged job shows up in the day report and on the
product's service history.  Rows already stored as "Mechanism repairs" are
deliberately left alone - reports read the stored string, and only new
submissions are gated by ``clean_service_type``.
"""
import html
import os
import re
import tempfile

import pytest

from app import create_app
from app.db import get_db
from app.services import cash, trailer_service

TODAY = cash.today_iso()

NEW_LABELS = [
    "Brake lining change",
    "Damper change",
    "Brake adjustment",
    "Brake drum change",
    "Front axle change",
    "Back axle change",
]

EXPECTED_LIST = [
    "Bearing service",
    "Trailer plug change",
    "Re-wiring",
    "Light repair/change",
    "Tyre change",
    "Rim & tyre change",
    "Minor body repairs",
    "Major body repairs",
    "Trailer repaint",
    "Brake lining change",
    "Damper change",
    "Brake adjustment",
    "Brake drum change",
    "Front axle change",
    "Back axle change",
    "Custom",
]

_TEXT_RUN = re.compile(r'/(F\d) ([\d.]+) Tf 1 0 0 1 ([\d.]+) ([\d.]+) Tm \(((?:[^()\\]|\\.)*)\) Tj')


def _pdf_text(blob):
    return ' '.join(text for _f, _s, _x, _y, text in _TEXT_RUN.findall(blob.decode('latin-1')))


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


def login(client):
    with client.application.app_context():
        row = get_db().execute(
            "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
        ).fetchone()
        user_id = row['id'] if row else None
    return client.post('/login', data={'user_id': str(user_id), 'password': 'admin123'},
                       follow_redirects=True)


def _seed_trailer(app):
    with app.app_context():
        db = get_db()
        ts = '2026-11-20 08:00'
        db.execute("INSERT INTO product_groups (name, active, sort_order, created_at, updated_at) "
                   "VALUES ('Trailers', 1, 1, ?, ?)", (ts, ts))
        group = db.execute("SELECT id FROM product_groups WHERE name = 'Trailers'").fetchone()['id']
        db.execute(
            """INSERT INTO products (name, product_type, tracking_method, description, sku, active,
            public_visible, price_amount, price_unit, security_deposit, hourly_extra_rate,
            product_group_id, quantity, branch_id, created_at)
            VALUES ('Road trailer', 'rental', 'bulk', '', 'ROAD-TRAILER', 1, 1, 100, 'day', 0, 0, ?, 1, 1, ?)""",
            (group, ts),
        )
        trailer_id = db.execute("SELECT id FROM products WHERE name = 'Road trailer'").fetchone()['id']
        db.commit()
        return trailer_id


def test_service_type_list_is_exactly_the_owners_list():
    assert trailer_service.TRAILER_SERVICE_TYPES == EXPECTED_LIST
    assert 'Mechanism repairs' not in trailer_service.TRAILER_SERVICE_TYPES
    assert trailer_service.TRAILER_SERVICE_TYPES[-1] == 'Custom'


def test_dashboard_offers_new_labels_in_order_without_mechanism_repairs(client, app):
    _seed_trailer(app)
    login(client)
    # Ticket ABI-341953339 moved the day-end panels onto /day-report.
    body = client.get('/day-report?branch=1').get_data(as_text=True)
    assert 'Service / maintenance type' in body
    # Ticket ABI-341953342 replaced the single-value select with a tick-any group
    # of checkboxes that post the same ``service_type`` field, one per tick.
    assert '<select name="service_type"' not in body
    group = body[body.index('data-trailer-service-types'):]
    group = group[:group.index('</fieldset>')]
    offered = [html.unescape(value) for value in re.findall(r'name="service_type" value="([^"]*)"', group)]
    assert offered == EXPECTED_LIST
    assert 'Mechanism repairs' not in body


@pytest.mark.parametrize('label', NEW_LABELS)
def test_new_labels_pass_validation_and_store_literally(client, app, label):
    trailer_id = _seed_trailer(app)
    login(client)
    body = client.post('/cash-up/trailer-service', data={
        'day': TODAY,
        'branch': '1',
        'product_id': str(trailer_id),
        'service_type': label,
    }, follow_redirects=True).get_data(as_text=True)
    assert 'Trailer service and maintenance saved' in body
    with app.app_context():
        row = get_db().execute(
            "SELECT * FROM trailer_service_history WHERE product_id = ?", (trailer_id,)
        ).fetchone()
        assert row['service_type'] == label
        assert row['service_date'] == TODAY
        assert trailer_service.clean_service_type(f'  {label}  ') == label


def test_mechanism_repairs_is_no_longer_accepted(client, app):
    trailer_id = _seed_trailer(app)
    with app.app_context():
        with pytest.raises(ValueError):
            trailer_service.clean_service_type('Mechanism repairs')
    login(client)
    body = client.post('/cash-up/trailer-service', data={
        'day': TODAY,
        'branch': '1',
        'product_id': str(trailer_id),
        'service_type': 'Mechanism repairs',
    }, follow_redirects=True).get_data(as_text=True)
    assert 'Choose the service or maintenance type' in body
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) AS n FROM trailer_service_history").fetchone()['n'] == 0


def test_unknown_label_is_still_rejected(client, app):
    trailer_id = _seed_trailer(app)
    with app.app_context():
        with pytest.raises(ValueError):
            trailer_service.clean_service_type('Brake service')
    login(client)
    body = client.post('/cash-up/trailer-service', data={
        'day': TODAY,
        'branch': '1',
        'product_id': str(trailer_id),
        'service_type': 'Brake service',
    }, follow_redirects=True).get_data(as_text=True)
    assert 'Choose the service or maintenance type' in body
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) AS n FROM trailer_service_history").fetchone()['n'] == 0


def test_new_label_reaches_day_report_and_product_history(client, app):
    trailer_id = _seed_trailer(app)
    login(client)
    client.post('/cash-up/trailer-service', data={
        'day': TODAY,
        'branch': '1',
        'product_id': str(trailer_id),
        'service_type': 'Brake drum change',
    }, follow_redirects=True)

    with app.app_context():
        rows = trailer_service.services_for_day(TODAY, branch_id=1)
        assert [row['label'] for row in rows] == ['Brake drum change']

    day_report_page = client.get(f'/day-report?branch=1&day={TODAY}').get_data(as_text=True)
    assert 'Brake drum change' in day_report_page

    report = _pdf_text(client.get(f'/cash-up/report.pdf?day={TODAY}&branch=1').data)
    assert 'Brake drum change' in report

    product_page = client.get(f'/inventory/{trailer_id}/edit').get_data(as_text=True)
    assert 'Brake drum change' in product_page


def test_existing_mechanism_repairs_rows_are_kept(client, app):
    """Old rows stay readable: only new submissions are gated by the list."""
    trailer_id = _seed_trailer(app)
    with app.app_context():
        db = get_db()
        db.execute(
            """INSERT INTO trailer_service_history
            (product_id, service_type, custom_description, service_date, branch_id,
             created_by_user_id, created_at)
            VALUES (?, 'Mechanism repairs', '', ?, 1, NULL, ?)""",
            (trailer_id, TODAY, '2026-11-20 09:00'),
        )
        db.commit()
    login(client)
    product_page = client.get(f'/inventory/{trailer_id}/edit').get_data(as_text=True)
    assert 'Mechanism repairs' in product_page
