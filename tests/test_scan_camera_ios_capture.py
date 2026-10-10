"""The scan-camera path must open, fill the screen, and degrade safely.

The phone failures this file guards against were never a JS error or a missing
script block:

  * an ``<input type=file capture="environment">`` carried the ``hidden``
    attribute -> ``[hidden]{display:none!important}`` -> ``display:none``, and
    iOS Safari refuses to open a file picker from ``.click()`` on a display:none
    input. That photo path is now RETIRED entirely — the camera decodes the
    PDF417 in the browser and there is no photo fallback at all;
  * the ``<video>`` relied on the ``muted``/``playsinline`` *attributes* only,
    and the frame was made visible *after* ``srcObject`` was attached.

The scanner is a SHARED full-screen overlay (markup in
``templates/admin/_scan_camera.html``, behaviour in ``static/js/scan-camera.js``)
used by both pages. These tests pin the behaviour at the level that matters: the
rendered markup, the shared partial, and the shipped module.
"""
import re
from pathlib import Path

from app import create_app
from app.db import get_db

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = (ROOT / "templates/admin/scan_vehicle.html", ROOT / "templates/admin/scan_return.html")
PARTIAL = ROOT / "templates/admin/_scan_camera.html"
MODULE = ROOT / "static/js/scan-camera.js"
CSS = ROOT / "static/css/app.css"


def _owner_client(app):
    client = app.test_client()
    with app.app_context():
        owner = get_db().execute("SELECT id FROM users WHERE role='owner'").fetchone()
        assert owner is not None
    with client.session_transaction() as s:
        s["user_id"] = owner["id"]
        s["user_role"] = "owner"
    return client


# ── rendered page (the real route + template) ───────────────────────────────


def test_the_scan_pages_have_no_photo_path_at_all(tmp_path):
    """The camera reads the barcode live; there is no file input and no photo fallback."""
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "scan-camera.db")})
    client = _owner_client(app)
    for url in ("/scan-vehicle", "/scan-return"):
        html = client.get(url).get_data(as_text=True)
        assert 'name="disk_image"' not in html, url
        assert 'type="file"' not in html, url
        assert 'capture="environment"' not in html, url
        assert 'multipart/form-data' not in html, url
        assert 'id="scan-camera-capture"' not in html, url
        assert 'id="scan-camera-fallback"' not in html, url
        # The decoded text is posted through the page's own form.
        assert 'name="disc_text"' in html, url


def test_both_pages_render_the_full_screen_scanner(tmp_path):
    """The overlay, guide and controls are actually in the served HTML."""
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "scan-render.db")})
    client = _owner_client(app)
    for url in ("/scan-vehicle", "/scan-return"):
        html = client.get(url).get_data(as_text=True)
        assert 'id="scan-camera-live"' in html, url
        assert 'role="dialog"' in html and 'aria-modal="true"' in html, url
        assert 'id="scan-camera-video"' in html, url
        assert 'class="scan-barcode-guide"' in html, url
        assert 'id="scan-camera-cancel"' in html, url
        assert 'id="scan-camera-playstart"' in html, url
        assert 'id="scan-camera-status" aria-live="polite"' in html, url
        assert 'id="scan-camera-blocked"' in html, url
        # The manual fallback button and the local decoder are both shipped.
        assert 'id="scan-camera-manual"' in html, url
        assert "js/scan-camera.js" in html, url
        assert "vendor/zxing/zxing-library-0.21.3.min.js" in html, url


# ── shared module source ─────────────────────────────────────────────────────


def test_the_video_is_ios_ready():
    partial = PARTIAL.read_text(encoding="utf-8")
    video = re.search(r"<video[^>]*>", partial).group(0)
    for attr in ("playsinline", "webkit-playsinline", "muted", "autoplay"):
        assert attr in video, f"<video> missing {attr}"
    module = MODULE.read_text(encoding="utf-8")
    # The properties, not just the attributes, are what iOS Safari honours.
    assert "video.muted = true" in module
    assert "video.playsInline = true" in module


def test_the_overlay_is_revealed_before_the_stream_is_attached():
    """iOS will not paint a stream into a <video> still inside display:none."""
    module = MODULE.read_text(encoding="utf-8")
    handler = module[module.index("function openScanner"):]
    assert handler.index("overlay.hidden = false") < handler.index("attachAndPlay(mediaStream)")


def test_a_cancelled_camera_request_is_invalidated_and_stopped():
    """A getUserMedia that resolves after Cancel must not open the camera."""
    module = MODULE.read_text(encoding="utf-8")
    assert "var session = 0" in module
    assert "var token = ++session" in module
    # The late-stream guard: token mismatch -> stop the tracks, never attach.
    assert "if (token !== session || !isOpen)" in module
    assert "stopTracks(mediaStream)" in module


def test_camera_and_playback_failures_are_reported_separately():
    module = MODULE.read_text(encoding="utf-8")
    # A constraint ladder, not a single ideal-constraint object.
    assert "function requestCamera(" in module
    assert "{video: {facingMode: 'environment'}, audio: false}" in module
    assert "{video: true, audio: false}" in module
    # A blocked permission gets its own message/branch; a playback refusal gets a
    # visible retry ("Start preview") rather than being reported as a block. Every
    # failure goes to MANUAL entry — never a photo.
    assert "NotAllowedError" in module
    assert "scan-camera-playstart" in module or "playStart" in module
    assert "fallBackToManual" in module
    assert "fallBackToPhoto" not in module


def test_the_status_region_and_pagehide_stop_survive():
    module = MODULE.read_text(encoding="utf-8")
    assert "window.addEventListener('pagehide'" in module
    assert "getTracks().forEach" in module


def test_the_blocked_permission_message_is_present_and_honest():
    partial = PARTIAL.read_text(encoding="utf-8")
    assert "Camera permission is blocked" in partial
    # The page must say it cannot force the permission, not imply it can.
    assert "cannot switch the camera on" in partial


# ── shared CSS: full-screen, safe-area, contain, forgiving guide ──────────────


def test_the_overlay_css_is_fixed_full_screen_with_safe_areas():
    css = CSS.read_text(encoding="utf-8")
    assert re.search(r"\.scan-camera-live\{[^}]*position:fixed", css)
    assert re.search(r"\.scan-camera-live\{[^}]*inset:0", css)
    assert "safe-area-inset-top" in css and "safe-area-inset-bottom" in css
    # Keep the FULL camera image: contain, never cover.
    assert re.search(r"\.scan-camera-stage video\{[^}]*object-fit:contain", css)
    assert "aspect-ratio:6/1" in css  # forgiving area, not the exact 153/13 silhouette


def test_the_guide_and_measurements_are_shared_by_both_pages():
    for template in TEMPLATES:
        src = template.read_text(encoding="utf-8")
        assert "_scan_camera.html" in src, template.name
    partial = PARTIAL.read_text(encoding="utf-8")
    assert 'class="scan-barcode-guide"' in partial
    # A simple "fit it inside the guide" instruction, not a demand for the exact
    # physical barcode size (which no screen can guarantee).
    assert "Fit the whole barcode inside the guide" in partial
    assert "153 mm × 13 mm" not in partial
