"""Programme phase 8 (feature B / §B1) — branch portal schema, shareable link and QR.

The acceptance list from ``docs/plans/2026-09-23-branch-public-portal.md`` §B1, one test per line:

* the slug backfill is stable across two migration runs;
* slug collisions resolve deterministically;
* an unknown slug 404s, and a branch whose portal is switched off 404s;
* the QR endpoint returns a real PNG **that decodes back to the portal URL**;
* ``public_base_url`` wins when it is set.

Decisions from the master plan this file pins:

* **D5** — the portal URL shape is ``/portal/<branch-slug>`` and the QR encodes the *absolute* URL
  built from the ``public_base_url`` setting, falling back to the request's own host. Slugs are the
  stable half: they are stored, backfilled once, and never silently re-derived on a rename.
* **D4** — nothing is written to disk. The QR is rendered in-process from the URL and the same
  branch still serves the same QR after an app restart (Render's filesystem is ephemeral), which is
  what "survives a restart" means here.
* **D7** — the portal route lives under the ungated ``public.`` prefix, so it must not accept a
  submission before §B2 builds (and hardens) the real form. ``/portal/<slug>`` is a GET-only
  placeholder today; a POST is refused by the router rather than by a validation branch.
* **D11 / POPIA** — the placeholder page must carry no bracketed placeholder token, because a
  bracketed token on a customer-facing page is exactly what the POPIA gate exists to prevent.

Nothing in §B1 touches vehicle identity, so no licence-disc identifier appears in this file.
"""

import io
import os
import sqlite3
import tempfile

import pytest
import zxingcpp
from PIL import Image

from app import create_app
from app.db import get_db, run_migrations
from app.services import branches as branches_service
from app.services import portal


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
    return build_app(db_path)


@pytest.fixture()
def client(app):
    return app.test_client()


def make_branch(app, name, active=1, portal_enabled=None):
    """Create a branch through the real service path, so the slug comes from the real code.

    ``branches_service.create_branch`` is what the admin form calls, so this also covers the
    DB↔UI half of §B1: a branch staff create behaves the same as one the migration backfills.
    """
    with app.app_context():
        branch_id = branches_service.create_branch({"name": name, "active": active})
        if portal_enabled is not None:
            db = get_db()
            db.execute("UPDATE branches SET portal_enabled = ? WHERE id = ?", (portal_enabled, branch_id))
            db.commit()
    return branch_id


def slug_of(app, branch_id):
    with app.app_context():
        return get_db().execute("SELECT public_slug FROM branches WHERE id = ?", (branch_id,)).fetchone()["public_slug"]


def decode_qr(png_bytes):
    """Read the QR back with zxing-cpp — the same engine the disc decoder ships."""
    image = Image.open(io.BytesIO(png_bytes))
    results = zxingcpp.read_barcodes(image)
    assert results, "the QR endpoint's bytes did not decode as a barcode"
    return results[0].text


# --- slugify ------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,expected",
    [
        ("Roodepoort", "roodepoort"),
        ("Midrand Depot", "midrand-depot"),
        ("Sano Trailers — Midrand", "sano-trailers-midrand"),
        ("  Depot   #2  ", "depot-2"),
        ("Müller Park", "muller-park"),
        ("Branch 1", "branch-1"),
        ("!!!", ""),
        ("", ""),
    ],
)
def test_slugify_rules(name, expected):
    assert portal.slugify(name) == expected


# --- schema + backfill (D5) ---------------------------------------------------------------------

def test_backfill_fills_every_blank_slug_on_startup(app):
    with app.app_context():
        rows = get_db().execute("SELECT name, public_slug FROM branches ORDER BY id").fetchall()
    assert rows, "the app creates its default branches on first run"
    assert all(row["public_slug"] for row in rows), [dict(row) for row in rows]
    assert rows[0]["public_slug"] == portal.slugify(rows[0]["name"])


def test_backfill_is_idempotent_across_two_migration_runs(app):
    before = slug_of(app, 1)
    with app.app_context():
        run_migrations(get_db())
        get_db().commit()
        run_migrations(get_db())
        get_db().commit()
    assert slug_of(app, 1) == before, "a second migration run must not suffix an existing slug"


def test_collision_gets_a_deterministic_suffix(app):
    # Three DIFFERENT branch names (case and punctuation) that slugify to the same stem, which is
    # the real collision: staff type "Roodepoort", "ROODEPOORT" and "Roodepoort!" for one depot.
    # The suffix follows branch-id order, so it is the same on every machine.
    first = make_branch(app, "Roodepoort")
    second = make_branch(app, "ROODEPOORT")
    third = make_branch(app, "Roodepoort!")
    assert (slug_of(app, first), slug_of(app, second), slug_of(app, third)) == (
        "roodepoort",
        "roodepoort-2",
        "roodepoort-3",
    )


def test_ensure_slug_keeps_an_existing_slug(app):
    branch_id = make_branch(app, "Pretoria")
    with app.app_context():
        db = get_db()
        db.execute("UPDATE branches SET public_slug = 'pts-north' WHERE id = ?", (branch_id,))
        db.commit()
        assert portal.ensure_slug(branch_id) == "pts-north"
        # ...and a rename later never silently changes a printed QR code's target.
        branches_service.update_branch(branch_id, {"name": "Pretoria North", "active": 1})
        assert portal.ensure_slug(branch_id) == "pts-north"


