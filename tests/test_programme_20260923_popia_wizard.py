"""Programme phase W1 (feature P) — the POPIA setup wizard's engine, service half.

This covers the engine only (no routes, no UI): the saved state, the pre-fill
from what the app knows, the answer-driven light notice generator with its eight
s18(1) guarantees, the publish gate and version/hash, and the placeholder guard.

What these tests pin, one per line of the W1 acceptance:

* ``prefill`` fills every fact the app knows (from ``company_settings`` /
  ``branches``) and leaves a fact it does not know **blank, never invented**
  (the trading name has no column in the app, so it is blank);
* ``save_step`` persists each step so a closed tab loses nothing, and ``state``
  reads it back;
* each Step-3 answer toggles its paragraph — CCTV yes shows the CCTV paragraph,
  no drops it — and ``left_out`` lists exactly the skipped paragraphs with the
  answer that skipped them;
* all eight s18(1) items are present **across a grid of answer combinations**
  (property-style over the nine toggles) — the one non-negotiable;
* publication is refused while the Information Officer is unregistered and while
  a required question is unanswered, and the refusal **names the field**;
* re-publishing bumps the version while old consent rows keep their old version,
  and the stored content hash is the ``sha256`` of the stored text;
* the renderer refuses any text containing a ``[...]`` token, so a placeholder
  can never be published;
* the two new tables carry no IP address / user agent and are created by both
  ``SCHEMA`` and ``run_migrations``.

No test needs a network; nothing here reads or writes the real disc sample.
"""

import itertools
import os
import re
import tempfile
from pathlib import Path

import pytest

from app import create_app
from app.db import get_db, run_migrations
from app.services import consent, popia_wizard
from app.services.customers import create_customer

#: Every s18(1) element the generated notice must carry, as the strings the
#: rendered text must contain. One tuple per element so a failure names the gap.
#: These all live in the *fixed* part of the template, so they must be present
#: whatever the nine Step-3 answers are.
S18_ELEMENTS = [
    ("(a) what is collected", "What personal information we collect"),
    ("(a) the source (given by the data subject)", "given to us by you"),
    ("(b) the responsible party's name", "responsible party for your personal information"),
    ("(b) the responsible party's address", "Address:"),
    ("(c) the purposes", "Why we collect it"),
    ("(d) supply is voluntary", "voluntary"),
    ("(d) consequences of not supplying", "we may not be able to hire a trailer"),
    ("(e) the law requiring collection", "Tax Administration Act"),
    ("(f) cross-border transfer", "outside South Africa"),
    ("(f) the protection level", "equivalent to the protection"),
    ("(g) the recipients", "categories of recipients"),
    ("(h) the right of access", "ask to see (access)"),
    ("(h) the right to correct or delete", "correct or delete"),
    ("(h) the right to object", "object to our use"),
    ("(h) complain to the Regulator", "complain to the Information Regulator"),
    ("(h) the Regulator's contact details", "complaints.IR@justice.gov.za"),
]

#: The marker that proves each Step-3 paragraph is switched on, one per question.
PARAGRAPH_MARKERS = {
    "cctv": "closed-circuit television (CCTV)",
    "marketing": "## Marketing",
    "id_documents": "identity document or driver's licence",
    "share_info": "debt collectors, tracing agents, insurers, assessors and attorneys",
    "service_providers": "our accountant, IT support and other providers",
    "card_payments": "card and electronic payments",
    "under_18": "children (under 18)",
    "vehicle_registration": "We keep the registration number of the vehicle you use to tow the trailer",
    "credit_checks": "credit check or reference check",
}


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": path,
            "SECRET_KEY": "test",
            "ADMIN_EMAIL": "admin@abi.local",
            "ADMIN_PASSWORD": "admin123",
        }
    )
    yield app
    os.unlink(path)


def make_state(**overrides):
    """A complete, publishable state dict (all nine answers 'yes'), overridable.

    Used for the renderer/grid tests, which run on an explicit dict rather than
    the database so every combination is cheap.
    """
    state = {
        "business_name": "Sano Trailers (Pty) Ltd",
        "registration_number": "2018/521057/07",
        "vat_number": "4880322591",
        "trading_name": "",
        "address": "229 Summit Road, Midrand, Gauteng, South Africa",
        "telephone": "010 221 1723",
        "contact_email": "info@sanotrailers.co.za",
        "officer_name": "Donovan Jackson",
        "officer_position": "Owner",
        "officer_email": "donovan@sanotrailers.co.za",
        "officer_telephone": "010 221 1723",
        "officer_registered": "yes",
        "officer_registration_date": "2026-09-23",
        "officer_registration_ref": "",
        "cctv_branches_json": "[]",
        "cctv_signage": "",
        "published": False,
    }
    for question in popia_wizard.QUESTION_KEYS:
        state[question] = "yes"
    state.update(overrides)
    return state


