/* Full-screen web camera scanner that reads a licence-disk PDF417 barcode LIVE.
 *
 * Shared by the Scan vehicle and Scan return pages — the mechanism, overlay,
 * hints and lifecycle are identical; only the destination (form action and
 * messages) differs. Design decisions that matter on a real phone:
 *
 *  - The scanner is a fixed-position overlay (position:fixed; inset:0), NOT the
 *    Fullscreen API: iOS Safari does not implement requestFullscreen on a <div>,
 *    so a fixed overlay is the only thing that reliably fills the screen.
 *  - The barcode is decoded **in the browser**, one bounded frame at a time, on a
 *    ~300 ms cadence, using the ZXing PDF417 engine vendored locally under
 *    static/vendor/zxing (no CDN, no third-party script at runtime). Nothing is
 *    ever uploaded: the decoded text is put in the form's hidden `disc_text`
 *    field and the page's own form is posted, so the server still owns
 *    validation.
 *  - There is NO photo path. A blocked camera cannot be forced by a page, and
 *    the old photo fallback is gone by decision — the ONLY fallback is manual
 *    entry, which each page already renders. On failure we surface a clear
 *    message and reveal that page's manual action.
 *  - The camera is requested through a constraint ladder (ideal rear 1080p, then
 *    plain rear, then any camera) so a phone that rejects the ideal constraints
 *    still opens. A cancelled request is invalidated by a session token, and a
 *    stream that resolves AFTER the user cancelled is stopped immediately, so the
 *    camera (and its indicator light) never keeps running behind a closed overlay.
 *  - A playback refusal gets a visible "Start preview" retry rather than being
 *    reported as a broken camera.
 *  - Decoding is armed exactly once per session: the first valid payload settles
 *    the scan, the overlay closes, the stream stops and the form is submitted a
 *    single time. A decode that resolves after a cancel/close is discarded.
 */
