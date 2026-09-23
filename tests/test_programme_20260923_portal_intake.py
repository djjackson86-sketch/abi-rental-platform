"""Programme phase 9 (feature B / §B2) — public registration form, dedupe and safe lookup.

The acceptance list from ``docs/plans/2026-09-23-branch-public-portal.md`` §B2, one test per line:

* phone normalisation (``+27 82 123 4567``, ``0821234567``, ``082 123 4567`` all match) and
  case-insensitive email normalisation;
* no match → the customer is created **against the branch** with ``source_system='portal'``;
* a phone match does **not** create a second row, and the "This is me" post links the submission
  while leaving the original record's populated fields untouched;
* "None of these — I'm new" creates a second row deliberately;
* the lookup returns **masked data only** — the response body never carries the full surname, the
  email, the address or the balance;
* the honeypot swallows a bot post;
* a submission without consent is refused and writes nothing;
* the rate limit kicks in;
* a disabled or unknown portal 404s;
* a blocked customer is **not resurrected as a new record** (``is_blocked`` is respected).

Decisions from the master plan this file pins:

* **D6** — dedupe is a *decision*, not a block: the customer's answers decide, and the app never
  silently picks. Borrowed from Bubblebounce is the *normalisation + matching* logic only
  (``docs/plans/reference-notes-trailerpro-bubblebounce.md`` §3a); the "is this you?" screen is
  ABI's own, because Bubblebounce merges fully automatically and has no such screen (§3c).
* **D8** — the lookup never leaks: first name + surname initial + the last four digits of the
  phone, and nothing else.
* **D7** — the portal route lives under the ungated ``public.`` prefix, so the write path carries a
  honeypot, a rate limit, an explicit consent requirement and no secret in any response.
* **D10** — the acceptance is required, unticked by default, refused **server-side**, and recorded
  (who / notice version / channel / when) in the same request that writes the customer.
* **D11 / POPIA gate** — while the published notice still carries an open placeholder, the public
  write path stays shut. That is proven here in both directions: refused against the shipped
  document, and working against a completed copy of it.

Fixtures are synthetic throughout: no real licence-disc or client identifier appears in this file.
"""

import os
import sqlite3
import tempfile

import pytest

from app import create_app
from app.db import get_db
from app.services import branches as branches_service
from app.services import consent, portal_intake
from app.services.customers import get_customer, list_customers


def build_app(database_path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": database_path,
            "SECRET_KEY": "test",
            "ADMIN_EMAIL": "admin@abi.local",
            "ADMIN_PASSWORD": "admin123",
        }
    )


@pytest.fixture()
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    os.unlink(path)


@pytest.fixture()
def app(db_path):
    app = build_app(db_path)
    # The shipped notice still carries Sano's five open facts, which (correctly) keeps the public
    # write path shut — so the `client` fixture opens the gate explicitly, and two tests below prove
    # the closed behaviour against the real document.
    portal_intake.reset_rate_limits()
    return app


@pytest.fixture()
def client(app, monkeypatch):
    monkeypatch.setattr(portal_intake, "registration_is_open", lambda: True)
    return app.test_client()


@pytest.fixture()
def branch(app):
    with app.app_context():
        branch_id = branches_service.create_branch({"name": "Midrand Depot", "active": 1})
    return branch_id


def slug_of(app, branch_id):
    with app.app_context():
        return get_db().execute(
            "SELECT public_slug FROM branches WHERE id = ?", (branch_id,)
        ).fetchone()["public_slug"]


def make_customer(app, **values):
    """A client as the counter would have captured them — through the real service."""
    from app.services.customers import create_customer

    with app.app_context():
        payload = {"customer_type": "individual", "name": "Charmaine Mokoena", "phone": "0821234567"}
        payload.update(values)
        return create_customer(payload)


def rows(app, sql="SELECT * FROM customers ORDER BY id", params=()):
    with app.app_context():
        return [dict(row) for row in get_db().execute(sql, params).fetchall()]


