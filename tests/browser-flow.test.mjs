// Focused product-flow regressions. Native bridge and fetch are fixtures;
// the real web/app.js runs unchanged, with no provider or microphone calls.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8')
  .replace(/^import[^\n]+\n/, '');
const flush = () => new Promise(setImmediate);

async function harness() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      id, hidden: false, disabled: false, open: false, dataset: {}, listeners: {}, value: '',
      addEventListener(kind, listener) { this.listeners[kind] = listener; },
      click() { this.listeners.click?.({}); },
      showModal() { this.open = true; },
      close() { this.open = false; },
      removeAttribute(name) { delete this[name]; },
    });
    return elements.get(id);
  }
  const messages = [], requests = [], timeouts = [];
  let networkAvailable = false;
  const sandbox = {
    URL, URLSearchParams, Blob, Uint8Array, Intl, Date, Math, Number, String,
    Object, Array, Promise, Error, console, setTimeout, clearTimeout, performance,
    AbortSignal: { timeout(ms) { timeouts.push(ms); return new AbortController().signal; } },
    location: { href: 'https://my-mediabank.kenigevents.ru/', protocol: 'https:', pathname: '/', search: '', hash: '' },
    navigator: { onLine: true },
    history: { replaceState() {} },
    document: { getElementById: element, addEventListener() {}, activeElement: null, hidden: false },
    crypto: { randomUUID: () => 'test-request' },
    fetch: async url => {
      requests.push(url);
      if (!networkAvailable) throw new Error('Network unavailable');
      return { ok: true, status: 200, json: async () => ({ authenticated: true, mira_available: false }) };
    },
    createLiveClient(options) {
      let generation = 0;
      return {
        sessionId: null, starting: false, playingCount: 0,
        get generation() { return generation; },
        stop({ reason }) { generation += 1; options.onState('off', { reason }); },
        start: async () => { throw new Error('No provider calls are permitted in these UI tests'); },
        input: async () => {},
      };
    },
  };
  sandbox.window = {
    MediaBankAndroid: { postMessage(raw) { messages.push(JSON.parse(raw)); } },
    addEventListener() {},
  };
  vm.createContext(sandbox);
  vm.runInContext(source + '\n;globalThis.test = { state, ui, requestPhoto, request };', sandbox);
  await flush();
  sandbox.test.state.voiceWanted = false;
  return {
    ...sandbox.test, native: sandbox.window.MyMediaBank, messages, requests, timeouts,
    restoreNetwork() { networkAvailable = true; },
  };
}

const photo = (index, total = 3) => ({
  id: `native-${index}`, index, total, dataUrl: 'data:image/jpeg;base64,/9j/',
  width: 2, height: 2, takenAt: 1,
});

test('photo permissions reselection accepts the native index-zero response', async () => {
  const h = await harness();
  h.requestPhoto(2);
  h.native.onPhoto(photo(2));
  h.ui.photoAccess.click();
  assert.equal(h.state.wantedIndex, 0);
  assert.equal(h.state.loadingPhoto, true);
  assert.equal(h.messages.at(-1).action, 'permissions');
  h.native.onPhoto(photo(0));
  assert.equal(h.state.photo.index, 0);
  assert.equal(h.state.loadingPhoto, false);
});

test('Next skips an unreadable middle photo and clears its predecessor assessment', async () => {
  const h = await harness();
  h.native.onPhoto(photo(0));
  h.requestPhoto(1);
  h.state.evaluation = { score: 8 };
  h.native.onNativeError({ code: 'photo_unavailable', index: 1 });
  assert.equal(h.state.photo, null);
  assert.equal(h.state.evaluation, null);
  assert.equal(h.ui.next.disabled, false);
  h.ui.next.click();
  assert.equal(h.messages.at(-1).action, 'photo');
  assert.equal(h.messages.at(-1).index, 2);
});

test('explicit Start recovers an existing cookie after a transient me request failure', async () => {
  const h = await harness();
  h.state.loadingPhoto = false;
  h.restoreNetwork();
  h.ui.miraToggle.click();
  await flush();
  assert.equal(h.state.authenticated, true);
  assert.equal(h.ui.loginDialog.open, false);
  assert.ok(h.requests.filter(url => url === '/api/me').length >= 2);
});

test('Live start request permits the backend 35-second readiness window', async () => {
  const h = await harness();
  h.restoreNetwork();
  await h.request('/api/live', { method: 'POST' });
  assert.equal(h.timeouts.at(-1), 40_000);
});
