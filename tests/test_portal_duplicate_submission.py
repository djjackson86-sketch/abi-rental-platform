"""Double-submit protection for the public customer portal (client portal changes).

The bug this file pins: tapping the branch / universal registration button twice while the first
request was still "loading" created **two companies**. A sequential second tap was already safe —
the dedupe screen ("Is this you?") catches it once the first customer is committed — so the bug was
only ever the *concurrent* case, where neither request can yet see the other's customer.

The fix has two halves, both pinned here:

* **Server.** Every rendered form carries an unguessable ``request_key``. Before writing a customer
  the request *claims* that key in the database (a unique index on ``(scope, request_key)``): the
  first request wins and writes, a replay of the same key answers with the **same** success and
  writes nothing, and a key another request still holds is refused with a clear message. The claim
  is a single INSERT on purpose — the libSQL adapter autocommits and its commit()/rollback() are
  no-ops (``app/turso_db.py``), so a claim that rode on a transaction would not be visible to a
  concurrent request. A request that carries no key (an old fixture, a hand-written POST) still gets
  the guard, derived from a digest of the submission itself.
* **Browser.** The form disables and relabels its submit button the moment a valid form is
  submitted, so a second tap never leaves the browser at all.

Fixtures are synthetic; no real client data and no live service is touched.
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
    return build_app(str(tmp_path / "duplicate-submission.sqlite"))


@pytest.fixture()
def client(app, monkeypatch):
    # The shipped privacy notice still carries Sano's open facts, which correctly keeps the public
    # write path shut; the gate itself is pinned elsewhere, so open it here.
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


def register_url(app, branch_id):
    return f"/portal/{slug_of(app, branch_id)}/register"


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
        "alternative_contact_name": "Jane Smith",
        "alternative_contact_number": "082 111 2222",
        "alternative_contact_relationship": "Sister",
        "popia_consent": "1",
    }
    data.update(overrides)
    # Synthetic full-name fixtures now supply the explicit portal name parts.
    parts = data["name"].strip().split(" ", 1)
    data["first_names"] = parts[0]
    data["surname"] = parts[1] if len(parts) > 1 else ""
    return data


def count(app, sql, params=()):
    with app.app_context():
        return get_db().execute(sql, params).fetchone()[0]


def customers(app):
    return count(app, "SELECT COUNT(*) FROM customers")


def KEY(n=1):
    """A stand-in browser key: long enough to pass the server's floor, distinct per n."""
    import hashlib

    return hashlib.sha256(f"synthetic-key-{n}".encode()).hexdigest()


def stored_key(app, url):
    """The request_key a freshly rendered form carries."""
    body = app.test_client().get(url).get_data(as_text=True)
    match = re.search(r'name="request_key" value="([^"]+)"', body)
    assert match, "the form must render a request_key field"
    return match.group(1)


# ---------------------------------------------------------------- the browser half

def test_the_form_renders_an_unguessable_request_key(client, app, branch):
    url = register_url(app, branch)
    key = stored_key(app, url)
    assert len(key) >= portal_intake.REQUEST_KEY_MIN_LEN
    # A fresh render gets a fresh key — two forms are two submissions.
    assert stored_key(app, url) != key


def test_the_capture_form_carries_the_busy_state(client, app, branch):
    """The capture form disables its button on submit, keeps a named button's value and resets
    itself when Safari restores it from the back/forward cache."""
    url = register_url(app, branch)
    form = client.get(url).get_data(as_text=True)
    assert "aria-busy" in form and "Sending" in form
    assert "pageshow" in form and "submitter" in form, "bfcache reset and submitter mirror are present"
    # Chrome restores the previous form state onto the response document, so a validation
    # re-render can arrive with the button stuck disabled on "Sending...": every load resets it,
    # not only a persisted pageshow.
    assert "resetAll" in form and "DOMContentLoaded" in form and "'load'" in form
    # A customer-facing page must not carry a bracketed placeholder token.
    assert "[" not in form


