"""Ticket ABI-341953342 - more than one service / maintenance type per entry.

The owner asked to be able to tick several jobs for the same trailer on the
dashboard's "Trailer service and maintenance" panel.  There is no schema change:
one ``trailer_service_history`` row is written per selected type, so the day
panel, the day report (page, PDF and CSV) and the product's own service history
- all of which already loop the rows - show one line per job.

These tests pin the service layer (dedupe, TRAILER_SERVICE_TYPES order, all or
nothing validation, Custom needing its description) and the route, then check
that each ticked label really reaches every reader.
"""
import os
import re
import tempfile

import pytest
from werkzeug.datastructures import MultiDict

from app import create_app
from app.db import get_db
from app.services import cash, trailer_service

TODAY = cash.today_iso()

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


def _rows(app):
    with app.app_context():
        return get_db().execute(
            """SELECT * FROM trailer_service_history
            ORDER BY id"""
        ).fetchall()


def _post(client, trailer_id, types, custom_description=None):
    # A browser posts the same field name once per ticked box, which is a
    # MultiDict - a plain dict would collapse the repeat.
    data = MultiDict()
    for key, value in (('day', TODAY), ('branch', '1'), ('product_id', str(trailer_id))):
        data.add(key, value)
    for label in types:
        data.add('service_type', label)
    if custom_description is not None:
        data.add('custom_description', custom_description)
    body = client.post('/cash-up/trailer-service', data=data,
                       follow_redirects=True).get_data(as_text=True)
    return body


# --- service layer -----------------------------------------------------------

def test_clean_service_types_orders_by_the_list_and_drops_duplicates():
    assert trailer_service.clean_service_types(
        ['Tyre change', 'Bearing service', 'Tyre change', '  ', 'Custom']
    ) == ['Bearing service', 'Tyre change', 'Custom']
    # A single string still works: the old one-value caller is unchanged.
    assert trailer_service.clean_service_types('Brake drum change') == ['Brake drum change']


def test_clean_service_types_rejects_empty_and_unknown_submissions():
    for values in ([], '', [''], ['   '], None):
        with pytest.raises(ValueError, match='Choose the service or maintenance type'):
            trailer_service.clean_service_types(values)
    for values in (['Bearing service', 'Brake service'], ['Mechanism repairs']):
        with pytest.raises(ValueError, match='Choose the service or maintenance type'):
            trailer_service.clean_service_types(values)


# --- route ------------------------------------------------------------------

def test_three_types_write_three_rows_on_the_same_day_branch_and_user(client, app):
    trailer_id = _seed_trailer(app)
    login(client)
    body = _post(client, trailer_id, ['Bearing service', 'Tyre change', 'Brake adjustment'])
    assert '3 trailer service lines saved' in body

    rows = _rows(app)
    assert [row['service_type'] for row in rows] == ['Bearing service', 'Tyre change', 'Brake adjustment']
    assert {row['service_date'] for row in rows} == {TODAY}
    assert {row['branch_id'] for row in rows} == {1}
    assert len({row['created_by_user_id'] for row in rows}) == 1
    assert rows[0]['created_by_user_id'] is not None


def test_one_type_still_says_one_line_and_writes_one_row(client, app):
    trailer_id = _seed_trailer(app)
    login(client)
    body = _post(client, trailer_id, ['Tyre change'])
    assert 'Trailer service and maintenance saved' in body
    assert len(_rows(app)) == 1


def test_duplicate_posted_types_collapse_to_one_row_each(client, app):
    trailer_id = _seed_trailer(app)
    login(client)
    _post(client, trailer_id, ['Tyre change', 'Tyre change', 'Tyre change', 'Bearing service'])
    assert [row['service_type'] for row in _rows(app)] == ['Bearing service', 'Tyre change']


def test_one_unknown_label_writes_nothing(client, app):
    trailer_id = _seed_trailer(app)
    login(client)
    body = _post(client, trailer_id, ['Bearing service', 'Brake service', 'Tyre change'])
    assert 'Choose the service or maintenance type' in body
    assert _rows(app) == []


def test_no_type_ticked_writes_nothing_and_says_so(client, app):
    trailer_id = _seed_trailer(app)
    login(client)
    body = _post(client, trailer_id, [])
    assert 'Choose the service or maintenance type' in body
    assert _rows(app) == []