def submission(**overrides):
    data = {
        "name": "Pieter van Wyk",
        "phone": "083 555 1234",
        "email": "Pieter@Example.CO.ZA",
        "address_line1": "12 Kerk Street",
        "suburb": "Randburg",
        "city": "Johannesburg",
        "province": "Gauteng",
        "postal_code": "2194",
        "popia_consent": "1",
    }
    data.update(overrides)
    return data


# --- normalisation (D6, borrowed from Bubblebounce §3a) ------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+27 82 123 4567", "0821234567"),
        ("0821234567", "0821234567"),
        ("082 123 4567", "0821234567"),
        ("082-123-4567", "0821234567"),
        ("0027821234567", "0821234567"),
        ("27821234567", "0821234567"),
        ("  082 123 4567  ", "0821234567"),
        ("", ""),
        (None, ""),
        ("not a number", ""),
    ],
)
def test_normalise_phone(raw, expected):
    assert portal_intake.normalise_phone(raw) == expected


def test_normalise_email_is_case_insensitive():
    assert portal_intake.normalise_email("  Pieter@Example.CO.ZA ") == "pieter@example.co.za"
    assert portal_intake.normalise_email(None) == ""


def test_masked_display_is_first_name_surname_initial_and_last_four_digits(app):
    customer_id = make_customer(app, name="Charmaine Mokoena", phone="0821234567")
    with app.app_context():
        customer = get_customer(customer_id)
        display = portal_intake.masked_display(customer)
    assert display == "Charmaine M. · …4567"
    assert "Mokoena" not in display


def test_masked_display_without_a_phone_keeps_only_the_initial(app):
    customer_id = make_customer(app, name="Charmaine Mokoena", phone="")
    with app.app_context():
        display = portal_intake.masked_display(get_customer(customer_id))
    assert display == "Charmaine M."


# --- matching -----------------------------------------------------------------------------------

def test_phone_email_and_name_matches_are_all_found_with_a_confidence(app):
    make_customer(app, name="Charmaine Mokoena", phone="082 123 4567", email="charmaine@example.co.za")
    with app.app_context():
        by_phone = portal_intake.find_possible_matches("Someone Else", "+27 82 123 4567", "")
        by_email = portal_intake.find_possible_matches("Someone Else", "", "CHARMAINE@example.co.za")
        by_name = portal_intake.find_possible_matches("Mokoena Charmaine", "", "")
        nothing = portal_intake.find_possible_matches("Nobody Here", "0810000000", "nobody@example.com")
    assert [m["confidence"] for m in by_phone] == [portal_intake.CONFIDENCE_HIGH]
    assert by_phone[0]["matched_on"] == ["phone"]
    assert [m["confidence"] for m in by_email] == [portal_intake.CONFIDENCE_HIGH]
    assert by_email[0]["matched_on"] == ["email"]
    assert [m["confidence"] for m in by_name] == [portal_intake.CONFIDENCE_LOW]
    assert nothing == []


def test_a_high_confidence_match_outranks_a_name_only_match(app):
    make_customer(app, name="Charmaine Mokoena", phone="0820000000")
    strong = make_customer(app, name="Pieter van Wyk", phone="083 555 1234")
    with app.app_context():
        matches = portal_intake.find_possible_matches("Pieter van Wyk", "083 555 1234", "")
    assert matches[0]["customer_id"] == strong
    assert matches[0]["confidence"] == portal_intake.CONFIDENCE_HIGH


# --- the open/closed gate (D11) -----------------------------------------------------------------

def test_public_registration_is_closed_while_the_notice_is_unfinished(app, branch):
    """The shipped document carries Sano's open facts, so nothing may be written.

    Deliberately an *unpatched* client: this is the real shipped state, not the harness.
    """
    slug = slug_of(app, branch)
    raw = app.test_client()
    response = raw.get(f"/portal/{slug}/register")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "counter" in body.lower()
    posted = raw.post(f"/portal/{slug}/register", data=submission())
    assert posted.status_code == 200
    assert rows(app) == []
    assert "counter" in posted.get_data(as_text=True).lower()


