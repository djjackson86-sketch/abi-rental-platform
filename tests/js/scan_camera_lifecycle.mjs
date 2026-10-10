// Lifecycle regression harness for static/js/scan-camera.js
//
// Runs the real scanner module inside a Node `vm` sandbox with a tiny hand-rolled
// DOM (no jsdom, no network) and a controllable fake ZXing engine, so the parts
// that only show up on a real phone can be asserted deterministically:
//
//   * a payload is accepted exactly once (no double submit, no stale re-submit)
//   * a decode that would land after cancel/close submits nothing
//   * a cancelled getUserMedia is invalidated and its late stream is stopped
//   * pagehide tears the camera down
//   * an empty/whitespace payload is ignored and scanning continues
//   * a playback refusal offers the "Start preview" retry instead of failing
//   * a missing decoder / denied camera fall back to MANUAL entry (never a photo)
//
// Usage: node tests/js/scan_camera_lifecycle.mjs
// Exits non-zero on the first failed expectation.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const MODULE_PATH = resolve(HERE, '..', '..', 'static', 'js', 'scan-camera.js');
const SOURCE = readFileSync(MODULE_PATH, 'utf8');

let failures = 0;
let checks = 0;

function ok(condition, label) {
  checks += 1;
  if (condition) {
    console.log(`  ok   ${label}`);
  } else {
    failures += 1;
    console.log(`  FAIL ${label}`);
  }
}

function equal(actual, expected, label) {
  ok(actual === expected, `${label} (got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)})`);
}

// ── fake DOM ─────────────────────────────────────────────────────────────────

function makeElement(id) {
  const listeners = {};
  return {
    id,
    hidden: false,
    textContent: '',
    value: '',
    width: 0,
    height: 0,
    srcObject: null,
    muted: false,
    playsInline: false,
    videoWidth: 0,
    videoHeight: 0,
    attributes: {},
    children: [],
    classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, has(c) { return this._s.has(c); } },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    dispatch(type, event) { (listeners[type] || []).forEach((fn) => fn(event || { type })); },
    click() { this.dispatch('click', { type: 'click', preventDefault() {} }); },
    focus() {},
    scrollIntoView() {},
    setAttribute(name, value) { this.attributes[name] = value; },
    appendChild(child) { this.children.push(child); },
    getContext() { return { drawImage() {}, getImageData(x, y, w, h) { return { data: new Uint8ClampedArray(w * h * 4) }; } }; },
    play() { return this._playResult; },
    _playResult: undefined,
  };
}

