"""Client portal changes — frontend (templates + CSS) contracts.

Pins the visible half of the owner's approved changes, so the markup cannot drift from the
backend contract that ``tests/test_client_portal_pending_registrations.py`` already fixes:

* the public registration form carries the 11 mandatory fields with a small red asterisk and
  browser validation, and no longer offers Country or the vehicle fields;
* ``website`` is a company-only field (``name="website"``), while ``company_website`` stays the
  invisible honeypot — off-screen, out of the tab order;
* the name label follows the customer type everywhere customer details are typed
  (Individual → "Name & Surname", Company → "Company Name"), with the ``name`` attribute unchanged;
* a self-registered client shows, at the top of Customers, in a movement-card queue and, on their
  edit/detail page, in a verification panel that offers ID verification and main-vehicle capture in
  either order and keeps approval disabled until both are met;
* the portal confirmation names the branch the submission was attributed to.

Fixtures are synthetic and notifications are off; no message is ever sent.
"""

import re

import pytest

from app import create_app
from app.db import get_db
from app.services import branches as branches_service
from app.services import portal_intake


def build_app(database_path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": database_path,
            "SECRET_KEY": "test",
            "ADMIN_EMAIL": "admin@abi.local",
            "ADMIN_PASSWORD": "admin123",
            "TELEGRAM_NOTIFICATIONS_ENABLED": "",
        }
    )


@pytest.fixture()
def app(tmp_path):
    return build_app(str(tmp_path / "frontend.sqlite"))


@pytest.fixture()
def client(app, monkeypatch):
    monkeypatch.setattr(portal_intake, "registration_is_open", lambda: True)
    return app.test_client()


@pytest.fixture()
def branch(app):
    with app.app_context():
        return branches_service.create_branch({"name": "Midrand Depot", "active": 1})


def slug_of(app, branch_id):
    with app.app_context():
        return get_db().execute(
            "SELECT public_slug FROM branches WHERE id = ?", (branch_id,)
        ).fetchone()["public_slug"]


SUBMISSION = {
    "name": "Pieter van Wyk",
    "email": "pieter@example.co.za",
    "phone": "083 555 1234",
    "address_line1": "12 Kerk Street",
    "suburb": "Randburg",
    "city": "Johannesburg",
    "province": "Gauteng",
    "postal_code": "2194",
    "alternative_contact_name": "Jane Smith",
    "alternative_contact_number": "082 111 2222",
    "alternative_contact_relationship": "Sister",
    "popia_consent": "1",
}

REQUIRED_FIELDS = (
    "name", "email", "phone", "address_line1", "suburb", "city", "province", "postal_code",
    "alternative_contact_name", "alternative_contact_number", "alternative_contact_relationship",
)

REMOVED_FIELDS = ("country", "vehicle_make", "vehicle_color", "vehicle_reg_no")


def register(client, app, branch_id, **overrides):
    data = dict(SUBMISSION)
    data.update(overrides)
    return client.post(f"/portal/{slug_of(app, branch_id)}/register", data=data)


