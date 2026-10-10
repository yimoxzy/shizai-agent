const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const workerSource = fs.readFileSync(require.resolve('../service_worker.js'), 'utf8');
const ownerA = 'a'.repeat(64);
const ownerB = 'b'.repeat(64);

// Run the real service worker against a Chrome API fixture. No browser I/O.
function browserFixture() {
  const data = {};
  const tabs = new Map([[10, {id: 10, windowId: 1, url: 'https://example.test', active: true}]]);
  const removed = [];
  let id = 11;
  const event = {addListener() {}};
  const chrome = {
    runtime: {onMessage: event, onStartup: event, onInstalled: event},
    alarms: {create() {}, onAlarm: event},
    storage: {session: {
      async get(key) { return {[key]: data[key]}; },
      async set(values) { Object.assign(data, values); },
      async remove(key) { delete data[key]; },
    }},
    tabs: {
      async get(tabId) {
        if (!tabs.has(tabId)) throw new Error('No tab with id: ' + tabId);
        return {...tabs.get(tabId)};
      },
      async create(options) {
        const tab = {id: id++, windowId: 1, ...options};
        tabs.set(tab.id, tab);
        return {...tab};
      },
      async update(tabId, changes) {
        const tab = await this.get(tabId);
        Object.assign(tab, changes);
        tabs.set(tabId, tab);
        return {...tab};
      },
      async remove(tabId) { await this.get(tabId); tabs.delete(tabId); removed.push(tabId); },
    },
    windows: {async get() { return {state: 'normal'}; }, async update() {}},
  };
  function worker() {
    const context = vm.createContext({chrome, URL});
    vm.runInContext(workerSource, context);
    return (method, ownerKey, url = 'https://example.test') => context.dispatch({
      protocolVersion: '1.0', requestId: 'fixture', method, params: {ownerKey, url},
    }, {});
  }
  return {worker, tabs, removed};
}

test('concurrent opens reuse one owned tab, preserving a same-URL user tab', async () => {
  const f = browserFixture(), call = f.worker();
  const results = await Promise.all(Array.from({length: 6}, () => call('open_design_tab', ownerA)));
  assert.ok(results.every(r => r.ok));
  assert.equal(new Set(results.map(r => r.result.tabId)).size, 1);
  assert.equal(f.tabs.size, 2);
  assert.ok(f.tabs.has(10));
});

test('reopening preserves search state and ownership survives worker suspension', async () => {
  const f = browserFixture();
  const opened = await f.worker()('open_design_tab', ownerA);
  const tab = f.tabs.get(opened.result.tabId);
  tab.url = 'https://example.test/search?q=phone';
  const reused = await f.worker()('open_design_tab', ownerA);
  assert.equal(reused.result.reused, true);
  assert.equal(reused.result.url, tab.url);
  assert.equal(f.tabs.size, 2);
});

test('closing removes only its own project tab, even if user tab has same URL', async () => {
  const f = browserFixture(), call = f.worker();
  const a = await call('open_design_tab', ownerA);
  const b = await call('open_design_tab', ownerB);
  assert.equal((await call('close_design_tab', ownerA)).result.status, 'closed');
  assert.equal((await call('close_design_tab', ownerA)).result.status, 'already_closed');
  assert.deepEqual(f.removed, [a.result.tabId]);
  assert.ok(f.tabs.has(b.result.tabId));
  assert.ok(f.tabs.has(10));
});

test('a manually closed owned tab is recreated once on the next request', async () => {
  const f = browserFixture(), call = f.worker();
  const a = await call('open_design_tab', ownerA);
  f.tabs.delete(a.result.tabId);
  const b = await call('open_design_tab', ownerA);
  assert.notEqual(a.result.tabId, b.result.tabId);
  assert.equal(f.tabs.size, 2);
});

test('invalid owners and non-web URLs cause no tab creation or closing', async () => {
  const f = browserFixture(), call = f.worker();
  assert.equal((await call('close_design_tab', '10')).ok, false);
  assert.equal((await call('open_design_tab', ownerA, 'javascript:alert(1)')).ok, false);
  assert.equal(f.tabs.size, 1);
  assert.equal(f.removed.length, 0);
});