def test_database_refuses_two_branches_with_the_same_slug(app):
    first = make_branch(app, "Kempton Park")
    with app.app_context():
        db = get_db()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO branches (name, code, active, public_slug, created_at, updated_at) "
                "VALUES ('Kemptonpark', '', 1, ?, '2026-09-23T14:00:00', '2026-09-23T14:00:00')",
                (slug_of(app, first),),
            )


def test_editing_a_branch_keeps_its_portal_settings(app):
    branch_id = make_branch(app, "Vereeniging")
    with app.app_context():
        db = get_db()
        db.execute(
            "UPDATE branches SET portal_enabled = 0, portal_intro = 'Bring your ID.' WHERE id = ?",
            (branch_id,),
        )
        db.commit()
        slug = slug_of(app, branch_id)
        branches_service.update_branch(branch_id, {"name": "Vereeniging Depot", "active": 1})
        row = get_db().execute(
            "SELECT public_slug, portal_enabled, portal_intro FROM branches WHERE id = ?", (branch_id,)
        ).fetchone()
    assert (row["public_slug"], row["portal_enabled"], row["portal_intro"]) == (slug, 0, "Bring your ID.")


# --- routes -------------------------------------------------------------------------------------

def test_unknown_slug_is_404(client):
    assert client.get("/portal/no-such-branch").status_code == 404
    assert client.get("/portal/no-such-branch/qr.png").status_code == 404


def test_disabled_portal_is_404_for_page_and_qr(app, client):
    branch_id = make_branch(app, "Bloemfontein", portal_enabled=0)
    slug = slug_of(app, branch_id)
    assert client.get(f"/portal/{slug}").status_code == 404
    assert client.get(f"/portal/{slug}/qr.png").status_code == 404


def test_portal_page_carries_the_registration_form(app, client, monkeypatch):
    """§B1 shipped a placeholder here; §B2 replaced it — the QR target is the form itself.

    The gate is opened explicitly because the *shipped* notice still carries Sano's open facts, and
    while it does the page correctly shows no form at all (proven in the §B2 test file).
    """
    from app.services import portal_intake

    monkeypatch.setattr(portal_intake, "registration_is_open", lambda: True)
    branch_id = make_branch(app, "Polokwane")
    slug = slug_of(app, branch_id)
    response = client.get(f"/portal/{slug}")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Polokwane" in body
    assert 'name="name"' in body, "the page a printed QR opens must be the form"
    assert 'name="popia_consent"' in body, "the shared consent block belongs on the capture point"
    assert "[" not in body, "a customer-facing page must not carry a bracketed placeholder token"


def test_portal_page_accepts_no_submission_yet(app, client):
    # D7: the route is ungated, so until §B2's hardened form exists the URL must not accept a POST.
    slug = slug_of(app, make_branch(app, "Sasolburg"))
    assert client.post(f"/portal/{slug}", data={"name": "Anyone"}).status_code == 405


def test_qr_png_decodes_back_to_the_portal_url(app, client):
    branch_id = make_branch(app, "Secunda")
    slug = slug_of(app, branch_id)
    response = client.get(f"/portal/{slug}/qr.png")
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "image/png"
    body = response.get_data()
    assert body.startswith(b"\x89PNG\r\n\x1a\n"), "the QR endpoint must serve a real PNG"
    assert decode_qr(body) == f"http://localhost/portal/{slug}"


def test_qr_png_is_cacheable(client):
    slug = slug_of(client.application, make_branch(client.application, "Witbank"))
    response = client.get(f"/portal/{slug}/qr.png")
    cache_control = response.headers.get("Cache-Control", "")
    assert "public" in cache_control
    assert int(cache_control.split("max-age=")[1].split(",")[0]) >= 3600


def test_public_base_url_wins_over_the_request_host(app, client):
    branch_id = make_branch(app, "Gqeberha")
    slug = slug_of(app, branch_id)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE company_settings SET public_base_url = 'https://sano-trailers.example' WHERE id = 1")
        db.commit()
    body = client.get(f"/portal/{slug}/qr.png").get_data()
    assert decode_qr(body) == f"https://sano-trailers.example/portal/{slug}"
    # ...and the page shows the same absolute link the QR carries.
    assert f"https://sano-trailers.example/portal/{slug}" in client.get(f"/portal/{slug}").get_data(as_text=True)


def test_slug_and_qr_survive_an_app_restart(db_path):
    first = build_app(db_path)
    with first.app_context():
        branch_id = make_branch(first, "Mthatha")
    slug = slug_of(first, branch_id)
    with first.test_client() as c:
        first_body = c.get(f"/portal/{slug}/qr.png").get_data()

    restarted = build_app(db_path)  # same database file, brand-new app instance
    with restarted.test_client() as c:
        assert c.get(f"/portal/{slug}").status_code == 200
        second_body = c.get(f"/portal/{slug}/qr.png").get_data()
    assert slug_of(restarted, branch_id) == slug
    assert second_body == first_body
    assert decode_qr(second_body) == f"http://localhost/portal/{slug}"


def test_all_portal_links_covers_every_branch(app):
    make_branch(app, "Nelspruit")
    with app.app_context():
        links = portal.all_portal_links()
        branch_count = get_db().execute("SELECT COUNT(*) AS c FROM branches").fetchone()["c"]
    assert len(links) == branch_count
    assert {"branch_id", "name", "slug", "portal_path", "portal_url", "enabled"} <= set(links[0])
    assert all(link["slug"] for link in links)
