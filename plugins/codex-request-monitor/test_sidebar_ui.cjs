const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const url = 'codex://threads/00000000-0000-0000-0000-000000000001';

function host(openResult = {}) {
  const calls = [];
  let receive;
  const parent = {
    postMessage(message) {
      calls.push(message);
      if (!message.id) return;
      const result = message.method === 'tools/call' ? {_meta: {status: {threads: []}}}
        : message.method === 'ui/open-link' ? openResult : {};
      receive({source: parent, origin: 'https://host.test', data: {jsonrpc: '2.0', id: message.id, result}});
    },
  };
  const window = {parent, addEventListener: (_, handler) => { receive = handler; }};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'sidebar_ui.js'), 'utf8'), {
    window, setTimeout, clearTimeout,
  });
  return {window, calls, teardown: () => receive({source: parent, origin: 'https://host.test',
    data: {jsonrpc: '2.0', method: 'ui/resource-teardown', id: 99}})};
}

test('opening a chat uses host navigation without a model or archive call', async () => {
  const {window, calls} = host();
  await window.readLocalMonitorStatus();
  const before = calls.length;
  await window.openLocalMonitorLink(url);
  const navigation = calls.slice(before);
  assert.equal(navigation.length, 1);
  assert.equal(navigation[0].method, 'ui/open-link');
  assert.deepEqual(JSON.parse(JSON.stringify(navigation[0].params)), {url});
});

test('host refusal and disconnected widgets report failure', async () => {
  const {window, teardown} = host({isError: true});
  await window.readLocalMonitorStatus();
  await assert.rejects(window.openLocalMonitorLink(url), /Unable to open chat/);
  teardown();
  await assert.rejects(window.openLocalMonitorLink(url), /disconnected/);
});

test('only exact chat links are sent to the host', async () => {
  const {window, calls} = host();
  await window.readLocalMonitorStatus();
  const before = calls.length;
  for (const invalid of ['javascript:alert(1)', 'https://host.test', url + '?prompt=hello',
    'codex://threads/new', url + '/archive']) {
    await assert.rejects(window.openLocalMonitorLink(invalid), /Invalid chat link/);
  }
  assert.equal(calls.length, before);
});

test('a navigation failure remains visible when polling replaces the title row', async () => {
  const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
  const start = html.indexOf('function bindChatLink(');
  const end = html.indexOf('\nfunction ', start + 1);
  let rejectOpen;
  const oldError = {id: 'chat-error', hidden: true};
  const currentError = {id: 'chat-error', hidden: true};
  const bind = vm.runInNewContext(html.slice(start, end) + '; bindChatLink', {
    sidebar: true,
    window: {openLocalMonitorLink: () => new Promise((_, reject) => { rejectOpen = reject; })},
    document: {getElementById: () => currentError},
  });
  const link = {};
  bind(link, url.split('/').at(-1), oldError);
  let stopped = false;
  const opening = link.onclick({stopPropagation() { stopped = true; }, preventDefault() {}});
  rejectOpen(new Error('Host refused'));
  await opening;
  assert.equal(stopped, true);
  assert.equal(currentError.hidden, false);
});