// A sandbox runs the module once; each scenario gets a fresh one.
function makeEnv() {
  const ids = [
    'scan-camera-live', 'scan-camera-video', 'scan-camera-cancel',
    'scan-camera-playstart', 'scan-camera-manual', 'scan-camera-status',
    'scan-camera-blocked', 'scan-disc-text', 'scan-manual', 'scan-camera-start',
    'scan-capture-form',
  ];
  const elements = {};
  for (const id of ids) { elements[id] = makeElement(id); }
  elements['scan-camera-live'].hidden = true;
  elements['scan-camera-playstart'].hidden = true;
  elements['scan-camera-blocked'].hidden = true;
  elements['scan-camera-start'].hidden = false;

  const timers = [];
  let timerSeq = 1;
  const state = {
    submissions: 0,
    stopped: 0,
    getUserMediaCalls: 0,
    pendingMedia: null,
    decodeQueue: [],
    errors: [],
  };

  const form = elements['scan-capture-form'];
  form.submit = function () { state.submissions += 1; };

  const windowListeners = {};
  const sandboxDoc = {
    activeElement: null,
    body: makeElement('body'),
    getElementById: (id) => elements[id] || null,
    querySelector: (sel) => (sel === '.scan-capture-form' ? form : null),
    createElement: (tag) => makeElement(`created-${tag}`),
  };

  function makeZXing() {
    return {
      PDF417Reader: function () {
        this.decode = function () {
          const next = state.decodeQueue.length ? state.decodeQueue.shift() : { error: true };
          if (next.error) {
            const err = new Error('no barcode');
            err.name = 'NotFoundException';
            throw err;
          }
          return { getText: () => next.text };
        };
      },
      HTMLCanvasElementLuminanceSource: function () {},
      BinaryBitmap: function () {},
      HybridBinarizer: function () {},
      DecodeHintType: { TRY_HARDER: 3 },
    };
  }

  const navigator = {
    mediaDevices: {
      getUserMedia() {
        state.getUserMediaCalls += 1;
        return new Promise((resolve, reject) => { state.pendingMedia = { resolve, reject }; });
      },
    },
  };

  const sandbox = {
    console, Map, Set, Promise, Error, Math, Date, JSON, Object, Array, String, Number, RegExp,
    Uint8ClampedArray, Int32Array, isNaN, parseInt, parseFloat,
    navigator,
    document: sandboxDoc,
    ZXing: makeZXing(),
    setTimeout(fn, ms) { const id = timerSeq++; timers.push({ id, fn, ms }); return id; },
    clearTimeout(id) { const i = timers.findIndex((t) => t.id === id); if (i >= 0) { timers.splice(i, 1); } },
    addEventListener(type, fn) { (windowListeners[type] = windowListeners[type] || []).push(fn); },
    dispatchWindow(type) { (windowListeners[type] || []).forEach((fn) => fn({ type })); },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;

  const context = vm.createContext(sandbox);
  vm.runInContext(SOURCE, context, { filename: MODULE_PATH });

  return {
    window: sandbox,
    elements,
    state,
    timers,
    flushTimers() { const batch = timers.splice(0, timers.length); batch.forEach((t) => t.fn()); return batch.length; },
    pendingTimers() { return timers.length; },
    dispatchWindow: sandbox.dispatchWindow,
    AbiScanCamera: sandbox.window.AbiScanCamera,
  };
}

function fakeStream(state) {
  return { getTracks: () => [{ stop() { state.stopped += 1; } }] };
}

async function settle() {
  // let the module's getUserMedia/play promise chains run to completion
  for (let i = 0; i < 6; i += 1) { await new Promise((r) => setImmediate(r)); }
}

function startScanner(env) {
  env.AbiScanCamera.init({
    form: env.elements['scan-capture-form'],
    start: 'scan-camera-start',
    manual: 'scan-manual',
    readingMessage: 'Reading disk…',
  });
  env.elements['scan-camera-start'].click();
}

// ── scenarios ────────────────────────────────────────────────────────────────

console.log('scan-camera lifecycle');

// 1. A decoded payload is accepted exactly once and posts the form once.
{
  const env = makeEnv();
  env.state.decodeQueue = [{ text: 'MVL1 148 %DISC%PAYLOAD' }, { text: 'SHOULD NOT BE USED' }];
  startScanner(env);
  env.state.pendingMedia.resolve(fakeStream(env.state));
  await settle();
  env.elements['scan-camera-video'].videoWidth = 1280;
  env.elements['scan-camera-video'].videoHeight = 720;
  env.flushTimers();
  await settle();

  equal(env.state.submissions, 1, 'one valid payload posts the form exactly once');
  equal(env.elements['scan-disc-text'].value, 'MVL1 148 %DISC%PAYLOAD', 'decoded text lands in the hidden disc_text field');
  equal(env.elements['scan-camera-live'].hidden, true, 'overlay is closed after a decode');
  equal(env.state.stopped, 1, 'camera tracks are stopped after a decode');

  // A stale/late frame must not re-submit: force any remaining work to run.
  env.flushTimers();
  await settle();
  equal(env.state.submissions, 1, 'no second submit from a later frame');
  equal(env.state.decodeQueue.length, 1, 'decoding is disarmed after the first valid payload');
}

// 2. An empty/whitespace payload is ignored and scanning continues.
{
  const env = makeEnv();
  env.state.decodeQueue = [{ text: '   ' }, { error: true }, { text: 'REAL DISK TEXT 123' }];
  startScanner(env);
  env.state.pendingMedia.resolve(fakeStream(env.state));
  await settle();
  env.elements['scan-camera-video'].videoWidth = 640;
  env.elements['scan-camera-video'].videoHeight = 480;
  env.flushTimers();
  equal(env.state.submissions, 0, 'empty payload is not submitted');
  env.flushTimers();
  equal(env.state.submissions, 0, 'a not-found frame is not submitted');
  env.flushTimers();
  equal(env.state.submissions, 1, 'the next real payload is submitted once');
  equal(env.elements['scan-disc-text'].value, 'REAL DISK TEXT 123', 'the real payload is the one posted');
}

// 3. Cancelling invalidates the in-flight camera request and stops the late stream.
{
  const env = makeEnv();
  startScanner(env);
  env.elements['scan-camera-cancel'].click();       // cancel while permission is pending
  env.state.pendingMedia.resolve(fakeStream(env.state));
  await settle();
  equal(env.elements['scan-camera-video'].srcObject, null, 'a stream that arrives after cancel is never attached');
  equal(env.state.stopped, 1, 'the late stream is stopped (camera light off)');
  equal(env.state.submissions, 0, 'cancelling never submits');
  equal(env.pendingTimers(), 0, 'cancelling clears any pending decode timer');
}

// 4. A decode timer that is pending when the scanner closes never fires/submits.
{
  const env = makeEnv();
  env.state.decodeQueue = [{ error: true }, { text: 'LATE' }];
  startScanner(env);
  env.state.pendingMedia.resolve(fakeStream(env.state));
  await settle();
  env.elements['scan-camera-video'].videoWidth = 1280;
  env.elements['scan-camera-video'].videoHeight = 720;
  env.flushTimers();                                  // first tick schedules the next
  env.elements['scan-camera-cancel'].click();         // close before the next tick
  env.flushTimers();
  await settle();
  equal(env.state.submissions, 0, 'a tick pending at close does not submit');
}

// 5. pagehide tears the camera down.
{
  const env = makeEnv();
  startScanner(env);
  env.state.pendingMedia.resolve(fakeStream(env.state));
  await settle();
  env.dispatchWindow('pagehide');
  equal(env.elements['scan-camera-live'].hidden, true, 'pagehide hides the overlay');
  equal(env.state.stopped, 1, 'pagehide stops the camera tracks');
  equal(env.elements['scan-camera-start'].hidden, false, 'pagehide restores the Scan button');
}

// 6. A playback refusal offers the visible retry rather than failing the camera.
{
  const env = makeEnv();
  const video = env.elements['scan-camera-video'];
  video._playResult = Promise.reject(new Error('NotAllowedError'));
  startScanner(env);
  env.state.pendingMedia.resolve(fakeStream(env.state));
  await settle();
  equal(env.elements['scan-camera-playstart'].hidden, false, 'a refused play reveals "Start preview"');

  video._playResult = Promise.resolve();
  env.elements['scan-camera-playstart'].click();
  await settle();
  equal(env.elements['scan-camera-playstart'].hidden, true, 'the retry hides once play succeeds');
  ok(env.pendingTimers() >= 1, 'the retry starts the decode cadence');
}

// 7. A denied camera permission falls back to MANUAL entry, never a photo.
{
  const env = makeEnv();
  let errorKind = null;
  env.AbiScanCamera.init({
    form: env.elements['scan-capture-form'],
    start: 'scan-camera-start',
    manual: 'scan-manual',
    onError(msg, kind) { errorKind = kind; },
  });
  env.elements['scan-camera-start'].click();
  const err = new Error('denied');
  err.name = 'NotAllowedError';
  env.state.pendingMedia.reject(err);
  await settle();
  equal(env.elements['scan-camera-live'].hidden, true, 'a denied camera closes the overlay');
  equal(env.elements['scan-camera-blocked'].hidden, false, 'a denied camera shows the honest blocked banner');
  equal(errorKind, 'denied', 'the page is told why so it can reveal manual entry');
  equal(env.state.submissions, 0, 'a denied camera never submits');
}

// 8. A missing decoder falls back to manual entry instead of dying silently.
{
  const env = makeEnv();
  env.window.ZXing = undefined;
  let errorKind = null;
  env.AbiScanCamera.init({
    form: env.elements['scan-capture-form'],
    start: 'scan-camera-start',
    manual: 'scan-manual',
    onError(msg, kind) { errorKind = kind; },
  });
  env.elements['scan-camera-start'].click();
  await settle();
  equal(env.elements['scan-camera-live'].hidden, true, 'a missing decoder closes the overlay');
  equal(errorKind, 'decoder', 'the page is told the decoder is unavailable');
  equal(env.state.getUserMediaCalls, 0, 'a missing decoder never opens the camera');
}

// 9. The overlay's manual button routes to the page's own manual action.
{
  const env = makeEnv();
  let manualClicks = 0;
  env.elements['scan-manual'].addEventListener('click', () => { manualClicks += 1; });
  startScanner(env);
  env.elements['scan-camera-manual'].click();
  await settle();
  equal(manualClicks, 1, 'the overlay manual button triggers the page manual action');
  equal(env.elements['scan-camera-live'].hidden, true, 'the overlay is closed before manual entry opens');
}

console.log(`\n${checks - failures}/${checks} checks passed`);
if (failures > 0) {
  console.error(`${failures} lifecycle check(s) failed`);
  process.exitCode = 1;
}