def test_a_repeated_contact_now_gets_the_duplicate_warning(client, app, branch):
    """The old behaviour asked "Is this you?"; the new rule refuses an exact match outright."""
    with app.app_context():
        from app.services.customers import create_customer

        create_customer({"customer_type": "individual", "name": "Charmaine Mokoena", "phone": "0821234567"})
    body = client.post(
        register_url(app, branch),
        data=submission(name="Charmaine Mokoena", phone="082 123 4567", email="charmaine@example.co.za"),
    ).get_data(as_text=True)

    assert portal_intake.DUPLICATE_MESSAGE in body
    assert "Is this you" not in body, "the candidate decision screen is gone for the public route"
    assert "Charmaine" not in body, "the duplicate warning must not expose the matched client's name"
    assert customers(app) == 1, "an exact contact match never creates a second customer"


def test_a_crafted_link_decision_cannot_bypass_the_duplicate_refusal(client, app, branch):
    with app.app_context():
        from app.services.customers import create_customer

        existing = create_customer(
            {"customer_type": "individual", "name": "Existing Person",
             "phone": "0821119999", "email": "existing@example.co.za"}
        )
    body = client.post(
        register_url(app, branch),
        data=submission(name="Existing Person", phone="082 111 9999", email="existing@example.co.za",
                        decision=f"link:{existing}"),
    ).get_data(as_text=True)
    assert portal_intake.DUPLICATE_MESSAGE in body
    assert customers(app) == 1


def test_a_crafted_create_decision_cannot_bypass_the_duplicate_refusal(client, app, branch):
    with app.app_context():
        from app.services.customers import create_customer

        create_customer({"customer_type": "individual", "name": "Existing Person", "phone": "0832228888"})
    body = client.post(
        register_url(app, branch),
        data=submission(name="Existing Person", phone="083 222 8888", email="other@example.test",
                        decision="create"),
    ).get_data(as_text=True)
    assert portal_intake.DUPLICATE_MESSAGE in body
    assert customers(app) == 1


def test_an_exact_email_match_is_refused_even_with_a_different_phone(client, app, branch):
    with app.app_context():
        from app.services.customers import create_customer

        create_customer({"customer_type": "individual", "name": "Email Person",
                         "phone": "0810000001", "email": "shared@example.test"})
    body = client.post(
        register_url(app, branch),
        data=submission(name="Email Person", phone="081 999 7777", email="Shared@Example.Test"),
    ).get_data(as_text=True)
    assert portal_intake.DUPLICATE_MESSAGE in body, "the email key is normalised before matching"
    assert customers(app) == 1


def test_a_name_only_match_is_not_a_duplicate(client, app, branch):
    """Two genuine people can share a name: only an exact phone/email match refuses."""
    with app.app_context():
        from app.services.customers import create_customer

        create_customer({"customer_type": "individual", "name": "Charmaine Mokoena", "phone": "0820000000"})
    response = client.post(
        register_url(app, branch),
        data=submission(name="Charmaine Mokoena", phone="082 111 0003", email="different@example.test"),
    )
    assert response.status_code == 200
    assert portal_intake.DUPLICATE_MESSAGE not in response.get_data(as_text=True)
    assert customers(app) == 2, "a shared name alone must never block a genuine new sign-up"


def test_a_removed_sign_up_can_register_again(client, app, branch):
    """A tombstoned (deleted) registration is ignored by the lookup, so the person may re-register."""
    with app.app_context():
        from app.services.customers import create_customer

        cid = create_customer({"customer_type": "individual", "name": "Removed Applicant",
                               "phone": "0821234567", "email": "removed@example.co.za"})
        db = get_db()
        db.execute("UPDATE customers SET source_system = 'portal' WHERE id = ?", (cid,))
        db.execute(
            "INSERT INTO portal_registrations "
            "(reference, customer_id, branch_id, name, email, phone, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'deleted', '2026-10-11T09:00:00')",
            ("MID-DEL001", cid, branch, "Removed Applicant", "removed@example.co.za", "0821234567"),
        )
        db.commit()
    response = client.post(
        register_url(app, branch),
        data=submission(name="Removed Applicant", phone="082 123 4567", email="removed@example.co.za"),
    )
    assert response.status_code == 200
    assert portal_intake.DUPLICATE_MESSAGE not in response.get_data(as_text=True)
    assert customers(app) == 2, "a rejected/removed sign-up must be able to register anew"


