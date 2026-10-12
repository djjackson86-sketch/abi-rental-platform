// Regression harness for the scan screen's "Allocate to client" typeahead.
//
// The script under test is the inline <script> at the bottom of
// templates/admin/scan_vehicle.html (the one that drives #scan-customer-pick).
// It is extracted from the template and run in a Node `vm` sandbox with a tiny
// hand-rolled DOM and a controllable fake `fetch`, so the parts a server-side test
// cannot see are asserted deterministically:
//
//   * a client PICKED out of the typeahead keeps its id, even though the picker's own
//     debounced refetch runs for the label just filled in — that label ("Name · 0748848822")
//     matches no name or phone, so the search answers with an empty list, and re-resolving
//     the unchanged box against that empty list used to blank the hidden id and refuse the
//     save of a client whose name (or number) is shared;
//   * a picked client's explicit id survives a later refetch that returns the SAME label;
//   * ambiguous TYPED text (a label two clients share, a shared bare name, a number on file
//     for two clients) never resolves to "first in the list" — no id is written at all,
//     because the save trusts the hidden id;
//   * a keystroke is never a selection: a UNIQUE PREFIX typed into the box selects nobody and
//     is not rewritten, so the picker cannot quietly allocate the vehicle to a client whose
//     name merely starts with what is being typed — resolution happens on the explicit
//     pick/change, or once at save time;
//   * editing the box away from a picked client drops that client (no stale id rides along);
//   * a genuinely unique typed name or number still resolves (no over-refusal).
//
// Usage: node tests/js/scan_customer_picker.mjs [path/to/scan_vehicle.html]
// Exits non-zero on the first failed expectation.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const TEMPLATE = process.argv[2]
  ? resolve(process.argv[2])
  : resolve(HERE, '..', '..', 'templates', 'admin', 'scan_vehicle.html');

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

// ── the script under test, straight out of the template ──────────────────────

function pickerSource() {
  const html = readFileSync(TEMPLATE, 'utf8');
  const blocks = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
  const source = blocks.find((block) => block.includes('scan-customer-pick'));
  if (!source) {
    throw new Error(`the client-picker script is not in ${TEMPLATE}`);
  }
  // Jinja renders this to the real endpoint; the sandbox needs the same string.
  return source.replace('{{ url_for("vehicles.customer_search") }}', '/api/customers/search');
}

// ── fake DOM + fake fetch ────────────────────────────────────────────────────

function makeElement(id) {
  const listeners = {};
  let value = '';
  return {
    id,
    // A real input stringifies whatever is assigned to it; the picker writes raw ids from JSON.
    get value() { return value; },
    set value(next) { value = next === undefined || next === null ? '' : String(next); },
    innerHTML: '',
    children: [],
    form: null,
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    dispatch(type, event) { (listeners[type] || []).forEach((fn) => fn(event || { type })); },
    appendChild(child) { this.children.push(child); },
  };
}

function makeEnv(responses) {
  const pick = makeElement('scan-customer-pick');
  const list = makeElement('scan-customer-options');
  const hidden = makeElement('scan-customer-id');
  const elements = {
    'scan-customer-pick': pick,
    'scan-customer-options': list,
    'scan-customer-id': hidden,
  };
  const formListeners = {};
  const submitted = [];
  const form = {
    addEventListener(type, fn) { (formListeners[type] = formListeners[type] || []).push(fn); },
    submit() { submitted.push({ hidden: hidden.value, pick: pick.value }); },
  };
  pick.form = form;

  const queue = [...responses];
  const urls = [];
  const timers = new Map();
  let nextTimer = 1;

  const sandbox = {
    document: {
      getElementById: (id) => elements[id] || null,
      createElement: () => ({ value: '' }),
    },
    fetch(url) {
      urls.push(url);
      const body = queue.length ? queue.shift() : { customers: [] };
      return Promise.resolve({ json: () => Promise.resolve(body) });
    },
    setTimeout(fn) { const id = nextTimer++; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); },
    encodeURIComponent,
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(pickerSource(), sandbox, { filename: TEMPLATE });

  return {
    pick, list, hidden, urls, formListeners, submitted,
    timers,
    // The debounced search that would land 200 ms after a keystroke / pick.
    async settle() {
      for (const [id, fn] of [...timers]) { timers.delete(id); fn(); }
      await flush();
    },
  };
}

async function flush() {
  for (let i = 0; i < 6; i += 1) {
    await new Promise((done) => setImmediate(done));
  }
}

