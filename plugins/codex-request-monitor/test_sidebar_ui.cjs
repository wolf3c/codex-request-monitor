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
      const result = message.method === 'tools/call' ? message.params.name === 'read_network_diagnostics'
        ? {_meta: {network: {codex: {status: 'unconfirmed'}}}}
        : message.params.name === 'open_temporary_proxy_page' ? {_meta: {external_opened: true}}
        : {_meta: {status: {threads: []}}}
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

test('network inspection initializes independently and calls only its private tool', async () => {
  const {window, calls} = host();
  const result = await window.readLocalMonitorNetwork();
  assert.equal(result.codex.status, 'unconfirmed');
  const tools = calls.filter(call => call.method === 'tools/call');
  assert.equal(tools.length, 1);
  assert.equal(tools[0].params.name, 'read_network_diagnostics');
  assert.deepEqual(JSON.parse(JSON.stringify(tools[0].params.arguments)), {});
});

test('temporary proxy entry opens only the external page, never a restart tool', async () => {
  const {window, calls} = host();
  assert.equal(typeof window.openTemporaryProxyPage, 'function');
  await window.openTemporaryProxyPage();
  const tools = calls.filter(call => call.method === 'tools/call');
  assert.equal(tools.length, 1);
  assert.equal(tools[0].params.name, 'open_temporary_proxy_page');
  assert.deepEqual(JSON.parse(JSON.stringify(tools[0].params.arguments)), {});
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

test('proxy summary uses only observed route evidence and the three approved messages', () => {
  const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
  const start = html.indexOf('function proxyRouteStatus(');
  const end = html.indexOf('\nfunction ', start + 1);
  const describe = vm.runInNewContext(html.slice(start, end) + '; proxyRouteStatus');
  assert.equal(describe({route: {status: 'proxied'}}), '代理已生效，无需操作。');
  assert.equal(describe({route: {status: 'direct'}}), '代理未生效，建议开启临时代理。');
  assert.equal(describe({route: {status: 'unconfirmed'}}), '待确认');
  assert.equal(describe({codex: {status: 'configured'}, tun: {status: 'detected'}}), '待确认');
  assert.equal(describe(null), '待确认');
});

test('alert filtering stays active independently of system notifications', async () => {
  const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {dataset: {}, classList: {toggle() {}}, setAttribute(name, value) { this[name] = value; }});
    return nodes.get(id);
  };
  const data = {
    now: 1000, max_event_id: 2,
    threads: [{id: 'first', title: 'First', group: 'chat', transport_time: 990, transport: 'HTTP'},
      {id: 'second', title: 'Second', group: 'other', transport_time: 995, transport: 'WebSocket'},
      {id: 'third', title: 'Third', group: 'chat', transport_time: 600, transport: 'WebSocket'}],
    sizes: [{id: 1, thread: 'first', time: 990, compressed: 20 * 1048576}],
    nonincremental_events: [{id: 2, thread: 'second', time: 995}, {id: 0, thread: 'third', time: 600}],
  };
  let shown, sent = 0, throwNotification = false;
  function Notification() {
    if (throwNotification) throw Error('Notification unavailable');
    sent++;
  }
  Notification.permission = 'granted';
  const context = vm.createContext({
    $: node, threshold: {value: 10}, recentWindow: 300, group: 'chat', alertsOnly: false,
    initialized: false, lastId: 0, latest: data, locale: 'zh-CN', notificationsSupported: true,
    readFailed: false, refreshing: false, readStatus: async () => data,
    notificationsEnabled: false, Notification, notificationMessage: '',
    setNotificationPreference: enabled => { context.notificationsEnabled = enabled; },
    t: (text, ...args) => text.replace(/\{(\d+)\}/g, (_, i) => args[i]),
    sessionTitle: thread => thread.title, mib: n => (n / 1048576).toFixed(2) + ' MiB',
    renderSessions: entries => { shown = Array.from(entries, entry => entry.id); },
  });
  const renderStart = html.indexOf('function render(d)');
  vm.runInContext(html.slice(renderStart, html.indexOf('\nasync function readStatus', renderStart)), context);
  const readState = html.indexOf('function renderReadState()');
  vm.runInContext(html.slice(readState, html.indexOf('\nfunction selectGroup', readState)), context);
  const click = html.split('\n').find(line => line.startsWith("$('filterAlerts').onclick"));
  vm.runInContext(click, context);
  vm.runInContext('render(latest)', context);
  assert.match(node('filterAlerts').textContent, /近 5 分钟.*2 个会话提醒/);
  assert.equal(node('filterAlerts').disabled, false);
  assert.deepEqual(shown, ['first', 'third']);
  node('filterAlerts').onclick();
  assert.deepEqual(shown, ['first', 'second']);
  assert.equal(node('filterAlerts')['aria-pressed'], 'true');
  node('filterAlerts').onclick();
  assert.deepEqual(shown, ['first', 'third']);
  const fresh = id => {
    data.max_event_id = id;
    data.sizes.push({...data.sizes[0], id});
    vm.runInContext('render(latest)', context);
  };
  fresh(3);
  assert.equal(sent, 0);
  assert.match(node('filterAlerts').textContent, /2 个会话提醒/);
  context.notificationsEnabled = true;
  fresh(4);
  assert.equal(sent, 1);
  context.notificationsEnabled = false;
  fresh(5);
  assert.equal(sent, 1);
  context.notificationsEnabled = true;
  throwNotification = true;
  fresh(6);
  assert.equal(context.notificationsEnabled, false);
  assert.match(node('filterAlerts').textContent, /2 个会话提醒/);
  context.readFailed = true;
  vm.runInContext('render(latest)', context);
  assert.equal(node('filterAlerts').disabled, true);
  assert.equal(node('filterAlerts').textContent, '日志读取失败');
  await vm.runInContext('refresh()', context);
  assert.equal(node('filterAlerts').disabled, false);
  data.now = 1400;
  vm.runInContext('alertsOnly = true; render(latest)', context);
  assert.equal(node('filterAlerts').disabled, true);
  assert.equal(node('filterAlerts')['aria-pressed'], 'false');
  assert.deepEqual(shown, ['first', 'third']);
});