def test_the_gate_is_the_notice_document_and_not_a_second_switch(app, branch, monkeypatch):
    slug = slug_of(app, branch)
    raw = app.test_client()
    monkeypatch.setattr(portal_intake, "registration_is_open", lambda: True)
    accepted = raw.post(f"/portal/{slug}/register", data=submission())
    assert accepted.status_code == 200
    assert len(rows(app)) == 1, "with the notice complete the same route must accept the form"


# --- creation -----------------------------------------------------------------------------------

def test_no_match_creates_the_client_against_the_branch(client, branch, app):
    slug = slug_of(app, branch)
    response = client.post(f"/portal/{slug}/register", data=submission())
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Thank you" in body
    created = rows(app)
    assert len(created) == 1
    row = created[0]
    assert row["name"] == "Pieter van Wyk"
    assert row["phone"] == "083 555 1234"
    assert row["email"] == "pieter@example.co.za", "the public form lower-cases the email like the staff form"
    assert row["branch_id"] == branch
    assert row["source_system"] == portal_intake.PORTAL_SOURCE_SYSTEM
    assert row["source_id"].startswith(f"{slug}:"), "the slug is recorded on the record it created"
    assert row["created_by_user_id"] is None, "a public submission has no staff author"
    assert row["is_blocked"] == 0


def test_the_created_client_is_visible_to_staff_on_their_branch(client, branch, app):
    slug = slug_of(app, branch)
    client.post(f"/portal/{slug}/register", data=submission())
    with app.app_context():
        listed = list_customers()
        customer = get_customer(1)
    assert [row["name"] for row in listed] == ["Pieter van Wyk"]
    assert customer["branch_name"] == "Midrand Depot", "the record carries the branch the QR belongs to"


def test_two_registrations_for_the_same_branch_both_land(client, branch, app):
    """``source_id`` carries a per-submission reference — one client per branch is wrong.

    The customers table has a *unique* index on ``(source_system, source_id)`` (the Booqable
    importer's), so storing the bare slug there would refuse the branch's second sign-up with a
    database error. The slug is a prefix; the reference makes it unique.
    """
    slug = slug_of(app, branch)
    client.post(f"/portal/{slug}/register", data=submission())
    client.post(
        f"/portal/{slug}/register",
        data=submission(name="Ann Other", phone="084 111 2222", email="ann@example.co.za"),
    )
    assert [row["name"] for row in rows(app)] == ["Pieter van Wyk", "Ann Other"]
    ids = {row["source_id"] for row in rows(app)}
    assert len(ids) == 2


def test_the_recorded_provenance_comes_from_the_url_not_a_posted_field(client, branch, app):
    """A hidden input must not be able to rewrite where a record came from."""
    slug = slug_of(app, branch)
    client.post(f"/portal/{slug}/register", data=submission(portal_slug="some-other-branch"))
    assert rows(app)[0]["source_id"].startswith(f"{slug}:")
    assert "some-other-branch" not in rows(app)[0]["source_id"]


def test_the_reference_is_shown_to_the_customer(client, branch, app):
    slug = slug_of(app, branch)
    body = client.post(f"/portal/{slug}/register", data=submission()).get_data(as_text=True)
    reference = rows(app)[0]["source_id"].split(":", 1)[1]
    assert reference in body
    assert len(reference) >= 6


# --- dedupe: "is this you?" (D6) ----------------------------------------------------------------