def test_an_identical_contact_in_flight_blocks_a_different_key(client, app, branch):
    """A shared contact claim: two different submission keys with the same phone/email cannot both
    be in flight, which is the only guard SQLite's serialised writes do not already give."""
    slug = slug_of(app, branch)
    with app.app_context():
        portal_intake.claim_submission(
            slug, portal_intake.contact_digest({"phone": "082 555 0000", "email": "race@example.test"})
        )
        get_db().commit()  # a claim another request left behind is committed, not a live transaction
    response = client.post(
        register_url(app, branch),
        data=submission(customer_type="company", name="Race Co", phone="082 555 0000",
                        email="race@example.test", request_key=KEY(9)),
    )
    assert response.status_code == 409
    assert customers(app) == 0


# ---------------------------------------------------------------- the server half (via the routes)

def test_a_double_submit_of_one_company_creates_one_customer(client, app, branch):
    """The reported bug: a company form submitted twice must not create two companies."""
    url = register_url(app, branch)
    key = KEY()
    data = submission(
        customer_type="company",
        name="Acme Trailers CC",
        email="orders@acme.example.test",
        phone="011 555 0000",
        company_reg_no="2024/123456/07",
        decision="create",
        request_key=key,
    )
    first = client.post(url, data=data)
    second = client.post(url, data=data)

    assert first.status_code == 200 and second.status_code == 200
    assert customers(app) == 1, "one submission must write exactly one customer"

    reference = with_reference(app, "orders@acme.example.test")
    assert reference in first.get_data(as_text=True)
    assert reference in second.get_data(as_text=True), "a replay answers with the same success"
    # The acceptance is recorded once, by the request that actually wrote the customer.
    assert count(app, "SELECT COUNT(*) FROM consent_records") == 1


def with_reference(app, email):
    with app.app_context():
        row = get_db().execute(
            "SELECT source_id FROM customers WHERE email = ?", (email,)
        ).fetchone()
    return row["source_id"].split(":", 1)[1]


def test_a_concurrent_submit_is_refused_while_the_first_is_in_flight(client, app, branch):
    """Double-click while the first request is loading: the second is refused, nothing is written."""
    slug = slug_of(app, branch)
    key = KEY(7)
    with app.app_context():
        db = get_db()
        # The state the first request leaves behind the instant it wins the claim, before the
        # customer row exists — exactly the window the bug lived in.
        db.execute(
            "INSERT INTO portal_submission_claims (scope, request_key, status, created_at) "
            "VALUES (?, ?, 'in_flight', '2026-10-11T09:00:00')",
            (slug, key),
        )
        db.commit()

    response = client.post(register_url(app, branch), data=submission(decision="create", request_key=key))

    assert response.status_code == 409
    body = response.get_data(as_text=True)
    assert "already saving this registration" in body
    assert customers(app) == 0, "a refused concurrent submit must write nothing"
    assert count(app, "SELECT COUNT(*) FROM consent_records") == 0


def test_an_in_flight_key_does_not_block_a_different_submission(client, app, branch):
    slug = slug_of(app, branch)
    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO portal_submission_claims (scope, request_key, status, created_at) "
            "VALUES (?, ?, 'in_flight', '2026-10-11T09:00:00')",
            (slug, KEY(1)),
        )
        db.commit()

    response = client.post(register_url(app, branch), data=submission(request_key=KEY(2)))
    assert response.status_code == 200
    assert customers(app) == 1


def test_the_key_is_optional_so_an_old_fixture_still_registers(client, app, branch):
    """No key posted (a hand-written client): the write still happens, exactly as before."""
    response = client.post(register_url(app, branch), data=submission())
    assert response.status_code == 200
    assert customers(app) == 1


def test_a_keyless_double_submit_is_deduplicated_by_the_submission_digest(client, app, branch):
    """The compatibility path: the same submission with no key replays instead of writing twice."""
    url = register_url(app, branch)
    data = submission(decision="create")  # the route no longer shows an "Is this you?" screen
    first = client.post(url, data=data)
    second = client.post(url, data=data)

    assert first.status_code == 200 and second.status_code == 200
    assert customers(app) == 1
    reference = with_reference(app, "pieter@example.co.za")
    assert reference in first.get_data(as_text=True)
    assert reference in second.get_data(as_text=True)


