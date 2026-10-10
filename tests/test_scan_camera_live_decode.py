"""The licence-disk scanner reads the barcode LIVE in the browser — no photo, no upload.

This pins the parts of the live-decoder work the server-side tests cannot see:

* the PDF417 decoder is a **locally vendored, version-pinned, licence-recorded**
  build of ZXing (``static/vendor/zxing``) — there is no runtime CDN dependency,
  and the exact bytes the browser loads are asserted by hash;
* the vendored engine **actually decodes a real PDF417** — a synthetic barcode is
  generated with ``zxingcpp`` and decoded by the shipped bundle in Node;
* the scanner module's lifecycle regressions (accept exactly once, never
  re-submit on a stale/late frame, stop the camera on cancel/pagehide, fall back
  to MANUAL entry, never a photo) hold, exercised by a Node harness with a fake DOM;
* both scan pages ship the SAME overlay/mechanism and contain **no** photo path
  (no capture button, no file input, no image upload), posting the decoded text in
  a hidden ``disc_text`` field through the page's existing form.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app import create_app
from app.db import get_db

ROOT = Path(__file__).resolve().parents[1]
PARTIAL = ROOT / "templates/admin/_scan_camera.html"
TEMPLATES = (ROOT / "templates/admin/scan_vehicle.html", ROOT / "templates/admin/scan_return.html")
MODULE = ROOT / "static/js/scan-camera.js"
VENDOR_DIR = ROOT / "static/vendor/zxing"
VENDOR_JS = VENDOR_DIR / "zxing-library-0.21.3.min.js"
VENDOR_LICENSE = VENDOR_DIR / "LICENSE-zxing-library-0.21.3.txt"
JS_DIR = ROOT / "tests/js"

#: The pinned bundle, byte for byte.
VENDOR_SHA256 = "d7cc8f69dd70bdcf3ac00c9ae572bf2acb9f4132ba379c72df842e4db918652d"

#: A real modern positional NaTIS disc payload (the shape ``parse_natis_positional`` accepts).
DISC_PAYLOAD = (
    "MVL1CC53%0148%4522A001%1%5120367QP4HD%NB72XMGP%QWR419V%"
    "Station wagon / Stasiewa%MITSUBISHI%ASX%White / Wit%"
    "JHTFR22G10L654321%K9K7654321%2027-07-31%"
)

NODE = shutil.which("node")


def _owner_client(app):
    client = app.test_client()
    with app.app_context():
        owner = get_db().execute("SELECT id FROM users WHERE role='owner'").fetchone()
        assert owner is not None
    with client.session_transaction() as s:
        s["user_id"] = owner["id"]
        s["user_role"] = "owner"
    return client


def _run_node(script: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [NODE, str(script), *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


# ── the vendored decoder asset ───────────────────────────────────────────────


def test_the_pdf417_decoder_is_vendored_locally_pinned_and_licenced():
    assert VENDOR_JS.is_file(), "the decoder must be bundled into the repo, not fetched at runtime"
    digest = hashlib.sha256(VENDOR_JS.read_bytes()).hexdigest()
    assert digest == VENDOR_SHA256, "the vendored bundle changed — re-pin the version/hash deliberately"

    # Version is pinned in the file name and recorded next to a verbatim licence.
    assert "0.21.3" in VENDOR_JS.name
    assert VENDOR_LICENSE.is_file()
    license_text = VENDOR_LICENSE.read_text(encoding="utf-8")
    assert "Apache License" in license_text and "Version 2.0" in license_text

    readme = (VENDOR_DIR / "README.md").read_text(encoding="utf-8")
    for needle in ("0.21.3", "Apache", "npm install", "sha256"):
        assert needle in readme


def test_the_partial_loads_the_local_bundle_not_a_cdn():
    partial = PARTIAL.read_text(encoding="utf-8")
    assert "vendor/zxing/zxing-library-0.21.3.min.js" in partial
    # Every <script src> is a local static asset; nothing is fetched from the network.
    for src in re.findall(r'<script[^>]*\bsrc="([^"]+)"', partial):
        assert "://" not in src and not src.startswith("//")
        assert "url_for('static'" in src


# ── the vendored engine really decodes a PDF417 ──────────────────────────────


@pytest.mark.skipif(NODE is None, reason="Node is not available")
def test_the_vendored_engine_decodes_a_synthetic_pdf417(tmp_path):
    """Generate a real PDF417 with zxingcpp, decode it with the shipped bundle in Node."""
    zxingcpp = pytest.importorskip("zxingcpp")
    from PIL import Image

    barcode = zxingcpp.create_barcode(DISC_PAYLOAD, zxingcpp.BarcodeFormat.PDF417)
    image = zxingcpp.write_barcode_to_image(barcode)
    height, width = image.shape
    pil = Image.frombuffer("L", (width, height), image, "raw", "L", 0, 1)
    # 3x nearest-neighbour + a white quiet zone, like a clean phone capture of a disc.
    upscaled = pil.resize((width * 3, height * 3), Image.NEAREST)
    canvas = Image.new("L", (upscaled.width + 40, upscaled.height + 40), 255)
    canvas.paste(upscaled, (20, 20))
    pgm = tmp_path / "disc.pgm"
    canvas.save(pgm)

    result = _run_node(JS_DIR / "pdf417_pgm_decode.mjs", str(pgm))
    assert result.returncode == 0, f"vendored decode failed: {result.stdout!r} {result.stderr!r}"
    outcome = json.loads(result.stdout.strip().splitlines()[-1])
    assert outcome["ok"] is True, outcome
    assert outcome["text"] == DISC_PAYLOAD


# ── the scanner module's lifecycle ───────────────────────────────────────────


@pytest.mark.skipif(NODE is None, reason="Node is not available")
def test_the_scanner_lifecycle_regressions_hold():
    """Exactly-once, stale-frame, cancel, pagehide, manual fallback — via a fake DOM harness."""
    result = subprocess.run(
        [NODE, str(JS_DIR / "scan_camera_lifecycle.mjs")],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "checks passed" in result.stdout
    assert "FAIL" not in result.stdout


def test_the_module_decodes_locally_and_never_uploads_an_image():
    module = MODULE.read_text(encoding="utf-8")
    # It drives the vendored ZXing engine one frame at a time on a fixed cadence.
    assert "window.ZXing" in module
    assert "PDF417Reader" in module
    assert "HTMLCanvasElementLuminanceSource" in module
    assert "SCAN_INTERVAL_MS" in module
    assert "setTimeout(tick" in module or "set(tick" in module
    # No photo/upload path at all.
    assert "toBlob" not in module
    assert "DataTransfer" not in module
    assert "new File(" not in module
    assert "fileInput" not in module
    assert "captureFrame" not in module
    # The decoded text goes into the hidden field and the page's form is posted.
    assert "discText.value = text" in module
    assert "form.submit()" in module
    # The exactly-once guard is present and is checked before acting on a frame.
    assert "if (settled) { return; }" in module


def test_the_scanner_opens_and_then_decodes_a_bounded_frame():
    module = MODULE.read_text(encoding="utf-8")
    # Bounded frame: capped decode dimensions, never a full 12 MP frame.
    assert "MAX_DECODE_WIDTH" in module and "MAX_DECODE_HEIGHT" in module
    # The existing rear-camera constraint ladder is preserved (ideal 1080p, then rear, then any).
    assert "width: {ideal: 1920}" in module
    assert "{video: {facingMode: 'environment'}, audio: false}" in module
    assert "{video: true, audio: false}" in module


# ── both pages ship the same live scanner and no photo path ──────────────────


def test_both_scan_pages_ship_the_same_live_scanner(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "scan-live.db")})
    client = _owner_client(app)
    for url in ("/scan-vehicle", "/scan-return"):
        html = client.get(url).get_data(as_text=True)
        # The shared overlay and its controls.
        assert 'id="scan-camera-live"' in html, url
        assert 'id="scan-camera-video"' in html, url
        assert 'class="scan-barcode-guide"' in html, url
        assert 'id="scan-camera-cancel"' in html, url
        assert 'id="scan-camera-playstart"' in html, url
        assert 'id="scan-camera-status" aria-live="polite"' in html, url
        assert 'id="scan-camera-blocked"' in html, url
        # The manual fallback button, the hidden post field and the local decoder.
        assert 'id="scan-camera-manual"' in html, url
        assert 'name="disc_text"' in html, url
        assert "js/scan-camera.js" in html, url
        assert "vendor/zxing/zxing-library-0.21.3.min.js" in html, url
        # The photo path is gone: no capture button, no fallback button, no file input,
        # no multipart form.
        assert 'id="scan-camera-capture"' not in html, url
        assert 'id="scan-camera-fallback"' not in html, url
        assert 'name="disk_image"' not in html, url
        assert 'capture="environment"' not in html, url
        assert 'type="file"' not in html, url
        assert 'multipart/form-data' not in html, url
        # A Scan action and a manual action are both retained.
        assert 'id="scan-camera-start"' in html, url
        assert 'id="scan-manual"' in html, url


def test_the_shared_mechanism_differs_only_by_destination():
    """Both templates include the same partial and the same initialiser; the title differs."""
    vehicle = (ROOT / "templates/admin/scan_vehicle.html").read_text(encoding="utf-8")
    ret = (ROOT / "templates/admin/scan_return.html").read_text(encoding="utf-8")
    for src in (vehicle, ret):
        assert 'include "admin/_scan_camera.html"' in src
        assert "AbiScanCamera.init({" in src
        # No photo options are passed to the module any more.
        assert "fileInput:" not in src
        assert "filename:" not in src
    # Only the destination (form action / message) differs.
    assert "action=\"{{ url_for('returns.scan_return') }}\"" in ret
    assert "action=\"{{ url_for('returns.scan_return') }}\"" not in vehicle
