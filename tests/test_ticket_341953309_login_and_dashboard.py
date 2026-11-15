"""Ticket ABI-341953309: sign-in alerts and the dashboard Catalog size card.

Two independent asks from the same ticket:

* only the head-office admin's *successful* sign-in stops being announced on
  Telegram. The account is recognised by the existing main-profile
  classification (``access.is_main_session`` / ``recovery.main_profile``, i.e.
  ``users.role = 'owner'``), never by a hardcoded name or email. Every other
  account's alert and every other Telegram event is untouched.
* the "Catalog size" card is removed from the dashboard, including the lazy
  loading shell and the desktop grid, which now lays the three remaining cards
  out three across while keeping the existing tablet/phone breakpoints.
"""
import re
from pathlib import Path

import pytest

from app import create_app
from app.db import get_db
from app.services import telegram
from test_branch_login_choice import add_staff, sign_in

HEADLINE_LABELS = ('Total orders', 'Customer base', 'Gross revenue')


@pytest.fixture()
def app(tmp_path):
    return create_app({
        'TESTING': True,
        'DATABASE': str(tmp_path / 'ticket.db'),
        'SECRET_KEY': 'test',
        'ADMIN_EMAIL': 'admin@abi.local',
        'ADMIN_PASSWORD': 'admin123',
        'TELEGRAM_NOTIFICATIONS_ENABLED': '1',
        'TELEGRAM_BOT_TOKEN': 'token',
        'TELEGRAM_CHAT_ID': 'chat',
    })


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def messages(monkeypatch):
    sent = []
    monkeypatch.setattr(
        telegram, '_send_message',
        lambda text: sent.append(text) or {'ok': True, 'sent': True},
    )
    return sent


# --- sign-in alerts ---------------------------------------------------------

def test_head_office_admin_signin_sends_nothing(client, messages):
    assert sign_in(client, follow=False).status_code == 302
    assert messages == []
    # Refreshing and bypassing the chooser cannot resurrect a queued alert.
    client.get('/dashboard')
    client.get('/dashboard')
    client.get('/select-branch')
    assert messages == []
    client.post('/logout')
    sign_in(client, follow=False)
    assert messages == []


def test_classification_is_the_main_profile_role_not_a_name_or_email(client, app, messages):
    """Renaming the account and changing its email must not revive the alert."""
    with app.app_context():
        db = get_db()
        owner = db.execute("SELECT id, email FROM users WHERE role = 'owner'").fetchone()
        db.execute(
            "UPDATE users SET name = ?, email = ? WHERE id = ?",
            ('Renamed Head Office', 'renamed.owner@example.test', owner['id']),
        )
        db.commit()
        renamed = db.execute(
            "SELECT name FROM users WHERE id = ?", (owner['id'],)
        ).fetchone()['name']
    assert renamed == 'Renamed Head Office'

    sign_in(client, follow=False)
    assert messages == []


def test_an_additional_all_branch_account_still_alerts(client, app, messages):
    """A head-office-style *additional* account is not the main profile.

    It sees every branch yet keeps its sign-in alert, proving the suppression is
    scoped to role 'owner' and does not silence staff alerts.
    """
    add_staff(client, app, 'All Branch Staff', [1])
    with app.app_context():
        db = get_db()
        db.execute(
            "UPDATE users SET branch_id = NULL, can_view_all_branches = 1 WHERE name = ?",
            ('All Branch Staff',),
        )
        db.commit()
    messages.clear()

    sign_in(client, 'All Branch Staff', 'staff123', follow=False)

    assert len(messages) == 1
    assert '<b>User logged in</b>' in messages[0]
    assert 'Name: All Branch Staff' in messages[0]
    assert 'Branch: All branches' in messages[0]


# --- dashboard Catalog size card -------------------------------------------

def test_catalog_card_is_gone_from_the_eager_dashboard_and_partial(client):
    sign_in(client)
    body = client.get('/dashboard').get_data(as_text=True)
    partial = client.get('/dashboard/partials/period').get_data(as_text=True)

    for page in (body, partial):
        assert 'Catalog size' not in page
    # "Added · <range>" still belongs to the Customer base card exactly once.
    assert partial.count('Added ·') == 1
    for label in HEADLINE_LABELS:
        assert label in partial
    assert partial.count('class="metric-card"') == 3


def test_lazy_shell_mirrors_the_three_cards(client, app):
    app.config['TESTING'] = False  # turns on the lazy shell, as production runs it
    sign_in(client, follow=False)
    shell = client.get('/dashboard').get_data(as_text=True)

    assert 'data-dashboard-section="period"' in shell
    section = re.search(
        r'<section class="metrics dashboard-metrics dashboard-period-metrics".*?</section>',
        shell, re.S,
    )
    assert section, 'the period metrics section is missing'
    period = section.group()
    assert 'Catalog size' not in period
    assert period.count('metric-card') == 3
    for label in HEADLINE_LABELS:
        assert label in period


def test_period_grid_is_three_across_on_desktop_and_still_responsive(app):
    css = (Path(app.root_path).parent / 'static' / 'css' / 'app.css').read_text(encoding='utf-8')

    assert re.search(
        r'@media\(min-width:1101px\)\{\.dashboard-period-metrics\{'
        r'grid-template-columns:repeat\(3,minmax\(0,1fr\)\)\}\}',
        css,
    ), 'the period cards should be three across on a desktop'
    # The shared .metrics rules keep the tablet two-column and phone one-column
    # fallbacks, so removing the fourth card did not flatten the layout.
    assert '@media(max-width:1100px){.setup-layout{grid-template-columns:1fr}' in css
    assert '.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}}' in css
    assert '@media(max-width:520px){.metrics{grid-template-columns:1fr}' in css
