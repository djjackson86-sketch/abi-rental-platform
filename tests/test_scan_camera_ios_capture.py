"""The scan-camera capture path must open, fill the screen, and degrade safely.

The phone failure this file guards against was NOT a JS error, a missing script
block, or a CSP/Permissions-Policy header: the inline script ran and
``getUserMedia`` worked in every desktop/mobile-emulated browser. The break was
iOS-only and shared by both scan pages:

  * the ``<input type=file capture="environment">`` carried the ``hidden``
    attribute -> ``[hidden]{display:none!important}`` -> ``display:none``, and
    iOS Safari refuses to open a file picker from ``.click()`` on a display:none
    input. That input is the camera path whenever ``getUserMedia`` is missing or
    blocked (in-app browsers, denied permission), so on an iPhone both the live
    camera and the photo fallback were dead;
  * the ``<video>`` relied on the ``muted``/``playsinline`` *attributes* only,
    and the frame was made visible *after* ``srcObject`` was attached.

The scanner is now a SHARED full-screen overlay (markup in
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


def test_the_camera_file_input_is_not_display_none(tmp_path):
    """A display:none file input cannot be opened by .click() on iOS Safari."""
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "scan-camera.db")})
    client = _owner_client(app)
    for url in ("/scan-vehicle", "/scan-return"):
        html = client.get(url).get_data(as_text=True)
        tag = re.search(r'<input[^>]*name="disk_image"[^>]*>', html)
        assert tag, url
        assert " hidden" not in tag.group(0), f"{url}: disk_image must not be display:none"
        assert 'capture="environment"' in tag.group(0)


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
        assert 'id="scan-camera-capture"' in html and 'id="scan-camera-cancel"' in html, url
        assert 'id="scan-camera-playstart"' in html, url
        assert 'id="scan-camera-status" aria-live="polite"' in html, url
        assert 'id="scan-camera-fallback" hidden' in html, url
        assert 'id="scan-camera-blocked"' in html, url
        assert "js/scan-camera.js" in html, url


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
    # visible retry ("Start preview") rather than being reported as a block.
    assert "NotAllowedError" in module
    assert "scan-camera-playstart" in module or "playStart" in module
    assert "fallBackToPhoto" in module


def test_the_status_region_and_pagehide_stop_survive():
    module = MODULE.read_text(encoding="utf-8")
    assert "window.addEventListener('pagehide'" in module
    assert "getTracks().forEach" in module


def test_the_blocked_permission_message_is_present_and_honest():
    partial = PARTIAL.read_text(encoding="utf-8")
    assert "Camera permission is blocked" in partial
    # The page must say it cannot force the permission, not imply it can.
    assert "cannot switch the camera on" in partial


# ── shared CSS: full-screen, safe-area, contain, 153/13 guide ─────────────────


def test_the_overlay_css_is_fixed_full_screen_with_safe_areas():
    css = CSS.read_text(encoding="utf-8")
    assert re.search(r"\.scan-camera-live\{[^}]*position:fixed", css)
    assert re.search(r"\.scan-camera-live\{[^}]*inset:0", css)
    assert "safe-area-inset-top" in css and "safe-area-inset-bottom" in css
    # Keep the FULL camera image: contain, never cover.
    assert re.search(r"\.scan-camera-stage video\{[^}]*object-fit:contain", css)
    assert "aspect-ratio:153/13" in css


def test_the_guide_and_measurements_are_shared_by_both_pages():
    for template in TEMPLATES:
        src = template.read_text(encoding="utf-8")
        assert "_scan_camera.html" in src, template.name
    partial = PARTIAL.read_text(encoding="utf-8")
    assert 'class="scan-barcode-guide"' in partial
    assert "153 mm × 13 mm" in partial