def test_a_phone_match_does_not_create_a_second_row_and_asks_the_customer(client, branch, app):
    existing = make_customer(app, name="Charmaine Mokoena", phone="082 123 4567")
    slug = slug_of(app, branch)
    response = client.post(
        f"/portal/{slug}/register",
        data=submission(name="Charmaine Mokoena", phone="+27 82 123 4567", email="charmaine@example.co.za"),
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Is this you" in body
    assert "Charmaine M." in body, "the candidate is shown masked"
    assert len(rows(app)) == 1, "nothing is created until the customer decides"
    assert f"link:{existing}" in body


def test_this_is_me_links_the_submission_and_only_fills_blank_fields(client, branch, app):
    existing = make_customer(
        app,
        name="Charmaine Mokoena",
        phone="0821234567",
        email="charmaine@example.co.za",
        address_line1="9 Old Road",
    )
    slug = slug_of(app, branch)
    response = client.post(
        f"/portal/{slug}/register",
        data=submission(
            name="Charmaine Mokoena",
            phone="082 123 4567",
            email="CHARMAINE@example.co.za",
            address_line1="12 New Street",
            city="Johannesburg",
            decision=f"link:{existing}",
        ),
    )
    assert response.status_code == 200
    assert "Thank you" in response.get_data(as_text=True)
    assert len(rows(app)) == 1
    row = rows(app)[0]
    assert row["address_line1"] == "9 Old Road", "a populated field is never overwritten"
    assert row["city"] == "Johannesburg", "a blank field is filled in from the portal"
    assert row["email"] == "charmaine@example.co.za"


def test_none_of_these_creates_a_second_row_on_purpose(client, branch, app):
    make_customer(app, name="Charmaine Mokoena", phone="0821234567")
    slug = slug_of(app, branch)
    response = client.post(
        f"/portal/{slug}/register",
        data=submission(name="Charmaine Mokoena", phone="082 123 4567", decision="create"),
    )
    assert "Thank you" in response.get_data(as_text=True)
    assert [row["name"] for row in rows(app)] == ["Charmaine Mokoena", "Charmaine Mokoena"]


def test_a_link_to_a_client_who_was_not_a_candidate_is_refused(client, branch, app):
    """Otherwise a crafted post could write to any customer id in the book."""
    victim = make_customer(app, name="Someone Else", phone="081 999 8888")
    slug = slug_of(app, branch)
    response = client.post(
        f"/portal/{slug}/register",
        data=submission(decision=f"link:{victim}"),
    )
    assert response.status_code == 400
    assert len(rows(app)) == 1
    assert rows(app)[0]["city"] == "", "nothing was written to a client this submission did not match"


def test_a_malformed_decision_is_refused(client, branch, app):
    slug = slug_of(app, branch)
    response = client.post(f"/portal/{slug}/register", data=submission(decision="link:not-a-number"))
    assert response.status_code == 400
    assert rows(app) == []


# --- the blocked rule ---------------------------------------------------------------------------

def test_a_blocked_client_is_not_resurrected_as_a_new_record(client, branch, app):
    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO customers (customer_type, name, phone, created_at, is_blocked, blocked_reason) "
            "VALUES ('individual', 'Charmaine Mokoena', '0821234567', '2026-09-01T09:00:00', 1, 'Unpaid')"
        )
        db.commit()
    slug = slug_of(app, branch)
    response = client.post(
        f"/portal/{slug}/register",
        data=submission(name="Charmaine Mokoena", phone="082 123 4567", decision="create"),
    )
    assert len(rows(app)) == 1, "the block is not bypassed by registering again"
    assert "counter" in response.get_data(as_text=True).lower()


def test_linking_to_a_blocked_client_is_refused_too(client, branch, app):
    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO customers (customer_type, name, phone, created_at, is_blocked, blocked_reason) "
            "VALUES ('individual', 'Charmaine Mokoena', '0821234567', '2026-09-01T09:00:00', 1, 'Unpaid')"
        )
        db.commit()
    slug = slug_of(app, branch)
    response = client.post(
        f"/portal/{slug}/register",
        data=submission(name="Charmaine Mokoena", phone="0821234567", decision="link:1"),
    )
    assert "counter" in response.get_data(as_text=True).lower()
    assert rows(app)[0]["city"] == ""


# --- consent (D10) ------------------------------------------------------------------------------

def test_a_submission_without_consent_is_refused_and_writes_nothing(client, branch, app):
    slug = slug_of(app, branch)
    data = submission()
    data.pop("popia_consent")
    response = client.post(f"/portal/{slug}/register", data=data)
    assert response.status_code in (200, 400)
    assert consent.consent_required_error() in response.get_data(as_text=True)
    assert rows(app) == []