def test_custom_needs_its_description_and_keeps_it_on_the_custom_row_only(client, app):
    trailer_id = _seed_trailer(app)
    login(client)
    body = _post(client, trailer_id, ['Custom', 'Tyre change', 'Brake adjustment'])
    assert 'Enter the custom service or maintenance done' in body
    assert _rows(app) == []

    body = _post(client, trailer_id, ['Custom', 'Tyre change', 'Brake adjustment'],
                 custom_description='  Welded a cracked mudguard  ')
    assert '3 trailer service lines saved' in body
    stored = {row['service_type']: row['custom_description'] for row in _rows(app)}
    assert stored == {
        'Custom': 'Welded a cracked mudguard',
        'Tyre change': '',
        'Brake adjustment': '',
    }


# --- every reader shows every label -----------------------------------------

def test_every_ticked_label_reaches_day_panel_report_pdf_csv_and_product_history(client, app):
    trailer_id = _seed_trailer(app)
    login(client)
    labels = ['Bearing service', 'Brake drum change', 'Custom']
    _post(client, trailer_id, labels, custom_description='Fitted a customer bracket')

    with app.app_context():
        entries = trailer_service.services_for_day(TODAY, branch_id=1)
    assert [entry['label'] for entry in entries] == [
        'Bearing service', 'Brake drum change', 'Custom — Fitted a customer bracket',
    ]
    assert {entry['product_name'] for entry in entries} == {'Road trailer'}

    day_page = client.get(f'/day-report?branch=1&day={TODAY}').get_data(as_text=True)
    assert '3 lines' in day_page
    for text in ('Bearing service', 'Brake drum change', 'Custom — Fitted a customer bracket'):
        assert text in day_page

    report = _pdf_text(client.get(f'/cash-up/report.pdf?day={TODAY}&branch=1').data)
    for text in ('Bearing service', 'Brake drum change', 'Custom'):
        assert text in report
    assert 'Fitted a customer bracket' in report
    assert 'Mechanism repairs' not in report

    export = client.get(f'/cash-up/export.csv?day={TODAY}&branch=1').get_data(as_text=True)
    service_lines = [line for line in export.splitlines()
                     if line.startswith('Trailer service and maintenance')]
    assert len(service_lines) == 3
    for text in ('Bearing service', 'Brake drum change', 'Custom — Fitted a customer bracket'):
        assert text in export

    product_page = client.get(f'/inventory/{trailer_id}/edit').get_data(as_text=True)
    for text in ('Bearing service', 'Brake drum change', 'Custom — Fitted a customer bracket'):
        assert text in product_page


def test_stale_page_posting_a_single_value_still_works(client, app):
    """A cached page that still posts one value must not start failing."""
    trailer_id = _seed_trailer(app)
    login(client)
    body = client.post('/cash-up/trailer-service', data={
        'day': TODAY,
        'branch': '1',
        'product_id': str(trailer_id),
        'service_type': 'Brake lining change',
    }, follow_redirects=True).get_data(as_text=True)
    assert 'Trailer service and maintenance saved' in body
    assert [row['service_type'] for row in _rows(app)] == ['Brake lining change']


def test_two_types_for_two_trailers_do_not_mix(client, app):
    """Each submission owns its own rows: no carry-over between trailers."""
    trailer_id = _seed_trailer(app)
    with app.app_context():
        db = get_db()
        db.execute(
            """INSERT INTO products (name, product_type, tracking_method, description, sku, active,
            public_visible, price_amount, price_unit, security_deposit, hourly_extra_rate,
            product_group_id, quantity, branch_id, created_at)
            VALUES ('Box trailer', 'rental', 'bulk', '', 'BOX-TRAILER', 1, 1, 100, 'day', 0, 0,
            (SELECT id FROM product_groups WHERE name = 'Trailers'), 1, 1, '2026-11-20 08:00')"""
        )
        db.commit()
        box_id = db.execute("SELECT id FROM products WHERE name = 'Box trailer'").fetchone()['id']
    login(client)
    _post(client, trailer_id, ['Tyre change', 'Bearing service'])
    _post(client, box_id, ['Trailer repaint'])
    with app.app_context():
        by_product = {}
        for row in get_db().execute("SELECT product_id, service_type FROM trailer_service_history").fetchall():
            by_product.setdefault(row['product_id'], []).append(row['service_type'])
    assert by_product[trailer_id] == ['Bearing service', 'Tyre change']
    assert by_product[box_id] == ['Trailer repaint']