def customer_by_email(app, email):
    with app.app_context():
        row = get_db().execute("SELECT * FROM customers WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None


def registration_for(app, customer_id):
    with app.app_context():
        row = get_db().execute(
            "SELECT * FROM portal_registrations WHERE customer_id = ?", (customer_id,)
        ).fetchone()
        return dict(row) if row else None


def owner_id(app):
    with app.app_context():
        return get_db().execute(
            "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
        ).fetchone()["id"]


def sign_in(client, app, role="owner"):
    with client.session_transaction() as session:
        session["user_id"] = owner_id(app)
        session["user_role"] = role
        session["user_name"] = "Synthetic Tester"
        session["initials"] = "ST"
        session["branch_id"] = None
        session["can_view_all_branches"] = True
        session["branch_ids"] = []
        session["staff_modules"] = []


def scoped_csrf(client):
    with client.session_transaction() as session:
        return (session.get("_csrf_tokens") or {}).get("portal_approval")


@pytest.fixture()
def pending_client(client, app, branch):
    """A self-registered client plus the owner session, ready to render the staff pages."""
    register(client, app, branch)
    stored = customer_by_email(app, "pieter@example.co.za")
    registration = registration_for(app, stored["id"])
    sign_in(client, app)
    return stored, registration


# ---------------------------------------------------------------- the public registration form

def test_the_form_has_the_eleven_mandatory_fields_with_a_red_asterisk(client, app, branch):
    body = client.get(f"/portal/{slug_of(app, branch)}/register").get_data(as_text=True)
    for field in REQUIRED_FIELDS:
        assert f'name="{field}"' in body, field
        assert re.search(rf'name="{field}"[^>]*\brequired\b', body), f"{field} must be required"
    # One small red asterisk per mandatory field — and the marker class the stylesheet colours.
    assert body.count('class="req-star"') >= 11
    assert body.count("req-star") >= 11


def test_country_and_the_vehicle_fields_are_gone_from_the_public_form(client, app, branch):
    body = client.get(f"/portal/{slug_of(app, branch)}/register").get_data(as_text=True)
    for field in REMOVED_FIELDS:
        assert f'name="{field}"' not in body, f"{field} must not be on the public form"


def test_website_is_a_company_only_field_and_the_honeypot_stays_hidden(client, app, branch):
    body = client.get(f"/portal/{slug_of(app, branch)}/register").get_data(as_text=True)
    # The genuine Website field lives inside the company-only section, which is hidden for an
    # individual.
    company_section = body.split('data-company-details', 1)[1].split("</section>", 1)[0]
    assert 'name="website"' in company_section
    assert "hidden" in company_section.split(">", 1)[0]
    # The honeypot is a different field, kept off-screen and out of the tab order.
    honeypot = re.search(r'<div class="honeypot"[^>]*>(.*?)</div>', body, re.S)
    assert honeypot, "the honeypot block must be present"
    assert 'name="company_website"' in honeypot.group(1)
    assert 'tabindex="-1"' in honeypot.group(1)


def test_the_stylesheet_hides_the_honeypot_offscreen():
    from pathlib import Path

    css = Path(__file__).resolve().parents[1] / "static" / "css" / "app.css"
    rule = re.search(r"\.honeypot\{([^}]*)\}", css.read_text(encoding="utf-8"))
    assert rule, "the honeypot needs a .honeypot rule"
    assert "left:-9999px" in rule.group(1)
    assert "position:absolute" in rule.group(1)


def test_the_required_field_marker_is_a_small_red_asterisk():
    from pathlib import Path

    css = Path(__file__).resolve().parents[1] / "static" / "css" / "app.css"
    rule = re.search(r"\.req-star\{([^}]*)\}", css.read_text(encoding="utf-8"))
    assert rule and "color:#d4342c" in rule.group(1)


def test_the_name_label_switches_with_the_customer_type(client, app, branch):
    # Portal form: the marker the script drives, plus the default individual wording.
    portal = client.get(f"/portal/{slug_of(app, branch)}/register").get_data(as_text=True)
    assert "data-portal-name-label" in portal
    assert "Name &amp; Surname" in portal
    assert "'Company Name'" in portal and "'Name & Surname'" in portal
    # Staff customer form (new + edit): same marker, and the preserved company-section JS line.
    sign_in(client, app)
    staff = client.get("/customers/new").get_data(as_text=True)
    assert "data-customer-name-label" in staff
    assert "companySection.hidden = !selected || selected.value !== 'company'" in staff


def test_the_order_forms_switch_their_name_labels_too(client, app):
    sign_in(client, app)
    body = client.get("/orders/new").get_data(as_text=True)
    assert "data-inline-customer-name-label" in body
    assert "data-edit-customer-name-label" in body


def test_the_portal_confirmation_names_the_branch(client, app, branch):
    response = register(client, app, branch)
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Midrand Depot" in body, "the confirmation must show the branch the submission belongs to"


# ---------------------------------------------------------------- the pending queue on Customers

def test_the_pending_queue_appears_at_the_top_of_customers(client, app, branch, pending_client):
    stored, registration = pending_client
    body = client.get("/customers").get_data(as_text=True)
    assert "Awaiting verification" in body
    assert "pending-queue-card" in body, "the queue reuses the movement-card styling"
    assert "movement-card" in body
    assert registration["reference"] in body
    assert "Pieter van Wyk" in body
    assert "Midrand Depot" in body
    assert f"/customers/{stored['id']}/edit" in body, "staff open the pending customer directly"


def test_the_pending_queue_is_absent_for_an_ordinary_customer(client, app):
    sign_in(client, app)
    body = client.get("/customers").get_data(as_text=True)
    assert "pending-queue-card" not in body


# ---------------------------------------------------------------- the verification panel

def test_the_edit_page_shows_the_verification_panel_and_a_disabled_approval(client, app, pending_client):
    stored, registration = pending_client
    body = client.get(f"/customers/{stored['id']}/edit").get_data(as_text=True)
    assert "pending-registration-panel" in body
    assert registration["reference"] in body
    # Approval is a POST to the scoped endpoint, carrying the hidden CSRF token.
    action = f"/customers/portal/{registration['id']}/approve"
    assert f'action="{action}"' in body
    assert re.search(r'name="csrf_token" value="[^"]+"', body)
    assert scoped_csrf(client), "the scoped token is issued for the approve form"
    # Both checks are open, so the button is disabled.
    assert re.search(r'<button[^>]*\bdisabled\b[^>]*>\s*Approve client', body)
    # ...and the panel offers both workflow actions.
    assert f"/scan-vehicle?customer_id={stored['id']}" in body or f"customer_id={stored['id']}" in body
    assert "Set ID verification" in body


def test_the_approval_form_sits_outside_the_customer_form(client, app, pending_client):
    stored, _registration = pending_client
    body = client.get(f"/customers/{stored['id']}/edit").get_data(as_text=True)
    # The panel (and its own <form>) is rendered before the customer form — never nested inside it.
    assert body.index("pending-approve-form") < body.index("settings-form panel customer-form")


def test_the_detail_page_shows_the_verification_panel(client, app, pending_client):
    stored, registration = pending_client
    body = client.get(f"/customers/{stored['id']}").get_data(as_text=True)
    assert "pending-registration-panel" in body
    assert registration["reference"] in body
    assert "Approve client" in body


def test_an_ordinary_customer_has_no_verification_panel(client, app):
    sign_in(client, app)
    with app.app_context():
        from app.services.customers import create_customer

        created = create_customer({"name": "Walk In", "phone": "0831112222"})
    body = client.get(f"/customers/{created}/edit").get_data(as_text=True)
    assert "pending-registration-panel" not in body


def test_approval_enables_once_id_and_main_vehicle_are_both_recorded(client, app, branch):
    register(client, app, branch)
    stored = customer_by_email(app, "pieter@example.co.za")
    sign_in(client, app)

    # ID verification first, then the main vehicle — either order must end the same way.
    client.post(
        f"/customers/{stored['id']}/edit",
        data={
            "name": "Pieter van Wyk", "email": "pieter@example.co.za", "phone": "083 555 1234",
            "client_verified": "1",
        },
    )
    body = client.get(f"/customers/{stored['id']}/edit").get_data(as_text=True)
    assert "disabled" in body.split("Approve client")[0].rsplit("<button", 1)[1], "still missing a vehicle"

    client.post(
        "/scan-vehicle/save",
        data={"customer_id": str(stored["id"]), "registration": "NB72XMGP", "make": "Toyota", "make_main": "yes"},
    )
    body = client.get(f"/customers/{stored['id']}/edit").get_data(as_text=True)
    button = body.split("Approve client")[0].rsplit("<button", 1)[1]
    assert "disabled" not in button, "both checks are met, so approval is enabled"