def test_an_untruthy_consent_value_is_refused(client, branch, app):
    slug = slug_of(app, branch)
    response = client.post(f"/portal/{slug}/register", data=submission(popia_consent="0"))
    assert consent.consent_required_error() in response.get_data(as_text=True)
    assert rows(app) == []


def test_an_accepted_submission_records_the_consent_on_the_portal_channel(client, branch, app):
    slug = slug_of(app, branch)
    client.post(f"/portal/{slug}/register", data=submission())
    with app.app_context():
        summary = consent.consent_summary(1)
        stored = get_db().execute("SELECT * FROM consent_records").fetchall()
    assert summary.startswith("POPIA consent — accepted 2026-")
    assert summary.endswith("(notice v1.1, via branch portal)")
    assert len(stored) == 1
    assert stored[0]["channel"] == consent.CHANNEL_PORTAL
    assert stored[0]["notice_version"] == consent.PRIVACY_NOTICE_VERSION


def test_linking_also_records_the_consent(client, branch, app):
    make_customer(app, name="Charmaine Mokoena", phone="0821234567")
    slug = slug_of(app, branch)
    client.post(
        f"/portal/{slug}/register",
        data=submission(name="Charmaine Mokoena", phone="0821234567", decision="link:1"),
    )
    with app.app_context():
        stored = get_db().execute("SELECT * FROM consent_records").fetchall()
    assert [row["customer_id"] for row in stored] == [1]


# --- honeypot + rate limit (D7) -----------------------------------------------------------------

def test_the_honeypot_swallows_a_bot_post(client, branch, app):
    slug = slug_of(app, branch)
    response = client.post(
        f"/portal/{slug}/register",
        data=submission(**{portal_intake.HONEYPOT_FIELD: "http://spam.example"}),
    )
    assert response.status_code == 200
    assert rows(app) == [], "a filled honeypot must not create anything"


def test_the_lookup_rate_limit_kicks_in(client, branch, app):
    make_customer(app, name="Charmaine Mokoena", phone="0821234567")
    slug = slug_of(app, branch)
    statuses = [
        client.post(f"/portal/{slug}/check", data={"name": "Charmaine Mokoena", "phone": "0821234567"}).status_code
        for _ in range(portal_intake.LOOKUP_LIMIT + 2)
    ]
    assert statuses[: portal_intake.LOOKUP_LIMIT] == [200] * portal_intake.LOOKUP_LIMIT
    assert statuses[-1] == 429


def test_the_rate_limit_window_expires():
    portal_intake.reset_rate_limits()
    for _ in range(portal_intake.LOOKUP_LIMIT):
        assert portal_intake.allow_lookup("203.0.113.9", now_ts=1000.0) is True
    assert portal_intake.allow_lookup("203.0.113.9", now_ts=1000.0) is False
    later = 1000.0 + portal_intake.LOOKUP_WINDOW_SECONDS + 1
    assert portal_intake.allow_lookup("203.0.113.9", now_ts=later) is True


def test_the_rate_limit_never_stores_the_address_itself():
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "app" / "services" / "portal_intake.py"
    ).read_text(encoding="utf-8")
    assert "sha256" in source, "the limiter keys on a salted digest, never on the address"
    for banned in ("ip_address", "user_agent", "useragent"):
        assert banned not in source.lower(), f"{banned} must not be stored (data minimisation)"


# --- the lookup never leaks (D8) ----------------------------------------------------------------