def save_full_state(app, **overrides):
    """Persist a complete state through save_step, with per-step overrides."""
    step1 = {
        "business_name": "Sano Trailers (Pty) Ltd",
        "registration_number": "2018/521057/07",
        "vat_number": "4880322591",
        "trading_name": "",
        "address": "229 Summit Road, Midrand, Gauteng, South Africa",
        "telephone": "010 221 1723",
        "contact_email": "info@sanotrailers.co.za",
    }
    step2 = {
        "officer_name": "Donovan Jackson",
        "officer_position": "Owner",
        "officer_email": "donovan@sanotrailers.co.za",
        "officer_telephone": "010 221 1723",
        "officer_registered": "yes",
        "officer_registration_date": "2026-09-23",
        "officer_registration_ref": "",
    }
    step3 = {question: "yes" for question in popia_wizard.QUESTION_KEYS}
    for overrides_source in (step1, step2, step3):
        overrides_source.update({k: v for k, v in overrides.items() if k in overrides_source})
    with app.app_context():
        popia_wizard.save_step("1", step1)
        popia_wizard.save_step("2", step2)
        popia_wizard.save_step("3", step3)


def _versions(app):
    with app.app_context():
        return [
            row["notice_version"]
            for row in get_db().execute(
                "SELECT notice_version FROM popia_notice_versions ORDER BY id"
            ).fetchall()
        ]


# ---------------------------------------------------------------------------
# pre-fill
# ---------------------------------------------------------------------------


def test_prefill_fills_what_the_app_knows_and_leaves_unknown_fields_blank(app):
    with app.app_context():
        get_db().execute(
            "UPDATE company_settings SET company_name=?, company_reg_no=?, vat_number=?, phone=?, email=?, "
            "address_line1=?, address_line2=?, city=?, province=?, postcode=?, country=? WHERE id=1",
            (
                "Sano Trailers",
                "2018/521057/07",
                "4880322591",
                "010 221 1723",
                "info@sanotrailers.co.za",
                "229 Summit Road",
                "",
                "Midrand",
                "Gauteng",
                "",
                "South Africa",
            ),
        )
        get_db().commit()
        pre = popia_wizard.prefill()
    assert pre["business_name"] == "Sano Trailers"
    assert pre["registration_number"] == "2018/521057/07"
    assert pre["vat_number"] == "4880322591"
    assert pre["telephone"] == "010 221 1723"
    assert pre["contact_email"] == "info@sanotrailers.co.za"
    assert "229 Summit Road" in pre["address"] and "Midrand" in pre["address"]
    # The app has no trading-name field — it arrives blank, never invented.
    assert pre["trading_name"] == ""
    assert isinstance(pre["branches"], list) and all("name" in b for b in pre["branches"])


def test_prefill_never_invents_a_fact_the_app_does_not_know(app):
    with app.app_context():
        get_db().execute("UPDATE company_settings SET company_reg_no='', vat_number='' WHERE id=1")
        get_db().commit()
        pre = popia_wizard.prefill()
    assert pre["registration_number"] == ""
    assert pre["vat_number"] == ""
    assert pre["trading_name"] == ""


# ---------------------------------------------------------------------------
# save_step / state / progress
# ---------------------------------------------------------------------------


def test_save_step_persists_each_step_and_state_reads_it_back(app):
    with app.app_context():
        assert popia_wizard.state()["business_name"] == ""
        popia_wizard.save_step(
            "1",
            {
                "business_name": "Sano Trailers",
                "registration_number": "2018/521057/07",
                "vat_number": "4880322591",
                "trading_name": "",
                "address": "229 Summit Road, Midrand",
                "telephone": "010 221 1723",
                "contact_email": "info@sanotrailers.co.za",
            },
        )
        popia_wizard.save_step(
            "2",
            {
                "officer_name": "Donovan Jackson",
                "officer_position": "Owner",
                "officer_email": "d@sanotrailers.co.za",
                "officer_telephone": "010 221 1723",
                "officer_registered": "not_yet",
            },
        )
        st = popia_wizard.state()
    assert st["business_name"] == "Sano Trailers"
    assert st["officer_name"] == "Donovan Jackson"
    assert st["officer_registered"] == "not_yet"
    assert st["vehicle_registration"] == "yes"  # the disc scanner already records this


