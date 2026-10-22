/* Exercise static/sw.js in Node with a fake service-worker global scope.
 *
 * Usage: node tests/js/push_service_worker.mjs <path to sw.js>
 * Prints one JSON object; the pytest suite asserts on it. No browser needed, and
 * nothing here touches the network.
 */
import { readFileSync } from 'node:fs';
import { createContext, runInContext } from 'node:vm';

const sourcePath = process.argv[2];
const source = readFileSync(sourcePath, 'utf8');

const listeners = new Map();
const shown = [];
const opened = [];
let windows = [];

const scope = {
  location: { origin: 'https://sano.test' },
  addEventListener(type, fn) {
    listeners.set(type, fn);
  },
  skipWaiting() {},
  clients: {
    claim() {},
    async matchAll() {
      return windows;
    },
    async openWindow(url) {
      opened.push(url);
      return {};
    },
  },
  registration: {
    async showNotification(title, options) {
      shown.push({ title, options });
    },
  },
};

const context = createContext({
  self: scope,
  URL,
  Promise,
  console,
  setTimeout,
  clearTimeout,
  JSON,
  String,
  RegExp,
  Uint8Array,
});
runInContext(source, context);

function deliver(type, event) {
  const handler = listeners.get(type);
  if (typeof handler !== 'function') throw new Error(`no ${type} listener`);
  const pending = [];
  handler({ ...event, waitUntil: (promise) => pending.push(promise) });
  return Promise.all(pending);
}

async function show(payload) {
  const before = shown.length;
  await deliver('push', {
    data: { json: () => payload },
  });
  if (shown.length !== before + 1) throw new Error('expected exactly one notification');
  return shown[shown.length - 1];
}

async function clickNotification(dataUrl) {
  const openedBefore = opened.length;
  await deliver('notificationclick', {
    notification: { data: dataUrl === null ? {} : { url: dataUrl }, close() {} },
  });
  return opened.slice(openedBefore);
}

const results = {};

// 1. A customer submission notification.
const customer = await show({
  title: 'New customer',
  body: 'Thabo Mokoena submitted their details',
  customer_id: 12,
  tag: 'customer-12',
});
results.customer = {
  title: customer.title,
  body: customer.options.body,
  url: customer.options.data.url,
  icon: customer.options.icon,
  tag: customer.options.tag,
};

// 2. An empty payload still shows something usable and never opens a bad URL.
const empty = await show({});
results.empty = { title: empty.title, body: empty.options.body, url: empty.options.data.url };

// 3. A hostile / malformed customer id degrades to the app root.
const hostile = await show({ customer_id: '12/../../admin?x=1' });
results.hostileId = hostile.options.data.url;

// 4. Missing body/title types are tolerated.
const odd = await show({ title: { evil: true }, body: 42, customer_id: '7' });
results.oddTypes = {
  title: odd.title,
  body: odd.options.body,
  url: odd.options.data.url,
};

// 5. Clicking focuses the app already open and takes it to the customer.
let focused = false;
let navigated = null;
windows = [
  {
    url: 'https://sano.test/dashboard',
    async focus() {
      focused = true;
      return {};
    },
    async navigate(target) {
      navigated = target;
      return {};
    },
  },
];
const openedExisting = await clickNotification('/customers/4');
results.clickExisting = { focused, navigated, opened: openedExisting };

// 6. A window on another origin is left alone; a new same-origin window opens.
windows = [
  {
    url: 'https://evil.example/phish',
    async focus() {
      focused = 'cross-origin';
    },
    async navigate(target) {
      navigated = target;
    },
  },
];
focused = false;
navigated = null;
const openedForeign = await clickNotification('/customers/5');
results.clickForeign = { focused, navigated, opened: openedForeign };

// 7. A cross-origin notification target degrades to the app root.
windows = [];
const openedUnsafe = await clickNotification('https://evil.example/customers/6');
results.clickUnsafe = { opened: openedUnsafe };

// 8. No window at all: a new one opens on the customer screen.
const openedNew = await clickNotification('/customers/9');
results.clickNewWindow = { opened: openedNew };

// 9. A relative, scheme-less or backslash target never leaves the origin either.
const openedGarbage = await clickNotification('//evil.example/customers/10');
const openedBackslash = await clickNotification('/\\evil.example/customers/11');
results.clickGarbage = { opened: openedGarbage, backslash: openedBackslash };

results.listeners = Array.from(listeners.keys()).sort();
results.registrations = {
  install: typeof listeners.get('install'),
  activate: typeof listeners.get('activate'),
};

process.stdout.write(JSON.stringify(results));
