"""Programme feature T4 — the printable A4 QR sheet for a branch's portal code.

Don's decision on 23 September: the QR sheet gets a **real PDF endpoint**, not just the
on-screen print page. The acceptance list:

* the endpoint returns a real A4 PDF (``%PDF`` header, A4 MediaBox, ``application/pdf``);
* the code on the sheet IS the code the endpoint encodes — the dark modules in the PDF's
  content stream are read back and compared cell-for-cell with ``portal.qr_matrix``;
* the sheet carries the branch, its address and the link in plain text, so a customer whose
  camera will not cooperate can still type the address in;
* the same gate as the page and the PNG: an unknown slug 404s, a switched-off portal 404s;
* the admin portal page offers the download next to the existing print sheet.
"""

import io
import os
import re
import tempfile

import pytest
from PIL import Image
import zxingcpp

from app import create_app
from app.db import get_db
from app.services import branches as branches_service, portal
from app.services.pdf_documents import (
    A4_PORTRAIT_WIDTH,
    QR_SHEET_PLATE_PAD,
    QR_SHEET_PLATE_TOP,
    QR_SHEET_QR_WIDTH,
)


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


def make_branch(app, name, active=1, portal_enabled=1):
    with app.app_context():
        branch_id = branches_service.create_branch({"name": name, "active": active})
        db = get_db()
        db.execute("UPDATE branches SET portal_enabled = ? WHERE id = ?", (portal_enabled, branch_id))
        db.commit()
        row = db.execute("SELECT * FROM branches WHERE id = ?", (branch_id,)).fetchone()
        slug = row["public_slug"]
    return branch_id, slug


def cells_in_pdf(pdf_bytes, matrix):
    """Read the sheet's dark squares back into (row, column) grid positions."""
    module = QR_SHEET_QR_WIDTH / len(matrix)
    plate_left = (A4_PORTRAIT_WIDTH - (QR_SHEET_QR_WIDTH + (QR_SHEET_PLATE_PAD * 2))) / 2
    origin_x = plate_left + QR_SHEET_PLATE_PAD
    origin_top = QR_SHEET_PLATE_TOP - QR_SHEET_PLATE_PAD
    rects = re.findall(rb"q 0 0 0 rg ([\d.]+) ([\d.]+) ([\d.]+) ([\d.]+) re f Q", pdf_bytes)
    assert rects, "the sheet drew no dark modules at all"
    cells = set()
    for x, y, width, height in rects:
        x, y, width, height = (float(v) for v in (x, y, width, height))
        first_column = round((x - origin_x) / module)
        row = round((origin_top - (y + height)) / module)
        for column in range(first_column, first_column + max(1, round(width / module))):
            cells.add((row, column))
    return cells


def dark_cells(matrix):
    return {(row_index, column) for row_index, row in enumerate(matrix) for column, on in enumerate(row) if on}


# --- the artefact ------------------------------------------------------------------------------

def test_the_sheet_is_a_real_a4_pdf(app, client):
    _, slug = make_branch(app, "Roodepoort")
    response = client.get(f"/portal/{slug}/qr.pdf")
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/pdf"
    assert "inline" in response.headers["Content-Disposition"]
    assert f"{slug}-trailer-portal.pdf" in response.headers["Content-Disposition"]
    assert response.data.startswith(b"%PDF")
    assert b"[0 0 595 842]" in response.data, "the sheet must be A4 portrait"
    assert response.data.rstrip().endswith(b"%%EOF")


def test_the_printed_code_is_the_code_the_endpoint_encodes(app, client):
    """The whole point of drawing vectors: what is on the paper is what the URL is."""
    _, slug = make_branch(app, "Midrand Depot")
    response = client.get(f"/portal/{slug}/qr.pdf")
    with app.app_context():
        branch = get_db().execute("SELECT * FROM branches WHERE public_slug = ?", (slug,)).fetchone()
        url = portal.portal_url(branch, "http://localhost/")
    assert cells_in_pdf(response.data, portal.qr_matrix(url)) == dark_cells(portal.qr_matrix(url))


def test_the_sheet_names_the_branch_the_address_and_the_link(app, client):
    branch_id, slug = make_branch(app, "Roodepoort West")
    with app.app_context():
        db = get_db()
        db.execute("UPDATE branches SET address_line1 = '12 Main Reef Road', city = 'Roodepoort' WHERE id = ?", (branch_id,))
        db.commit()
        branch = db.execute("SELECT * FROM branches WHERE id = ?", (branch_id,)).fetchone()
        url = portal.portal_url(branch, "http://localhost/")
    body = client.get(f"/portal/{slug}/qr.pdf").data
    assert b"Roodepoort West" in body
    assert b"12 Main Reef Road" in body
    assert url.encode() in body
    assert b"Scan to register your details before you hire." in body


def test_two_branches_do_not_share_a_code(app, client):
    _, first = make_branch(app, "Roodepoort")
    _, second = make_branch(app, "Pretoria")
    assert client.get(f"/portal/{first}/qr.pdf").data != client.get(f"/portal/{second}/qr.pdf").data


# --- the gate (the same one the page and the PNG use) -------------------------------------------

def test_an_unknown_slug_is_a_404(app, client):
    assert client.get("/portal/no-such-branch/qr.pdf").status_code == 404


def test_a_switched_off_portal_is_a_404(app, client):
    _, slug = make_branch(app, "Pretoria", portal_enabled=0)
    assert client.get(f"/portal/{slug}").status_code == 404
    assert client.get(f"/portal/{slug}/qr.pdf").status_code == 404


def test_an_inactive_branch_is_a_404(app, client):
    _, slug = make_branch(app, "Closed Depot", active=0)
    assert client.get(f"/portal/{slug}/qr.pdf").status_code == 404


# --- the way staff reach it ---------------------------------------------------------------------

def test_the_admin_portal_page_offers_the_download(app, client):
    make_branch(app, "Roodepoort")
    with app.app_context():
        owner = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
        index_path = url_for_index(app)
    client.post("/login", data={"user_id": owner["id"], "password": "admin123"}, follow_redirects=True)
    page = client.get(index_path)
    assert page.status_code == 200
    assert b"/qr.pdf" in page.data, "staff must be able to reach the sheet from the portal page"
    assert b"Download A4 PDF" in page.data


def url_for_index(app):
    from flask import url_for

    with app.test_request_context():
        return url_for("settings.portal_index")