def test_progress_counts_the_steps_done(app):
    with app.app_context():
        assert popia_wizard.progress()["completed"] == 0
    save_full_state(app)
    with app.app_context():
        before_publish = popia_wizard.progress()
    assert before_publish == {"completed": 4, "total": 5}  # steps 1-3 + the reachable check step
    with app.app_context():
        popia_wizard.publish()
        assert popia_wizard.progress()["completed"] == 5


# ---------------------------------------------------------------------------
# the answer-driven notice: toggles, left-out, and the eight s18(1) items
# ---------------------------------------------------------------------------


def test_each_step3_answer_toggles_its_paragraph(app):
    with app.app_context():
        for question in popia_wizard.QUESTION_KEYS:
            marker = PARAGRAPH_MARKERS[question]
            all_yes = popia_wizard.build_notice(make_state())
            assert marker in all_yes, f"{question}: yes did not show its paragraph"
            flipped = popia_wizard.build_notice(make_state(**{question: "no"}))
            assert marker not in flipped, f"{question}: no still shows its paragraph"
            # every *other* paragraph survives the flip (the toggle is independent).
            for other, other_marker in PARAGRAPH_MARKERS.items():
                if other != question:
                    assert other_marker in flipped


def test_cctv_paragraph_is_switched_by_the_cctv_answer(app):
    with app.app_context():
        assert "closed-circuit television (CCTV)" in popia_wizard.build_notice(make_state(cctv="yes"))
        assert "closed-circuit television (CCTV)" not in popia_wizard.build_notice(make_state(cctv="no"))


def test_left_out_lists_each_skipped_paragraph_and_the_answer_that_skipped_it(app):
    with app.app_context():
        skipped = popia_wizard.left_out(make_state(cctv="no", marketing="no"))
    by_key = {entry["key"]: entry for entry in skipped}
    assert by_key["cctv"]["answer"] == "no"
    assert by_key["cctv"]["paragraph"] == "CCTV at branches"
    assert by_key["marketing"]["answer"] == "no"
    # everything answered yes is not listed
    assert "share_info" not in by_key
    assert "vehicle_registration" not in by_key


def test_left_out_matches_the_rendered_skips(app):
    with app.app_context():
        st = make_state(cctv="no", id_documents="", share_info="no")
        text = popia_wizard.build_notice(st)
        skipped = {entry["key"] for entry in popia_wizard.left_out(st)}
        for key in ("cctv", "id_documents", "share_info"):
            assert key in skipped
            assert PARAGRAPH_MARKERS[key] not in text
        for key in popia_wizard.QUESTION_KEYS:
            if key not in skipped:
                assert PARAGRAPH_MARKERS[key] in text


def test_all_eight_s18_items_present_across_a_grid_of_answer_combinations(app):
    """The one non-negotiable: whatever the answers, all eight s18(1) items are
    present — proven property-style over every combination of the nine toggles."""
    with app.app_context():
        for combo in itertools.product(["yes", "no"], repeat=len(popia_wizard.QUESTION_KEYS)):
            overrides = dict(zip(popia_wizard.QUESTION_KEYS, combo))
            text = popia_wizard.build_notice(make_state(**overrides))
            missing = [label for label, needle in S18_ELEMENTS if needle not in text]
            assert missing == [], f"answers {overrides} left out: {missing}"


# ---------------------------------------------------------------------------
# the publish gate, version and hash
# ---------------------------------------------------------------------------


def test_publish_is_refused_while_the_officer_is_unregistered_and_names_the_field(app):
    save_full_state(app, officer_registered="not_yet", officer_registration_date="")
    with app.app_context():
        with pytest.raises(ValueError) as excinfo:
            popia_wizard.publish()
        assert "officer_registered" in str(excinfo.value)
        assert get_db().execute("SELECT COUNT(*) AS c FROM popia_notice_versions").fetchone()["c"] == 0


def test_publish_is_refused_while_a_required_question_is_unanswered_and_names_the_field(app):
    save_full_state(app, share_info="")
    with app.app_context():
        with pytest.raises(ValueError) as excinfo:
            popia_wizard.publish()
        assert "share_info" in str(excinfo.value)
        assert get_db().execute("SELECT COUNT(*) AS c FROM popia_notice_versions").fetchone()["c"] == 0


def test_publish_errors_lists_every_missing_field(app):
    with app.app_context():
        assert popia_wizard.publish_errors()  # nothing saved -> blocks, non-empty
        errors = popia_wizard.publish_errors(make_state())
    assert errors == []


