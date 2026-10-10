"""The scan-camera capture path must open on a phone.

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
    and the frame was made visible *after* ``srcObject`` was attached, with
    ``video.play()`` rejection funnelled into the same branch as a getUserMedia
    failure.

These tests pin the fixes at the level that matters: the rendered markup and the
shipped template source.
"""
import re
from pathlib import Path

from app import create_app
from app.db import get_db

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ("scan_vehicle.html", "scan_return.html")


def _owner_client(app):
    client = app.test_client()
    with app.app_context():
        owner = get_db().execute("SELECT id FROM users WHERE role='owner'").fetchone()
        assert owner is not None
    with client.session_transaction() as s:
        s["user_id"] = owner["id"]
        s["user_role"] = "owner"
    return client


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


def test_the_video_is_ios_ready_in_both_pages():
    for name in TEMPLATES:
        src = (ROOT / "templates/admin" / name).read_text(encoding="utf-8")
        video_tag = re.search(r"<video[^>]*>", src)
        assert video_tag, name
        video = video_tag.group(0)
        for attr in ("playsinline", "webkit-playsinline", "muted", "autoplay"):
            assert attr in video, f"{name}: <video> missing {attr}"
        # The properties, not just the attributes, are what iOS Safari honours.
        assert "video.muted = true" in src, name
        assert "video.playsInline = true" in src, name


def test_the_frame_is_revealed_before_the_stream_is_attached():
    """iOS will not paint a stream into a <video> still inside display:none."""
    for name in TEMPLATES:
        src = (ROOT / "templates/admin" / name).read_text(encoding="utf-8")
        handler = src[src.index("start.addEventListener"):]
        assert handler.index("live.hidden = false") < handler.index("video.srcObject = mediaStream"), name


def test_camera_and_playback_failures_are_reported_separately():
    for name in TEMPLATES:
        src = (ROOT / "templates/admin" / name).read_text(encoding="utf-8")
        # A constraint ladder, not a single ideal-constraint object.
        assert "function requestCamera(" in src, name
        assert "{video: {facingMode: 'environment'}, audio: false}" in src, name
        assert "{video: true, audio: false}" in src, name
        # A playback refusal is retried on the next tap, never reported as a block.
        assert "retryPlay" in src, name
        assert "NotAllowedError" in src, name
        # Fallback stays hidden until a failure actually happens.
        assert 'id="scan-camera-fallback" hidden' in src, name


def test_the_status_region_and_pagehide_stop_survive():
    for name in TEMPLATES:
        src = (ROOT / "templates/admin" / name).read_text(encoding="utf-8")
        assert 'id="scan-camera-status" aria-live="polite"' in src, name
        assert "window.addEventListener('pagehide', stopCamera)" in src, name
        assert "getTracks().forEach" in src, name