test('session detail byte formatting remains available after compacting the overview', () => {
  const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
  const start = html.indexOf('function byteSize(');
  const end = html.indexOf('\nfunction ', start + 1);
  const format = vm.runInNewContext(html.slice(start, end) + '; byteSize', {
    mib: n => (n / 1048576).toFixed(2) + ' MiB',
  });
  assert.equal(format(512), '512 B');
  assert.equal(format(1536), '1.50 KiB');
  assert.equal(format(2097152), '2.00 MiB');
});

function notificationToggleFixture({permission = 'granted', saved, supported = true, storageFails = false} = {}) {
  const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
  const values = new Map(saved ? [['codex-request-monitor.notifications', saved]] : []);
  const button = {setAttribute(name, value) { this[name] = value; }};
  let requests = 0;
  const listeners = new Map();
  const Notification = {permission, requestPermission: async () => {
    requests++;
    if (Notification.permission !== 'denied') Notification.permission = 'granted';
    return Notification.permission;
  }};
  const context = vm.createContext({notificationsSupported: supported, Notification,
    window: {addEventListener: (name, callback) => listeners.set(name, callback)},
    $: () => button, t: text => text,
    localStorage: {getItem: key => values.get(key) ?? null, setItem(key, value) {
      if (storageFails) throw Error('Storage unavailable');
      values.set(key, value);
    }},
  });
  const start = html.indexOf('const notificationKey =');
  const end = html.indexOf('function renderReadState()', start);
  vm.runInContext(html.slice(start, end) + '; renderNotificationToggle();', context);
  return {context, button, values, listeners, requests: () => requests,
    toggle: () => vm.runInContext('toggleSystemNotifications()', context)};
}

test('notification switch remembers off/on and reuses an existing browser grant', async () => {
  const fixture = notificationToggleFixture();
  assert.equal(fixture.button.textContent, '系统通知：开');
  await fixture.toggle();
  assert.equal(fixture.button.textContent, '系统通知：关');
  assert.equal(fixture.button['aria-checked'], 'false');
  assert.equal(fixture.values.get('codex-request-monitor.notifications'), 'off');
  const reloaded = notificationToggleFixture({saved: 'off'});
  assert.equal(reloaded.button.textContent, '系统通知：关');
  await reloaded.toggle();
  assert.equal(reloaded.button['aria-checked'], 'true');
  assert.equal(reloaded.values.get('codex-request-monitor.notifications'), 'on');
  assert.equal(reloaded.requests(), 0);
});

test('notification permission is requested only when switching on; failures stay off', async () => {
  const fresh = notificationToggleFixture({permission: 'default'});
  assert.equal(fresh.requests(), 0);
  await Promise.all([fresh.toggle(), fresh.toggle()]);
  assert.equal(fresh.requests(), 1);
  assert.equal(fresh.button['aria-checked'], 'true');
  const denied = notificationToggleFixture({permission: 'denied'});
  await denied.toggle();
  assert.equal(denied.button['aria-checked'], 'false');
  assert.match(denied.button.title, /拒绝/);
  const unsupported = notificationToggleFixture({supported: false});
  assert.equal(unsupported.button.disabled, true);
  await unsupported.toggle();
  assert.equal(unsupported.requests(), 0);
  const privateMode = notificationToggleFixture({storageFails: true});
  await privateMode.toggle();
  assert.equal(privateMode.button['aria-checked'], 'false');
  assert.match(privateMode.button.title, /保存/);
});

test('turning notifications off in another page updates the current page without requesting permission', () => {
  const fixture = notificationToggleFixture();
  const storage = fixture.listeners.get('storage');
  assert.equal(typeof storage, 'function');
  storage({key: 'codex-request-monitor.language', newValue: 'off'});
  assert.equal(fixture.button['aria-checked'], 'true');
  fixture.values.set('codex-request-monitor.notifications', 'off');
  storage({key: 'codex-request-monitor.notifications', newValue: 'off'});
  assert.equal(fixture.button['aria-checked'], 'false');
  assert.equal(vm.runInContext('notificationsEnabled', fixture.context), false);
  storage({key: 'codex-request-monitor.notifications', newValue: 'on'});
  assert.equal(fixture.button['aria-checked'], 'true');
  assert.equal(fixture.requests(), 0);
  assert.equal(fixture.values.get('codex-request-monitor.notifications'), 'off');
});

test('a pending permission result cannot overwrite a newer choice from another page', async () => {
  const fixture = notificationToggleFixture({permission: 'default'});
  let grant;
  fixture.context.Notification.requestPermission = () => new Promise(resolve => { grant = resolve; });
  const pending = fixture.toggle();
  fixture.values.set('codex-request-monitor.notifications', 'off');
  fixture.listeners.get('storage')({key: 'codex-request-monitor.notifications', newValue: 'off'});
  fixture.context.Notification.permission = 'granted';
  grant('granted');
  await pending;
  assert.equal(fixture.button['aria-checked'], 'false');
  assert.equal(fixture.values.get('codex-request-monitor.notifications'), 'off');
});