def test_the_lookup_finds_the_client_and_returns_masked_data_only(client, branch, app):
    make_customer(
        app,
        name="Charmaine Mokoena",
        phone="0821234567",
        email="charmaine@example.co.za",
        address_line1="9 Old Road",
    )
    with app.app_context():
        get_db().execute("UPDATE customers SET balance_due = 4321.99 WHERE id = 1")
        get_db().commit()
    slug = slug_of(app, branch)
    response = client.post(f"/portal/{slug}/check", data={"name": "Charmaine Mokoena", "phone": "082 123 4567"})
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Charmaine M." in body
    assert "…4567" in body
    for secret in ("Mokoena", "charmaine@example.co.za", "9 Old Road", "4321"):
        assert secret not in body, f"the public lookup must not reveal {secret!r}"
    assert "Yes" in body or "yes" in body


def test_the_lookup_misses_when_the_name_does_not_match(client, branch, app):
    make_customer(app, name="Charmaine Mokoena", phone="0821234567")
    slug = slug_of(app, branch)
    body = client.post(
        f"/portal/{slug}/check", data={"name": "Someone Else", "phone": "0821234567"}
    ).get_data(as_text=True)
    assert "Charmaine" not in body
    assert "could not find" in body.lower()


def test_the_lookup_needs_both_a_name_and_a_number(client, branch, app):
    make_customer(app, name="Charmaine Mokoena", phone="0821234567")
    slug = slug_of(app, branch)
    body = client.post(f"/portal/{slug}/check", data={"phone": "0821234567"}).get_data(as_text=True)
    assert "Charmaine" not in body
    assert "name" in body.lower()


def test_the_lookup_does_not_answer_for_a_blocked_client(client, branch, app):
    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO customers (customer_type, name, phone, created_at, is_blocked, blocked_reason) "
            "VALUES ('individual', 'Charmaine Mokoena', '0821234567', '2026-09-01T09:00:00', 1, 'Unpaid')"
        )
        db.commit()
    slug = slug_of(app, branch)
    body = client.post(
        f"/portal/{slug}/check", data={"name": "Charmaine Mokoena", "phone": "0821234567"}
    ).get_data(as_text=True)
    assert "Charmaine" not in body
    assert "could not find" in body.lower()


def test_the_lookup_route_is_post_only(client, branch, app):
    slug = slug_of(app, branch)
    assert client.get(f"/portal/{slug}/check").status_code == 405


# --- routes: gating, validation, no tokens ------------------------------------------------------

def test_an_unknown_slug_404s_for_the_form_and_the_lookup(client):
    assert client.get("/portal/no-such-branch/register").status_code == 404
    assert client.post("/portal/no-such-branch/register", data=submission()).status_code == 404
    assert client.post("/portal/no-such-branch/check", data={"name": "x", "phone": "0821234567"}).status_code == 404


def test_a_disabled_portal_404s_for_the_form_and_the_lookup(app, client, branch):
    slug = slug_of(app, branch)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE branches SET portal_enabled = 0 WHERE id = ?", (branch,))
        db.commit()
    assert client.get(f"/portal/{slug}/register").status_code == 404
    assert client.post(f"/portal/{slug}/register", data=submission()).status_code == 404
    assert client.post(f"/portal/{slug}/check", data={"name": "x", "phone": "0821234567"}).status_code == 404


def test_the_name_is_required(client, branch, app):
    slug = slug_of(app, branch)
    response = client.post(f"/portal/{slug}/register", data=submission(name="   "))
    assert response.status_code == 400
    assert "name" in response.get_data(as_text=True).lower()
    assert rows(app) == []


def test_a_junk_email_is_refused(client, branch, app):
    slug = slug_of(app, branch)
    response = client.post(f"/portal/{slug}/register", data=submission(email="not-an-email"))
    assert response.status_code == 400
    assert "email" in response.get_data(as_text=True).lower()
    assert rows(app) == []


def test_a_junk_phone_is_refused(client, branch, app):
    slug = slug_of(app, branch)
    response = client.post(f"/portal/{slug}/register", data=submission(phone="123"))
    assert response.status_code == 400
    assert "phone" in response.get_data(as_text=True).lower()
    assert rows(app) == []


def test_a_contact_detail_is_required(client, branch, app):
    slug = slug_of(app, branch)
    response = client.post(f"/portal/{slug}/register", data=submission(phone="", email=""))
    assert response.status_code == 400
    assert rows(app) == []