(function (window, document) {
  'use strict';

  function byId(id) { return id ? document.getElementById(id) : null; }

  // One decode per tick. The TrailerPro reference runs its PDF417 reader at
  // ~300 ms between attempts; matching that keeps the CPU/battery cost bounded on
  // a counter phone while still feeling continuous to staff.
  var SCAN_INTERVAL_MS = 300;

  // The frame handed to the decoder is bounded: never a full 12 MP phone frame and
  // never larger than 1080p even if the camera hands one over. Downscaling only
  // when a device ignores the requested size; a well-behaved camera is decoded 1:1.
  var MAX_DECODE_WIDTH = 1920;
  var MAX_DECODE_HEIGHT = 1080;

  // The alignment guide is what staff aim at, so the crop underneath it is decoded
  // FIRST, at native camera pixels. The whole-frame bound above turns a portrait
  // stream (1080x1920) into 608x1080 — a 0.5625 downscale — and a disc barcode that
  // fits inside the guide is then only ~100 px wide, which the vendored engine does
  // not read even on a sharp frame. The crop is small, so it is never downscaled:
  // it is scaled UP (up to ROI_UPSCALE) to give the decoder more pixels per module,
  // and capped by ROI_MAX_PIXELS so the transient canvas stays bounded.
  var ROI_UPSCALE = 2;
  var ROI_MAX_PIXELS = 1244160;   // ~1.2 MP, below the whole-frame bound

  function AbiScanCamera() {}

  AbiScanCamera.init = function (options) {
    options = options || {};
    var form = typeof options.form === 'string' ? document.querySelector(options.form) : options.form;
    var start = byId(options.start || 'scan-camera-start');
    var manual = byId(options.manual || 'scan-manual');
    var overlay = byId('scan-camera-live');
    var video = byId('scan-camera-video');
    var manualButton = byId(options.manualButton || 'scan-camera-manual');
    var cancel = byId('scan-camera-cancel');
    var status = byId('scan-camera-status');
    var blocked = byId('scan-camera-blocked');
    var playStart = byId('scan-camera-playstart');
    var guide = byId(options.guide || 'scan-barcode-guide');

    if (!form || !start || !overlay || !video) {
      return null;
    }

    // The decoded barcode text lands here and then rides the page's existing POST.
    // Declared in the shared partial; created defensively if a caller forgot it.
    var discText = byId(options.discText || 'scan-disc-text');
    if (!discText) {
      discText = document.createElement('input');
      discText.type = 'hidden';
      discText.name = 'disc_text';
      discText.id = 'scan-disc-text';
      form.appendChild(discText);
    }

    var readingMessage = options.readingMessage || 'Reading disk…';

    var stream = null;
    var session = 0;      // Bumped on every open/close. A getUserMedia() that
                          // resolves with a stale token is stopped, never attached.
    var isOpen = false;
    var settled = false;  // A payload has been accepted (or the scan was given up):
                          // decoding is disarmed and the form is submitted once.
    var scanTimer = null;
    var reader = null;
    var canvas = null;
    var canvasCtx = null;
    var hints = null;
    var lastFocus = null;

    // iOS Safari honours muted/playsinline on a srcObject-fed <video> only as DOM
    // properties; the content attributes alone are not always enough. Set both.
    video.muted = true;
    video.playsInline = true;
    video.setAttribute('playsinline', '');
    video.setAttribute('webkit-playsinline', '');

    function say(message) { if (status) { status.textContent = message || ''; } }

    function stopTracks(mediaStream) {
      if (mediaStream && mediaStream.getTracks) {
        mediaStream.getTracks().forEach(function (track) {
          try { track.stop(); } catch (err) { /* already stopped */ }
        });
      }
    }

    function setPlayStart(on) { if (playStart) { playStart.hidden = !on; } }

    function hideBanners() {
      if (blocked) { blocked.hidden = true; }
    }

    function restoreFocus() {
      if (lastFocus && lastFocus.focus) {
        try { lastFocus.focus(); } catch (err) { /* element gone */ }
      }
    }

    // ── The decoder (vendored ZXing PDF417, driven one frame at a time) ────────

    function buildReader() {
      var ZX = window.ZXing;
      if (!ZX || !ZX.PDF417Reader || !ZX.HTMLCanvasElementLuminanceSource || !ZX.BinaryBitmap || !ZX.HybridBinarizer) {
        return null;
      }
      canvas = document.createElement('canvas');
      canvasCtx = canvas.getContext && canvas.getContext('2d');
      if (!canvasCtx) { return null; }
      try {
        hints = (typeof window.Map === 'function' && ZX.DecodeHintType)
          ? new window.Map([[ZX.DecodeHintType.TRY_HARDER, true]])
          : null;
      } catch (err) { hints = null; }
      return new ZX.PDF417Reader();
    }

    // The guide's rectangle in NATIVE video pixels, or null when it cannot be
    // measured (no layout yet, a browser that will not report it, or a clipped-away
    // guide). The video is painted with object-fit:contain, so the picture is
    // letterboxed inside the video element: the guide is mapped onto that picture
    // first, then into camera pixels, then clamped to the frame.
    function guideRegion() {
      if (!guide || typeof guide.getBoundingClientRect !== 'function'
        || typeof video.getBoundingClientRect !== 'function') { return null; }
      var vw = video.videoWidth, vh = video.videoHeight;
      if (!vw || !vh) { return null; }
      var vr = video.getBoundingClientRect();
      if (!vr.width || !vr.height) { return null; }
      var fit = Math.min(vr.width / vw, vr.height / vh);
      if (!(fit > 0)) { return null; }
      var picLeft = vr.left + (vr.width - vw * fit) / 2;
      var picTop = vr.top + (vr.height - vh * fit) / 2;
      var gr = guide.getBoundingClientRect();
      if (!gr.width || !gr.height) { return null; }
      var left = Math.max(gr.left, picLeft), top = Math.max(gr.top, picTop);
      var right = Math.min(gr.right, picLeft + vw * fit), bottom = Math.min(gr.bottom, picTop + vh * fit);
      if (right - left < 16 || bottom - top < 8) { return null; }
      var x = Math.max(0, Math.min(vw - 1, Math.round((left - picLeft) / fit)));
      var y = Math.max(0, Math.min(vh - 1, Math.round((top - picTop) / fit)));
      return {
        x: x,
        y: y,
        w: Math.max(1, Math.min(vw - x, Math.round((right - left) / fit))),
        h: Math.max(1, Math.min(vh - y, Math.round((bottom - top) / fit)))
      };
    }

    // Draw ONE region of the video (native pixels; null = the whole frame) into a
    // bounded canvas and run one decode attempt. Returns the payload text, or null
    // when this frame held no barcode (or the video is not ready yet). Never throws
    // for "no barcode found".
    function decodeOneFrame(region) {
      var vw = video.videoWidth;
      var vh = video.videoHeight;
      if (!vw || !vh) { return null; }
      var sx = 0, sy = 0, sw = vw, sh = vh;
      var scale = Math.min(1, MAX_DECODE_WIDTH / vw, MAX_DECODE_HEIGHT / vh);
      if (region) {
        sx = region.x; sy = region.y; sw = region.w; sh = region.h;
        if (!(sw > 0) || !(sh > 0)) { return null; }
        var budget = Math.sqrt(ROI_MAX_PIXELS / (sw * sh));
        scale = Math.min(ROI_UPSCALE, Math.max(1, budget));
        if (scale > budget) { scale = budget; }     // a huge crop still respects the budget
      }
      var width = Math.max(1, Math.round(sw * scale));
      var height = Math.max(1, Math.round(sh * scale));
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      canvasCtx.drawImage(video, sx, sy, sw, sh, 0, 0, width, height);
      // HTMLCanvasElementLuminanceSource reads the canvas itself and converts to
      // grayscale (with an inverted pass on retry, which helps a light barcode).
      var source = new window.ZXing.HTMLCanvasElementLuminanceSource(canvas, true);
      var bitmap = new window.ZXing.BinaryBitmap(new window.ZXing.HybridBinarizer(source));
      var result = hints ? reader.decode(bitmap, hints) : reader.decode(bitmap);
      return result ? result.getText() : null;
    }

    function stopScan() {
      if (scanTimer !== null) {
        try { (window.clearTimeout || clearTimeout)(scanTimer); } catch (err) { /* ignore */ }
        scanTimer = null;
      }
    }

    // A decoded payload must not be empty or control-only noise — an empty decode
    // is dropped and scanning resumes. The SERVER is still the validator for
    // whether the payload is a real licence disk; this only stops obvious junk.
    function looksLikePayload(text) {
      if (!text) { return false; }
      var trimmed = String(text).trim();
      if (!trimmed) { return false; }
      return /[0-9A-Za-z]/.test(trimmed);
    }

    function scheduleTick() {
      if (!isOpen || settled) { return; }
      var set = window.setTimeout || setTimeout;
      scanTimer = set(tick, SCAN_INTERVAL_MS);
    }

    // Decode what the guide is AIMED at first (the crop, at native camera pixels),
    // and only then the whole frame, in case the barcode sits outside the guide.
    // One decode at a time, at the module's cadence; a success stops the rest.
    // A frame without a barcode THROWS from the decoder (ZXing's NotFoundException),
    // so each attempt is wrapped here: otherwise the first miss would skip the
    // whole-frame fallback entirely.
    function tryFrame(region) {
      try {
        return decodeOneFrame(region);
      } catch (err) {
        return null;      // NotFound / Checksum / Format == "no readable barcode here"
      }
    }

    function decodeTick() {
      var region = guideRegion();
      var text = tryFrame(region);
      if (!text && region) { text = tryFrame(null); }
      return text;
    }

    function tick() {
      scanTimer = null;
      if (!isOpen || settled || !reader) { return; }
      var text = null;
      try {
        text = decodeTick();
      } catch (err) {
        // NotFound / Checksum / Format all mean "no readable barcode in this
        // frame" — keep scanning. Any other error is also non-fatal here.
        text = null;
      }
      if (settled || !isOpen) { return; }   // closed/cancelled during the decode
      if (looksLikePayload(text)) { acceptPayload(text); return; }
      scheduleTick();
    }

    function startScan() {
      if (!reader) { return; }
      if (scanTimer === null && !settled) { scheduleTick(); }
    }

    // ── Lifecycle ──────────────────────────────────────────────────────────────

    function closeScanner(message) {
      session += 1;               // invalidate any in-flight getUserMedia()
      isOpen = false;
      stopScan();
      stopTracks(stream);
      stream = null;
      try { video.srcObject = null; } catch (err) { /* ignore */ }
      overlay.hidden = true;
      document.body.classList.remove('scan-fs-open');
      setPlayStart(false);
      start.hidden = false;
      if (typeof options.onClose === 'function') { options.onClose(); }
      say(message || '');
      restoreFocus();
    }

    // The camera is unavailable or refused: close the scanner and hand the user to
    // the page's manual entry — the only fallback there is.
    function fallBackToManual(message, asBlock, kind) {
      closeScanner(message);
      if (asBlock && blocked) {
        blocked.hidden = false;
        // Reveal the manual action BEFORE scrolling, so the banner is not pushed
        // back below the fold by the manual button appearing above it.
        if (blocked.scrollIntoView) {
          try { blocked.scrollIntoView({block: 'end'}); } catch (err) { /* ignore */ }
        }
      }
      if (typeof options.onError === 'function') { options.onError(message, kind || 'error'); }
    }

    // A payload was accepted: settle ONCE, put it in the hidden field and post the
    // page's own form. The server validates and renders the review — this never
    // saves or confirms anything.
    function acceptPayload(text) {
      if (settled) { return; }     // exactly once, even if a stale frame arrives late
      settled = true;
      stopScan();
      discText.value = text;
      closeScanner(readingMessage);
      try {
        form.submit();
      } catch (err) { /* the page will show the form again */ }
    }

    function attachAndPlay(mediaStream) {
      stream = mediaStream;
      video.srcObject = mediaStream;
      var playing = video.play();
      if (playing && typeof playing.then === 'function') {
        playing.then(function () {
          setPlayStart(false);
          startScan();
        }).catch(function () {
          // A muted stream can still be refused (Low Power Mode, user-gesture
          // rules). Offer a visible tap-to-start retry rather than reporting the
          // camera as broken.
          setPlayStart(true);
        });
      } else {
        setPlayStart(false);
        startScan();
      }
    }

    if (playStart) {
      playStart.addEventListener('click', function () {
        if (!stream) { return; }
        var again = video.play();
        if (again && typeof again.then === 'function') {
          again.then(function () {
            setPlayStart(false);
            startScan();
          }).catch(function () {
            fallBackToManual('The camera preview would not start — enter the disk details manually.', false);
          });
        } else {
          setPlayStart(false);
          startScan();
        }
      });
    }

    // Some phones reject the ideal-constraint object outright (OverconstrainedError);
    // step down to a plain rear-camera request, then to any camera, before giving up.
    var cameraOptions = [
      {video: {facingMode: {ideal: 'environment'}, width: {ideal: 1920}, height: {ideal: 1080}}, audio: false},
      {video: {facingMode: 'environment'}, audio: false},
      {video: true, audio: false}
    ];

    function requestCamera(index) {
      return navigator.mediaDevices.getUserMedia(cameraOptions[index]).catch(function (err) {
        var retryable = err && (err.name === 'OverconstrainedError' || err.name === 'NotFoundError' || err.name === 'TypeError');
        if (retryable && index + 1 < cameraOptions.length) { return requestCamera(index + 1); }
        throw err;
      });
    }

    function openScanner() {
      if (isOpen) { return; }
      lastFocus = document.activeElement;
      settled = false;
      hideBanners();
      // Reveal the overlay BEFORE the stream is attached: iOS Safari will not paint
      // a MediaStream into a <video> that is still inside a display:none container.
      overlay.hidden = false;
      document.body.classList.add('scan-fs-open');
      start.hidden = true;
      if (typeof options.onOpen === 'function') { options.onOpen(); }
      setPlayStart(false);

      reader = buildReader();
      if (!reader) {
        // The vendored decoder did not load (or this browser cannot give us a 2D
        // context). Manual entry is the only route left; say so plainly.
        fallBackToManual('The barcode reader is unavailable in this browser — enter the disk details manually.', false, 'decoder');
        return;
      }

      say('Opening camera…');
      isOpen = true;

      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        // No getUserMedia at all (old WebView, or an insecure/in-app browser): the
        // live camera is impossible here, so go straight to manual entry.
        fallBackToManual('This browser cannot open the live camera — enter the disk details manually.', false, 'unsupported');
        return;
      }

      var token = ++session;
      requestCamera(0).then(function (mediaStream) {
        if (token !== session || !isOpen) {
          // The user cancelled (or closed) while the permission prompt was open.
          // Stop the late stream so the camera light does not stay on.
          stopTracks(mediaStream);
          return;
        }
        attachAndPlay(mediaStream);
        say('Hold the whole barcode inside the guide — the scan is automatic, no button to press.');
      }).catch(function (err) {
        if (token !== session || !isOpen) { return; }
        var denied = err && (err.name === 'NotAllowedError' || err.name === 'SecurityError');
        var missing = err && (err.name === 'NotFoundError' || err.name === 'DevicesNotFoundError');
        if (denied) {
          fallBackToManual('Camera permission is blocked. Allow the camera in your browser and reload, or enter the disk details manually.', true, 'denied');
        } else if (missing) {
          fallBackToManual('No camera was found on this device — enter the disk details manually.', false, 'missing');
        } else {
          fallBackToManual('The camera could not be started — enter the disk details manually.', false, 'error');
        }
      });
    }

    // The overlay's manual action routes to each page's own manual-entry UX.
    if (manualButton) {
      manualButton.addEventListener('click', function () {
        closeScanner('');
        if (typeof options.onManual === 'function') {
          options.onManual();
        } else if (manual) {
          try { manual.click(); } catch (err) { /* the page owns that button */ }
        }
      });
    }

    start.addEventListener('click', openScanner);
    if (cancel) {
      cancel.addEventListener('click', function () { closeScanner('Cancelled.'); });
    }
    overlay.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' || event.key === 'Esc') { closeScanner('Cancelled.'); }
    });
    // Stop the tracks when the page is hidden or torn down — a camera left open on
    // a counter phone drains the battery and keeps the indicator on.
    window.addEventListener('pagehide', function () { closeScanner(''); });

    return {open: openScanner, close: closeScanner};
  };

  window.AbiScanCamera = AbiScanCamera;
})(window, document);