// Staff typing / picking.
function type(env, text) { env.pick.value = text; env.pick.dispatch('input'); }
function choose(env, label) {
  env.pick.value = label;
  env.pick.dispatch('input');
  env.pick.dispatch('change');
}
function submit(env) {
  let prevented = false;
  for (const fn of env.formListeners.submit || []) { fn({ preventDefault() { prevented = true; } }); }
  return prevented;
}

// ── the clients ──────────────────────────────────────────────────────────────
// The live cluster: one client whose number is on file in two formats (identical digits),
// a third Sandile Nkosi with a different number, and one unique client.

const SAME_NUMBER_TWO_FORMATS = [
  { id: 2312, name: 'Sandile Nkosi', phone: '+27748848822' },
  { id: 3060, name: 'Sandile Nkosi', phone: '0748848822' },
  { id: 2271, name: 'Sandile Nkosi', phone: '+2764187062' },
];
const PICKED_LABEL = 'Sandile Nkosi · 0748848822';

// ── scenarios ────────────────────────────────────────────────────────────────

async function thePickedClientSurvivesTheRefetchOfItsOwnLabel() {
  console.log('a picked client keeps its id when the refetch for its own label answers with nothing');
  // 1st search (the name being typed) returns the cluster; 2nd search is for the full label
  // the pick just wrote into the box — the real endpoint matches nothing and answers [].
  const env = makeEnv([{ customers: SAME_NUMBER_TWO_FORMATS }, { customers: [] }]);
  type(env, 'Sandile Nko');
  await env.settle();
  ok(env.list.children.length >= 3, 'the typeahead offered the matching clients');

  choose(env, PICKED_LABEL);
  equal(env.hidden.value, '3060', 'picking that label resolves to that one client');

  await env.settle();
  equal(env.hidden.value, '3060', 'the late empty refetch did not throw the client away');

  const prevented = submit(env);
  await flush();
  equal(env.hidden.value, '3060', 'the id posted by the save is still the picked client');
  ok(!prevented, 'the save was not held back for another lookup');
  equal(env.submitted.length, 0, 'no extra submit was forced — the form posts its resolved id itself');
}

async function aPickedIdSurvivesALaterIdenticalLabel() {
  console.log('an explicit pick survives a later refetch that returns an identical label');
  const duplicated = [
    { id: 3060, name: 'Sandile Nkosi', phone: '0748848822' },
    { id: 9999, name: 'Sandile Nkosi', phone: '0748848822' },   // renders the SAME label
  ];
  const env = makeEnv([{ customers: SAME_NUMBER_TWO_FORMATS }, { customers: duplicated }]);
  type(env, 'Sandile Nko');
  await env.settle();
  choose(env, PICKED_LABEL);
  equal(env.hidden.value, '3060', 'the picked client is resolved');
  await env.settle();
  equal(env.hidden.value, '3060', 'an identical label does not overwrite the picked id');

  submit(env);
  await flush();
  equal(env.hidden.value, '3060', 'the picked client is still the one saved');
}

async function anIdenticalLabelTypedByHandIsNeverGuessed() {
  console.log('a label two clients share is refused when it is typed, never resolved to the first');
  const duplicated = [
    { id: 3060, name: 'Sandile Nkosi', phone: '0748848822' },
    { id: 9999, name: 'Sandile Nkosi', phone: '0748848822' },
  ];
  const env = makeEnv([{ customers: duplicated }, { customers: duplicated }]);
  type(env, 'Sandile Nkosi');
  await env.settle();
  type(env, PICKED_LABEL);
  equal(env.hidden.value, '', 'no id is written for a label that identifies two clients');
  ok(env.hidden.value !== '3060', 'the first of the ambiguous clients was not chosen');

  submit(env);
  await flush();
  equal(env.submitted[0].hidden, '', 'the save posted no client id (the server will refuse the name)');
}

async function aSharedNameTypedByHandIsNeverGuessed() {
  console.log('a name two clients share is refused when it is typed, never resolved to the first');
  const env = makeEnv([{ customers: SAME_NUMBER_TWO_FORMATS }]);
  type(env, 'Sandile');
  await env.settle();
  type(env, 'Sandile Nkosi');
  equal(env.hidden.value, '', 'a shared bare name writes no id');
  ok(env.hidden.value !== '2312', 'the first of the shared-name clients was not chosen');
}

