/* Full-screen web camera scanner with a licence-disk barcode guide.
 *
 * Shared by the Scan vehicle and Scan return pages. Design decisions that matter
 * on a real phone (and are the reason this is a module rather than inline code):
 *
 *  - The scanner is a fixed-position overlay (position:fixed; inset:0), NOT the
 *    Fullscreen API: iOS Safari does not implement requestFullscreen on a <div>,
 *    so a fixed overlay is the only thing that reliably fills the screen.
 *  - A blocked camera PERMISSION cannot be bypassed by a page. The browser owns
 *    that decision, so we surface a clear message and reveal the photo fallback
 *    (the page's own file input) instead of pretending the camera can be forced.
 *  - Cancelling invalidates any getUserMedia() still pending. A stream that
 *    resolves AFTER the user cancelled is stopped immediately, so the camera
 *    never keeps running (and its indicator light never stays on) behind a
 *    closed overlay.
 *  - A playback refusal gets a visible "Start preview" retry, then the fallback.
 */
(function (window, document) {
  'use strict';

  function byId(id) { return id ? document.getElementById(id) : null; }

  function AbiScanCamera() {}

  AbiScanCamera.init = function (options) {
    options = options || {};
    var form = typeof options.form === 'string' ? document.querySelector(options.form) : options.form;
    var fileInput = byId(options.fileInput || 'scan-disk-image');
    var start = byId(options.start || 'scan-camera-start');
    var overlay = byId('scan-camera-live');
    var video = byId('scan-camera-video');
    var capture = byId('scan-camera-capture');
    var cancel = byId('scan-camera-cancel');
    var status = byId('scan-camera-status');
    var fallback = byId('scan-camera-fallback');
    var blocked = byId('scan-camera-blocked');
    var playStart = byId('scan-camera-playstart');

    if (!form || !fileInput || !start || !overlay || !video || !capture || !cancel) {
      return null;
    }

    var filename = options.filename || 'disk-scan.jpg';
    var readingMessage = options.readingMessage || 'Reading disk…';

    var stream = null;
    var session = 0;      // Bumped on every open/close. A getUserMedia() that
                          // resolves with a stale token is stopped, never attached.
    var isOpen = false;
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
      if (fallback) { fallback.hidden = true; }
    }

    function restoreFocus() {
      if (lastFocus && lastFocus.focus) {
        try { lastFocus.focus(); } catch (err) { /* element gone */ }
      }
    }

    function closeScanner(message) {
      session += 1;               // invalidate any in-flight getUserMedia()
      isOpen = false;
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

    // The camera is unavailable or refused: close the scanner and give the user
    // the photo path with a message that says what to do next.
    function fallBackToPhoto(message, asBlock) {
      closeScanner(message);
      if (fallback) { fallback.hidden = false; }
      if (asBlock && blocked) {
        blocked.hidden = false;
        // Reveal the fallback button BEFORE scrolling, so the banner is not pushed
        // back below the fold by the button appearing above it.
        if (blocked.scrollIntoView) {
          try { blocked.scrollIntoView({block: 'end'}); } catch (err) { /* ignore */ }
        }
      } else {
        say(message);
      }
    }

    function attachAndPlay(mediaStream) {
      stream = mediaStream;
      video.srcObject = mediaStream;
      var playing = video.play();
      if (playing && typeof playing.then === 'function') {
        playing.then(function () {
          setPlayStart(false);
        }).catch(function () {
          // A muted stream can still be refused (Low Power Mode, user-gesture
          // rules). Offer a visible tap-to-start retry rather than reporting the
          // camera as broken.
          setPlayStart(true);
        });
      } else {
        setPlayStart(false);
      }
    }

    if (playStart) {
      playStart.addEventListener('click', function () {
        if (!stream) { return; }
        var again = video.play();
        if (again && typeof again.then === 'function') {
          again.then(function () {
            setPlayStart(false);
          }).catch(function () {
            fallBackToPhoto('The camera preview would not start — use the photo button instead.', false);
          });
        } else {
          setPlayStart(false);
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
      hideBanners();
      // Reveal the overlay BEFORE the stream is attached: iOS Safari will not paint
      // a MediaStream into a <video> that is still inside a display:none container.
      overlay.hidden = false;
      document.body.classList.add('scan-fs-open');
      start.hidden = true;
      if (typeof options.onOpen === 'function') { options.onOpen(); }
      setPlayStart(false);
      say('Opening camera…');
      isOpen = true;
      if (capture && capture.focus) { try { capture.focus(); } catch (err) { /* ignore */ } }

      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        // No getUserMedia at all (old WebView, or an insecure/in-app browser): the
        // live camera is impossible here, so go straight to the photo path.
        fallBackToPhoto('This browser cannot open the live camera — use the photo button instead.', false);
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
        say('Align the barcode in the guide, then tap Capture.');
      }).catch(function (err) {
        if (token !== session || !isOpen) { return; }
        var denied = err && (err.name === 'NotAllowedError' || err.name === 'SecurityError');
        var missing = err && (err.name === 'NotFoundError' || err.name === 'DevicesNotFoundError');
        if (denied) {
          fallBackToPhoto('Camera permission is blocked. Allow the camera in your browser and reload, or use the photo button below.', true);
        } else if (missing) {
          fallBackToPhoto('No camera was found on this device — use the photo button instead.', false);
        } else {
          fallBackToPhoto('The camera could not be started — use the photo button instead.', false);
        }
      });
    }

    function captureFrame() {
      if (!video.videoWidth || !video.videoHeight) {
        say('The camera is still starting — give it a moment.');
        return;
      }
      var canvas = document.createElement('canvas');
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      var ctx = canvas.getContext('2d');
      if (!ctx) {
        fallBackToPhoto('This browser cannot capture from the camera — use the photo button instead.', false);
        return;
      }
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      canvas.toBlob(function (blob) {
        if (!blob) {
          fallBackToPhoto('The capture came back empty — use the photo button instead.', false);
          return;
        }
        try {
          var transfer = new DataTransfer();
          transfer.items.add(new File([blob], filename, {type: 'image/jpeg'}));
          fileInput.files = transfer.files;
        } catch (err) {
          fallBackToPhoto('This browser cannot attach the capture — use the photo button instead.', false);
          return;
        }
        closeScanner(readingMessage);
        form.submit();
      }, 'image/jpeg', 0.92);
    }

    // The file input is deliberately NOT display:none: iOS Safari refuses to open
    // a file picker from .click() on a display:none input, which is exactly how
    // staff reach the camera when getUserMedia is unavailable or blocked.
    if (fallback) {
      fallback.addEventListener('click', function () { fileInput.click(); });
    }
    fileInput.addEventListener('change', function () {
      if (!fileInput.files || !fileInput.files.length) { return; }
      closeScanner(readingMessage);
      form.submit();
    });

    start.addEventListener('click', openScanner);
    capture.addEventListener('click', captureFrame);
    cancel.addEventListener('click', function () { closeScanner('Cancelled.'); });
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
