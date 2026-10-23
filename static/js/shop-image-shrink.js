/* Downscale shop photos in the staff browser before they are uploaded.
 *
 * The server still enforces its own byte and pixel limits; this only avoids
 * sending a 12 MP phone photo through the request, where the single worker has
 * to read it, decode it and re-encode it. Files that are already small are left
 * untouched, and any failure falls back to uploading the original.
 */
(function () {
  'use strict';
  var MAX_EDGE = 1280;
  var MAX_BYTES = 4 * 1024 * 1024;
  var QUALITY = 0.82;

  function toBlob(canvas) {
    return new Promise(function (resolve) {
      if (canvas.toBlob) {
        canvas.toBlob(function (blob) { resolve(blob); }, 'image/jpeg', QUALITY);
      } else {
        resolve(null);
      }
    });
  }

  function draw(source, width, height) {
    var scale = Math.min(1, MAX_EDGE / Math.max(width, height));
    var targetWidth = Math.max(1, Math.round(width * scale));
    var targetHeight = Math.max(1, Math.round(height * scale));
    var canvas = document.createElement('canvas');
    canvas.width = targetWidth;
    canvas.height = targetHeight;
    var context = canvas.getContext('2d');
    if (!context) { return Promise.resolve(null); }
    // JPEG has no alpha: flatten transparency onto white instead of black.
    context.fillStyle = '#ffffff';
    context.fillRect(0, 0, targetWidth, targetHeight);
    context.drawImage(source, 0, 0, targetWidth, targetHeight);
    return toBlob(canvas);
  }

  function shrink(file) {
    if (!file || !/^image\/(jpeg|png|webp)$/i.test(file.type || '')) {
      return Promise.resolve(null);
    }
    if (typeof createImageBitmap === 'function') {
      return createImageBitmap(file, { imageOrientation: 'from-image' })
        .catch(function () { return createImageBitmap(file); })
        .then(function (bitmap) {
          if (Math.max(bitmap.width, bitmap.height) <= MAX_EDGE && file.size <= MAX_BYTES) {
            if (bitmap.close) { bitmap.close(); }
            return null;
          }
          return draw(bitmap, bitmap.width, bitmap.height).then(function (blob) {
            if (bitmap.close) { bitmap.close(); }
            return blob;
          });
        })
        .catch(function () { return null; });
    }
    return new Promise(function (resolve) {
      var url = URL.createObjectURL(file);
      var image = new Image();
      image.onload = function () {
        draw(image, image.naturalWidth, image.naturalHeight)
          .then(function (blob) { URL.revokeObjectURL(url); resolve(blob); })
          .catch(function () { URL.revokeObjectURL(url); resolve(null); });
      };
      image.onerror = function () { URL.revokeObjectURL(url); resolve(null); };
      image.src = url;
    });
  }

  function attach(input) {
    var serial = 0;
    if (input.form) input.form.addEventListener('submit', function (event) {
      if (input.dataset.preparing === '1') event.preventDefault();
    });
    input.addEventListener('change', function () {
      var file = input.files && input.files[0];
      if (!file) { return; }
      var ticket = ++serial;
      input.dataset.preparing = '1';
      var form = input.form;
      var buttons = form ? form.querySelectorAll('button[type=submit],button:not([type]),input[type=submit]') : [];
      for (var b = 0; b < buttons.length; b++) buttons[b].disabled = true;
      var note = form ? form.querySelector('[data-shrink-status]') : null;
      var originalText = note ? note.textContent : '';
      if (note) { note.textContent = 'Preparing photo…'; }
      shrink(file).then(function (blob) {
        if (ticket !== serial) return;
        if (!blob || blob.size >= file.size) {
          if (note) { note.textContent = originalText; }
          return;
        }
        if (typeof DataTransfer === 'undefined') {
          if (note) { note.textContent = originalText; }
          return;
        }
        try {
          var transfer = new DataTransfer();
          transfer.items.add(new File([blob], file.name.replace(/\.[^.]+$/, '') + '.jpg', {
            type: 'image/jpeg',
            lastModified: Date.now()
          }));
          input.files = transfer.files;
          if (note) {
            note.textContent = 'Ready to upload: ' + Math.round(blob.size / 1024) + ' KB (scaled to ' + MAX_EDGE + 'px).';
          }
        } catch (error) {
          if (note) { note.textContent = originalText; }
        }
      }).catch(function () {
        if (note) note.textContent = originalText;
      }).then(function () {
        if (ticket !== serial) return;
        delete input.dataset.preparing;
        for (var b = 0; b < buttons.length; b++) buttons[b].disabled = false;
      });
    });
  }

  function start() {
    var inputs = document.querySelectorAll('input[type="file"][data-shrink-image]');
    for (var index = 0; index < inputs.length; index += 1) { attach(inputs[index]); }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