async function aNumberOnFileForTwoClientsTypedByHandIsNeverGuessed() {
  console.log('a number two clients share (stored in two formats) is refused when typed');
  const env = makeEnv([{ customers: SAME_NUMBER_TWO_FORMATS }]);
  type(env, '074');
  await env.settle();
  type(env, '0748848822');
  equal(env.hidden.value, '', 'the digits alone write no id');
}

async function aUniqueTypedClientStillResolves() {
  console.log('a unique client still resolves from a typed name or number (no over-refusal)');
  const env = makeEnv([{ customers: [{ id: 4, name: 'Charmaine Mokoena', phone: '+27821234567' }] }]);
  type(env, 'Charmaine');
  await env.settle();
  type(env, 'Charmaine Mokoena');
  equal(env.hidden.value, '4', 'the only client with that name is resolved');
  equal(env.pick.value, 'Charmaine Mokoena · +27821234567', 'and the box is rewritten to their label');

  const byNumber = makeEnv([{ customers: [{ id: 7, name: 'Pieter van Wyk', phone: '0821234567' }] }]);
  type(byNumber, '082');
  await byNumber.settle();
  type(byNumber, '0821234567');
  equal(byNumber.hidden.value, '7', 'the only client with that number is resolved');
}

async function aUniquePrefixIsNeverSelectedWhileTyping() {
  console.log('a unique prefix is NOT selected before the name is finished');
  const clients = [{ id: 11, name: 'Charmaine Mokoena', phone: '+27821234567' }];
  const env = makeEnv([{ customers: clients }, { customers: clients }]);
  type(env, 'Cha');
  await env.settle();
  type(env, 'Charm');
  equal(env.hidden.value, '', 'a partial name writes NO id while the name is still being typed');
  equal(env.pick.value, 'Charm', 'and the box is left exactly as it was typed');
  await env.settle();
  type(env, 'Charmaine');
  equal(env.hidden.value, '', 'a longer unique prefix is still not a selection');
  equal(env.pick.value, 'Charmaine', 'the box still holds what was typed');
  await env.settle();
  type(env, 'Charmaine Moke');
  equal(env.hidden.value, '', 'and neither is a longer partial');

  choose(env, 'Charmaine Mokoena · +27821234567');       // the explicit pick
  equal(env.hidden.value, '11', 'picking the client from the list still resolves');
  equal(env.pick.value, 'Charmaine Mokoena · +27821234567', 'and the picked label stays in the box');
}

async function aUniquePrefixStillResolvesWhenTheFormIsSaved() {
  console.log('a unique prefix resolves once, at save time (not while typing)');
  const clients = [{ id: 4, name: 'Charmaine Mokoena', phone: '+27821234567' }];
  const env = makeEnv([{ customers: clients }]);
  type(env, 'Charmaine');
  await env.settle();
  equal(env.hidden.value, '', 'nothing is selected while typing');
  const prevented = submit(env);
  await flush();
  ok(!prevented, 'the save was not held back for another lookup');
  equal(env.hidden.value, '4', 'the typed prefix resolves only when the form is actually saved');
  equal(env.submitted.length, 0, 'the form posts its own resolved id');
}

async function typingAwayFromAPickedClientDropsIt() {
  console.log('editing the box away from a picked client drops its id');
  const clients = [{ id: 5, name: 'Charmaine Mokoena', phone: '+27821234567' }];
  const env = makeEnv([{ customers: clients }, { customers: clients }]);
  type(env, 'Cha');
  await env.settle();
  choose(env, 'Charmaine Mokoena · +27821234567');
  equal(env.hidden.value, '5', 'the pick resolved');
  type(env, 'Charmaine Mokoena · +27821234567x');
  equal(env.hidden.value, '', 'a stale id cannot ride along with edited text');
}

// ── run ──────────────────────────────────────────────────────────────────────

const scenarios = [
  thePickedClientSurvivesTheRefetchOfItsOwnLabel,
  aPickedIdSurvivesALaterIdenticalLabel,
  anIdenticalLabelTypedByHandIsNeverGuessed,
  aSharedNameTypedByHandIsNeverGuessed,
  aNumberOnFileForTwoClientsTypedByHandIsNeverGuessed,
  aUniqueTypedClientStillResolves,
  aUniquePrefixIsNeverSelectedWhileTyping,
  aUniquePrefixStillResolvesWhenTheFormIsSaved,
  typingAwayFromAPickedClientDropsIt,
];

for (const scenario of scenarios) {
  await scenario();
}

console.log(`\n${checks - failures}/${checks} checks passed`);
if (failures) {
  console.log(`${failures} FAILED`);
  process.exit(1);
}