def test_publish_writes_the_version_and_the_hash_of_the_rendered_text(app):
    save_full_state(app)
    with app.app_context():
        result = popia_wizard.publish()
        import hashlib

        assert re.match(r"^\d{4}-\d{2}-\d{2}\.\d+$", result["version"])
        assert result["content_hash"] == hashlib.sha256(result["text"].encode("utf-8")).hexdigest()
        published = popia_wizard.published_notice()
        assert published["notice_version"] == result["version"]
        assert published["notice_text"] == result["text"]
        assert published["notice_content_hash"] == result["content_hash"]
        # the notice is stored in the DB — no placeholder, and the facts are in it
        assert "[" not in published["notice_text"]
        assert "Sano Trailers (Pty) Ltd" in published["notice_text"]


def test_version_bumps_on_republish_and_old_consent_rows_keep_their_old_version(app):
    save_full_state(app)
    with app.app_context():
        first = popia_wizard.publish()
        v1 = first["version"]
        customer_id = create_customer(
            {"customer_type": "individual", "name": "Charmaine Mokoena", "phone": "0821234567", "email": ""}
        )
        consent.record_consent(customer_id, "Midrand portal", True, notice_version=v1)

        second = popia_wizard.publish()
        v2 = second["version"]
        assert v2 != v1
        assert v2.startswith(v1.rsplit(".", 1)[0] + ".")
        assert int(v2.rsplit(".", 1)[1]) == int(v1.rsplit(".", 1)[1]) + 1
        # the customer who consented to v1 is still traced to v1, not v2
        assert consent.consent_for(customer_id)["notice_version"] == v1
        assert [v1, v2] == _versions(app)


# ---------------------------------------------------------------------------
# the placeholder guard (impossible by construction)
# ---------------------------------------------------------------------------


def test_the_renderer_refuses_any_placeholder_token(app):
    with app.app_context():
        with pytest.raises(ValueError):
            popia_wizard._assert_placeholder_free("Fine text. [TO CONFIRM — something].")
        with pytest.raises(ValueError):
            popia_wizard._assert_placeholder_free("A blank ____ here.")
        assert popia_wizard._assert_placeholder_free("Clean notice text.") == "Clean notice text."


def test_nothing_containing_a_placeholder_can_be_published(app, monkeypatch):
    save_full_state(app)  # every gate is otherwise open
    monkeypatch.setattr(popia_wizard, "_render_notice", lambda st: "Broken [SOMETHING] notice.")
    with app.app_context():
        with pytest.raises(ValueError):
            popia_wizard.publish()
        assert get_db().execute("SELECT COUNT(*) AS c FROM popia_notice_versions").fetchone()["c"] == 0


# ---------------------------------------------------------------------------
# schema: data minimisation and the additive migration
# ---------------------------------------------------------------------------


def test_the_wizard_tables_store_no_ip_address_and_no_user_agent(app):
    with app.app_context():
        for table in ("popia_wizard_state", "popia_notice_versions"):
            columns = {row["name"] for row in get_db().execute(f"PRAGMA table_info({table})").fetchall()}
            for banned in ("ip", "ip_address", "user_agent", "fingerprint", "device"):
                assert banned not in columns


def test_the_wizard_tables_are_declared_in_the_schema_constant():
    from app.db import SCHEMA

    assert "CREATE TABLE IF NOT EXISTS popia_wizard_state" in SCHEMA
    assert "CREATE TABLE IF NOT EXISTS popia_notice_versions" in SCHEMA
    assert "notice_content_hash" in SCHEMA
    assert "vehicle_registration" in SCHEMA


def test_run_migrations_creates_the_wizard_tables_on_an_existing_database(app):
    with app.app_context():
        db = get_db()
        db.execute("DROP TABLE popia_notice_versions")
        db.execute("DROP TABLE popia_wizard_state")
        db.commit()
        run_migrations(db)
        db.commit()
        state_cols = {row["name"] for row in db.execute("PRAGMA table_info(popia_wizard_state)").fetchall()}
        assert "vehicle_registration" in state_cols and "officer_registered" in state_cols
        version_cols = {row["name"] for row in db.execute("PRAGMA table_info(popia_notice_versions)").fetchall()}
        assert "notice_content_hash" in version_cols and "notice_text" in version_cols
        indexes = {row["name"] for row in db.execute("PRAGMA index_list(popia_notice_versions)").fetchall()}
        assert "idx_popia_notice_versions_version" in indexes
