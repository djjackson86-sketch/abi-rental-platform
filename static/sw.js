/* Sano Trailers push service worker (served from /sw.js, scope /).
 *
 * Deliberately minimal: it shows the notification the backend pushes and opens
 * the customer the notification is about. It never stores a response — the staff
 * screens behind the login must never be replayed to a signed-out browser, so
 * there is no fetch handler and no offline storage here at all.
 */
'use strict';

// A new deploy should take over without asking anyone to close every tab, and
// an already-open page should start using it straight away.
self.addEventListener('install', function () {
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  event.waitUntil(self.clients.claim());
});

var FALLBACK_TITLE = 'Sano Trailers';
var DEFAULT_URL = '/';
var CUSTOMER_PATH = /^\/customers\/\d+$/;
var ENQUIRY_PATH = /^\/online-store\/enquiries\/\d+$/;

function text(value, limit) {
  return typeof value === 'string' ? value.slice(0, limit) : '';
}

/** Only a same-origin customer screen may be opened from a payload. */
function targetPath(customerId) {
  var id = String(customerId == null ? '' : customerId);
  if (!/^\d+$/.test(id)) return DEFAULT_URL;
  var path = '/customers/' + id;
  return CUSTOMER_PATH.test(path) ? path : DEFAULT_URL;
}

/** Re-check the stored path on click: it must be a local customer screen. */
function safePath(value) {
  var candidate = typeof value === 'string' ? value.trim() : '';
  if (!candidate || candidate.charAt(0) !== '/') return DEFAULT_URL;
  if (candidate.indexOf('//') === 0 || candidate.indexOf('\\') !== -1) return DEFAULT_URL;
  if (candidate === DEFAULT_URL || CUSTOMER_PATH.test(candidate) || ENQUIRY_PATH.test(candidate)) return candidate;
  return DEFAULT_URL;
}

self.addEventListener('push', function (event) {
  var payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (err) {
    payload = {};
  }
  if (!payload || typeof payload !== 'object') payload = {};

  var title = text(payload.title, 120).trim() || FALLBACK_TITLE;
  var options = {
    body: text(payload.body, 300),
    icon: '/static/img/favicon-180.png',
    badge: '/static/img/favicon-32.png',
    // Same tag replaces the previous notification about the same thing.
    tag: text(payload.tag, 64) || undefined,
    renotify: false,
    data: { url: /^\d+$/.test(String(payload.enquiry_id || '')) ? '/online-store/enquiries/' + payload.enquiry_id : (payload.customer_id != null ? targetPath(payload.customer_id) : safePath(payload.url)) }
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function (event) {
  event.notification.close();

  var data = event.notification.data || {};
  var path = safePath(data.url);

  event.waitUntil((function () {
    return self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (windows) {
      var list = windows || [];
      for (var i = 0; i < list.length; i += 1) {
        var client = list[i];
        var origin;
        try {
          origin = new URL(client.url).origin;
        } catch (err) {
          continue;
        }
        if (origin !== self.location.origin) continue;
        // Focus the app the staff member already has open and take it to the
        // customer, rather than piling up duplicate windows.
        return Promise.resolve(typeof client.focus === 'function' ? client.focus() : null)
          .then(function () {
            if (typeof client.navigate === 'function') return client.navigate(path);
            return null;
          })
          .catch(function () {
            return openWindow(path);
          });
      }
      return openWindow(path);
    });
  })());
});

function openWindow(path) {
  if (!self.clients || typeof self.clients.openWindow !== 'function') return null;
  return self.clients.openWindow(path).catch(function () {
    return null;
  });
}