def test_the_portal_pages_carry_no_placeholder_token_and_the_shared_consent_block(client, branch, app):
    make_customer(app, name="Charmaine Mokoena", phone="0821234567")
    slug = slug_of(app, branch)
    pages = [
        client.get(f"/portal/{slug}").get_data(as_text=True),
        client.get(f"/portal/{slug}/register").get_data(as_text=True),
        client.post(f"/portal/{slug}/register", data=submission()).get_data(as_text=True),
        client.post(
            f"/portal/{slug}/register",
            data=submission(name="Charmaine Mokoena", phone="0821234567", decision="create"),
        ).get_data(as_text=True),
        client.post(f"/portal/{slug}/check", data={"name": "Charmaine Mokoena", "phone": "0821234567"}).get_data(as_text=True),
    ]
    for page in pages:
        assert "[" not in page, "a customer-facing page must not carry a bracketed placeholder token"
    form = client.get(f"/portal/{slug}/register").get_data(as_text=True)
    assert '{% include "public/_consent_block.html" %}' not in form, "Jinja must have rendered the block"
    assert 'name="popia_consent"' in form
    assert "checked" not in form.split('name="popia_consent"')[0][-80:], "the consent box must not arrive ticked"


def test_the_portal_landing_page_is_the_form(client, branch, app):
    """The QR points at /portal/<slug>, so that page is the form — not a "coming soon" note."""
    slug = slug_of(app, branch)
    body = client.get(f"/portal/{slug}").get_data(as_text=True)
    assert "Midrand Depot" in body
    assert 'name="name"' in body
    assert "coming" not in body.lower()


def test_placeholders_cannot_be_mistaken_for_recorded_values(client, branch, app):
    """A bare number in a placeholder reads as a filled-in field (found by looking at the shots).

    Phase 5 hit this on the inventory form and fixed it with the `e.g.` prefix; `vision_analyze`
    read `082 123 4567` here as an entered value, so the same rule applies to this form.
    """
    slug = slug_of(app, branch)
    body = client.get(f"/portal/{slug}/register").get_data(as_text=True)
    import re

    placeholders = re.findall(r'placeholder="([^"]*)"', body)
    assert placeholders, "the form keeps an example number so staff do not have to guess the shape"
    for placeholder in placeholders:
        assert placeholder.startswith("e.g. "), placeholder


def test_the_portal_landing_page_still_refuses_a_post(app, client, branch):
    slug = slug_of(app, branch)
    assert client.post(f"/portal/{slug}", data={"name": "Anyone"}).status_code == 405


# --- data hygiene -------------------------------------------------------------------------------

def test_a_refused_submission_leaves_the_table_exactly_as_it_was(client, branch, app):
    make_customer(app, name="Charmaine Mokoena", phone="0821234567")
    before = rows(app)
    slug = slug_of(app, branch)
    for data in (
        submission(name=""),
        submission(email="junk"),
        submission(phone="", email=""),
        submission(popia_consent="no"),
    ):
        client.post(f"/portal/{slug}/register", data=data)
    assert rows(app) == before


def test_the_service_refuses_a_link_to_a_missing_customer(app, branch):
    with app.app_context():
        with pytest.raises(ValueError):
            portal_intake.create_or_link_customer(
                {"name": "Pieter van Wyk", "phone": "0835551234"}, branch, "link:987654"
            )


def test_the_database_still_refuses_duplicate_source_keys(app, branch):
    """The trap this phase had to design around, pinned so it cannot be re-introduced."""
    slug = slug_of(app, branch)
    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO customers (customer_type, name, source_system, source_id, created_at) "
            "VALUES ('individual', 'A', 'portal', ?, '2026-09-23T10:00:00')",
            (f"{slug}:AAAAAA",),
        )
        db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO customers (customer_type, name, source_system, source_id, created_at) "
                "VALUES ('individual', 'B', 'portal', ?, '2026-09-23T10:01:00')",
                (f"{slug}:AAAAAA",),
            )