def test_legitimate_new_customers_who_share_a_name_are_all_created(client, app, branch):
    """Same name is not the same person: the guard must never treat these as one submission."""
    url = register_url(app, branch)
    client.post(url, data=submission(name="Charmaine Mokoena", phone="082 111 0001", email="c1@example.test", decision="create"))
    client.post(url, data=submission(name="Charmaine Mokoena", phone="082 111 0002", email="c2@example.test", decision="create"))

    assert count(
        app, "SELECT COUNT(*) FROM customers WHERE name = 'Charmaine Mokoena'"
    ) == 2


def test_two_different_submissions_with_their_own_keys_both_land(client, app, branch):
    url = register_url(app, branch)
    client.post(url, data=submission(request_key=KEY(1)))
    client.post(
        url,
        data=submission(name="Ann Other", phone="084 111 2222", email="ann@example.co.za", request_key=KEY(2)),
    )
    assert customers(app) == 2


# ---------------------------------------------------------------- the claim itself (unit)

def test_claiming_scopes_the_key_to_the_portal(app):
    with app.app_context():
        assert portal_intake.claim_submission("branch-a", "K")[0] == portal_intake.SUBMISSION_CLAIMED
        assert portal_intake.claim_submission("branch-a", "K")[0] == portal_intake.SUBMISSION_IN_FLIGHT
        # The same key at another portal is a different claim.
        assert portal_intake.claim_submission("branch-b", "K")[0] == portal_intake.SUBMISSION_CLAIMED


def test_a_completed_claim_replays_its_stored_success(app):
    with app.app_context():
        portal_intake.claim_submission("branch-a", "K")
        portal_intake.complete_submission(
            "branch-a", "K",
            {"customer_id": 7, "reference": "MID-ABC123", "linked": False, "created": True, "registration_id": 3},
        )
        state, row = portal_intake.claim_submission("branch-a", "K")
        assert state == portal_intake.SUBMISSION_REPLAY
        replayed = portal_intake.submission_result_from_claim(row)
        assert replayed["customer_id"] == 7
        assert replayed["reference"] == "MID-ABC123"
        assert replayed["created"] is True and replayed["replayed"] is True


def test_a_released_claim_allows_a_retry_but_a_done_claim_does_not(app):
    with app.app_context():
        # A failed write releases the claim, so the customer can correct and resubmit.
        portal_intake.claim_submission("branch-a", "K")
        portal_intake.release_submission("branch-a", "K")
        assert portal_intake.claim_submission("branch-a", "K")[0] == portal_intake.SUBMISSION_CLAIMED
        # A completed claim is never released: a late replay still gets the original success.
        portal_intake.complete_submission("branch-a", "K", {"customer_id": 1, "reference": "MID-AAA111"})
        portal_intake.release_submission("branch-a", "K")
        assert portal_intake.claim_submission("branch-a", "K")[0] == portal_intake.SUBMISSION_REPLAY


def test_the_derived_key_ignores_case_and_replays_the_same_submission(app, branch):
    """submission_values canonicalises the email, so a re-typed form digests the same."""
    from app.services.portal_intake import submission_values

    with app.app_context():
        first = portal_intake.create_or_link_customer(
            submission_values(submission()), branch, "create", slug="midrand"
        )
        second = portal_intake.create_or_link_customer(
            submission_values(submission(email="PIETER@example.co.za")), branch, "create", slug="midrand"
        )
    assert first["created"] and not first.get("replayed")
    assert second.get("replayed") is True
    assert second["reference"] == first["reference"]
    assert customers(app) == 1


def test_the_claim_table_is_created_by_migrations_on_an_existing_database(tmp_path):
    """An existing database gains the table additively (no column is added to customers)."""
    path = str(tmp_path / "existing.sqlite")
    app = build_app(path)
    with app.app_context():
        columns = {
            row["name"]
            for row in get_db().execute("PRAGMA table_info(portal_submission_claims)").fetchall()
        }
    assert {"scope", "request_key", "status", "customer_id", "reference"} <= columns
