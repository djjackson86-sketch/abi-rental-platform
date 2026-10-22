/* Sano Trailers — staff push notifications, opt-in on a deliberate tap only.
 *
 * Rules this file follows, because they are easy to get wrong:
 *
 * 1. Nothing is subscribed, and no permission prompt is shown, unless a staff
 *    member taps "Enable notifications". iOS refuses a prompt that is not tied
 *    to a gesture, and a silent push opt-in surprises everyone else.
 * 2. Permission is never requested on page load. The only load-time network
 *    activity is a plain GET /push/config and - when permission is already
 *    granted - an idempotent re-register of the same subscription, so a device
 *    that was signed out and back in starts receiving again without a prompt.
 * 3. On iPhone/iPad the guidance comes before capability checks: Safari in a
 *    normal tab has no PushManager at all, so "not supported" would be wrong
 *    when the real fix is adding the app to the Home Screen.
 * 4. Logging out unsubscribes *this browser only*: the server is told the
 *    endpoint (before the session goes away) and the provider subscription is
 *    dropped, leaving the same account's other devices untouched.
 */
(function () {
  'use strict';

  if (typeof window === 'undefined' || typeof document === 'undefined') return;

  var CONFIG_URL = '/push/config';
  var SUBSCRIBE_URL = '/push/subscribe';
  var UNSUBSCRIBE_URL = '/push/unsubscribe';
  var TEST_URL = '/push/test';
  var SW_URL = '/sw.js';
  var SW_SCOPE = '/';
  var ICON_URL = '/static/img/favicon-180.png';
  var UNSUBSCRIBE_TIMEOUT_MS = 1200;

  var LABELS = {
    loading: 'Notifications',
    off: 'Notifications off',
    subscribed: 'Notifications on',
    denied: 'Notifications blocked',
    unsupported: 'Notifications not supported here',
    'ios-install': 'Add to Home Screen',
    disabled: 'Notifications unavailable',
    error: 'Notifications off'
  };

  var root = document.querySelector('[data-push-controls]');
  if (!root) return;

  var statusEl = root.querySelector('[data-push-status]');
  var hintEl = root.querySelector('[data-push-hint]');
  var enableBtn = root.querySelector('[data-push-enable]');
  var testBtn = root.querySelector('[data-push-test]');

  var state = 'loading';
  var config = null;
  var busy = false;

  function setState(next, message) {
    state = next;
    root.setAttribute('data-push-state', next);
    if (statusEl) statusEl.textContent = LABELS[next] || LABELS.off;
    if (hintEl) {
      hintEl.textContent = message || '';
      hintEl.hidden = !message;
    }
    if (enableBtn) {
      var retry = next === 'error' || (next === 'off' && !config);
      enableBtn.hidden = !(retry || next === 'off');
      enableBtn.textContent = retry ? 'Try again' : 'Enable notifications';
      enableBtn.disabled = busy;
    }
    if (testBtn) testBtn.hidden = next !== 'subscribed';
  }

  function isIOS() {
    var ua = navigator.userAgent || '';
    if (/iPad|iPhone|iPod/.test(ua)) return true;
    // iPadOS 13+ reports itself as a Mac; the touch points give it away.
    return navigator.platform === 'MacIntel' && (navigator.maxTouchPoints || 0) > 1;
  }

  function isStandalone() {
    if (navigator.standalone === true) return true;
    try {
      return !!(window.matchMedia && window.matchMedia('(display-mode: standalone)').matches);
    } catch (err) {
      return false;
    }
  }

  function supported() {
    return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  }

  function readJson(response) {
    if (!response || !response.ok) return Promise.resolve(null);
    return response.json().catch(function () {
      return null;
    });
  }

  function loadConfig() {
    return fetch(CONFIG_URL, {
      method: 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { Accept: 'application/json' }
    })
      .then(readJson)
      .catch(function () {
        return null;
      });
  }

  function adoptConfig(payload) {
    if (!payload || payload.enabled !== true) return false;
    // The backend names the VAPID public key ``vapid_public_key``; accept the
    // shorter ``public_key`` too so the page keeps working if that is renamed.
    var key = payload.vapid_public_key || payload.public_key;
    if (!key) return false;
    config = payload;
    config.public_key = key;
    return true;
  }

  function csrfToken() {
    return (config && config.csrf_token) || '';
  }

  function base64ToBytes(value) {
    var normalised = String(value || '').replace(/-/g, '+').replace(/_/g, '/');
    while (normalised.length % 4) normalised += '=';
    var raw = atob(normalised);
    var bytes = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
    return bytes;
  }

  function postJson(url, payload) {
    var body = payload || {};
    if (csrfToken() && !body.csrf_token) body.csrf_token = csrfToken();
    var headers = { Accept: 'application/json', 'Content-Type': 'application/json' };
    if (csrfToken()) headers['X-CSRF-Token'] = csrfToken();
    return fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: headers,
      body: JSON.stringify(body)
    }).catch(function () {
      return null;
    });
  }

  function ensureRegistration() {
    return navigator.serviceWorker
      .getRegistration(SW_SCOPE)
      .then(function (registration) {
        if (registration) return registration;
        return navigator.serviceWorker.register(SW_URL, { scope: SW_SCOPE });
      })
      .then(function (registration) {
        return navigator.serviceWorker.ready.then(function () {
          return registration;
        });
      });
  }

  /* The shared register step used both by a tap and by the load-time refresh. */
  function registerSubscription() {
    if (!config || !config.public_key) {
      return Promise.reject(new Error('push is not configured'));
    }
    var registration = null;
    return ensureRegistration()
      .then(function (value) {
        registration = value;
        return registration.pushManager.getSubscription();
      })
      .then(function (existing) {
        if (existing) return existing;
        return registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: base64ToBytes(config.public_key)
        });
      })
      .then(function (subscription) {
        var json = subscription.toJSON ? subscription.toJSON() : {};
        // Idempotent: the server keeps one row per endpoint, so sending the same
        // subscription again changes nothing.
        return postJson(SUBSCRIBE_URL, {
          endpoint: subscription.endpoint,
          keys: json.keys || {}
        }).then(function (response) {
          if (!response || !response.ok) throw new Error('subscribe failed');
          return subscription;
        });
      });
  }

  function enableNotifications(event) {
    if (event) event.preventDefault();
    if (busy) return;
    if (!supported()) {
      setState('unsupported', 'This browser cannot show notifications. Use Chrome, or add Sano Trailers to your Home Screen on iPhone or iPad.');
      return;
    }
    busy = true;
    setState('loading', 'Turning on notifications…');

    var permission = Notification.permission;
    var asking = permission === 'default'
      // Only ever inside this tap handler (iOS requires the gesture).
      ? Notification.requestPermission().catch(function () {
          return 'denied';
        })
      : Promise.resolve(permission);

    asking
      .then(function (result) {
        if (result !== 'granted') {
          throw new Error('permission not granted');
        }
        if (config) return null;
        return loadConfig().then(function (payload) {
          if (!adoptConfig(payload)) throw new Error('push is not configured');
          return null;
        });
      })
      .then(function () {
        return registerSubscription();
      })
      .then(function () {
        busy = false;
        setState('subscribed', 'This device will get a notification when a customer submits their details.');
      })
      .catch(function (err) {
        busy = false;
        var message = String((err && err.message) || '');
        if (message === 'permission not granted') {
          setState('denied', 'Notifications are blocked for this site. Allow them in your browser settings, then reload.');
        } else if (message === 'push is not configured') {
          setState('disabled', 'Notifications are not set up for this site yet.');
        } else {
          setState('error', 'Notifications could not be turned on. Check your connection and tap Try again.');
        }
      });
  }

  function sendTestNotification(event) {
    if (event) event.preventDefault();
    if (busy) return;
    busy = true;
    if (enableBtn) enableBtn.disabled = true;
    if (testBtn) testBtn.disabled = true;

    postJson(TEST_URL, {})
      .then(function (response) {
        if (response && response.ok) return true;
        throw new Error('Server push delivery failed');
      })
      .then(function () {
        busy = false;
        if (testBtn) testBtn.disabled = false;
        setState('subscribed', 'Test notification sent.');
      })
      .catch(function () {
        busy = false;
        if (testBtn) testBtn.disabled = false;
        setState('error', 'The test notification could not be shown.');
      });
  }

  function withTimeout(promise, ms) {
    return new Promise(function (resolve) {
      var timer = setTimeout(resolve, ms);
      Promise.resolve(promise).then(
        function (value) {
          clearTimeout(timer);
          resolve(value);
        },
        function () {
          clearTimeout(timer);
          resolve(null);
        }
      );
    });
  }

  /* Tell the server to forget this one endpoint, not every device. */
  function notifyServerUnsubscribe(endpoint) {
    if (!endpoint) return Promise.resolve(null);
    // JSON + the scoped CSRF header: the endpoint handlers read a JSON body and
    // app/services/csrf.py accepts the X-CSRF-Token header. ``keepalive`` lets the
    // request finish while the logout navigation starts, and the session is still
    // alive at this point, so the row is removed for this endpoint only.
    var headers = { Accept: 'application/json', 'Content-Type': 'application/json' };
    if (csrfToken()) headers['X-CSRF-Token'] = csrfToken();
    return fetch(UNSUBSCRIBE_URL, {
      method: 'POST',
      credentials: 'same-origin',
      keepalive: true,
      headers: headers,
      body: JSON.stringify({
        endpoint: endpoint,
        csrf_token: csrfToken()
      })
    }).catch(function () {
      /* the session is ending anyway; logout must never be blocked */
      return null;
    });
  }

  function leavePush(endpointField) {
    if (!supported()) return Promise.resolve();
    return navigator.serviceWorker
      .getRegistration(SW_SCOPE)
      .then(function (registration) {
        return registration ? registration.pushManager.getSubscription() : null;
      })
      .catch(function () {
        return null;
      })
      .then(function (subscription) {
        if (!subscription) return null;
        if (endpointField) endpointField.value = subscription.endpoint;
        // 1) the session-bound POST /push/unsubscribe, then 2) the provider.
        // Both are bounded so logging out never hangs on a slow network.
        return withTimeout(
          notifyServerUnsubscribe(subscription.endpoint),
          UNSUBSCRIBE_TIMEOUT_MS
        ).then(function () {
          return withTimeout(subscription.unsubscribe(), UNSUBSCRIBE_TIMEOUT_MS);
        });
      });
  }

  function onLogoutSubmit(event) {
    var form = event.target;
    if (!form || form.getAttribute('data-push-leaving') === '1') return;
    event.preventDefault();
    var endpointField = form.querySelector('input[name="push_endpoint"]');
    leavePush(endpointField).then(function () {
      form.setAttribute('data-push-leaving', '1');
      form.submit();
    });
  }

  function watchLogoutForms() {
    var forms = document.querySelectorAll('form[action$="/logout"]');
    for (var i = 0; i < forms.length; i += 1) {
      forms[i].addEventListener('submit', onLogoutSubmit);
    }
  }

  function init() {
    // Home-screen guidance first: on iOS in a browser tab the Push API is absent.
    if (isIOS() && !isStandalone()) {
      setState(
        'ios-install',
        'On iPhone or iPad: tap the Share button, choose “Add to Home Screen”, then open Sano Trailers from your Home Screen and tap Enable notifications.'
      );
      return;
    }
    if (!supported()) {
      setState('unsupported', 'This browser cannot show notifications. Use a recent Chrome, Edge or Safari.');
      return;
    }
    if (Notification.permission === 'denied') {
      setState('denied', 'Notifications are blocked for this site. Allow them in your browser settings, then reload.');
      return;
    }

    setState('loading', '');
    loadConfig()
      .then(function (payload) {
        if (!adoptConfig(payload)) {
          setState('disabled', 'Notifications are not set up for this site yet.');
          return null;
        }
        if (Notification.permission !== 'granted') {
          setState('off', 'Enable notifications to hear about new customer submissions on this device.');
          return null;
        }
        // Already allowed: re-register silently. Nothing is asked of the person
        // and a device that signed out and back in starts receiving again.
        return registerSubscription()
          .then(function () {
            setState('subscribed', 'This device will get a notification when a customer submits their details.');
          })
          .catch(function () {
            setState('error', 'Notifications could not be registered. Tap Try again.');
          });
      });
  }

  if (enableBtn) enableBtn.addEventListener('click', enableNotifications);
  if (testBtn) testBtn.addEventListener('click', sendTestNotification);
  watchLogoutForms();
  init();
})();
